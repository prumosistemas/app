# Relatório de auditoria e limpeza — 2026-07-10

## Resultado

O projeto ficou com os caminhos críticos validados localmente e a produção foi inspecionada sem alterar o fluxo principal do Portal Nacional. A API está saudável, os dois túneis estão ativos/validados e a capacidade declarada do Browserless bate com o pool usado pela API.

## Evidências executadas

- `pytest -q`: 42 testes passando.
- `python -m py_compile`: passou para os módulos principais.
- `node --check cloudflare/worker.js`: passou.
- `npm run check --prefix cloudflare`: passou.
- `wrangler deploy --dry-run`: bundle 149,57 KiB; gzip 30,47 KiB; bindings esperados.
- `wrangler check startup`: gerou CPU profile sem erro.
- `docker compose config --quiet` em produção: passou após a correção do token do Browserless.
- `curl http://127.0.0.1:8000/`: API OK, 30 browsers Modal, acesso local direto desativado.
- `cloudflared --config ... tunnel ingress validate`: OK para os dois túneis.
- Handshake do Browserless Modal: HTTP `101`.

## Achados ISS

O histórico da Laryssa confirmou um problema de notas, não de login:

- raiz: erro em prestadas por timeout;
- retries: falhas de XML e paginação travada;
- último filho: retomada por checkpoint concluída, 242 registros únicos em prestadas e 4 em tomadas.

O backend agora evita reler arquivos de log grandes a cada polling. O frontend evita chamadas concorrentes no modal. O login também não espera mais a escrita do log de auditoria.

## Achados de segurança

1. O diagnóstico inicial desta rodada expôs valores secretos no output por uma máscara inadequada. Esses valores devem ser tratados como comprometidos.
2. O script local do solver continha chaves literais de um provedor desativado. O script e o Secret correspondente foram removidos na migração para Google Modo IA.
3. O token do Browserless foi republicado e atualizado no pool do servidor.
4. A rotação do segredo Worker↔API/monitor foi tentada, mas revertida porque o serviço root do monitor não pôde ser reiniciado pela conta SSH. A ação final está em `CONTEXTO_ATUAL_2026-07-10.md`.

## Limpeza recomendada/concluída

- caches e artefatos gerados locais não fazem parte do deploy e devem permanecer fora do Git;
- scripts de manutenção one-shot no servidor devem ficar em arquivo de operação, não em `/opt/prumo/data` ativo;
- backups antigos de `.env` devem ser removidos/arquivados com permissão restrita após validar a rotação;
- `server/output` local é dado de desenvolvimento, não evidência de produção.

## Próxima ação administrativa

```bash
sudo loginctl enable-linger server
sudo systemctl restart prumo-monitor.service
```

Depois, rotacionar novamente o segredo Worker/API/monitor e confirmar o painel master. Não apagar nem reprocessar runs do Portal Nacional durante essa ação.
