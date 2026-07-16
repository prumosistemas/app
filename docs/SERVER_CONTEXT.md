# Contexto do Servidor Prumo

Versao: 1.0.48
Data: 2026-07-15
Modo atual: producao unica, sem homologacao ativa

## Resumo rapido

A Prumo roda em cinco partes:

1. HTMLs críticos servidos diretamente pelo Cloudflare Worker em `https://app.prumosistemas.com.br`; Netlify permanece como publicação complementar ligada ao GitHub.
2. Cloudflare Worker `morning-credit-8a59`, com D1 `db`, cuidando das telas críticas, login, sessoes, empresas, usuarios, pagamentos, logs e proxy para a API Python.
3. API Python no servidor Linux, container `prumo-api`, exposta internamente em `127.0.0.1:8000` e publicamente por `https://api.prumosistemas.com.br`.
4. Navegadores remotos no Modal, app `prumo-browserless`, atualmente com 30 sessoes turbo pela API.
5. API Google Modo IA no Modal, app `prumo-portal-nacional-google-solver`, usada apenas para hCaptcha do Portal Nacional.

Nao existe mais homologacao configurada no codigo. Os HTMLs sempre apontam para producao. O antigo Worker/D1 de homologacao deve ser considerado legado/removivel.

## Acesso ao servidor

Entrar no servidor:

```powershell
ssh -o ProxyCommand="cloudflared access ssh --hostname ssh.prumosistemas.com.br" server@localhost
```

Pasta principal do deploy:

```bash
cd /opt/prumo/app/deploy
```

Codigo espelhado no servidor:

```bash
cd /home/server/prumo-src
```

## Containers atuais

O Compose atual deve manter apenas:

- `prumo-api`: API FastAPI/Playwright.

O container local `browserless` foi retirado do Compose de producao. Ele pode ser derrubado com:

```bash
cd /opt/prumo/app/deploy
docker compose up -d --remove-orphans
docker ps
```

O esperado depois disso e aparecer apenas `prumo-api` entre os containers da Prumo.

## Capacidade de navegadores

Configuracao atual de producao:

```env
BASE_BROWSER_SLOTS=0
MAX_BROWSERS=30
MAX_BROWSER_LIMIT=96
BROWSER_CDP_POOL=modal-turbo|30|wss://ryangurgell20--prumo-browserless-browserless-server.modal.run?token=...
```

Isso significa:

- 0 navegadores locais.
- 30 navegadores pelo Modal.
- A fila da API cria no maximo 30 workers globais.
- O ISS sai direto pelo Modal por padrao. Em 2026-07-11 uma exportacao completa
  sem proxy validou 25 paginas/242 notas prestadas e 1 pagina/4 notas tomadas.
- O proxy em `127.0.0.1:31381` e o tunel `modal-proxy.prumosistemas.com.br`
  continuam ativos no ThinkPad. O probe HTTPS a partir do Modal expira no
  Cloudflare Access; nao definir `PRUMO_MODAL_PROXY_HOSTNAME` antes de criar e
  validar um service token de máquina.

Conferir pela API:

```bash
curl -fsS http://127.0.0.1:8000/
```

O esperado:

```json
{
  "version": "1.0.48",
  "max_browsers": 30,
  "base_browsers": 0,
  "browser_turbo_extra": 30,
  "browser_pool_configured": true
}
```

## Modal

Conta/perfil do Browserless ISS no CLI: `ryanzin` (`ryangurgell20`).

App Modal:

- Nome: `prumo-browserless`
- Arquivo local: `deploy/modal_browserless.py`
- Configuracao atual: `8` containers maximos x `4` sessoes por container = `32`; a API usa `30`.
- Secret esperado no Modal: `prumo-browserless`
- Secret deve conter pelo menos `TOKEN=<token_browserless>`.

Deploy do Modal:

```powershell
cd C:\Users\ryang\Desktop\projetosv2\projeto
modal profile activate ryanzin
modal deploy deploy\modal_browserless.py
```

Relatorio de custo vivo:

```powershell
modal billing report --for "this month" --json
```

O painel master usa `modal.Workspace.billing.report()` e consulta separadamente
as duas contas do solver Portal. O container `prumo-api` precisa receber:

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

