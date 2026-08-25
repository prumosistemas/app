# Operacao Prumo Detalhada

Este documento e a fonte de contexto operacional da versao 1.0.107.

## Estado desejado

- Producao unica.
- Sem homologacao no codigo.
- HTMLs criticos no Worker com URLs limpas; Netlify como publicacao complementar.
- Worker de producao `morning-credit-8a59`.
- D1 de producao `db`.
- API Python no servidor local Linux.
- `prumo-api` como unico container principal da Prumo.
- Browserless local desligado.
- Modal `prumo-browserless` em múltiplas contas, com seleção ponderada, cooldown por endpoint e retorno automático.
- Modal `prumo-portal-nacional-google-solver` separado, com Google Modo IA, usado so para resolver hCaptcha do Portal Nacional.
- Portal retomavel por checkpoint: `Continuar` processa todas as partes incompletas sem reconstruir um indice valido. Outages HTTP/solver usam um unico probe global com lease persistente, sem consumir a tentativa da nota. HTTP 503 preserva o motivo JSON; `unusual` nao encerra a run. Falhas de transporte sao sondadas em ate 60 s; bloqueio visual repetido cresce ate 120 s para respeitar os cooldowns dos provedores. A recuperacao reabre a concorrencia em 1→2→4.
- Captura automática diária do Portal distribuída pelas 24 horas, com uma execução automática global por vez, sem pular a tentativa do dia após rebalanceamento, primeira janela de 123 dias, sobreposição de dois dias e retenção de 123 dias.
- Histórico automático separado por certificado, inclusive para ciclos com erro, com contagem incremental deduplicada e download filtrável por data de emissão e competência.
- GitHub, pasta local e servidor na mesma versao.
- Dois Spaces HF privados são tentados antes do egress Modal; as duas contas Modal de contingência vêm depois da principal e o ThinkPad permanece por último.
- Falta de crédito/`workspace disabled` no Modal principal abre quarentena compartilhada de 30 minutos. A próxima atividade o sonda novamente e restaura sua prioridade automaticamente após sucesso; não existe regra fixa para o dia da renovação.
- Cold start Modal não executa prewarm no Google. Cada egress faz somente uma tentativa por análise; `unusual traffic` é telemetria de rota e não reduz a capacidade por minutos. Sucesso confirmado limpa a penalidade. As três contas podem trabalhar em paralelo, limitadas por conta, sem antecipar carga para o ThinkPad.
- A captura temporal completa cobre 8,7 s em 30 quadros; ocupacao fica sincrona e montagem/overlay/MP4 sao gerados em fila de debug fora do caminho critico.
- A segunda conta HF fica no cofre, mas ainda nao pode criar ZeroGPU; detalhes em `docs/HUGGINGFACE_CONTEXT.md`.
- O master exibe a auditoria visual espelhada no ThinkPad: rota, tempo, navegadores, `unusual`, cliques, trocas, imagens e vídeo, com retenção de sete dias.
- `Checar encerramento` usa requests do ThinkPad, não Browserless. Aceita várias contas cadastradas, mantém cinco históricos compartilhados pela empresa com identificação do executor e compartilha um limite global de seis sessões HTTP para não prejudicar outras operações.
- O Worker transforma falhas de infraestrutura da API (`530`, túnel `1033` e respostas 5xx não JSON) em erro JSON 503 amigável e rastreável. Respostas válidas de arquivos continuam em streaming.
- A autenticação reconhece timeouts/resets transitórios do D1 em rate limit, sessão, usuário, cobrança e CSRF. O Worker tenta até cinco vezes com backoff e jitter; login e restauração de sessão repetem respostas transitórias até três vezes no navegador. Falha prolongada vira `AUTH_TEMPORARILY_BUSY`, sem expor erro interno.
- O ZIP geral do ISS converte somente a apresentação da pasta para `Nome - CNPJ`; o layout persistido e os checkpoints permanecem inalterados.

## Onde fica cada coisa

Local Windows:

```powershell
C:\Users\ryang\Desktop\projetosv2\projeto
```

Servidor:

```bash
/home/server/prumo-src
/opt/prumo/app/deploy
/opt/prumo/data
```

