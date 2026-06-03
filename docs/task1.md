# Task 1 - Inspecao de timeouts no servidor

Data da inspecao: 2026-06-03  
Servidor acessado via:

```bash
ssh -o ProxyCommand="cloudflared access ssh --hostname ssh.prumosistemas.com.br" server@localhost
```

## Objetivo

Investigar por que ocorreram muitos erros:

```text
[TIMEOUT] Tempo excedido durante a execucao do fluxo.
```

Foco principal em Thais e Gabriel, verificando versao em producao, containers Docker, logs da API, logs do Browserless, estado da fila e metricas do monitor.

## O que foi feito

1. Verifiquei containers em execucao com `docker ps`, imagens e `docker inspect`.
2. Conferi variaveis relevantes dos containers com valores sensiveis mascarados.
3. Analisei a estrutura de dados em `/opt/prumo/data`.
4. Consultei o SQLite local `/opt/prumo/data/_api_data/iss_automacao.db` apenas para mapear chaves, datasets e aliases, sem expor senhas.
5. Li os `logs.txt` das tentativas em:
   - `/opt/prumo/data/empresas/HOpfZgXYdk0PMALbPUGCZg/colaboradores/FsW0e9o-ijdnXSp6OvuCNQ/runs`
   - `/opt/prumo/data/empresas/HOpfZgXYdk0PMALbPUGCZg/colaboradores/btGJUFE5yf5Gtqp80e_JUw/runs`
6. Consultei metricas internas da API em `/api/internal/runtime-metrics` usando o segredo interno do proprio container, sem imprimir o segredo.
7. Consultei `/opt/prumo/data/_monitor/latest.json` e `metrics.sqlite3`.
8. Consultei `docker logs` de `prumo-api` e `browserless`.

## Versao em producao

O container atual da API esta rodando:

```text
prumo-api -> ryang20/prumo-api:1.0.1
browserless -> browserless/chrome@sha256:57d19e414d9fe4ae9d2ab12ba768c97f38d51246c5b31af55a009205c136012f
```

Porem a imagem `ryang20/prumo-api:1.0.1` ainda nao contem o ultimo commit local enviado para o GitHub:

```text
49d5194 fix run logs artifacts and master monitoring
```

Evidencias dentro do container:

```text
/app/main.py ainda contem log_scope="attempt_full"
/app/domain.py ainda tem collect_unified_zip_entries(ctx, root_id) sem o parametro de CNPJ
```

Conclusao: as correcoes recentes de logs/arquivos foram commitadas no repositorio, mas nao foram buildadas/deployadas nesse container.

## Ambiente Docker observado

Compose em `/opt/prumo/app/deploy/docker-compose.yml`:

```yaml
browserless:
  cpus: 8
  mem_limit: 12g
  shm_size: 2g
  CONCURRENT: "15"
  MAX_CONCURRENT_SESSIONS: "15"
  QUEUED: "30"
  QUEUE_LENGTH: "30"
  TIMEOUT: "600000"
  CONNECTION_TIMEOUT: "600000"
```

Estado atual:

```text
browserless: running, restart_count=0, OOMKilled=false
prumo-api: running, restart_count=0, OOMKilled=false
```

Observacao importante: o host reportou aproximadamente `7.44 GB` de RAM total, apesar do container Browserless estar configurado com `mem_limit=12g`. O limite de 12 GB nao vira memoria real disponivel se o host so expoe cerca de 7 GB.

## Monitoramento

O monitor esta ativo, mas ainda com persistencia de 5 em 5 minutos:

```text
MONITOR_COLLECT_INTERVAL=10
MONITOR_PERSIST_INTERVAL=300
```

Isso confirma que a mudanca recente para persistir a cada 30 segundos ainda nao foi deployada.

Picos recentes nas ultimas amostras:

```text
host cpu max: ~44.64%
host memoria max: ~66.35%
load_1m max: ~7.16
browserless cpu max: ~317%
browserless memoria max: ~32.76%
```

Nao houve evidencia de OOM ou restart:

```text
OOMKilled=false
restart_count=0
```

## Mapeamento dos usuarios

No KV local:

```text
FsW0e9o-ijdnXSp6OvuCNQ -> dataset "Empresas thais", 45 itens
btGJUFE5yf5Gtqp80e_JUw -> dataset "GABRIEL", 126 itens
```

Tambem havia outros membros:

```text
T_KI-r9Fp9JhBthA0Rf-Fg -> Isack
aqA0ZiuFZtdQckbrgrUj8w -> alanzin
```

## Estado da fila no momento da inspecao

Endpoint interno da API:

```json
{
  "queue": {
    "workers": 15,
    "workers_busy": 0,
    "workers_idle": 15,
    "pending_groups": 0
  },
  "runs": {
    "loaded": 15,
    "active": 0,
    "errors": 777
  }
}
```

Ou seja: no momento da inspecao nao havia run realmente executando.

Porem o KV ainda marcava uma run da Thais como `running`:

```text
Thais: runs_state -> {'finished': 4, 'running': 1}
```

Conclusao: existe estado persistido stale para pelo menos uma run. A API interna dizia `active=0`, entao a run nao estava rodando de verdade.

## Resumo dos timeouts por usuario

### Thais

Pasta:

```text
/opt/prumo/data/empresas/HOpfZgXYdk0PMALbPUGCZg/colaboradores/FsW0e9o-ijdnXSp6OvuCNQ/runs
```

Resumo geral:

```text
runs fisicas: 1
tentativas com logs: 5
TIMEOUT total: 521
LOGIN_TIMEOUT: 354
SEARCH_TIMEOUT: 55
SCREENSHOT_TIMEOUT: 19
MENU_FAIL: 50
```

Por tentativa:

