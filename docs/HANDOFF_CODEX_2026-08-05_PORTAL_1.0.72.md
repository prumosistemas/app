# Handoff para o Codex — fechamento da resiliência do Portal 1.0.72

Data operacional: 2026-08-05 UTC / 2026-08-04 America/Fortaleza
Projeto local: `C:\Users\ryang\Desktop\projetosv2\projeto`
Branch: `main`

## Objetivo

Concluir com segurança o trabalho interrompido na thread Codex `019f4c27-1c9f-7950-b19f-710e50d56f2a`, sem perder nem reiniciar a run da Thaís, publicando a correção que diferencia bloqueio explícito do Google de HTTP 503 genérico.

## Contexto recuperado

A run lógica da Thaís possui duas filhas:

- Emitidas: `20260804-121037-emitidas-20260701-20260731-cert-202608041210-ambos`
- Recebidas: `20260804-121037-recebidas-20260701-20260731-cert-202608041210-ambos`

Antes de qualquer alteração em produção, as emitidas já estavam finalizadas em 71/71 e a recebida permanecia ativa, reaproveitando `indice.json`, sessão, certificado e arquivos já baixados. Nenhum deploy foi iniciado enquanto o processo da recebida estava vivo.

## Defeito que havia ficado pendente

O endpoint Modal devolvia HTTP 503 com JSON contendo o motivo real, por exemplo `google_ai_request_failed` e `unusual traffic`. O cliente chamava `raise_for_status()` antes de preservar esse JSON. Como consequência, a classificação via apenas um 503 genérico e reduzia o cooldown do Modal para 15 segundos.

Isso provocava:

1. repetição frequente em endpoints explicitamente bloqueados pelo Google;
2. gasto e latência desnecessários;
3. maior pressão sobre Modal e ThinkPad;
4. logs sem o motivo operacional real do bloqueio.

## Alteração de código

Arquivo: `server/portal_nacional_automation.py`

### Preservação do erro JSON em HTTP >= 400

`solve_captcha_once()` agora tenta ler o corpo JSON antes de levantar o erro HTTP. Quando o JSON é válido, gera uma exceção no formato:

```text
solver:<reason>: <detail>
```

Assim, um 503 com `reason=google_ai_request_failed` e `error=unusual traffic` não é reduzido a `503 Server Error`.

### Cooldown específico para bloqueio Google

`solver_endpoint_cooldown_seconds()` reconhece:

- `google_ai_request_failed`
- `unusual traffic`
- `sorry/index`

Esses motivos recebem cooldown de 300 segundos.

### Preservação do cooldown longo no Modal

`mark_solver_endpoint_unavailable()` continua reduzindo 5xx genérico de Modal para 15 segundos, porque o domínio pode balancear containers independentes. A redução não é aplicada quando existe prova explícita de bloqueio Google. Nesse caso, somente o endpoint afetado fica afastado por 300 segundos.

O fallback local do ThinkPad continua sem cooldown global para falhas visuais de uma única tentativa, evitando remover o último caminho residencial das outras threads.

## Testes adicionados

Arquivo: `tests/test_portal_network_retry.py`

Foram adicionados testes que confirmam:

1. bloqueio Google explícito em Modal gera cooldown de 300 segundos;
2. resposta HTTP 503 com JSON preserva `google_ai_request_failed` e `unusual traffic` na exceção;
3. HTTP 503 JSON genérico mantém o status anexado à exceção e continua usando cooldown curto de 15 segundos no pool Modal.

Durante a revisão pré-deploy foi identificado que converter todo erro JSON em `RuntimeError` poderia perder o `status_code` de um 503 genérico. A correção final anexa o objeto `response` à exceção. Isso mantém os dois comportamentos corretos: 300 segundos quando há prova de bloqueio Google e 15 segundos quando é apenas uma indisponibilidade genérica do pool.

## Versionamento preparado

A versão da API foi elevada de 1.0.71 para 1.0.72 nos pontos operacionais:

- `server/main.py`
- `deploy/docker-compose.yml`
- `README.md`
- `docs/AI_OPERATOR_CONTEXT.md`
- `docs/OPERACAO_PRUMO_DETALHADO.md`
- `docs/PROJECT_CONTEXT.md`
- `docs/SERVER_CONTEXT.md`

A documentação principal também passou a registrar a preservação do JSON do 503 e o cooldown de cinco minutos para bloqueio explícito do Google.

## Validação local antes do deploy

Comandos executados:

```powershell
python -m pytest -q
python -m py_compile server/main.py server/portal_nacional_automation.py
git diff --check
```

Resultado:

- 150 testes aprovados;
- compilação Python aprovada;
- nenhuma falha de whitespace ou patch;
- somente avisos normais de conversão LF/CRLF do Git no Windows.

## Proteção da run da Thaís

Regras seguidas:

1. não reiniciar o container enquanto `portal_nacional_automation.py` da recebida estiver ativo;
2. verificar `indice.json`, `run.json`, processo e log diretamente no ThinkPad;
3. confirmar que emitidas permanece 71/71;
4. confirmar que recebidas avança sem erro definitivo;
5. somente fazer o corte da API após fechamento seguro da filha recebida;
6. após o deploy, validar que os mesmos diretórios e índices continuam presentes.

## Fechamento seguro da run da Thaís antes do deploy

O deploy foi mantido bloqueado enquanto existia um processo `portal_nacional_automation.py` ativo. A filha recebida foi acompanhada de 14/24 até o fechamento natural, sem reinício do container e sem acionar uma nova indexação.

Estado confirmado imediatamente antes do deploy:

- emitidas: 71/71 baixadas, zero pendentes, zero erros, índice `finalizado`;
- recebidas: 24/24 baixadas, zero pendentes, zero erros, índice `finalizado`;
- arquivos físicos emitidas: 71 XML e 71 PDF;
- arquivos físicos recebidas: 24 XML e 24 PDF;
- `last_error`: ausente nas duas filhas;
- nenhum processo `portal_nacional_automation.py` permaneceu ativo.

A recebida terminou em `2026-08-05T03:19:44Z`. Portanto, a atualização da API pôde ocorrer sem derrubar a run, sem usar `Continuar` e sem reprocessar qualquer nota.

## Implantação e validação final

### Commit e publicação

O commit de código e documentação pré-deploy foi publicado na `main`:

```text
2aa2a07 fix: preserve portal solver block reasons
```

O push avançou `origin/main` de `22df225` para `2aa2a07`.

### Deploy no ThinkPad

Foi usado o caminho operacional padrão, sem credenciais literais:

```powershell
python -m ops.prumo_ops server deploy --apply
```

O servidor executou:

1. `git pull --ff-only`;
2. build de `ryang20/prumo-api:1.0.72`;
3. cópia do compose versionado;
4. recriação única do container `prumo-api`;
5. health check local.

Imagem construída: `36ffc749cc8a`.

Durante a primeira tentativa de health houve um `connection reset by peer` normal enquanto o container iniciava. O laço de health repetiu e concluiu com sucesso, sem intervenção manual.

### Health e estado operacional

Resposta confirmada depois do deploy:

```json
{
  "ok": true,
  "service": "Prumo API",
  "version": "1.0.72",
  "allow_direct_local": false,
  "max_browsers": 30,
  "queue": "global_fair_round_robin_by_member",
  "storage_scope": "member"
}
```

O container ficou ativo como `ryang20/prumo-api:1.0.72`, e o Git do servidor permaneceu limpo na `main`.

### Smoke test dentro da imagem implantada

Foi executado um teste isolado dentro do container, sem chamada externa real e sem consumir captcha:

```text
explicit_google_block 300
generic_reason solver:container_unavailable: temporary backend outage
generic_status 503
generic_modal_cooldown 15
```

Isso prova no artefato implantado que:

- bloqueio Google explícito aplica 300 segundos;
- o motivo JSON é preservado;
- o status HTTP 503 continua anexado à exceção;
- um 503 genérico do pool Modal mantém 15 segundos.

A primeira execução do heredoc de smoke não recebeu stdin porque `docker exec` estava sem `-i`; ela terminou sem executar código e sem efeitos. O teste foi repetido corretamente com `docker exec -i` e produziu os valores acima.

### Persistência da Thaís depois da recriação

Após o deploy, os mesmos diretórios foram relidos diretamente no armazenamento persistente:

- emitidas: `finalizado`, 71/71, 71 XML, 71 PDF, zero erro;
- recebidas: `finalizado`, 24/24, 24 XML, 24 PDF, zero erro.

Nenhuma filha voltou para `rodando`, nenhum índice foi recriado e nenhum arquivo precisou ser baixado novamente.

## Limpeza de temporários

A limpeza final foi limitada a artefatos descartáveis:

- sete diretórios de cache Python/pytest foram removidos do repositório (`__pycache__` e `.pytest_cache`);
- nenhum `python_*.py` temporário da ferramenta MCP permaneceu no diretório de runtime;
- nenhum arquivo não rastreado inesperado foi encontrado além deste handoff antes de seu commit;
- dados de runs, logs, índices, XML, PDF, certificados e imagens de rollback não foram removidos;
- as imagens 1.0.71 e 1.0.70 foram preservadas para rollback;
- nenhum segredo, cookie, senha, conteúdo de PFX ou URL privada completa foi incluído neste arquivo.

## Resultado final

O ciclo interrompido pelo Codex foi encerrado com a Thaís concluída antes do deploy, correção revisada, regressão adicional coberta, 150 testes aprovados, versão 1.0.72 implantada e armazenamento validado depois da recriação.
