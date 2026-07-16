# AGENTS.md

## Project orientation

This repository contains the Prumo/ISS Fortaleza application, its local source tree, deployment files, and operational documentation.

Before operational work, read the relevant project docs instead of guessing from directory names:

- `docs/AI_OPERATOR_CONTEXT.md` (canonical safe commands and credential indirection)
- `README.md`
- `docs/SERVER_CONTEXT.md`
- `docs/OPERACAO_PRUMO_DETALHADO.md`

## Credential-safe operation

- Never open, print, quote, or copy credential files, certificate material, `.env`, `.modal.toml`, Netlify auth cache, or the DPAPI vault.
- Use `python -m ops.prumo_ops ...` for Cloudflare, Netlify, Modal, server, and authenticated smoke tests.
- Commands and prompts must reference secret names or login aliases only. Never put a literal secret in a command.
- Cloudflare operations use the REST API and do not depend on Wrangler. Modal tokens are injected only into the child process environment.
- `docs/AI_OPERATOR_CONTEXT.md` supersedes older operational command snippets when they disagree.

## Local versus production

- `server/` and `server/output/` are local project data and are **not proof of current production state**.
- When the user asks to enter the server, use SSH, inspect production, or verify the current run, execute the documented connection with `run_command`.
- The documented SSH entry point is:

```powershell
ssh -o "ProxyCommand=cloudflared access ssh --hostname ssh.prumosistemas.com.br" server@localhost
```

For non-interactive checks, pass the remote command as the final SSH argument.

## Production data

- Persistent production data: `/opt/prumo/data`
- ISS API SQLite database: `/opt/prumo/data/_api_data/iss_automacao.db`
- SQLite table: `kv`
- Run-state keys end with `:runs_state`; each value contains a JSON object at `$.runs`.

When reporting retries, distinguish the root run from its latest child attempt. A successful child attempt does not change the historical result stored on the root record.

Do not print credential values, certificate material, tokens, or entire account-state blobs.
