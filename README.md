# Prumo Sistemas App

Versao: **1.0.48 - Portal seguro, fallback economico e recovery do hCaptcha**

## Estado atual

- HTMLs criticos servidos pelo Worker em `https://app.prumosistemas.com.br`; Netlify permanece como publicacao complementar ligada ao GitHub.
- Worker Cloudflare de producao: `morning-credit-8a59`.
- D1 de producao: `db`.
- API Python no servidor: `prumo-api`.
- Navegadores: `30` sessoes Modal/turbo.
- Portal Nacional: Google Modo IA v19 no Modal como rota primaria, segunda conta reservada para quota/indisponibilidade e o mesmo resolvedor no ThinkPad para falha visual especifica; sem Florence/Cohere.
- Browserless local: desligado por padrao, documentado como fallback.
- Homologacao: removida do codigo.

## Arquivos principais

| Caminho | Funcao |
| --- | --- |
| `login.html` | Login do app |
| `index.html` | Roteador pos-login |
| `admin.html` | Painel do administrador da empresa |
| `iss-fortaleza.html` | Operacao ISS Fortaleza |
| `master.html` | Painel master |
| `master-company.html` | Detalhe de empresa para master |
| `cloudflare/worker.js` | Auth, empresas, usuarios, pagamentos, D1 e proxy da API |
| `server/` | API FastAPI, filas e fluxos Playwright |
| `deploy/modal_browserless.py` | Browserless no Modal |
| `solver/google_ai_mode/` | Código versionado do único resolvedor do Portal |
| `deploy/docker-compose.yml` | Compose de producao com `prumo-api` |
| `docs/SERVER_CONTEXT.md` | Runbook do servidor |
| `docs/OPERACAO_PRUMO_DETALHADO.md` | Contexto operacional |
| `docs/CONTEXTO_ATUAL_2026-07-10.md` | Snapshot historico da arquitetura em 2026-07-10 |
| `docs/C4.md` | C4 canônico e decisões arquiteturais atuais |
| `docs/RELATORIO_AUDITORIA_2026-07-10.md` | Relatorio historico da auditoria de 2026-07-10 |

## Solver do Portal Nacional

O unico resolvedor ativo e o Google Modo IA do projeto organizado. Ele usa
saida direta do Modal por padrao e guarda apenas o estado anonimo em Volume
privado. O código validado está versionado em `solver/google_ai_mode/`. O deploy
normal usa a conta principal e a conta de fallback:

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

## Deploy rapido

```powershell
cd C:\Users\ryang\Desktop\projetosv2\projeto
python -m py_compile server\main.py server\db.py server\domain.py server\run_queue.py
git status
```

Worker:

```powershell
cd cloudflare
wrangler deploy
```

Modal:

```powershell
cd C:\Users\ryang\Desktop\projetosv2\projeto
modal profile activate ryanzin
modal deploy deploy\modal_browserless.py
```

API:

```powershell
docker build -f server/Dockerfile -t ryang20/prumo-api:1.0.48 .
# Opcional, somente quando a autenticacao do registry estiver valida:
docker push ryang20/prumo-api:1.0.48
```

O caminho validado em 2026-07-15 foi construir a imagem diretamente no
ThinkPad depois do `git pull`.

Servidor:

```bash
ssh -o ProxyCommand="cloudflared access ssh --hostname ssh.prumosistemas.com.br" server@localhost
cd /home/server/prumo-src
git pull --ff-only
docker build -f server/Dockerfile -t ryang20/prumo-api:1.0.48 .
cp deploy/docker-compose.yml /opt/prumo/app/deploy/docker-compose.yml
cd /opt/prumo/app/deploy
docker compose up -d --force-recreate --remove-orphans
curl -fsS http://127.0.0.1:8000/
```

## Documentacao

Leia primeiro:

- `docs/SERVER_CONTEXT.md`
- `docs/OPERACAO_PRUMO_DETALHADO.md`
