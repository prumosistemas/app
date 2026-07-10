# C4 — Arquitetura do Prumo

Snapshot baseado no código local e na inspeção de produção de 2026-07-10. Valores secretos, certificados, cookies e credenciais foram omitidos.

## Nível 1 — Contexto

```mermaid
C4Context
title Prumo / ISS Fortaleza — contexto do sistema

Person(master, "Master", "Administra empresas, pagamentos, logs e infraestrutura")
Person(owner, "Administrador", "Administra a empresa e seus colaboradores")
Person(member, "Colaborador", "Executa fluxos ISS e acessa o Portal Nacional")
System(prumo, "Prumo", "Automação multiempresa para ISS Fortaleza e Portal Nacional")
System_Ext(iss, "Portal ISS Fortaleza", "Portal externo de certidão, escrituração, DAM e notas")
System_Ext(nfse, "Portal Nacional NFS-e", "Portal externo de XML/PDF e hCaptcha")
System_Ext(modal, "Modal", "Browserless ISS e solver hCaptcha")

Rel(master, prumo, "Administra")
Rel(owner, prumo, "Configura empresa")
Rel(member, prumo, "Executa e consulta")
Rel(prumo, iss, "Automatiza")
Rel(prumo, nfse, "Consulta e baixa")
Rel(prumo, modal, "Usa navegadores e solver")
```

## Nível 2 — Containers

```mermaid
C4Container
title Containers de produção do Prumo

Person(member, "Colaborador")
System_Boundary(prumo, "Prumo") {
  Container(ui, "Web UI", "HTML/CSS/JS", "Login, ISS, Portal Nacional, admin e master")
  Container(worker, "Cloudflare Worker", "JavaScript + D1 binding", "Sessão, CSRF, billing, logs, roteamento e proxy autenticado")
  ContainerDb(d1, "D1 db", "SQLite/D1", "Empresas, usuários, sessões, pagamentos e logs")
  Container(api, "Prumo API", "FastAPI + Playwright", "Fila, contas ISS, runs, artefatos e Portal Nacional")
  ContainerDb(sqlite, "API SQLite", "SQLite WAL", "KV por colaborador e estados de runs")
  ContainerDb(files, "Output persistente", "Filesystem", "Runs, checkpoints, XML/PDF e certificados por escopo")
}
System_Ext(netlify, "Netlify", "Hospedagem estática principal")
System_Ext(modal, "Modal Browserless", "Browserless ISS com 30 slots turbo")
System_Ext(solver, "Modal Portal Solver", "Solver stateless de hCaptcha")
System_Ext(iss, "Portal ISS Fortaleza")
System_Ext(nfse, "Portal Nacional")
System_Ext(tunnels, "Cloudflare Tunnel", "browser + prumo-proxy")

Rel(member, ui, "HTTPS")
Rel(ui, netlify, "Carrega páginas")
Rel(ui, worker, "HTTPS /api e /py")
Rel(worker, d1, "Binding direto")
Rel(worker, api, "HTTPS autenticado")
Rel(api, sqlite, "Lê/escreve")
Rel(api, files, "Persiste artefatos")
Rel(api, modal, "CDP pool")
Rel(api, solver, "Resolve hCaptcha")
Rel(modal, tunnels, "Proxy de origem do servidor")
Rel(api, iss, "Requests/Playwright")
Rel(api, nfse, "Requests/Playwright")
```

## Nível 3 — Fluxos principais

### Login

```mermaid
sequenceDiagram
  participant U as UI
  participant W as Worker
  participant D as D1
  U->>W: POST /api/login
  W->>D: rate limit + usuário + billing
  W->>D: sessão e CSRF
  W-->>U: token/cookie/sessão
  W-->>D: log de auditoria via waitUntil
```

### ISS Fortaleza

```mermaid
flowchart LR
  UI[UI ISS] --> W[Worker auth + CSRF]
  W --> API[FastAPI /py]
  API --> Q[Fila global fair round-robin]
  Q --> B[Modal Browserless]
  B --> P[Proxy loopback do servidor]
  P --> ISS[Portal ISS Fortaleza]
  API --> DATA[(SQLite + files)]
```

### Portal Nacional — preservado nesta rodada

```mermaid
flowchart LR
  UI[UI Portal Nacional] --> W[Worker]
  W --> API[FastAPI]
  API --> CERT[PFX/sessão por colaborador]
  API --> NFSE[Portal Nacional]
  NFSE -. hCaptcha .-> SOLVER[Modal solver stateless]
  API --> FILES[(XML/PDF/checkpoints)]
```

## Limites de confiança

- O Worker é a fronteira pública de autenticação para a API ISS.
- `prumo-api` e Browserless local não devem ser publicados em interface externa.
- O túnel `browser` é separado do túnel `prumo-proxy`; eles não devem ser fundidos sem validar os hostnames.
- O Modal é stateless para o solver do Portal Nacional; certificados, sessões e arquivos finais ficam no escopo do servidor.
