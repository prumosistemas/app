# Contexto do Servidor Prumo

Versao: 1.0.32
Data: 2026-07-05
Modo atual: producao unica, sem homologacao ativa

## Resumo rapido

A Prumo roda em quatro partes:

1. HTMLs estaticos no Netlify, servidos em `https://app.prumosistemas.com.br`.
2. Cloudflare Worker `morning-credit-8a59`, com D1 `db`, cuidando de login, sessoes, empresas, usuarios, pagamentos, logs e proxy para a API Python.
3. API Python no servidor Linux, container `prumo-api`, exposta internamente em `127.0.0.1:8000` e publicamente por `https://api.prumosistemas.com.br`.
4. Navegadores remotos no Modal, app `prumo-browserless`, atualmente com 30 sessoes turbo pela API.
5. API resolvedora do Portal Nacional no Modal, app `prumo-portal-nacional-solver`, usada apenas para hCaptcha.

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
BROWSER_CDP_POOL=modal-turbo|30|wss://jorhinhogames--prumo-browserless-browserless-server.modal.run?token=...
```

Isso significa:

- 0 navegadores locais.
- 30 navegadores pelo Modal.
- A fila da API cria no maximo 30 workers globais.
- O portal ISS sai pelo IP do servidor porque o Browserless Modal sobe `cloudflared access tcp` e injeta proxy no Chrome.

Conferir pela API:

```bash
curl -fsS http://127.0.0.1:8000/
```

O esperado:

```json
{
  "version": "1.0.35",
  "max_browsers": 30,
  "base_browsers": 0,
  "browser_turbo_extra": 30,
  "browser_pool_configured": true
}
```

## Modal

Conta/perfil usado no CLI: `jorhinhogames`.

App Modal:

- Nome: `prumo-browserless`
- Arquivo local: `deploy/modal_browserless.py`
- Configuracao atual: `8` containers maximos x `4` sessoes por container = `32`; a API usa `30`.
- Secret esperado no Modal: `prumo-browserless`
- Secret deve conter pelo menos `TOKEN=<token_browserless>`.

Deploy do Modal:

```powershell
cd C:\Users\ryang\Desktop\projetosv2\projeto
modal profile use jorhinhogames
modal deploy deploy\modal_browserless.py
```

Relatorio de custo vivo:

```powershell
modal billing report --for "this month" --json
```

Em 2026-07-05 o relatorio retornou custo mensal aproximado de `1.79333653` USD para `prumo-browserless`. Com `MODAL_MONTHLY_CREDIT_USD=30.00`, o painel master calcula credito restante aproximado de `28.21` USD.

Para o painel master consultar o Modal pela API Python, o container `prumo-api` precisa receber:

```env
MODAL_TOKEN_ID=...
MODAL_TOKEN_SECRET=...
MODAL_MONTHLY_CREDIT_USD=30.00
MODAL_BILLING_APP_NAME=prumo-browserless
```

Nunca versionar esses tokens.

## Modal do Portal Nacional

O Portal Nacional usa um segundo app Modal, separado do Browserless do ISS:

- Nome: `prumo-portal-nacional-solver`
- URL: `https://jorhinhogames--prumo-portal-nacional-solver-solver-server.modal.run/solve`
- Health: `https://jorhinhogames--prumo-portal-nacional-solver-solver-server.modal.run/health`
- Arquivos locais:
  - `deploy/modal_portal_nacional_solver.py`
  - `deploy/portal_nacional_solver.py`
- Secret esperado: `prumo-portal-nacional-solver`
- Secret deve conter `COHERE_API_KEY`.

Deploy:

```powershell
cd C:\Users\ryang\Desktop\projetosv2\projeto
modal profile use jorhinhogames
modal deploy deploy\modal_portal_nacional_solver.py
```

Validar:

```powershell
Invoke-RestMethod https://jorhinhogames--prumo-portal-nacional-solver-solver-server.modal.run/health
```

Configuracao da API Python:

```env
PORTAL_NACIONAL_SOLVER_URL=https://jorhinhogames--prumo-portal-nacional-solver-solver-server.modal.run/solve
PORTAL_NACIONAL_SOLVER_TIMEOUT_SECONDS=240
```

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
BROWSER_CDP_POOL=browserless-local|5|ws://browserless:3000?token=...;;modal-turbo|30|wss://jorhinhogames--prumo-browserless-browserless-server.modal.run?token=...
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

Observacao de 2026-07-05: o Netlify bloqueou novos deploys por credito da conta. Para manter a Central atualizada e o Portal Nacional acessivel sem `.html`, o Worker `morning-credit-8a59` tambem atende as rotas Cloudflare `app.prumosistemas.com.br/` e `app.prumosistemas.com.br/portal-nacional*`, entregando `index.html` e `portal-nacional.html` diretamente. Quando o Netlify voltar a aceitar deploys, o `netlify.toml` continua sendo a fonte normal das rotas limpas.

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
- Solver Modal: `deploy/modal_portal_nacional_solver.py` e `deploy/portal_nacional_solver.py`

Endpoints Python:

- `GET /api/portal-nacional/state`
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
  sessions/sessao_nfse.txt
  runs/<run_id>/run.json
  runs/<run_id>/indice.json
  runs/<run_id>/downloads/
  runs/<run_id>/logs/
```

O download individual e o ZIP so expoem `downloads/`, `logs/`, `indice.json` e `run.json`. O arquivo `sessions/sessao_nfse.txt` nao e servido para download pela API.

Certificado digital:

- A UI pede para selecionar o certificado disponivel no runtime que esta gerando a sessao.
- Nao ha upload de arquivo de certificado.
- No Windows local, a sessao pode ser gerada usando a store de certificados do usuario.
- No servidor Linux, a store Windows nao existe. Para producao sem certificado no servidor, importar uma sessao `sessao_nfse.txt` gerada em maquina autorizada e usar "usar sessao salva".
- A sessao do Portal Nacional fica sensivel ao IP/origem. Para a producao usar a sessao, gere a sessao localmente passando pelo proxy do servidor.

Gerar sessao no Windows usando o IP do servidor:

```powershell
cloudflared access tcp --hostname modal-proxy.prumosistemas.com.br --url 127.0.0.1:31480
python server\portal_nacional_session.py --cert-index 3 --proxy http://127.0.0.1:31480 --out sessao_nfse.txt
```

Depois importe o JSON em `/portal-nacional` e rode com "usar sessao salva".

Teste confirmado em 2026-07-05:

- Run local `20260705-161546-recebidas-20260601-20260630-cert03-ambos`.
- Run de producao Gabriel `20260705-210520-recebidas-20260601-20260630-cert00-pdf`.
- Run de producao Gabriel `20260705-215220-recebidas-20260601-20260630-cert00-pdf`.
- Indexou 86 notas recebidas de 01/06/2026 a 30/06/2026.
- Baixou 1 XML valido no teste local e PDFs validos na producao via Modal solver.
- PDF validado pelo cabecalho `%PDF-1.4`.
- XML validado como documento `NFSe`.
- O XML em producao recebeu hCaptcha canvas nao-9 e ficou bloqueado por `solver:cohere_rate_limited` quando a Cohere retornou HTTP 429. O Modal/proxy/browser estavam saudaveis; a dependencia limitante era a API de visao. O solver usa estrategia hibrida: recarrega desafios nao-9 algumas vezes e depois chama IA uma vez, preservando erros especificos.
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
modal profile use jorhinhogames
modal deploy deploy\modal_browserless.py
```

Build e push da API:

```powershell
cd C:\Users\ryang\Desktop\projetosv2\projeto
docker build -t ryang20/prumo-api:1.0.32 server
docker push ryang20/prumo-api:1.0.32
```

Atualizar servidor:

```bash
ssh -o ProxyCommand="cloudflared access ssh --hostname ssh.prumosistemas.com.br" server@localhost
cd /home/server/prumo-src
git pull --ff-only
cp deploy/docker-compose.yml /opt/prumo/app/deploy/docker-compose.yml
cd /opt/prumo/app/deploy
# editar .env para PRUMO_API_IMAGE=ryang20/prumo-api:1.0.35 e pool Modal 30
docker compose pull prumo-api
docker compose up -d --remove-orphans
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
