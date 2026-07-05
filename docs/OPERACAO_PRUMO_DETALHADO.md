# Operacao Prumo Sistemas

Versao operacional: `1.0.30`
Data: `2026-07-05`

Este documento e a referencia operacional do app Prumo/ISS Fortaleza. Ele descreve o que roda no PC local, no GitHub, no Netlify, na Cloudflare, no servidor Linux e no Modal. Segredos, tokens e senhas nao devem ser gravados aqui nem no Git.

## Locais oficiais

- Pasta local Windows: `C:\Users\ryang\Desktop\projetosv2\projeto`
- GitHub: `https://github.com/prumosistemas/app`
- Branch de producao: `main`
- Espelho/build no servidor: `/home/server/prumo-src`
- Deploy Compose no servidor: `/opt/prumo/app/deploy`
- Dados persistentes no servidor: `/opt/prumo/data`
- Documentacao operacional resumida: `docs/SERVER_CONTEXT.md`
- Documentacao operacional detalhada: `docs/OPERACAO_PRUMO_DETALHADO.md`

## URLs publicas

- Producao: `https://app.prumosistemas.com.br`
- ISS Fortaleza: `https://app.prumosistemas.com.br/iss-fortaleza`
- Login: `https://app.prumosistemas.com.br/login`
- Admin: `https://app.prumosistemas.com.br/admin`
- Master: `https://app.prumosistemas.com.br/master`
- Master empresa: `https://app.prumosistemas.com.br/master-company?id=<company_id>`
- Homologacao Netlify: `https://homologacao--appprumo.netlify.app`

Os arquivos continuam existindo como `login.html`, `iss-fortaleza.html`, etc. O Netlify usa `netlify.toml` para redirecionar URLs antigas com `.html` para URLs limpas e reescrever as URLs limpas para os HTMLs correspondentes.

## Contas e acessos

- Cloudflare/Wrangler: conta `prumo.sistema@gmail.com`
- Netlify CLI: conta `prumo.sistema@gmail.com`, time `PRUMO`, site `appprumo`
- Netlify site id: `dd609699-b9df-497e-b83a-5b961a35a321`
- SSH servidor: usuario `server`, via Cloudflare Access
- Usuarios do app: ficam no D1 (`users`) e sao gerenciados pelo painel master/admin
- Senhas do app, tokens do Worker, tokens Browserless e secrets Modal: nunca versionar

Comando SSH:

```bash
ssh -o ProxyCommand="cloudflared access ssh --hostname ssh.prumosistemas.com.br" server@localhost
```

## Ambientes

### Producao

- Netlify: `https://app.prumosistemas.com.br`
- Worker: `morning-credit-8a59`
- Worker URL: `https://morning-credit-8a59.prumo-sistema.workers.dev`
- D1: `db`
- D1 id: `e69428e3-6524-427c-bae3-d32190e5c229`
- FastAPI: `https://api.prumosistemas.com.br`
- Docker API: container `prumo-api`
- Browserless local: container `browserless`

### Homologacao

- Netlify alias esperado: `https://homologacao--appprumo.netlify.app`
- Worker: `morning-credit-8a59-homologacao`
- Worker URL: `https://morning-credit-8a59-homologacao.prumo-sistema.workers.dev`
- D1: `db-homologacao`
- D1 id: `1c279092-2f87-4dce-9c56-aa12b8df38b6`
- FastAPI: por enquanto usa `https://api.prumosistemas.com.br`

O frontend resolve o Worker automaticamente:

- Host `app.prumosistemas.com.br`: producao
- Host com `homolog`, `deploy-preview` ou `branch-deploy`: homologacao
- `localhost` e `127.0.0.1`: homologacao
- Query `?env=prod`: forca producao
- Query `?env=homologacao`: forca homologacao

## Netlify

O Netlify esta linkado localmente pelo CLI:

```bash
netlify status
```

Deploy de producao normalmente acontece pelo push no GitHub `main`. Deploy manual de homologacao:

```bash
netlify deploy --alias homologacao --dir .
```

Deploy manual de producao, se for necessario:

```bash
netlify deploy --prod --dir .
```

O arquivo `.netlify/` e local e fica ignorado no Git.

## Cloudflare Worker

Arquivo principal: `cloudflare/worker.js`

Configuracao Wrangler versionada: `cloudflare/wrangler.toml`

Deploy producao, explicitando o ambiente raiz:

```bash
cd cloudflare
wrangler deploy --env=""
```

Deploy homologacao:

```bash
cd cloudflare
wrangler deploy --env homologacao
```

