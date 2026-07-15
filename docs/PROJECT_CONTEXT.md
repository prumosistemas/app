# Contexto do projeto Prumo

## Objetivo

O Prumo centraliza automações fiscais para ISS Fortaleza e Portal Nacional de NFS-e. O frontend é estático, a borda/autenticação roda em Cloudflare Worker e a API Python executa no servidor com dados persistentes por empresa e colaborador.

## Onde está cada coisa

| Área | Caminho |
|---|---|
| Frontend | `iss-fortaleza.html`, `portal-nacional.html` |
| Worker de borda | `cloudflare/worker.js` |
| API e filas | `server/main.py`, `server/run_queue.py` |
| ISS Fortaleza | `server/flow_*.py` |
| Portal Nacional | `server/portal_nacional.py`, `server/portal_nacional_automation.py` |
| Deploy Modal | `deploy/` |
| Testes | `tests/` |
| Operação | `docs/SERVER_CONTEXT.md`, `docs/OPERACAO_PRUMO_DETALHADO.md` |

## Estado validado em 2026-07-15

- API alvo: 1.0.47, com autenticação mTLS direta no ThinkPad, Modal principal, segunda conta Modal e fallback residencial do solver.
- Portal Alan/SIM7: run real finalizada com quatro navegadores, 169/169 notas baixadas. A validação física abriu o índice e confirmou 169 PDFs válidos e 169 XMLs parseáveis, sem arquivo ausente ou inválido.
- Solver Portal: Google Modo IA v18 unificado. A conta principal fica aquecida e a conta `fabriciofarofa5` escala a zero quando ociosa; cada conta pode escalar até quatro containers. Não há Florence, Cohere nem resolvedor separado para grade de nove imagens.
- ISS Laryssa: a run real mais recente validada concluiu na primeira tentativa, com 242 prestadas e 4 tomadas.
- ISS Gabriel: a run real mais recente validada concluiu 12/12 fluxos. A raiz histórica anterior continua mostrando 12 erros corretamente, mas retentativas de bloqueios definitivos deixaram de ser agendadas.
- ISS padrão: Modal direto. O proxy continua no ThinkPad, mas não deve ser ativado no Modal sem autenticação de máquina no Cloudflare Access.
- Token do Browserless rotacionado em 2026-07-12; deploy Modal e handshake WebSocket 101 validados após a rotação.
- Login Firefox: Bearer atual tem precedência sobre cookie antigo, as páginas autenticadas usam mesma origem e login/admin/master são entregues pelo Worker com `Cache-Control: no-store`.
- Monitor do ThinkPad: segredo sincronizado, arquivo de ambiente em modo `600` e `/api/internal/runtime-metrics` respondendo 200.
- Imagem do servidor: `ryang20/prumo-api:1.0.47`, ID `sha256:0612f93a2e9a...`; em 2026-07-15 os hashes de `main.py` e do solver dentro do container eram identicos aos da fonte em `/home/server/prumo-src`.
- Cloudflare: Worker `morning-credit-8a59` no deploy `b8dd0650-6555-41d1-bdac-aa34bda09e35`; bundle local validado em dry-run com 119,98 KiB gzip e zero vulnerabilidades no `npm audit`.
- Modal: somente `ryangurgell20` e `fabriciofarofa5` permanecem como solvers Portal ativos. O app Florence e os apps Prumo da conta desabilitada `jorhinhogames` foram parados em 2026-07-15; `prumo-browserless` foi migrado para `ryangurgell20` e validado por handshake real.
- Servidor: Docker, cloudflared, monitor e Fail2ban ativos; 23% do disco usado, 72 GiB livres e artefatos do solver em 3,0 GiB após a primeira compactacao.
- Testes locais: 69 aprovados para o deploy 1.0.47.

## Regras operacionais

- Estado local em `server/output/` não prova produção; confirme por SSH.
- Não exiba segredos, senhas, cookies, PFX ou blobs completos do banco.
- Uma tentativa filha bem-sucedida não altera o resultado histórico da run raiz no ISS.
- Teste Portal/ISS com lote mínimo antes de ampliar concorrência.
- GitHub e a fonte dos HTMLs. Login, master, admin, ISS, Portal e raiz são entregues diretamente pelo Worker; o fluxo automático GitHub para Netlify fica como publicação complementar quando a conta tiver créditos.
- Mudanças no Worker Cloudflare são separadas do deploy estático e devem preservar rotas internas bloqueadas.

## Pendências externas

- O deploy automático Netlify pode ser ignorado por limite de créditos da conta. As telas críticas atualizadas continuam ao vivo pelas rotas do Worker Cloudflare, sem deploy manual obrigatório.
- Debug visual fica por sete dias. Após 15 minutos, conteúdo textual é gzipado e PNG vira WebP lossless; o compose limita logs Docker a 3 x 10 MiB.
- O registro Docker externo não foi usado no último deploy: a imagem 1.0.47 foi construída e validada diretamente no ThinkPad. Manter a 1.0.46 como rollback local.
- O resolvedor anterior foi removido. O único caminho permitido para hCaptcha é o Google Modo IA versionado em `solver/google_ai_mode`, direto pelo Modal. A proxy do servidor só poderá ser ativada após autenticação de máquina no Cloudflare Access.