Em 2026-07-15 a consulta retornou aproximadamente `1.95442728` USD na conta
principal e `0.00` USD na conta fallback. O saldo mostrado e uma estimativa
sobre o credito mensal configurado, nao uma quota oficial exposta pelo Modal.
Nunca versionar esses tokens.

## Modal do Portal Nacional

O Portal Nacional usa um segundo app Modal, separado do Browserless do ISS:

- Nome: `prumo-portal-nacional-google-solver`
- URL: `https://ryangurgell20--prumo-portal-nacional-google-solver-solve-d8ccea.modal.run/solve`
- Health: `https://ryangurgell20--prumo-portal-nacional-google-solver-solve-d8ccea.modal.run/health`
- Arquivo local: `deploy/modal_portal_nacional_google_solver.py`
- Fonte versionada: `solver/google_ai_mode/`.
- Projeto externo original: apenas referência histórica; o deploy não depende mais dele.
- Volume privado: `prumo-portal-google-ai-state`.
- Rota padrao: direta, sem proxy. O proxy local responde no ThinkPad, mas o probe a partir do Modal expira no Cloudflare Access; só definir `PRUMO_MODAL_PROXY_HOSTNAME` após configurar e validar autenticação de máquina.

Deploy manual das duas contas:

```powershell
cd C:\Users\ryang\Desktop\projetosv2\projeto
modal profile activate ryanzin
$env:PORTAL_MODAL_MIN_CONTAINERS='1'
$env:PORTAL_MODAL_BUFFER_CONTAINERS='3'
modal deploy deploy\modal_portal_nacional_google_solver.py
modal profile activate fabriciofarofa5
$env:PORTAL_MODAL_MIN_CONTAINERS='0'
$env:PORTAL_MODAL_BUFFER_CONTAINERS='2'
modal deploy deploy\modal_portal_nacional_google_solver.py
modal profile activate ryanzin
```

Validar:

```powershell
Invoke-RestMethod https://ryangurgell20--prumo-portal-nacional-google-solver-solve-d8ccea.modal.run/health
```

Configuracao da API Python:

```env
PORTAL_NACIONAL_SOLVER_URL=https://ryangurgell20--prumo-portal-nacional-google-solver-solve-d8ccea.modal.run/solve
PORTAL_NACIONAL_SOLVER_FALLBACK_URLS=https://fabriciofarofa5--prumo-portal-nacional-google-solver-sol-ffa9e3.modal.run/solve,http://127.0.0.1:8876/solve
PORTAL_NACIONAL_SOLVER_TIMEOUT_SECONDS=420
```

A conta `ryangurgell20` continua principal e volta a ser escolhida automaticamente quando o cooldown expira ou a quota mensal reseta. `fabriciofarofa5` e somente fallback Modal e escala a zero quando ociosa. O master consulta o billing das duas contas e mostra o ultimo endpoint que concluiu uma resolucao.

Em 2026-07-15 foram parados o app Florence remanescente em `ryangurgell20` e o
terceiro deploy legado deste solver em `jorhinhogames`. Como esse workspace
antigo estava desabilitado, o Browserless ISS foi republicado em `ryangurgell20`,
o servidor passou a usar o endpoint novo e o handshake WebSocket foi validado.

O solver e stateless. Ele nao recebe cookies do usuario, nao grava arquivos finais e nao deve misturar dados de usuarios. Ele so recebe `sitekey/request_id`, resolve o hCaptcha e devolve token. Os XML/PDF ficam no servidor, dentro da arvore do colaborador.

## Fallback Browserless local

O Browserless local nao fica ativo em producao, mas o caminho esta preservado para emergencia se o Modal cair.

Subir Browserless local manualmente:

