# Relatorio de seguranca do Prumo / ISS Fortaleza

Data: 2026-06-15

## Objetivo

Fazer uma analise pratica de seguranca do sistema atual, considerando o modelo real de uso:

- autenticacao e permissoes no Cloudflare Worker `morning-credit-8a59`;
- HTMLs publicados no Netlify/app;
- API Python e Browserless em um servidor Linux simples;
- pagamento controlado manualmente pelo master, sem gateway automatico;
- foco em evitar vazamento entre usuarios/empresas, abuso simples, acumulacao de dados e falhas operacionais comuns.

Esta analise nao trata o sistema como banco ou governo de alta escala. A meta e manter simples, funcional e robusto para o uso atual.

## O que foi verificado

- Acesso ao Cloudflare com `wrangler whoami`.
- Historico de deploys do Worker `morning-credit-8a59`.
- Servidor via SSH usando Cloudflare Access.
- Containers em producao.
- Portas expostas dos containers.
- Uso de disco e tamanho atual dos dados.
- Variaveis seguras visiveis no `.env` de deploy.
- Fluxo de autenticacao, proxy `/py/*`, CSRF, rate limit e permissoes no Worker.
- Isolamento de dados por empresa/usuario no backend Python.
- Protecoes de caminho para download/delete de arquivos.
- Retencao de runs e limites de tamanho de payload.

## Estado atual resumido

O sistema esta razoavelmente seguro para o modelo atual.

Nao encontrei um problema critico evidente de isolamento entre empresas/usuarios no desenho principal. A parte mais importante esta correta: o usuario nao chama a API Python diretamente com identidade propria; o Worker autentica, valida permissao, aplica regras de empresa/usuario e so entao repassa para a API com segredo interno.

No servidor, os containers `prumo-api` e `browserless` estao publicados apenas em `127.0.0.1`, nao diretamente na internet. O backend tambem esta configurado com `ISS_ALLOW_DIRECT_LOCAL=false`, entao o caminho esperado de acesso e via Worker/segredo interno.

## Evidencias operacionais

Servidor:

- `prumo-api`: imagem `ryang20/prumo-api:1.0.11`, ativo ha 2 dias.
- `browserless`: ativo ha 11 dias.
- Portas: `127.0.0.1:8000` e `127.0.0.1:3000`.
- Disco: 98 GB total, 24 GB usado, 70 GB livre, 26% de uso.
- Dados em `/opt/prumo/data`: aproximadamente 95 MB.
- Deploy/config: `ISS_ALLOW_DIRECT_LOCAL=false`.
- Worker publico configurado: `https://morning-credit-8a59.workers.dev`.
- `MAX_BROWSERS=15`.

Worker:

- Conta Cloudflare: `prumo.sistema@gmail.com`.
- Worker analisado: `morning-credit-8a59`.
- Ultimo deploy listado inclui mudanca de segredo em 2026-06-12T19:23:50Z.

## Pontos fortes encontrados

### Autenticacao e sessoes

- Login passa pelo Worker.
- Senhas usam PBKDF2-SHA256.
- Existe limite de tentativas por IP/e-mail.
- Cookie de sessao e `HttpOnly`, `Secure` e `SameSite=None` em producao.
- Existem limites de sessoes por usuario.
- Usuarios com troca de senha obrigatoria nao conseguem seguir para areas sensiveis.

### Permissoes e separacao por papel

- Rotas de master, dono/admin da empresa e membro estao separadas por role.
- O Worker confere empresa e usuario antes de operar sobre usuarios da empresa.
- Pagamento/manual billing e aplicado antes de login/listagens relevantes.
- O admin da empresa nao vira gateway de pagamento; o master continua controlando o estado de pagamento.

### Proxy para a API Python

- `/py/*` exige usuario autenticado.
- Metodos com corpo exigem CSRF.
- O Worker injeta `X-Internal-Secret`, `X-Company-Id`, `X-User-Id` e dados do usuario.
- A API Python recusa chamadas sem o segredo interno quando nao esta em modo local.

### Isolamento de arquivos e dados

- Os dados ficam separados por empresa e colaborador.
- O backend usa `safe_slug` para IDs de empresa/usuario.
- Downloads, ZIPs e deletes usam `safe_path_inside`, reduzindo risco de path traversal.
- Conjuntos, contas e runs sao carregados no escopo do usuario/empresa autenticado.

### Limites e estabilidade

- Worker limita POST comum a 128 KB.
- Worker limita `/py/*` a 10 MB.
- API tem retencao de runs por membro: padrao de 8 runs e 30 dias.
- Browserless tem fila configurada (`QUEUE_LENGTH=30`) e API limita browsers (`MAX_BROWSERS=15`, maximo 30).
- O disco esta folgado no momento.

## Riscos praticos e recomendacoes

### 1. Token tambem fica acessivel ao JavaScript

