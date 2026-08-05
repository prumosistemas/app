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

Esta seção será completada depois do deploy com o commit publicado, a imagem implantada, o health check e os testes de comportamento dentro do container 1.0.72.

## Limpeza de temporários

No fechamento serão verificados:

- `git status --short`;
- arquivos não rastreados no projeto;
- `__pycache__`/`.pyc` gerados pelas validações, respeitando `.gitignore`;
- scripts temporários `python_*.py` criados pela ferramenta MCP durante esta intervenção;
- inexistência de arquivos de credencial ou dumps na documentação.

Nenhum segredo, cookie, senha, conteúdo de PFX ou URL privada completa deve ser incluído neste arquivo.
