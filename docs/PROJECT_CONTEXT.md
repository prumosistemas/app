# Contexto do projeto Prumo

## Objetivo

O Prumo centraliza automações fiscais para ISS Fortaleza e Portal Nacional de NFS-e. O frontend é estático, a borda/autenticação roda em Cloudflare Worker e a API Python executa no servidor com dados persistentes por empresa e colaborador.

## Onde está cada coisa

| Área | Caminho |
|---|---|
| Frontend | `iss-fortaleza.html`, `portal-nacional.html` |
| Worker de borda | `cloudflare/worker/` |
| API e filas | `server/main.py`, `server/run_queue.py` |
| ISS Fortaleza | `server/flow_*.py` |
| Portal Nacional | `server/portal_nacional.py`, `server/portal_nacional_automation.py` |
| Deploy Modal | `deploy/` |
| Testes | `tests/` |
| Operação | `docs/SERVER_CONTEXT.md`, `docs/OPERACAO_PRUMO_DETALHADO.md` |

## Estado validado em 2026-07-12

- API em produção: 1.0.43.
- ISS Laryssa: run real concluída na primeira tentativa, 242 prestadas e 4 tomadas.
- ISS padrão: Modal direto; proxy brasileira preservada como fallback configurável.
- Token do Browserless rotacionado em 2026-07-12; deploy Modal e handshake WebSocket 101 validados após a rotação.
- ISS Gabriel: bloqueado por cadastro sem usuário/senha; erro agora é classificado como `ACCOUNT_CREDENTIALS_MISSING`.
- Portal Alan: certificado recuperado e recriptografado; dois testes controlados concluíram XML e PDF pelo modo IA, sem erro.
- Testes locais: 53 aprovados no deploy 1.0.43.

## Regras operacionais

- Estado local em `server/output/` não prova produção; confirme por SSH.
- Não exiba segredos, senhas, cookies, PFX ou blobs completos do banco.
- Uma tentativa filha bem-sucedida não altera o resultado histórico da run raiz no ISS.
- Teste Portal/ISS com lote mínimo antes de ampliar concorrência.
- Frontend publicado pelo fluxo automático GitHub para Netlify; não faça deploy manual do Netlify.
- Mudanças no Worker Cloudflare são separadas do deploy estático e devem preservar rotas internas bloqueadas.

## Pendências externas

- O deploy automático Netlify recebe os commits, mas está sendo ignorado por limite de créditos da conta. O ISS otimizado está ao vivo; o novo HTML do Portal aguarda a regularização do Netlify.
- Preencher as credenciais do Gabriel para concluir o teste ISS dele.
- No servidor, habilitar linger do usuário e reiniciar o monitor com privilégios administrativos para ele carregar o segredo atual.
- O resolvedor Google residencial depende do processo local/túnel; o Cohere Modal deve permanecer configurado como fallback.