Secrets necessarios:

- `ISS_INTERNAL_SECRET`: usado para o proxy seguro com a FastAPI
- `SETUP_TOKEN`: necessario apenas durante setup inicial de um D1 novo
- `ADMIN_EMAIL`: necessario apenas durante setup inicial
- `ADMIN_PASSWORD`: necessario apenas durante setup inicial

Aplicar segredo em producao:

```bash
wrangler secret put ISS_INTERNAL_SECRET --env=""
```

Aplicar segredo em homologacao:

```bash
wrangler secret put ISS_INTERNAL_SECRET --env homologacao
```

O Worker faz:

- Login, logout e `/api/me`
- Roles `master`, `owner`, `member`
- Empresas, usuarios, billing simples e logs
- Proxy `/py/*` para a FastAPI com `ISS_INTERNAL_SECRET`
- Reconciliacao de exclusoes por cron
- CORS para os hosts configurados em `FRONTEND_ORIGINS`

Na versao `1.0.30`, o caminho de login foi aliviado:

- `migrate()` nao roda mais em toda requisicao; fica cacheado por instancia do Worker
- limpeza de `rate_limits`, sessoes expiradas, logs antigos e jobs concluidos saiu do caminho direto do login
- billing state fica cacheado por empresa por 60 segundos no Worker
- `/api/me` so regrava CSRF quando o token enviado nao bate com o hash atual
- logout e best-effort: mesmo com CSRF/sessao expirada, limpa cookie e tenta revogar a sessao
- erros internos retornam `request_id` para facilitar busca no `wrangler tail`

Diagnostico de erro interno:

```bash
cd cloudflare
wrangler tail morning-credit-8a59
wrangler d1 execute db --remote --command "SELECT COUNT(*) AS users FROM users;"
wrangler d1 execute db --remote --command "SELECT (SELECT COUNT(*) FROM sessions) AS sessions, (SELECT COUNT(*) FROM rate_limits) AS rate_limits, (SELECT COUNT(*) FROM logs) AS logs;"
```

Estado observado em `2026-07-05`: D1 de producao com 7 usuarios, 1 sessao, 1 sessao ativa, 0 rate limits, 208 logs e 0 deletion jobs. As consultas responderam em menos de 1 ms, entao o gargalo de login nao era banco inchado.

## D1

Tabelas principais:

- `companies`
- `users`
- `sessions`
- `rate_limits`
- `logs`
- `deletion_jobs`
- `billing_settings`
- `payments`

Indices importantes:

- `idx_users_email`
- `idx_users_company_id`
- `idx_sessions_user_id`
- `idx_sessions_absolute_expires_at`
- `idx_sessions_revoked_at`
- `idx_rate_limits_reset_at`
- `idx_logs_created_at`
- `idx_logs_company_id`

Homologacao com D1 novo fica vazia ate rodar setup. Nao misturar dados de producao no D1 de homologacao.

## FastAPI e automacoes

Container: `prumo-api`

Health local no servidor:

```bash
curl -s http://127.0.0.1:8000/
```

Health externo via tunnel:

```bash
curl -s https://api.prumosistemas.com.br/
```

Pastas importantes:

- Codigo espelho: `/home/server/prumo-src`
- Compose: `/opt/prumo/app/deploy/docker-compose.yml`
- Env Compose: `/opt/prumo/app/deploy/.env`
- Dados persistentes: `/opt/prumo/data`
- SQLite API: `/opt/prumo/data/_api_data/iss_automacao.db`
- Monitor: `/opt/prumo/data/_monitor/metrics.sqlite3`
- Runs: `/opt/prumo/data/empresas/<company_id>/colaboradores/<user_id>/runs/`

O container monta `/opt/prumo/data` em `/app/output`. Nao apagar esse volume.

## Capacidade de navegadores

Configuracao alvo `1.0.30`:

- Browserless local: 5 sessoes
- Modal turbo: 32 sessoes
- Total da API: 37 navegadores

Variaveis esperadas em `/opt/prumo/app/deploy/.env`:

```env
BASE_BROWSER_SLOTS=5
MAX_BROWSERS=37
BROWSER_CDP_POOL=browserless-local|5|ws://browserless:3000?token=...;;modal-turbo|32|wss://jorhinhogames--prumo-browserless-browserless-server.modal.run?token=...
MAX_BROWSER_LIMIT=96
```

No Compose:

```yaml
CONCURRENT: "5"
MAX_CONCURRENT_SESSIONS: "5"
```

## Modal turbo

Arquivo: `deploy/modal_browserless.py`

