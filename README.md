# Prumo Sistemas App

Versao: **1.0.99 - Portal resiliente e resolvedor supervisionado**

## Estado atual

- HTMLs criticos servidos pelo Worker em `https://app.prumosistemas.com.br`; Netlify permanece como publicacao complementar ligada ao GitHub.
- Worker Cloudflare de producao: `morning-credit-8a59`.
- Login: rate limits e persistencia de sessao usam batches D1 sequenciais; limpeza fica somente no cron. Timeouts e resets transitórios do D1 recebem até cinco tentativas no Worker e até três tentativas transparentes na tela, sem repetir credencial inválida nem reduzir a segurança do PBKDF2.
- D1 de producao: `db`.
- D1 com replicacao global de leitura `auto` e Sessions API `first-primary`; leituras posteriores podem usar replicas sem perder consistencia da autenticacao.
- API Python no servidor: `prumo-api`.
- Navegadores ISS: Browserless nas três contas Modal, ponderado em 18/4/8 (principal/reserva/terceira). `404 workspace disabled`, limite e falhas transitórias abrem cooldown por endpoint; as contas disponíveis assumem e a principal é sondada novamente automaticamente.
- Portal Nacional: Google Modo IA com dois Spaces privados Hugging Face como primeira análise visual; os navegadores rodam nas três contas Modal em failover e o ThinkPad usa apenas uma vaga de último recurso. Sem Florence/Cohere. A captura temporal usa 30 quadros/8,7 s e gera montagem/MP4 fora do caminho crítico.
- Um Modal sem crédito/`workspace disabled` fica em quarentena compartilhada por 30 minutos. A próxima atividade após o prazo sonda novamente o principal; sucesso o recoloca automaticamente na frente, sem regra de calendário ou intervenção manual.
- A auditoria do master mostra rota, latência, concorrência, bloqueio `unusual`, cliques, trocas de desafio, imagens-resumo e MP4. O ThinkPad guarda esse espelho por sete dias; frames do Modal permanecem nos Volumes Modal, enquanto uma resolução feita pelo fallback residencial conserva sua própria evidência local pelo mesmo prazo.
- Cada conta do espelho Modal possui timeout de sincronização e o manifesto conserva somente a janela atual, evitando thread presa e índice crescente indefinidamente.
- Runs do Portal podem ser continuadas do checkpoint sem reindexar ou perder XML/PDF. Indisponibilidade do Portal ou do solver mantém a run viva, reduz a concorrência para um probe e volta automaticamente à velocidade normal após sucesso. `Unusual traffic` é tratado como falha daquela tentativa, não como objetivo de otimização: endpoints Modal voltam ao pool em 10–15 s e o probe global cresce somente até 120 s. A run publica heartbeat e progresso a cada 10 s.
- O indexador reconhece tanto `Total de 1 registro` quanto `Total de N registros`; uma janela com exatamente uma nota não é mais confundida com resposta inválida.
- `Notas automático` consulta cada certificado uma vez por dia em XML+PDF, começa na data inicial escolhida, repete dois dias para segurança e conserva as capturas por 123 dias. Os horários são igualmente distribuídos pelas 24 horas e um rebalanceamento nunca pula a tentativa do dia. O histórico fica separado por certificado/empresa, inclui ciclos com erro, mostra `+novas` e total deduplicado e permite ZIP por data de emissão e competência. `Capturar agora` é bloqueado somente enquanto aquele colaborador possui uma run do Portal ativa.
- A lista principal de runs não percorre mais todos os XML/PDF a cada atualização. Os arquivos são enumerados somente ao abrir o detalhe, mantendo a tela rápida com histórico longo.
- A exportação de escrituração do ISS é obtida pelo link gerado dentro do navegador autenticado. Arquivos vazios, HTML de erro e planilhas estruturalmente inválidas deixam de ser aceitos como sucesso; o log registra bytes e linhas físicas dos XMLs internos, contornando metadados de dimensão incorretos do portal.
- Downloads gerais do ISS apresentam pastas como `Nome - CNPJ`, inclusive para runs antigas armazenadas internamente como `CNPJ - Nome`; checkpoints e dados persistidos não são renomeados.
- HTTP 530/erro 1033 e páginas HTML de infraestrutura da API são convertidos pelo Worker em JSON 503 curto, seguro e tentável. O HTML da Cloudflare não é exibido ao usuário nem inserido nas telas.
- `Checar encerramento` consulta uma ou mais contas ISS por requests diretos do ThinkPad, sem ocupar Browserless ou Modal. O backend limita seis sessões HTTP globais, até quatro por conta, permite usuários simultâneos e compartilha as cinco verificações mais recentes com todos os usuários da mesma empresa, identificando quem executou cada uma.
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
| `server/iss_closure_scan.py` | Varredura HTTP de encerramento da escrituração ISS |
| `deploy/modal_browserless.py` | Browserless no Modal |
| `solver/google_ai_mode/` | Código versionado do único resolvedor do Portal |
| `deploy/huggingface/navegador-headless/` | Casca Gradio/Chrome versionada dos Spaces HF; o deploy injeta o resolvedor canônico |
| `deploy/docker-compose.yml` | Compose de producao com `prumo-api` |
| `docs/SERVER_CONTEXT.md` | Runbook do servidor |
| `docs/AI_OPERATOR_CONTEXT.md` | Entrada canonica para IA operar sem ver credenciais |
| `docs/HUGGINGFACE_CONTEXT.md` | Contas, aliases, limite gratuito e operacao dos Spaces HF |
| `docs/OPERACAO_PRUMO_DETALHADO.md` | Contexto operacional |
| `docs/CONTEXTO_ATUAL_2026-07-10.md` | Snapshot historico da arquitetura em 2026-07-10 |
| `docs/C4.md` | C4 canônico e decisões arquiteturais atuais |
| `docs/RELATORIO_AUDITORIA_2026-07-10.md` | Relatorio historico da auditoria de 2026-07-10 |

## Solver do Portal Nacional

O unico resolvedor visual ativo e o Google Modo IA do projeto organizado. O
navegador do hCaptcha continua no Modal; somente a imagem efemera do desafio e
o prompt podem seguir aos Spaces privados Hugging Face. Certificado, cookies do
Portal e arquivos fiscais nunca saem do ThinkPad. O motor validado esta
versionado em `solver/google_ai_mode/`; a casca dos Spaces fica em
`deploy/huggingface/navegador-headless/` e recebe esse motor canonico durante o
deploy, sem depender de uma copia em Downloads.

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
docker build -f server/Dockerfile -t ryang20/prumo-api:1.0.99 .
# Opcional, somente quando a autenticacao do registry estiver valida:
docker push ryang20/prumo-api:1.0.99
```

O caminho validado em 2026-07-15 foi construir a imagem diretamente no
ThinkPad depois do `git pull`. O deploy conserva a imagem corrente e duas
anteriores para rollback e remove tags locais mais antigas somente depois do
health check da nova API.

Servidor:

```powershell
python -m ops.prumo_ops server deploy
python -m ops.prumo_ops server deploy --apply
```

## Documentacao

Leia primeiro:

- `docs/SERVER_CONTEXT.md`
- `docs/OPERACAO_PRUMO_DETALHADO.md`
