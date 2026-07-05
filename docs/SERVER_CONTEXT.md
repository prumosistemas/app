# Contexto do servidor Prumo

Versao operacional: `1.0.30`
Data de atualizacao: `2026-07-05`
Host: `server@ssh.prumosistemas.com.br` via Cloudflare Access

## Acesso

Use:

```bash
ssh -o ProxyCommand="cloudflared access ssh --hostname ssh.prumosistemas.com.br" server@localhost
```

O SSH depende do servico `cloudflared.service`, configurado em `/etc/cloudflared/config.yml`. Nao remover esse servico.

## Topologia atual

- API FastAPI: container `prumo-api`, porta local `127.0.0.1:8000`.
- Browserless local: container `browserless`, porta local `127.0.0.1:3000`.
- Worker publico: `https://morning-credit-8a59.prumo-sistema.workers.dev`.
- App publico/HTMLs: Netlify a partir dos HTMLs versionados no repo.
- Dados persistentes da API: `/opt/prumo/data`.
- Deploy Compose: `/opt/prumo/app/deploy`.

## Observacao da versao 1.0.30

A API faz uma limpeza controlada da home do ISS antes de entrar nos menus de topo. O modal benigno `Pesquisa Sefin` e respondido com `Nao`; modais reais de mensagem pendente continuam gerando `MENSAGEM_NA_TELA`; mascaras RichFaces/AJAX sem conteudo util sao removidas antes de acessar Escrituração, NFS-e e DAM.

Na Escrituração, a API tambem aguarda o resultado de `Consultar` antes de ler os links de `Escriturar/Reabrir`, aceita a tela `Escrituração Fiscal` já aberta como sucesso e exige estabilidade por leituras consecutivas para evitar falso erro quando o portal navega ou troca o DOM durante a automação.

A UI cria runs com retry automatico seguro preselecionado. O servidor limita a cadeia por `AUTO_RETRY_MAX_ATTEMPTS` e por uma trava dura de codigo de 3 tentativas, agendando nova tentativa somente para erros marcados como retryable que nao sejam finais de negocio/portal, como `CNPJ_INEXISTENTE`, `CNPJ_MISMATCH`, `MENSAGEM_NA_TELA`, `LOGIN_ERROR` e `PORTAL_ACCESS_BLOCKED`. Os botoes de ZIP/download e a paginacao da run selecionada mostram loader local durante a acao.

A versao 1.0.30 tambem separa producao e homologacao no Worker/D1, remove `.html` das URLs publicas via Netlify, deixa o caminho de login/logout mais leve no Worker e remove o box visual `browserTurboBox` da tela ISS Fortaleza.
- Codigo espelho no servidor: `/home/server/prumo-src`.
- Proxy do IP do servidor para Modal: `/home/server/prumo-proxy`.

## Cloudflare e tuneis

Ha dois conjuntos importantes:

- `/etc/cloudflared/config.yml`: tunel principal para:
  - `ssh.prumosistemas.com.br` -> `ssh://localhost:22`
  - `browser.prumosistemas.com.br` -> `http://localhost:3000`
  - `api.prumosistemas.com.br` -> `http://localhost:8000`
- `/home/server/prumo-proxy/tunnel-config.yml`: tunel TCP `modal-proxy.prumosistemas.com.br` para `tcp://localhost:31381`.

O `prumo-proxy` permite que o Browserless do Modal saia para o portal ISS usando o IP do servidor. Ele nao faz parte do Docker Compose principal, mas e intencional.

## Capacidade de navegadores

Configuracao de producao apos a organizacao:

- Browserless local: `5` sessoes.
- Modal turbo: `32` sessoes.
- Total API: `37` navegadores.

Variaveis relevantes em `/opt/prumo/app/deploy/.env`:

```env
BASE_BROWSER_SLOTS=5
MAX_BROWSERS=37
BROWSER_CDP_POOL=browserless-local|5|ws://browserless:3000?token=...;;modal-turbo|32|wss://...
```

No Compose, o Browserless local tambem deve ficar em:

```yaml
CONCURRENT: "5"
MAX_CONCURRENT_SESSIONS: "5"
```

## Servicos para preservar

- `docker.service`
- `containerd.service`
- `cloudflared.service`
- `fail2ban.service`
- `prumo-monitor.service`
- `ssh.service`
- `cron.service`

## Comandos operacionais

Status:

```bash
docker ps
curl -s http://127.0.0.1:8000/
systemctl --no-pager status cloudflared
systemctl --no-pager status prumo-monitor
```

Rede:

```bash
ss -ltnup
```

Proxy Modal usando IP do servidor:

```bash
curl -I --max-time 20 -x http://127.0.0.1:31381 https://iss.fortaleza.ce.gov.br/
curl -I --max-time 20 -x http://127.0.0.1:31381 https://idp2.sefin.fortaleza.ce.gov.br/
```

Use hosts do portal/IDP nesse teste. Hosts auxiliares como `api.ipify.org` podem retornar `403` se nao estiverem na allowlist do processo ativo.

Atualizar deploy:

```bash
cd /opt/prumo/app/deploy
docker compose --env-file .env pull
docker compose --env-file .env up -d
```

Logs:

```bash
docker logs --tail 200 prumo-api
docker logs --tail 100 browserless
journalctl -u cloudflared -n 100 --no-pager
journalctl -u prumo-monitor -n 100 --no-pager
```

## O que foi limpo nesta organizacao

- Processos soltos antigos de teste grafico/proxy foram encerrados quando nao estavam ligados aos servicos atuais.
- Imagens Docker antigas da API e imagens dangling foram removidas, preservando a imagem em uso e a imagem Browserless digestada.
- Backups/testes antigos de Chromium/Playwright em `/home/server/legado-server-20260626` foram removidos apos verificacao de que nao eram usados pelos servicos atuais.

## Cuidados

- Nao apagar `/opt/prumo/data`: contem SQLite, runs, arquivos e monitoramento.
- Nao apagar `/opt/prumo/app/deploy/.env`: contem secrets e pool ativo.
- Nao versionar `.env`, tokens, arquivos `.json` de tunnel ou dumps SQLite.
- Antes de mexer em Cloudflare, confirmar `wrangler whoami` e o Worker `morning-credit-8a59`.
