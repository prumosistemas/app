# Prumo Sistemas App

Versao: **1.0.74 - rota hibrida Hugging Face para o Modo IA**

## Estado atual

- HTMLs criticos servidos pelo Worker em `https://app.prumosistemas.com.br`; Netlify permanece como publicacao complementar ligada ao GitHub.
- Worker Cloudflare de producao: `morning-credit-8a59`.
- D1 de producao: `db`.
- API Python no servidor: `prumo-api`.
- Navegadores: `30` sessoes Modal/turbo.
- Portal Nacional: Google Modo IA em quatro contêineres Modal (2+2), com um Space privado Hugging Face como egress visual adicional e ThinkPad como último fallback; sem Florence/Cohere. A conta principal prefere HF e volta ao egress Modal se o Space falhar; a conta reserva faz a ordem inversa.
- Runs do Portal podem ser continuadas do checkpoint sem reindexar ou perder XML/PDF. Indisponibilidade do Portal ou do solver mantém a run viva com backoff crescente, reduz a concorrência para um probe e volta automaticamente à velocidade normal após sucesso. Respostas HTTP 503 do solver preservam o motivo JSON; bloqueio explícito do Google (`unusual traffic`/`sorry/index`) afasta somente o endpoint afetado por cinco minutos.
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
| `docs/AI_OPERATOR_CONTEXT.md` | Entrada canonica para IA operar sem ver credenciais |
| `docs/OPERACAO_PRUMO_DETALHADO.md` | Contexto operacional |
| `docs/CONTEXTO_ATUAL_2026-07-10.md` | Snapshot historico da arquitetura em 2026-07-10 |
| `docs/C4.md` | C4 canônico e decisões arquiteturais atuais |
| `docs/RELATORIO_AUDITORIA_2026-07-10.md` | Relatorio historico da auditoria de 2026-07-10 |

## Solver do Portal Nacional

O unico resolvedor visual ativo e o Google Modo IA do projeto organizado. O
navegador do hCaptcha continua no Modal; somente a imagem efemera do desafio e
o prompt podem seguir ao Space privado Hugging Face. Certificado, cookies do
Portal e arquivos fiscais nunca saem do ThinkPad. O código validado está
versionado em `solver/google_ai_mode/`.

```powershell
cd C:\Users\ryang\Desktop\projetosv2\projeto
python -m ops.prumo_ops modal deploy --account primary --target portal
python -m ops.prumo_ops modal deploy --account fallback --target portal
```

## Operacao segura e deploy rapido

Todos os provedores usam o cofre DPAPI local e aliases. Leia
`docs/AI_OPERATOR_CONTEXT.md`. Nenhum comando precisa conter credencial literal.

```powershell
cd C:\Users\ryang\Desktop\projetosv2\projeto
python -m ops.prumo_ops secrets migrate-local
python -m ops.prumo_ops status
python -m py_compile server\main.py server\db.py server\domain.py server\run_queue.py
git status
```

Worker:

```powershell
python -m ops.prumo_ops cloudflare deploy
python -m ops.prumo_ops cloudflare deploy --apply
```

Modal:

```powershell
python -m ops.prumo_ops modal deploy --account primary --target iss
python -m ops.prumo_ops modal deploy --account primary --target portal
python -m ops.prumo_ops modal deploy --account fallback --target portal
```

API:

```powershell
docker build -f server/Dockerfile -t ryang20/prumo-api:1.0.73 .
# Opcional, somente quando a autenticacao do registry estiver valida:
docker push ryang20/prumo-api:1.0.73
```

O caminho validado em 2026-07-15 foi construir a imagem diretamente no
ThinkPad depois do `git pull`.

Servidor:

```powershell
python -m ops.prumo_ops server deploy
python -m ops.prumo_ops server deploy --apply
```

## Documentacao

Leia primeiro:

- `docs/SERVER_CONTEXT.md`
- `docs/OPERACAO_PRUMO_DETALHADO.md`
