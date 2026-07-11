# Operacao Prumo Detalhada

Este documento e a fonte de contexto operacional da versao 1.0.42.

## Estado desejado

- Producao unica.
- Sem homologacao no codigo.
- HTMLs no Netlify com URLs limpas.
- Worker de producao `morning-credit-8a59`.
- D1 de producao `db`.
- API Python no servidor local Linux.
- `prumo-api` como unico container principal da Prumo.
- Browserless local desligado.
- Modal `prumo-browserless` com 30 sessoes turbo pela API.
- Modal `prumo-portal-nacional-solver` separado, usado so para resolver hCaptcha do Portal Nacional.
- GitHub, pasta local e servidor na mesma versao.

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

- Perfil CLI: `jorhinhogames`
- App ISS: `prumo-browserless`
- Arquivo ISS: `deploy/modal_browserless.py`
- App Portal Nacional: `prumo-portal-nacional-solver`
- Arquivo Portal Nacional: `deploy/modal_portal_nacional_solver.py`

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

O Worker expoe `/api/master/modal-billing` para o master e encaminha para a API Python. A API Python consulta a API `modal.billing.workspace_billing_report`.

Variaveis necessarias no servidor:

```env
MODAL_TOKEN_ID=...
MODAL_TOKEN_SECRET=...
MODAL_MONTHLY_CREDIT_USD=30.00
MODAL_BILLING_APP_NAME=prumo-browserless
```

O saldo exibido e calculado assim:

```text
credito_restante = MODAL_MONTHLY_CREDIT_USD - custo_modal_no_mes
```

Em 2026-07-05, o custo retornado pelo Modal para julho foi aproximadamente `1.79333653` USD; com credito mensal de `30.00`, o saldo estimado ficou `28.21`.

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
- Modal `prumo-portal-nacional-solver` apenas para hCaptcha;
- upload de certificado `.pfx`/`.p12` por colaborador, com senha validada e protegida no servidor;
- sessao gerada diretamente pelo PFX no runtime atual, sem depender da store Windows no Linux.

Em 2026-07-05 o Netlify bloqueou novos deploys por credito da conta. A central `/` e a rota limpa `/portal-nacional` foram mantidas ativas por rotas especificas do Cloudflare Worker `morning-credit-8a59` (`app.prumosistemas.com.br/` e `app.prumosistemas.com.br/portal-nacional*`), que entregam `index.html` e `portal-nacional.html` diretamente.

Arquivos principais:

```text
portal-nacional.html
server/portal_nacional.py
server/portal_nacional_automation.py
server/portal_nacional_session.py
deploy/modal_portal_nacional_solver.py
deploy/portal_nacional_solver.py
```

Teste local confirmado em 2026-07-06:

- PFX `LOQUICENTER LOCADORA 11728000148` com senha `Loqui450` abriu e gerou sessao logada no Portal Nacional;
- upload local pela API retornou `200`, apareceu no estado e foi excluido com `200`;
- indexacao por requests para 01/07/2026 a 06/07/2026 capturou `26/26` notas recebidas em 2 paginas;
- download real `XML e PDF`, `max=1`, falhou no solver por `solver:cohere_rate_limited` (`Cohere 429`), sem falha de certificado/sessao;
- teste anterior em 2026-07-05: indexacao por requests com 86 notas recebidas;
- teste anterior em 2026-07-05: download local com 1 XML e 1 PDF validos;
- producao Gabriel: run `20260705-210520-recebidas-20260601-20260630-cert00-pdf`, 1 PDF valido, status `finalizado_parcial`, erros `0`;
- producao Gabriel: run `20260705-215220-recebidas-20260601-20260630-cert00-pdf`, 1 PDF valido, status `finalizado_parcial`, erros `0`;
- PDF com cabecalho `%PDF-1.4`;
- XML com raiz `NFSe`;
- sessao local sem proxy caiu para login no servidor; sessao local com `--proxy http://127.0.0.1:31480` funcionou na producao.
- XML em producao recebeu hCaptcha canvas nao-9 e falhou quando a Cohere retornou `429 Too Many Requests`; o erro agora aparece como `solver:cohere_rate_limited`, sem mascarar como `token_nao_voltou`.
- O solver Modal `2026-07-05-modal-xvfb-proxy-hybrid-non9` usa proxy do servidor, recarrega desafios nao-9 algumas vezes e depois chama IA uma vez para evitar queima de cota.
- desafios hCaptcha ainda dependem da API de visao; por isso o timeout do solver deve ficar configurado por `PORTAL_NACIONAL_SOLVER_TIMEOUT_SECONDS=240` e os retries reaproveitam arquivos ja baixados.

Gerar sessao pelo IP do servidor usando store Windows, caminho legado:

```powershell
cloudflared access tcp --hostname modal-proxy.prumosistemas.com.br --url 127.0.0.1:31480
python server\portal_nacional_session.py --cert-index 3 --proxy http://127.0.0.1:31480 --out sessao_nfse.txt
```

Em producao, prefira cadastrar o PFX pela aba `Certificados` em `/portal-nacional`.

Health do solver:

```powershell
Invoke-RestMethod https://jorhinhogames--prumo-portal-nacional-solver-solver-server.modal.run/health
```

Deploy do solver:

```powershell
modal profile use jorhinhogames
modal deploy deploy\modal_portal_nacional_solver.py
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
wrangler deployments list --name morning-credit-8a59
modal billing report --for "this month" --json
```

Servidor:

```bash
docker ps
docker compose ps
curl -fsS http://127.0.0.1:8000/
docker logs --tail 100 prumo-api
```