```text
tentativa_1: 246 eventos de timeout/erro relevante
tentativa_2: 147
tentativa_3: 65
tentativa_4: 71
tentativa_5: 22
```

Top steps:

```text
Login code=TIMEOUT: 261
Login code=PW_TIMEOUT: 70
Pesquisar Empresa code=TIMEOUT: 44
Login code=SCREENSHOT_ERROR: 19
Pesquisar Empresa code=PW_TIMEOUT: 11
```

### Gabriel

Pasta:

```text
/opt/prumo/data/empresas/HOpfZgXYdk0PMALbPUGCZg/colaboradores/btGJUFE5yf5Gtqp80e_JUw/runs
```

Resumo geral:

```text
runs fisicas: 2
tentativas com logs: 4
TIMEOUT total: 1547
LOGIN_TIMEOUT: 1030
SEARCH_TIMEOUT: 215
SCREENSHOT_TIMEOUT: 33
BROWSER_429: 144
MENU_FAIL: 40
```

Por tentativa:

```text
run_ZGdzmVuu... tentativa_1: 867 eventos
run_ZGdzmVuu... tentativa_2: 393
run_ZGdzmVuu... tentativa_3: 164
run_7Cgt9i... tentativa_1: 280
```

Top steps:

```text
Login code=TIMEOUT: 782
Login code=PW_TIMEOUT: 203
Pesquisar Empresa code=TIMEOUT: 172
Pesquisar Empresa code=PW_TIMEOUT: 43
Login code=SCREENSHOT_ERROR: 33
```

## Padrao observado nos erros

O erro dominante e no inicio do fluxo:

```text
step=Login code=PW_TIMEOUT :: Timeout Playwright em 'Login': Page.click: Timeout 30000ms exceeded.
step=Login code=TIMEOUT :: Erro classificado: Tempo excedido durante a execucao do fluxo.
```

Tambem aparecem:

```text
Page.goto: Timeout 60000ms exceeded
Page.wait_for_selector: Timeout 15000ms exceeded
Page.screenshot: Timeout 30000ms exceeded
NFSE_MENU_FAIL
DAM_MENU_HOVER_FAIL
```

O fato de ate `Page.screenshot` travar por 30s indica pagina/browser lento ou travado, nao apenas um seletor errado no fluxo.

## Browserless

Nos logs do Browserless aparecem muitas criacoes de jobs e processos Chrome sendo encerrados:

```text
Adding new job to queue.
Sending SIGKILL signal to browser process ...
Current workload complete.
Health check stats: CPU 35%,32% MEM: 57%,61%
Current period usage: {"successful":40,"timedout":0,"maxConcurrent":15,...}
```

Em janelas antigas dos logs do Gabriel houve muitas ocorrencias de `429 / Too Many Requests`, que sao consistentes com saturacao do Browserless ou fila cheia no momento em que muitas sessoes foram abertas.

No momento atual, o Browserless nao esta em erro, nao reiniciou e nao foi morto por OOM.

## Diagnostico

Minha conclusao tecnica:

1. A causa principal dos muitos `[TIMEOUT]` nao parece ser um bug isolado em um CNPJ ou um fluxo especifico.
2. O padrao e sistemico: muitos fluxos falham no `Login`, antes de chegar no trabalho real.
3. Isso acontece principalmente quando ha muitas sessoes Playwright/Chrome sendo abertas em paralelo.
4. Gabriel tem 126 itens e rodou varias tentativas, concentrando volume muito maior de sessoes.
5. Thais tem 45 itens, tambem com varias tentativas, mas volume menor.
6. O servidor esta configurado para 15 browsers simultaneos, mas o host observado tem cerca de 7.44 GB de RAM total, nao 12 GB reais.
7. Mesmo sem OOM, o load chegou perto/acima de 7 e Browserless chegou a mais de 300% de CPU, entao ha pressao suficiente para deixar browser/portal lentos.
8. Os `429` nos logs do Gabriel mostram que em algum momento o Browserless rejeitou ou enfileirou demais conexoes.
9. Os timeouts de screenshot reforcam que o browser/page estava preso ou muito lento.
10. A API em producao ainda nao contem as correcoes recentes de logs/ZIP/monitoramento, entao parte dos problemas visuais ja corrigidos no GitHub ainda podem aparecer no ambiente atual.

## Recomendacoes

1. Buildar e deployar a API a partir do commit `49d5194` ou superior.
2. Atualizar `/opt/prumo/config/monitor-agent.env` para `MONITOR_PERSIST_INTERVAL=30` e reiniciar o `prumo-monitor`, se quiser o grafico mais detalhado.
3. Reduzir temporariamente a concorrencia real para testar estabilidade:
   - Browserless `CONCURRENT/MAX_CONCURRENT_SESSIONS`: testar `8` ou `10`.
   - Workers da API: manter 15 se houver round-robin, mas limitar sessoes Browserless pode suavizar login.
4. Se quiser manter 15 browsers, confirmar memoria real do servidor. Hoje o host reportou cerca de 7.44 GB, nao 12 GB.
5. Implementar/backlog tecnico: reutilizar login/sessao por conta quando possivel, em vez de abrir um navegador novo para cada CNPJ/fluxo. Essa seria a melhoria com maior impacto.
6. Tratar `429 Too Many Requests` como erro de capacidade/retry com backoff, nao como erro comum do fluxo.
7. Corrigir estado stale de runs: o KV indicava Thais `running`, mas a API interna indicava `active=0`.

## Conclusao curta

Os timeouts estao vindo principalmente de saturacao/instabilidade no inicio das sessoes de navegador, especialmente no login, agravada por alto paralelismo e muitas tentativas simultaneas. Nao encontrei evidencia de OOM ou restart de container. O ambiente atual tambem nao esta com a ultima versao corrigida do codigo.