```bash
cd /opt/prumo/app/deploy
docker run -d \
  --name browserless \
  --restart unless-stopped \
  --cpus 8 \
  --memory 12g \
  --shm-size 2g \
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

Alterar `.env` para fallback misto:

```env
BASE_BROWSER_SLOTS=5
MAX_BROWSERS=35
BROWSER_CDP_URL=ws://browserless:3000?token=...
BROWSER_CDP_POOL=browserless-local|5|ws://browserless:3000?token=...;;modal-turbo|30|wss://ryangurgell20--prumo-browserless-browserless-server.modal.run?token=...
```

Ou, se Modal estiver totalmente fora:

```env
BASE_BROWSER_SLOTS=5
MAX_BROWSERS=5
BROWSER_CDP_URL=ws://browserless:3000?token=...
BROWSER_CDP_POOL=browserless-local|5|ws://browserless:3000?token=...
```

Depois:

```bash
docker compose up -d
curl -fsS http://127.0.0.1:8000/
```

## Persistencia

Dados que nao somem se o PC ou servidor reiniciar:

- Worker/D1: empresas, usuarios, sessoes, pagamentos e logs ficam no Cloudflare D1 `db`.
- API Python: contas ISS, conjuntos, runs e arquivos ficam em `/opt/prumo/data`, montado no container como `/app/output`.
- SQLite local da API: `/opt/prumo/data/_api_data/iss_automacao.db`.
- Runs por colaborador: `/opt/prumo/data/empresas/<empresa>/colaboradores/<usuario>/runs`.
- Arquivos gerados por run ficam na mesma arvore de `/opt/prumo/data`.
- Portal Nacional por colaborador: `/opt/prumo/data/empresas/<empresa>/colaboradores/<usuario>/portal_nacional`.

O container `prumo-api` usa `restart: unless-stopped`; se o servidor voltar apos queda de energia, o Docker deve subir a API novamente. O Modal e stateless: containers sobem sob demanda.

Se a API cair no meio de uma run, o estado salvo em SQLite/pastas permanece. A run pode ficar como interrompida/running ate a proxima conciliacao manual ou retry seguro.

## Worker e D1

Worker de producao:

```bash
cd /home/server/prumo-src/cloudflare
wrangler deploy
```

D1 de producao:

- Binding: `db`
- Nome: `db`
- ID atual: `e69428e3-6524-427c-bae3-d32190e5c229`

Segredos do Worker:

- `ISS_INTERNAL_SECRET`
- `SETUP_TOKEN`
- `ADMIN_EMAIL`
- `ADMIN_PASSWORD`

Conferir deploys:

```bash
wrangler deployments list --name morning-credit-8a59
```

## Netlify e HTMLs

Site:

- Nome Netlify: `appprumo`
- URL final: `https://app.prumosistemas.com.br`
- Repo: `https://github.com/prumosistemas/app`
- Conta: `PRUMO`

Os HTMLs ficam na raiz do repo:

- `login.html`
- `index.html`
- `admin.html`
- `iss-fortaleza.html`
- `portal-nacional.html`
- `master.html`
- `master-company.html`
- `404.html`

As URLs publicas usam redirects do `netlify.toml`, entao o usuario acessa sem `.html`, por exemplo:

- `/login`
- `/admin`
- `/iss-fortaleza`
- `/portal-nacional`
- `/master`
- `/master-company`

Todos os HTMLs apontam para o Worker de producao `https://morning-credit-8a59.prumo-sistema.workers.dev`.

O Netlify pode bloquear novos deploys por crédito da conta. Para não misturar versões de login e API, o Worker `morning-credit-8a59` atende diretamente raiz, login, admin, master, ISS e Portal Nacional. Login/admin/master usam `Cache-Control: no-store`. Quando o Netlify voltar, o vínculo GitHub e o `netlify.toml` continuam válidos como publicação complementar.

## Retencao e crescimento de disco

- `/opt/prumo/data/_api_data/google_ai_solver_artifacts` guarda debug visual por 7 dias.
- Após 15 minutos sem alteração, `.html`, `.json` e `.txt` viram gzip; PNG vira WebP lossless quando o resultado é menor.
- XML/PDF, índices e certificados das empresas não são compactados nem removidos por essa rotina.
- O compose limita o log `json-file` da API a 3 arquivos de 10 MiB.
- A rotina fica em `solver/google_ai_mode/artifact_retention.py` e roda tanto no ThinkPad quanto no Modal.
- A primeira compactacao controlada foi executada em 2026-07-15. O processo roda em baixa concorrencia com a API ativa e preserva os artefatos dos sete dias definidos para depuracao.

## Monitor do host