Hoje o login retorna `session_token` e os HTMLs podem guardar token em `sessionStorage/localStorage`, alem do cookie. Isso ajudou a resolver login em cenario cross-origin/guia anonima, mas aumenta o impacto de qualquer XSS: se um script malicioso rodar na pagina, pode tentar capturar o token.

Risco: medio.

Recomendacao simples:

- caminho ideal: colocar app e Worker no mesmo dominio/site para depender somente de cookie `HttpOnly`;
- enquanto isso nao muda: preferir `sessionStorage` sobre `localStorage` quando possivel;
- adicionar CSP nos HTMLs/Netlify para reduzir chance de script injetado.

### 2. XLSX tem limite por tamanho, mas nao por linhas

O Worker limita o corpo em `/py/*` a 10 MB. Isso ja evita arquivos muito grandes, mas ainda e possivel um XLSX pequeno com muitas linhas gerar trabalho desnecessario no backend e na tela.

Risco: medio-baixo.

Recomendacao simples:

- adicionar limite explicito de linhas por conjunto, por exemplo 500 ou 1000 empresas;
- mostrar mensagem amigavel quando passar do limite;
- isso protege o servidor sem complicar o produto.

### 3. Retencao existe, mas vale manter monitoramento simples

Hoje ha retencao por quantidade e dias, e o servidor esta com pouco uso de disco. Ainda assim, como o servidor e um notebook, vale ter um alerta basico.

Risco: baixo agora, medio se o uso crescer.

Recomendacao simples:

- checar disco quando passar de 75%;
- checar `/opt/prumo/data` semanalmente;
- manter botao/rotina de apagar runs antigas;
- revisar `MAX_RUNS_PER_MEMBER` se muitos usuarios entrarem.

### 4. Jobs de exclusao precisam ser acompanhados

O sistema tem fluxo de delecao com jobs/reconciliacao para apagar usuario/empresa e dados Python. Isso e bom, mas se uma exclusao falhar no meio, pode sobrar pasta/dado ate ser reconciliado.

Risco: medio-baixo.

Recomendacao simples:

- no master, destacar jobs de exclusao com status `failed`;
- ter botao "tentar novamente" ou uma rotina periodica;
- conferir pastas orfas no resumo da empresa.

### 5. Master tem acesso sensivel por desenho

O master pode ver dados, contas e credenciais cadastradas. Isso faz sentido no seu modelo operacional, mas significa que proteger a conta master e essencial.

Risco: depende do cuidado com a conta master.

Recomendacao simples:

- senha forte no master;
- MFA na conta Cloudflare e GitHub;
- nao usar a conta master no dia a dia se nao precisar;
- manter logs de acoes administrativas.

### 6. Repositorio publico aumenta reconhecimento

Codigo publico nao e automaticamente falha se nao houver segredo commitado, mas facilita alguem entender rotas, nomes e fluxo do sistema.

Risco: baixo a medio.

Recomendacao simples:

- manter segredos apenas em Cloudflare/server, nunca no Git;
- se possivel, deixar o repositorio privado;
- revisar `.env`, backups e arquivos gerados antes de commits.

## Checklist de seguranca

- [x] Worker exige login para rotas sensiveis.
- [x] Worker usa role para separar master, owner/admin e member.
- [x] Mutacoes usam CSRF.
- [x] Login tem rate limit.
- [x] API Python recebe identidade pelo Worker.
- [x] API Python exige `ISS_INTERNAL_SECRET`.
- [x] Servidor de producao esta com `ISS_ALLOW_DIRECT_LOCAL=false`.
- [x] Containers da API/Browserless estao presos em `127.0.0.1`.
- [x] Dados sao separados por empresa e usuario.
- [x] Caminhos de arquivo usam protecao contra path traversal.
- [x] Existem limites de payload no Worker.
- [x] Existe retencao de runs por membro.
- [x] Disco esta saudavel no momento.
- [ ] Adicionar limite explicito de linhas por conjunto/XLSX.
- [ ] Adicionar CSP nos HTMLs/Netlify.
- [ ] Considerar migrar para cookie `HttpOnly` puro em dominio unificado.
- [ ] Melhorar visibilidade/retry de jobs de exclusao falhos.
- [ ] Tornar repositorio privado se ainda estiver publico.

## Ajustes feitos nesta rodada

- Removido o campo `runsSearch` da tela `iss-fortaleza.html`.
- Removido o texto "Relatorio HTML" dos botoes, mantendo "Relatorio".
- Adicionado filtro de status na run selecionada, com opcao para ver apenas fluxos que nao deram `ok`.

## Conclusao

Para o tamanho e modelo atual, o sistema esta em uma faixa boa: simples, funcional e com as barreiras principais no lugar. O maior cuidado agora nao e criar uma arquitetura pesada; e fechar pequenos pontos praticos: reduzir dependencia de token em JavaScript, limitar linhas de XLSX/conjunto, monitorar disco/jobs de exclusao e manter o master bem protegido.

Nao encontrei evidencia de que uma empresa consiga acessar diretamente conjunto, run ou pasta de outra empresa pelo fluxo normal autenticado.
