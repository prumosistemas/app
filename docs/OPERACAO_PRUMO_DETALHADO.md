# Operacao Prumo Detalhada

Este documento e a fonte de contexto operacional da versao 1.0.31.

## Estado desejado

- Producao unica.
- Sem homologacao no codigo.
- HTMLs no Netlify com URLs limpas.
- Worker de producao `morning-credit-8a59`.
- D1 de producao `db`.
- API Python no servidor local Linux.
- `prumo-api` como unico container principal da Prumo.
- Browserless local desligado.
- Modal `prumo-browserless` com 40 sessoes turbo.
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
- App: `prumo-browserless`
- Arquivo: `deploy/modal_browserless.py`

## Dados e volatilidade

Nao sao volateis:

- Empresas, usuarios, pagamentos e logs do app ficam no D1.
- Contas ISS, conjuntos, runs e arquivos ficam em `/opt/prumo/data`.
- O SQLite da API fica em `/opt/prumo/data/_api_data/iss_automacao.db`.
- O container monta `/opt/prumo/data:/app/output`.

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

O painel master consulta `/py/api/admin/modal-billing`, que usa a API `modal.billing.workspace_billing_report` dentro da API Python.

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

A homologacao foi removida da versao 1.0.31. Os arquivos HTML sempre apontam para producao.

Se existir recurso antigo no Cloudflare:

- Worker antigo: `morning-credit-8a59-homologacao`
- D1 antigo: `db-homologacao`

Eles nao sao usados pelo codigo atual.

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
