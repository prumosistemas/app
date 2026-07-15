# Portal Nacional - operação

## Arquitetura ativa

- A API Prumo executa `server/portal_nacional_automation.py` e persiste cada run em `/opt/prumo/data`.
- O único resolvedor é o Google Modo IA versionado em `solver/google_ai_mode`; Florence e Cohere não participam do fluxo.
- A sessão mTLS, a indexação e os downloads saem diretamente pelo ThinkPad. O login correto é `https://certificado.nfse.gov.br/EmissorNacional/Certificado`; o host `www` nesse endpoint responde 403.
- Somente a imagem do hCaptcha segue primeiro para o endpoint Modal, com até quatro containers. Se ele falhar, o mesmo resolvedor Google Modo IA roda em `127.0.0.1:8876`.
- O Portal Nacional não usa proxy. Um binding mTLS no Cloudflare foi testado, retornou 520 no login do certificado e foi removido; o acesso direto do ThinkPad retornou 200 em cerca de 3,5 segundos.

## Variáveis

```text
PORTAL_NACIONAL_SOLVER_URL=https://ryangurgell20--prumo-portal-nacional-google-solver-solve-d8ccea.modal.run/solve
PORTAL_NACIONAL_SOLVER_FALLBACK_URL=http://127.0.0.1:8876/solve
PORTAL_NACIONAL_SOLVER_TIMEOUT_SECONDS=420
```

Nunca grave cookies, PFX, senhas ou credenciais de túnel no Git. No Modal, o estado anônimo fica no Volume privado. No ThinkPad, fica em `/opt/prumo/data/_api_data/google_ai_solver_state`, coberto pelo volume persistente; somente o código entra no Git e na imagem.

## Procedimento seguro de teste

1. Confirme `/health`: `provider=google_ai_mode`, `route=direct` e circuitos fechados.
2. Confirme que o certificado abre antes de criar a run.
3. Em diagnóstico, inicie com `max_items=1`, `concorrencia=1` e pelo menos 6 tentativas. Em produção validada, use concorrência 4.
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
- A primeira amostra pós-correção concluiu 10 notas novas sem travar, elevando o total de 35 para 45; a run permaneceu ativa para completar todo o lote.
- A falha de um endpoint do solver agora abre cooldown por endpoint; 404 persistente, 429 e 5xx não queimam todas as tentativas imediatamente. Logs e artefatos de depuração têm retenção de sete dias.