Cloudflare:

- Worker: `morning-credit-8a59`
- D1: `db`
- Tunnel SSH: `ssh.prumosistemas.com.br`
- API publica: `https://api.prumosistemas.com.br`

Netlify:

- Site: `appprumo`
- Dominio: `https://app.prumosistemas.com.br`

Modal:

- Perfil ISS: `ryanzin` (`ryangurgell20`); app `prumo-browserless`.
- Perfil Portal principal: `ryanzin` (`ryangurgell20`).
- Perfil Portal fallback: `fabriciofarofa5`.
- Arquivo ISS: `deploy/modal_browserless.py`
- App Portal Nacional: `prumo-portal-nacional-google-solver`
- Arquivo Portal Nacional: `deploy/modal_portal_nacional_google_solver.py`

## Dados e volatilidade

Nao sao volateis:

- Empresas, usuarios, pagamentos e logs do app ficam no D1.
- Contas ISS, conjuntos, runs e arquivos ficam em `/opt/prumo/data`.
- O SQLite da API fica em `/opt/prumo/data/_api_data/iss_automacao.db`.
- O container monta `/opt/prumo/data:/app/output`.
- Portal Nacional fica em `/opt/prumo/data/empresas/<empresa>/colaboradores/<usuario>/portal_nacional`.
- Sessoes do Portal Nacional ficam em `portal_nacional/sessions/sessao_nfse.txt`.
- Runs do Portal Nacional ficam em `portal_nacional/runs/<run_id>`, com `downloads/`, `logs/`, `indice.json` e `run.json`.

Sao volateis:

- Containers Modal: sobem e descem sob demanda.
- Estado em RAM da fila durante uma execucao.
- Sessao de navegador de uma run em andamento.

Se o servidor desligar:

1. D1 continua intacto.
2. `/opt/prumo/data` continua no disco.
3. Docker reinicia `prumo-api` por `restart: unless-stopped`.
4. Runs em andamento podem precisar de retry, mas arquivos/dados salvos nao somem.

## Modal e custo

O painel master mostra creditos Modal na secao `Logs`, nao em `Pagamentos`.

O Worker expõe `/api/master/modal-billing` para o master e encaminha para a API Python. A API Python consulta `modal.Workspace.billing.report()` nas três contas do solver Portal.

Variaveis necessarias no servidor:

```env
MODAL_PRIMARY_TOKEN_ID=...
MODAL_PRIMARY_TOKEN_SECRET=...
MODAL_PRIMARY_WORKSPACE=ryangurgell20
MODAL_PRIMARY_MONTHLY_CREDIT_USD=30.00
MODAL_FALLBACK_TOKEN_ID=...
MODAL_FALLBACK_TOKEN_SECRET=...
MODAL_FALLBACK_WORKSPACE=fabriciofarofa5
MODAL_FALLBACK_MONTHLY_CREDIT_USD=30.00
MODAL_BILLING_APP_NAME=prumo-portal-nacional-google-solver
```

O saldo exibido e calculado assim:

```text
credito_restante = MODAL_MONTHLY_CREDIT_USD - custo_modal_no_mes
```

Em 2026-07-15, a conta principal retornou aproximadamente `1.95442728` USD no mes e a fallback `0.00` USD. O saldo e uma estimativa calculada sobre o credito configurado.

## Pagamentos

O master gerencia pagamentos manualmente em `/master`.

Operacoes:

- cadastrar PIX;
- lancar pagamento por empresa;
- excluir pagamento lancado errado;
- acompanhar historico.

Ao excluir pagamento:

1. O Worker valida role `master`.
2. Valida CSRF.
3. Exige `confirm: "DELETE"`.
4. Remove o pagamento.
5. Recalcula billing da empresa.
6. Registra log `billing_payment_deleted`.

## Homologacao

A homologacao foi removida em versao anterior. Os arquivos HTML sempre apontam para producao.

Se existir recurso antigo no Cloudflare:

- Worker antigo: `morning-credit-8a59-homologacao`
- D1 antigo: `db-homologacao`

Eles nao sao usados pelo codigo atual.

## App Notas Portal Nacional

Pagina publica: `/portal-nacional`.

