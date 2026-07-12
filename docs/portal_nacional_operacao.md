# Portal Nacional - operação

## Arquitetura ativa

- A API Prumo executa `server/portal_nacional_automation.py` e persiste cada run em `/opt/prumo/data`.
- O resolvedor primário pode ser o modo IA residencial do projeto organizado, exposto pelo gateway assíncrono `tools/solver_keepalive_gateway.py`.
- O endpoint Cohere Modal permanece como fallback de produção por meio de `PORTAL_NACIONAL_SOLVER_FALLBACK_URL`.
- O gateway transforma solves longos em jobs: `POST /solve` retorna `202`, e a API consulta `GET /jobs/{id}`. Isso evita o timeout 524 da Cloudflare.

## Variáveis

```text
PORTAL_NACIONAL_SOLVER_URL=<endpoint primário>/solve?token=<segredo fora do Git>
PORTAL_NACIONAL_SOLVER_FALLBACK_URL=https://jorhinhogames--prumo-portal-nacional-solver-solver-server.modal.run/solve
PORTAL_NACIONAL_SOLVER_TIMEOUT_SECONDS=240
```

Nunca grave cookies, PFX, senhas, token do gateway ou credenciais de túnel no Git. O estado anônimo do Google fica fora do repositório. O gateway público deve iniciar com `PORTAL_SOLVER_GATEWAY_TOKEN` definido; ele limita solves concorrentes e remove jobs antigos.

## Procedimento seguro de teste

1. Confirme `/health` do solver primário e do fallback.
2. Confirme que o certificado abre antes de criar a run.
3. Inicie com `max_items=1`, `concorrencia=1` e pelo menos 6 tentativas.
4. Aguarde a run terminar; não reinicie durante um solve ativo de até 240 segundos.
5. Valide `item_downloaded`, os arquivos XML/PDF e o status final.
6. Só depois aumente o lote.

As tentativas de rede usam backoff crescente. Erros transitórios no polling do job ou no DNS do Portal são repetidos dentro da própria tentativa antes de consumir uma nova tentativa do item.

## Certificados e troca de segredo

Senhas de PFX são criptografadas com `ISS_INTERNAL_SECRET`. Se esse segredo mudar, versões antigas não podem ser descriptografadas. A API agora falha cedo com HTTP 409 e pede novo envio, em vez de escrever uma senha vazia na run.

Uma run histórica pode servir para diagnóstico, mas nunca imprima a senha ou o PFX. Valide apenas resultado, tamanho e hashes reduzidos.

## Evidência de 2026-07-12

Dois testes controlados do Alan na run `20260707-150940-emitidas-20260601-20260630-cert-202607061735-ambos`, incluindo um após o deploy 1.0.43:

- limite: 1 nota;
- resultado: `finalizado_parcial`;
- acumulado validado: 2 baixados, 0 erros;
- método: `requests_captcha_xml+requests_captcha_pdf`;
- artefatos: XML e PDF.

O status parcial é esperado porque restaram 432 itens fora dos limites dos testes.
