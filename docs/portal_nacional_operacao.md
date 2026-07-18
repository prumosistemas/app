# Portal Nacional - operação

## Arquitetura ativa

- A API Prumo executa `server/portal_nacional_automation.py` e persiste cada run em `/opt/prumo/data`.
- O único resolvedor é o Google Modo IA versionado em `solver/google_ai_mode`; Florence e Cohere não participam do fluxo.
- A sessão mTLS, a indexação e os downloads saem diretamente pelo ThinkPad. O login correto é `https://certificado.nfse.gov.br/EmissorNacional/Certificado`; o host `www` nesse endpoint responde 403.
- Somente a imagem do hCaptcha segue primeiro para o endpoint Modal, com até quatro containers. Se a conta principal atingir quota ou ficar indisponivel, outra conta Modal recebe a proxima tentativa; o mesmo resolvedor Google Modo IA em `127.0.0.1:8876` e apenas o ultimo recurso.
- O Portal Nacional não usa proxy. Um binding mTLS no Cloudflare foi testado, retornou 520 no login do certificado e foi removido; o acesso direto do ThinkPad retornou 200 em cerca de 3,5 segundos.
- O Modo IA usa um contrato visual único para qualquer formato. A captura temporal só é usada quando quatro quadros mostram movimento suficiente; caso contrário, segue um quadro único. Essa distinção de evidência preserva coordenadas e melhora acerto sem manter resolvedores separados.

## Variáveis

```text
PORTAL_NACIONAL_SOLVER_URL=https://ryangurgell20--prumo-portal-nacional-google-solver-solve-d8ccea.modal.run/solve
PORTAL_NACIONAL_SOLVER_FALLBACK_URLS=https://fabriciofarofa5--prumo-portal-nacional-google-solver-sol-ffa9e3.modal.run/solve,http://127.0.0.1:8876/solve
PORTAL_NACIONAL_SOLVER_TIMEOUT_SECONDS=420
```

A lista e ordenada e aceita separacao por virgula, ponto e virgula ou quebra de linha. A variavel singular antiga continua aceita durante upgrades. Cada endpoint possui cooldown independente: 429 ou sessão Google confirmadamente indisponível troca de conta por 300 segundos; 5xx/circuito por 90 segundos; falhas visuais, inclusive `visual_challenge_not_ready`, ficam restritas ao captcha atual e nao derrubam a conta inteira.

Nunca grave cookies, PFX, senhas ou credenciais de túnel no Git. No Modal, o estado anônimo fica no Volume privado. No ThinkPad, fica em `/opt/prumo/data/_api_data/google_ai_solver_state`, coberto pelo volume persistente; somente o código entra no Git e na imagem.

## Procedimento seguro de teste

1. Confirme `/health`: `provider=google_ai_mode`, `route=direct` e circuitos fechados.
2. Confirme que o certificado abre antes de criar a run.
3. Em diagnóstico, inicie com `max_items=1` e pelo menos 6 tentativas. A concorrência é definida automaticamente pelo backend em quatro tarefas e não aparece no HTML.
4. Aguarde a run terminar; não reinicie durante um solve ativo de até 420 segundos.
5. Valide `item_downloaded`, os arquivos XML/PDF e o status final.
6. Só depois aumente o lote.

As tentativas de rede usam backoff crescente. Quando 429/503, falha de DNS/conexão ou circuito aberto afetam itens diferentes, a espera global cresce em 4, 8, 16, 32, 64 e 120 segundos e zera após um sucesso.

## Certificados e troca de segredo

Senhas de PFX são criptografadas com `ISS_INTERNAL_SECRET`. Se esse segredo mudar, versões antigas não podem ser descriptografadas. A API agora falha cedo com HTTP 409 e pede novo envio, em vez de escrever uma senha vazia na run.

Uma run histórica pode servir para diagnóstico, mas nunca imprima a senha ou o PFX. Valide apenas resultado, tamanho e hashes reduzidos.

## Evidência de 2026-07-13

Teste SIM7 na run `20260713-230754-recebidas-20260601-20260630-cert-202607131415-ambos`:

- 22 XML e 16 PDFs físicos preservados;
- quatro containers/navegadores Modal validados em paralelo;
- ao degradar o Modal, quatro navegadores do ThinkPad receberam o fallback;
- após configurar o Chromium residencial, o Modo IA respondeu 12/12 chamadas visuais e concluiu quatro PDFs novos sem abrir circuito;
- solver `2026-07-13-google-ai-mode-v17-unified-parallel-safe` nos dois caminhos.

Teste anterior do Alan:

Teste ampliado do Alan na run `20260707-150940-emitidas-20260601-20260630-cert-202607061735-ambos`:

- resultado final: `finalizado_parcial`;
- acumulado: 18 baixados, 0 erros, sendo 10 novos sobre a base inicial de 8;
- último lote: 6 itens com concorrência 1, seguido de 1 retry controlado após renovar a sessão anônima;
- método: XML e PDF com hCaptcha resolvido exclusivamente por Google Modo IA;
- solver final: `2026-07-13-google-ai-mode-v11-bundled-source`, rota direta, circuitos fechados;
- último item: 98 segundos entre início e `item_downloaded`.

O status parcial é esperado: `max_items` limitou o teste e 416 registros permaneceram fora dele. Nenhum desses pendentes foi contabilizado como erro.