O app aparece ao lado do `ISS Fortaleza` no `index.html`. Ele usa:

- servidor Python para guardar usuario, sessao, indice, runs e arquivos;
- Modal `prumo-portal-nacional-google-solver` apenas para hCaptcha;
- upload de certificado `.pfx`/`.p12` por colaborador, com senha validada e protegida no servidor;
- sessao gerada diretamente pelo PFX no runtime atual, sem depender da store Windows no Linux.
- aba `Notas automático`, com configuração por certificado e data inicial, captura diária fixa em XML+PDF, botão `Capturar agora` e retenção móvel de 123 dias;
- agenda global espalhada pelas 24 horas e execução automática serializada para não disputar o Portal e os solvers com outra run ativa.

O agendador nasce e encerra junto com o FastAPI. A configuração fica em
`portal_nacional/automatic.json` dentro do diretório do colaborador. Uma run
automática é uma run normal com checkpoint e os campos `automatic` e
`automatic_job_id`; por isso continua visível, baixável e retomável. A limpeza
de 123 dias seleciona somente essas runs e nunca remove capturas manuais.
Na inicialização, estados persistidos `criada`/`rodando` sem runtime vivo são
retomados automaticamente com `retry_only`, reaproveitando o índice e os
arquivos válidos. Isso cobre restart do Docker, deploy e queda do processo.

Operação da tela:

1. Em `Certificados`, clique numa linha ou em `Editar` para alterar o alias, a senha ou substituir o PFX. Arquivo e senha em branco são preservados.
2. Em `Notas automático`, escolha certificado, tipo de nota e data inicial. Salvar torna a captura diária em XML+PDF imediatamente elegível.
3. O horário mostrado é administrado pelo servidor e redistribuído quando configurações são incluídas ou excluídas.
4. `Capturar agora` ignora a espera da agenda, mas recusa se o mesmo colaborador já estiver executando outra run. Uma run de outro colaborador não desativa o botão.
5. `Parar run` fica ao lado de `Excluir run`. A navegação lateral mantém somente `Voltar`, que retorna à central da Prumo.

Em 2026-07-05 o Netlify bloqueou novos deploys por credito da conta. A central `/` e a rota limpa `/portal-nacional` foram mantidas ativas por rotas especificas do Cloudflare Worker `morning-credit-8a59` (`app.prumosistemas.com.br/` e `app.prumosistemas.com.br/portal-nacional*`), que entregam `index.html` e `portal-nacional.html` diretamente.

Arquivos principais:

```text
portal-nacional.html
server/portal_nacional.py
server/portal_nacional_automation.py
server/portal_nacional_session.py
deploy/modal_portal_nacional_google_solver.py
```

Teste local confirmado em 2026-07-06:

- PFX `LOQUICENTER LOCADORA 11728000148` abriu com a senha fornecida fora do Git e gerou sessao logada no Portal Nacional;
- upload local pela API retornou `200`, apareceu no estado e foi excluido com `200`;
- indexacao por requests para 01/07/2026 a 06/07/2026 capturou `26/26` notas recebidas em 2 paginas;
- o resolvedor antigo foi removido depois de limitar downloads; o caminho atual usa somente Google Modo IA;
- teste anterior em 2026-07-05: indexacao por requests com 86 notas recebidas;
- teste anterior em 2026-07-05: download local com 1 XML e 1 PDF validos;
- producao Gabriel: run `20260705-210520-recebidas-20260601-20260630-cert00-pdf`, 1 PDF valido, status `finalizado_parcial`, erros `0`;
- producao Gabriel: run `20260705-215220-recebidas-20260601-20260630-cert00-pdf`, 1 PDF valido, status `finalizado_parcial`, erros `0`;
- PDF com cabecalho `%PDF-1.4`;
- XML com raiz `NFSe`;
- sessao local sem proxy caiu para login no servidor; sessao local com `--proxy http://127.0.0.1:31480` funcionou na producao.
- O solver v19 usa exclusivamente Google Modo IA e um contrato visual único. A conta Modal principal tenta primeiro. As duas contas de contingência recebem failover de quota/indisponibilidade; falha visual específica segue para o mesmo solver no ThinkPad, evitando duplicar custo Modal no mesmo desafio e sem bloquear as outras notas.
- Se o widget hCaptcha nao abrir, a v19 recarrega o widget com espera crescente e registra `visual_challenge_not_opened`, separado de grade instavel. URLs persistidas em erros nunca mantem query string ou token transitorio.
- Na 1.0.53, `visual_challenge_not_ready` nao abre cooldown global. Cada container Modal aceita uma entrada ativa, a principal mantem um container e um buffer, e a reserva escala a zero. A sessao anonima recuperada sincroniza em 15 segundos e a recuperacao Chrome usa um ciclo curto.
- Na 1.0.54, falha real da sessao Google Modo IA ou do navegador preserva sua classificacao e tenta a conta Modal reserva. Apenas rejeicao visual do desafio segue direto ao ThinkPad.
- Na 1.0.55, timeout do health em cold start nao troca a rota da run inteira. O POST real tenta o pool principal e aplica failover por captcha; falha confirmada da sessao Google abre cooldown somente para aquele endpoint e evita recovery repetido no lote.
- Na 1.0.56, esse cooldown continua em cinco minutos nos Modal para poupar credito e fica limitado a 30 segundos no solver residencial, que pode tentar recuperar sem custo externo.
- Na 1.0.57, quadros temporais sem alvo sao recapturados sem penalizar o Modo IA e o fallback residencial aceita ate 240 segundos para preservar desafios longos; o Modal continua em 90 segundos para limitar custo.
- Na 1.0.58, o circuito interno do Modal registra o instante de abertura e se rearma em 300 segundos, evitando 503 permanente em container aquecido.
- Desafios hCaptcha ainda dependem do Modo IA; por isso o timeout deve ficar em `PORTAL_NACIONAL_SOLVER_TIMEOUT_SECONDS=420`, com retries/backoff que reaproveitam arquivos ja baixados.

Gerar sessao pelo IP do servidor usando store Windows, caminho legado:

```powershell
cloudflared access tcp --hostname modal-proxy.prumosistemas.com.br --url 127.0.0.1:31480
python server\portal_nacional_session.py --cert-index 3 --proxy http://127.0.0.1:31480 --out sessao_nfse.txt
```

Em producao, prefira cadastrar o PFX pela aba `Certificados` em `/portal-nacional`.

Health do solver:

```powershell
Invoke-RestMethod https://ryangurgell20--prumo-portal-nacional-google-solver-solve-d8ccea.modal.run/health
```

Deploy do solver sem trocar o perfil Modal global:

```powershell
python -m ops.prumo_ops modal deploy --account primary --target portal
python -m ops.prumo_ops modal deploy --account fallback --target portal
```

## Fallback local de navegador

Producao normal nao usa navegador local. Se Modal cair, subir fallback conforme `docs/SERVER_CONTEXT.md`.

Resumo minimo:

```bash
docker run -d --name browserless --restart unless-stopped \
  --cpus 8 --memory 12g --shm-size 2g \
  -p 127.0.0.1:3000:3000 \
  -e TOKEN="$BROWSERLESS_TOKEN" \
  -e CONCURRENT=5 \
  -e MAX_CONCURRENT_SESSIONS=5 \
  -e QUEUED=30 \
  -e QUEUE_LENGTH=30 \
  -e TIMEOUT=1200000 \
  -e CONNECTION_TIMEOUT=1200000 \
  -e DEFAULT_LAUNCH_ARGS='["--no-sandbox"]' \
  browserless/chrome@sha256:57d19e414d9fe4ae9d2ab12ba768c97f38d51246c5b31af55a009205c136012f
```

Depois ajustar `BROWSER_CDP_POOL` no `.env` e reiniciar `prumo-api`.

## Comandos de auditoria

```powershell
git status
git rev-parse HEAD
git ls-remote origin refs/heads/main
python -m ops.prumo_ops cloudflare status
python -m ops.prumo_ops modal billing --account primary --target portal
python -m ops.prumo_ops modal billing --account fallback --target portal
```

Servidor:

```bash
docker ps
docker compose ps
curl -fsS http://127.0.0.1:8000/
docker logs --tail 100 prumo-api
```
