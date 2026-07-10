# Contexto atual do Prumo — 2026-07-10

## Veredito rápido

A produção está operacional e com uma topologia coerente: um container `prumo-api`, API presa a `127.0.0.1:8000`, Worker Cloudflare como gatekeeper, Browserless ISS no Modal e solver do Portal Nacional separado. O servidor tinha folga de disco, memória e CPU na inspeção.

O trabalho desta rodada concentrou-se no ISS, observabilidade, login, limpeza e segurança. O Portal Nacional foi preservado; não houve refatoração ampla do fluxo de notas do Portal Nacional.

## Estado observado

- Código local e `/home/server/prumo-src` estavam no mesmo commit antes desta rodada.
- Container de produção: `prumo-api`, imagem `1.0.40-notascheckpoint` no início da auditoria.
- API: `version=1.0.38`, `allow_direct_local=false`, 30 slots Modal e 0 slots locais.
- Dados persistentes: `/opt/prumo/data`; SQLite da API em `/opt/prumo/data/_api_data/iss_automacao.db`.
- Saúde do host na inspeção: 72 dias de uptime, carga quase nula, 17% de disco usado e memória disponível ampla.
- Túneis ativos e distintos:
  - túnel `browser`: SSH, Browser e API;
  - túnel `prumo-proxy`: proxy TCP para o Browserless Modal em `modal-proxy.prumosistemas.com.br`.
- Os dois arquivos de ingress passaram em `cloudflared tunnel ingress validate`.

## ISS / run da Laryssa

A run raiz e o último retry não são o mesmo registro histórico.

- A raiz teve falha em `notas`, principalmente `TIMEOUT`/`STEP_TIMEOUT` na exportação de prestadas.
- Em uma retomada houve `XML_TODAS_ESTRATEGIAS_FALHARAM` e em outra `NFSE_PAGINATION_STALLED`.
- O último filho da cadeia retomou pelo checkpoint e terminou OK: 25 páginas de prestadas, 242 registros únicos, e uma página de tomadas com 4 registros.
- Portanto, o histórico da raiz continua com erro; o sucesso do filho não reescreve a raiz.

## Alterações desta rodada

1. O log-tail do backend lê a janela recente dos arquivos grandes, reduzindo trabalho repetido durante o polling.
2. O modal de logs não sobrepõe requisições: a próxima atualização só é agendada após a anterior terminar.
3. A gravação do evento de login foi movida para `ctx.waitUntil`, sem atrasar a criação da sessão.
4. HTML estático entregue pelo Worker pode usar cache curto; respostas de API continuam `no-store`.
5. Observabilidade do Worker foi habilitada com amostragem de 25%.
6. O configurador Cohere deixou de conter chaves literais e passa a solicitar entrada oculta em runtime.
7. O Secret/app do Browserless Modal foi republicado com token novo; o pool no servidor foi corrigido e o handshake respondeu HTTP `101`.
8. O `linger` do usuário `server` foi auditado, mas não pôde ser habilitado porque o SSH não tem `sudo` sem senha.

## Pendente de privilégio

O segredo Worker↔API/monitor apareceu durante o diagnóstico inicial e deve ser considerado comprometido. A tentativa de rotação foi revertida para não deixar o monitor root quebrado, pois o serviço não pôde ser reiniciado sem privilégio.

Executar como root no servidor, em janela controlada:

```bash
sudo systemctl restart prumo-monitor.service
sudo loginctl enable-linger server
```

Depois disso, gerar um segredo novo, atualizar `ISS_INTERNAL_SECRET` no Worker e nos arquivos `/opt/prumo/app/deploy/.env` e `/opt/prumo/config/monitor-agent.env`, recriar `prumo-api` e confirmar `/api/internal/runtime-metrics`.

As chaves Cohere que estavam literais no arquivo local também devem ser revogadas e substituídas no Secret do solver. O arquivo agora não armazena chaves.

## Regras de manutenção

- Não usar `server/output` local como prova de produção.
- Não expor valores de `.env`, certificados, cookies, tokens ou blobs de contas em diagnósticos.
- Manter `ISS_ALLOW_DIRECT_LOCAL=false` em produção.
- Manter o proxy para o ISS enquanto o login direto do Modal continuar sujeito a bloqueio de origem; o Portal Nacional deve ser tratado separadamente.
- Ao reportar retry, sempre dizer qual é a raiz e qual é o último filho.