- O servico `prumo-monitor.service` roda como root e le `/opt/prumo/config/monitor-agent.env`.
- O arquivo de ambiente deve permanecer em modo `600` e usar o mesmo `ISS_INTERNAL_SECRET` do compose da API.
- Depois de alterar o segredo, reinicie o servico; em 2026-07-15 o processo antigo foi reciclado e `/api/internal/runtime-metrics` voltou de 403 para 200.

## Master

O usuario master e o dono operacional do painel:

- Cria empresas.
- Reseta senha de admins.
- Desativa/apaga empresas.
- Configura PIX.
- Lanca pagamentos.
- Exclui pagamentos lancados errado.
- Ve logs e metricas de infraestrutura.
- Ve custo/creditos do Modal no mes.

Pagamentos sao manuais. Excluir pagamento recalcula imediatamente o estado de acesso da empresa afetada. Se a empresa ficar sem pagamento ativo, colaboradores podem ser bloqueados por regra de billing.

## ISS Fortaleza

O fluxo ISS usa:

- Login/seleção por requests quando possivel.
- Fallback por Playwright quando requests nao resolve.
- Modal benigno `Pesquisa Sefin` respondido com `Nao`.
- Mensagem real do portal como erro definitivo `MENSAGEM_NA_TELA`.
- Retry automatico seguro apenas para falhas retryable conhecidas, limitado por `AUTO_RETRY_MAX_ATTEMPTS`.

Fluxos:

- Certidao
- Escrituracao
- DAM
- Notas

Ordem operacional quando todos estao marcados:

1. Certidao
2. Escrituracao
3. DAM
4. Notas

## Notas Portal Nacional

Pagina: `/portal-nacional`.

Objetivo:

- Baixar XML e PDF/DANFSe de notas recebidas e emitidas no Portal Nacional.
- Manter dados separados por empresa e colaborador, usando o mesmo contexto autenticado da Prumo.
- Usar o servidor para persistencia e o Modal apenas para resolver hCaptcha quando o portal exige captcha no download.

Arquivos:

- UI: `portal-nacional.html`
- Router FastAPI: `server/portal_nacional.py`
- Automacao por requests/browser: `server/portal_nacional_automation.py`
- Gerador de sessao por certificado: `server/portal_nacional_session.py`
- Solver Modal: `deploy/modal_portal_nacional_google_solver.py`

Endpoints Python:

- `GET /api/portal-nacional/state`
- `POST /api/portal-nacional/certificates`
- `DELETE /api/portal-nacional/certificates/{cert_id}`
- `POST /api/portal-nacional/sessions/import`
- `POST /api/portal-nacional/runs`
- `GET /api/portal-nacional/runs`
- `GET /api/portal-nacional/runs/{run_id}`
- `POST /api/portal-nacional/runs/{run_id}/retry`
- `POST /api/portal-nacional/runs/{run_id}/stop`
- `GET /api/portal-nacional/runs/{run_id}/download`
- `GET /api/portal-nacional/runs/{run_id}/file?path=...`

Arvore de dados:

```text
/opt/prumo/data/empresas/<empresa>/colaboradores/<usuario>/portal_nacional/
  certificates/<cert_id>/cert.pfx
  certificates/<cert_id>/meta.json
  sessions/sessao_nfse.txt
  runs/<run_id>/run.json
  runs/<run_id>/indice.json
  runs/<run_id>/certificado/cert.pfx
  runs/<run_id>/certificado/password.txt
  runs/<run_id>/downloads/
  runs/<run_id>/logs/
```

O download individual e o ZIP so expoem `downloads/`, `logs/`, `indice.json` e `run.json`. O arquivo `sessions/sessao_nfse.txt`, os PFX e o arquivo interno de senha nao sao servidos para download pela API.

Certificado digital:

- A UI tem uma aba `Certificados` para upload de `.pfx`/`.p12`, com senha opcional.
- O PFX fica no escopo empresa/colaborador em `portal_nacional/certificates`.
- Ao iniciar a run, a API copia o PFX para `runs/<run_id>/certificado/` e grava a senha em arquivo interno para permitir renovacao de sessao durante indexacao/download.
- A senha do certificado fica protegida com o mesmo mecanismo de segredo da API quando `ISS_INTERNAL_SECRET` esta configurado.
- No Windows local, a store de certificados ainda pode ser listada como fallback de runtime.
- No servidor Linux, a store Windows nao existe; producao deve usar upload de PFX.
- A sessao do Portal Nacional fica sensivel ao IP/origem, mas o caminho PFX gera cookies direto no runtime atual.