## Incidente e correção de 2026-07-14

Run do Alan/SIM7: `20260714-114741-emitidas-20260601-20260630-cert-202607131415-ambos`.

- O PFX da run e o arquivo indicado no OneDrive têm o mesmo SHA-256; a senha abre o PFX e o certificado está válido até 2026-11-17.
- A causa do 403 era o endpoint de certificado com host incorreto (`www.nfse.gov.br`). O Firefox manual comprovou o host dedicado `certificado.nfse.gov.br`.
- Depois da correção, o ThinkPad gerou sessão 200 com `ASP.NET_SessionId`, `Emissor` e `ARRAffinity`, reindexou 169 notas em 12 páginas e retomou com quatro tarefas simultâneas.
- A primeira amostra pós-correção concluiu 10 notas novas sem travar. A run depois finalizou 169/169; a validação física confirmou 169 PDFs e 169 XMLs referenciados pelo índice, sem ausente ou inválido.
- Falhas reais do endpoint (404 persistente, 429 e 5xx) abrem cooldown e permitem failover para a conta Modal reserva. Falha visual especifica de um captcha segue para o ThinkPad; a conta reserva não repete o mesmo desafio e preserva quota. Logs e artefatos de depuração têm retenção de sete dias.
- `visual_challenge_not_ready` também pode ser apenas uma grade em movimento. A partir da 1.0.53, a tentativa atual segue ao ThinkPad, mas o endpoint Modal continua disponível para as outras notas. Quota, 5xx, circuito aberto e transporte continuam abrindo cooldown e usando a conta reserva.
- O deploy final usa o solver `2026-07-16-google-ai-mode-v19-open-recovery-safe-fallback` nas contas `ryangurgell20` e `fabriciofarofa5`; o deploy legado da conta `jorhinhogames` permanece parado.
- Em 2026-07-16, as quatro runs mais recentes do Alan/SIM7 terminaram 84/84, 35/35, 50/50 e 74/74. Todos os 486 arquivos referenciados pelo índice foram validados fisicamente. A v19 passou a distinguir widget que não abriu de grade instável e remove query strings/tokens transitórios dos erros persistidos.
- No teste isolado pós-deploy, o mesmo sitekey que não abriu na v18 foi resolvido pela v19 no ThinkPad: quatro etapas visuais capturadas e token devolvido, sem navegador órfão e sem consumir a conta Modal fallback.

## Períodos e notas retroativas - correção de 2026-07-17

- O Portal rejeita consultas acima de 30 dias e a visão sem filtro mostra apenas um conjunto recente; por isso nenhum dos dois caminhos garante o período solicitado.
- A Prumo divide o intervalo por mês em janelas inclusivas de no máximo 30 dias. Exemplo: 01/06 a 17/07 vira 01/06-30/06 e 01/07-17/07.
- Cada janela percorre todas as páginas e só é aceita quando os IDs únicos capturados coincidem com o total informado pelo Portal. Após três varreduras incompletas, a run falha antes dos downloads.
- Os IDs de todas as janelas são unidos e deduplicados. A competência da nota não é filtrada, então competências retroativas, inclusive maio dentro de uma consulta posterior, permanecem no resultado.
- O índice registra as janelas, totais individuais, soma bruta, quantidade global única e duplicados entre janelas. Na sessão SIM7, os totais observados foram 169 para 01/06-30/06 e 205 para 01/07-17/07.

## Desempenho e isolamento - correção de 2026-07-18

- A run SIM7 de 01/06 a 17/07 concluiu 374 PDFs e 374 XMLs referenciados, sem erro final. A duração de 8h26 e 546 inícios de item expôs retrabalho entre XML e PDF.
- A partir da 1.0.52, sucesso do XML é preservado no índice mesmo se o solver do PDF lançar exceção; o retry continua no PDF e não paga outro captcha do XML.
- Na 1.0.53, uma sessão Modo IA recuperada por container é sincronizada a cada 15 segundos com o Volume privado e imediatamente após o prewarm. Cada container recarrega a semente antes de iniciar; a recuperação Chrome usa uma tentativa curta configurada, em vez de três ciclos longos.
- Na 1.0.54, a causa do provedor não é sobrescrita pelo encerramento visual genérico: sessão Modo IA/navegador indisponível tenta a conta Modal reserva; rejeição visual comum continua no ThinkPad.
- Na 1.0.55, timeout do health durante cold start é apenas diagnóstico. A run preserva a URL principal e cada POST decide o failover; uma falha confirmada da sessão Google põe apenas aquele endpoint em cooldown por cinco minutos, evitando repetir recovery caro em todas as notas.
- Na 1.0.56, o cooldown de sessão permanece em cinco minutos nos Modal, mas cai para 30 segundos no ThinkPad residencial. Assim o fallback gratuito se recupera sem deixar todas as tarefas paradas.
- O timeout visual no Modal foi reduzido de 150 para 90 segundos; desafios não concluídos seguem para failover sem prender as quatro tarefas por vários minutos.
- Alan e Gabriel usam raízes de dados e runtimes distintos. A prova pela API em produção mostrou zero run IDs em comum; Gabriel recebeu 404 ao solicitar diretamente a run SIM7 do Alan.
- Logs e imagens de captcha são publicados no Volume privado a cada minuto, com nome por container, e continuam sujeitos à retenção de sete dias.