O Modal usa Browserless com proxy pelo IP do servidor, via tunnel/proxy em:

- `/home/server/prumo-proxy`
- tunnel `modal-proxy.prumosistemas.com.br`
- proxy local do servidor exposto dentro do Modal como `127.0.0.1:31480`

Esse desenho existe porque o portal ISS pode bloquear IPs de cloud. O Modal direto ja foi testado; o modo aprovado e Modal + proxy pelo servidor.

## Retry automatico

O retry automatico vem preselecionado na criacao da run, mas so roda para erros tecnicos marcados como retryable. Erros finais nao entram:

- `CNPJ_INEXISTENTE`
- `CNPJ_MISMATCH`
- `MENSAGEM_NA_TELA`
- `LOGIN_ERROR`
- `PORTAL_ACCESS_BLOCKED`

Garantia anti-loop infinito:

- `AUTO_RETRY_HARD_MAX_ATTEMPTS = 3` em `server/db.py`
- qualquer valor vindo de env, run antiga, duplicacao ou retry passa por `clamp_auto_retry_max_attempts()`
- `maybe_schedule_auto_retry()` verifica quantidade de tentativas do root antes de criar nova tentativa
- retry automatico inclui somente erros seguros/retryable e nao inclui cancelados/interrompidos
- retry manual continua existindo como acao consciente do usuario

## Regras de retencao

- Runs ficam disponiveis por ate 30 dias
- Cada colaborador mantem no maximo 8 runs recentes
- Ao passar do limite, runs antigas sao removidas automaticamente
- Contas e conjuntos nao tem limite de quantidade
- Cada colaborador ve apenas as proprias contas, conjuntos e runs dentro da empresa
- Master ve empresas e logs administrativos
- Owner gerencia colaboradores e pagamento da propria empresa

## Deploy completo

Fluxo recomendado:

```bash
cd C:\Users\ryang\Desktop\projetosv2\projeto
git status
python -m py_compile server\db.py server\main.py server\run_queue.py
node --check cloudflare\worker.js
cd cloudflare
wrangler deploy --dry-run --outdir .wrangler\dry-run-prod --env=""
wrangler deploy --env homologacao --dry-run --outdir .wrangler\dry-run-homologacao
cd ..
git add .
git commit -m "Prepara homologacao e otimiza login"
git push origin main
```

Worker:

```bash
cd cloudflare
wrangler deploy --env=""
wrangler deploy --env homologacao
```

Servidor:

```bash
ssh -o ProxyCommand="cloudflared access ssh --hostname ssh.prumosistemas.com.br" server@localhost
cd /home/server/prumo-src
git pull --ff-only origin main
docker build -t ryang20/prumo-api:1.0.30 -f server/Dockerfile server
cd /opt/prumo/app/deploy
docker compose --env-file .env up -d
curl -s http://127.0.0.1:8000/
```

Antes do `docker compose up`, conferir `.env`:

```bash
grep -E '^(PRUMO_API_IMAGE|BASE_BROWSER_SLOTS|MAX_BROWSERS|BROWSER_CDP_POOL|MAX_BROWSER_LIMIT)=' /opt/prumo/app/deploy/.env
```

## Servicos que nao podem ser removidos

- `docker.service`
- `containerd.service`
- `cloudflared.service`
- `fail2ban.service`
- `prumo-monitor.service`
- `ssh.service`
- `cron.service`
- container `browserless`
- container `prumo-api`
- pasta `/opt/prumo/data`
- pasta `/home/server/prumo-proxy`
- tunnel principal de `/etc/cloudflared/config.yml`
- tunnel/proxy Modal em `/home/server/prumo-proxy/tunnel-config.yml`

## Checklist de verificacao

- `git status` limpo localmente
- GitHub `main` no mesmo commit local
- `/home/server/prumo-src` no mesmo commit GitHub
- `curl http://127.0.0.1:8000/` retorna `version=1.0.30`
- health mostra `max_browsers=37`, `base_browsers=5`, `browser_turbo_extra=32`
- `docker compose ps` mostra `browserless` e `prumo-api` saudaveis
- `wrangler deployments list --name morning-credit-8a59` mostra deploy novo
- `wrangler deployments list --name morning-credit-8a59-homologacao` mostra deploy novo
- `https://app.prumosistemas.com.br/login` abre sem `.html`
- `https://app.prumosistemas.com.br/login.html` redireciona para `/login`
- `https://app.prumosistemas.com.br/iss-fortaleza` abre a tela ISS
- login/logout nao ficam presos em loader
- tela ISS nao exibe `browserTurboBox`