Gerar sessao no Windows usando o IP do servidor:

```powershell
cloudflared access tcp --hostname modal-proxy.prumosistemas.com.br --url 127.0.0.1:31480
python server\portal_nacional_session.py --cert-index 3 --proxy http://127.0.0.1:31480 --out sessao_nfse.txt
```

O caminho acima e legado/local. Em producao, prefira cadastrar o PFX pela aba `Certificados`.

Teste confirmado em 2026-07-06:

- PFX `LOQUICENTER LOCADORA 11728000148` abriu com a senha fornecida fora do Git; validade confirmada até `2027-03-12`.
- Geracao de sessao por PFX retornou `target_looks_logged_in=true`, status `200`, cookies `ASP.NET_SessionId`, `Emissor` e `ARRAffinity`.
- Upload local pela API retornou `200`, apareceu em `/api/portal-nacional/state` e a exclusao retornou `200`.
- `somente-index` de recebidas em 01/07/2026 a 06/07/2026 capturou `26/26` notas em 2 paginas.
- O resolvedor anterior limitava downloads sob rate limit. Ele foi removido; o unico caminho ativo agora e Google Modo IA.
- Em 2026-07-16 o Modo IA v19 manteve o contrato visual unificado e adicionou recovery do widget com backoff. `ryangurgell20` e a rota normal; `fabriciofarofa5` fica reservada a quota/indisponibilidade; `127.0.0.1:8876` recebe falha visual especifica sem duplicar custo na conta Modal reserva.
- O timeout do solver e configuravel por `PORTAL_NACIONAL_SOLVER_TIMEOUT_SECONDS` e retries parciais reaproveitam tipos ja baixados.

Status:

- `finalizado`: tudo baixado.
- `finalizado_parcial`: run limitada por `max_items`, sem erro real.
- `finalizado_com_erros`: houve erro real ou pendencias sem limite de teste.
- `parado`: parado manualmente.

## Deploy completo

No PC:

```powershell
cd C:\Users\ryang\Desktop\projetosv2\projeto
git status
python -m py_compile server\main.py server\db.py server\domain.py server\run_queue.py
```

Deploy Worker:

```powershell
cd C:\Users\ryang\Desktop\projetosv2\projeto\cloudflare
wrangler deploy
```

Deploy Modal:

```powershell
cd C:\Users\ryang\Desktop\projetosv2\projeto
modal profile activate ryanzin
modal deploy deploy\modal_browserless.py
```

Build local opcional e push somente quando o registry estiver autenticado:

```powershell
cd C:\Users\ryang\Desktop\projetosv2\projeto
docker build -f server/Dockerfile -t ryang20/prumo-api:1.0.48 .
docker push ryang20/prumo-api:1.0.48
```

O caminho validado em 2026-07-15 foi construir diretamente no ThinkPad:

Atualizar servidor:

```bash
ssh -o ProxyCommand="cloudflared access ssh --hostname ssh.prumosistemas.com.br" server@localhost
cd /home/server/prumo-src
git pull --ff-only
docker build -f server/Dockerfile -t ryang20/prumo-api:1.0.48 .
cp deploy/docker-compose.yml /opt/prumo/app/deploy/docker-compose.yml
cd /opt/prumo/app/deploy
# conferir .env sem imprimir segredos; PRUMO_API_IMAGE=ryang20/prumo-api:1.0.48
docker compose up -d --force-recreate --remove-orphans
curl -fsS http://127.0.0.1:8000/
```

Deploy Netlify normalmente acontece via push no GitHub `main`. Deploy manual, se necessario:

```powershell
cd C:\Users\ryang\Desktop\projetosv2\projeto
netlify deploy --prod --dir .
```

## Checklist de saude

```bash
docker ps
curl -fsS http://127.0.0.1:8000/
docker logs --tail 100 prumo-api
```

No PC:

```powershell
wrangler deployments list --name morning-credit-8a59
modal billing report --for "this month" --json
git status
git rev-parse HEAD
git ls-remote origin refs/heads/main
```
