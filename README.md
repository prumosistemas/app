# Prumo Sistemas App

Versao: **1.0.44 - filas resilientes, logs incrementais e Modo IA v11**

## Estado atual

- Frontend estatico no Netlify: `https://app.prumosistemas.com.br`.
- Worker Cloudflare de producao: `morning-credit-8a59`.
- D1 de producao: `db`.
- API Python no servidor: `prumo-api`.
- Navegadores: `30` sessoes Modal/turbo.
- Portal Nacional: Google Modo IA direto no Modal; proxy do ThinkPad preservado, mas bloqueado para o Modal até existir autenticação de máquina.
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
| `docs/CONTEXTO_ATUAL_2026-07-10.md` | Snapshot vivo da arquitetura e producao |
| `docs/C4.md` | C4 canônico e decisões arquiteturais atuais |
| `docs/RELATORIO_AUDITORIA_2026-07-10.md` | Evidencias, achados e pendencias |

## Solver do Portal Nacional

O unico resolvedor ativo e o Google Modo IA do projeto organizado. Ele usa
saida direta do Modal por padrao e guarda apenas o estado anonimo em Volume
privado. O código validado está versionado em `solver/google_ai_mode/`. Para publicar:

```powershell
cd C:\Users\ryang\Desktop\projetosv2\projeto
modal profile use jorhinhogames
modal deploy deploy\modal_portal_nacional_google_solver.py
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
modal profile use jorhinhogames
modal deploy deploy\modal_browserless.py
```

API:

```powershell
docker build -t ryang20/prumo-api:1.0.44 server
docker push ryang20/prumo-api:1.0.44
```

Servidor:

```bash
ssh -o ProxyCommand="cloudflared access ssh --hostname ssh.prumosistemas.com.br" server@localhost
cd /home/server/prumo-src
git pull --ff-only
cp deploy/docker-compose.yml /opt/prumo/app/deploy/docker-compose.yml
cd /opt/prumo/app/deploy
docker compose pull prumo-api
docker compose up -d --remove-orphans
curl -fsS http://127.0.0.1:8000/
```

## Documentacao

Leia primeiro:

- `docs/SERVER_CONTEXT.md`
- `docs/OPERACAO_PRUMO_DETALHADO.md`
