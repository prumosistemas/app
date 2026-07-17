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

## Estado validado em 2026-07-17

- API alvo: 1.0.51, com autenticação mTLS direta no ThinkPad, Modal principal, segunda conta Modal e fallback residencial do solver.
- Portal Nacional: o período é dividido por mês em janelas inclusivas de até 30 dias, cada janela é validada contra o total informado pelo Portal e os IDs são unidos sem duplicação. O período não filtra a competência: notas retroativas continuam incluídas. Para a SIM7, o Portal informou 169 emitidas em 01/06-30/06 e 205 em 01/07-17/07.
- Portal Alan/SIM7: as quatro runs mais recentes finalizaram 84/84, 35/35, 50/50 e 74/74. Os 243 PDFs e 243 XMLs referenciados existem, são não vazios e todos os XMLs são parseáveis. Houve retries recuperados por widget/captcha e renovação de sessão, sem nota pendente no resultado final.
- Solver Portal: Google Modo IA v19 unificado. A conta `ryangurgell20` fica aquecida; `fabriciofarofa5` escala a zero e e usada em quota/indisponibilidade. Falha visual especifica segue direto ao ThinkPad para não cobrar a mesma tentativa nas duas contas. Não há Florence, Cohere nem resolvedor separado para grade de nove imagens.
- ISS Laryssa: a prova real `run_OY1xfaaUUenSaIS_pgioDw` concluiu Notas na primeira tentativa em 6min56s, com 242 prestadas/25 páginas e 4 tomadas/1 página, 26 XMLs novos e zero erro.
- ISS Gabriel: a run real mais recente validada concluiu 12/12 fluxos. A raiz histórica anterior continua mostrando 12 erros corretamente, mas retentativas de bloqueios definitivos deixaram de ser agendadas.
- ISS padrão: Modal direto. O proxy continua no ThinkPad, mas não deve ser ativado no Modal sem autenticação de máquina no Cloudflare Access.
- Token do Browserless rotacionado em 2026-07-12; deploy Modal e handshake WebSocket 101 validados após a rotação.
- Login Firefox: Bearer atual tem precedência sobre cookie antigo, as páginas autenticadas usam mesma origem e login/admin/master são entregues pelo Worker com `Cache-Control: no-store`.
- Login/Worker: o incidente `1101` de 2026-07-17 revelou rejeições assíncronas escapando do `try/catch` porque os handlers eram retornados sem `await`. Todas as rotas assíncronas agora são aguardadas dentro da barreira de erro; respostas HTML de infraestrutura são reduzidas a uma mensagem segura com código de suporte, sem inserir o documento da Cloudflare no formulário.
- Monitor do ThinkPad: segredo sincronizado, arquivo de ambiente em modo `600` e `/api/internal/runtime-metrics` respondendo 200.
- Imagem alvo do servidor: `ryang20/prumo-api:1.0.51`; manter a 1.0.50 como rollback local até concluir a prova real.
- Cloudflare: Worker `morning-credit-8a59` no deploy `b8dd0650-6555-41d1-bdac-aa34bda09e35`; bundle local validado em dry-run com 119,98 KiB gzip e zero vulnerabilidades no `npm audit`.
- Modal: somente `ryangurgell20` e `fabriciofarofa5` permanecem como solvers Portal ativos. O app Florence e os apps Prumo da conta desabilitada `jorhinhogames` foram parados em 2026-07-15; `prumo-browserless` foi migrado para `ryangurgell20` e validado por handshake real.
- Servidor: Docker, cloudflared, monitor e Fail2ban ativos; 23% do disco usado, 72 GiB livres e artefatos do solver em 3,0 GiB após a primeira compactacao.
- Testes locais: 84 aprovados na versão 1.0.51; o Worker mantém a correção defensiva de autenticação.
- Prova isolada pós-deploy: o solver residencial v19 abriu o hCaptcha real após recovery, atravessou quatro etapas visuais e devolveu token; ao final havia 0/4 navegadores locais ativos.
- Billing em 2026-07-16: principal com US$ 6,38 no mês (US$ 4,46 do app Portal; saldo estimado US$ 23,62) e fallback com US$ 2,37 (saldo estimado US$ 27,63).

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
- O registro Docker externo não é necessário no caminho normal: a imagem 1.0.51 pode ser construída diretamente no ThinkPad após `git pull`. Manter a 1.0.50 como rollback local.
- O resolvedor anterior foi removido. O único caminho permitido para hCaptcha é o Google Modo IA versionado em `solver/google_ai_mode`, direto pelo Modal. A proxy do servidor só poderá ser ativada após autenticação de máquina no Cloudflare Access.
