# Portal Nacional - operação

## Arquitetura ativa

- A API Prumo executa `server/portal_nacional_automation.py` e persiste cada run em `/opt/prumo/data`.
- O único resolvedor é o Google Modo IA versionado em `solver/google_ai_mode` e publicado no Modal.
- A API chama `POST /solve` diretamente no endpoint Modal, com timeout de 240 segundos e circuit breaker. Não existe fallback para outro modelo.
- A rota padrão é direta. A proxy do ThinkPad funciona no próprio servidor, mas o acesso pelo Modal expira no Cloudflare Access sem service token; não a ative antes de corrigir essa autenticação.

## Variáveis

```text
PORTAL_NACIONAL_SOLVER_URL=https://jorhinhogames--prumo-portal-nacional-google-solver-solve-30b985.modal.run/solve
PORTAL_NACIONAL_SOLVER_FALLBACK_URL=
PORTAL_NACIONAL_SOLVER_TIMEOUT_SECONDS=240
```

Nunca grave cookies, PFX, senhas ou credenciais de túnel no Git. Cookies e artefatos do Google ficam no Volume privado montado em `/google-ai`; somente o código entra na imagem e no repositório.

## Procedimento seguro de teste

1. Confirme `/health`: `provider=google_ai_mode`, `route=direct` e circuitos fechados.
2. Confirme que o certificado abre antes de criar a run.
3. Inicie com `max_items=1`, `concorrencia=1` e pelo menos 6 tentativas.
4. Aguarde a run terminar; não reinicie durante um solve ativo de até 240 segundos.
5. Valide `item_downloaded`, os arquivos XML/PDF e o status final.
6. Só depois aumente o lote.

As tentativas de rede usam backoff crescente. Quando 429/503, falha de DNS/conexão ou circuito aberto afetam itens diferentes, a espera global cresce em 4, 8, 16, 32, 64 e 120 segundos e zera após um sucesso.

## Certificados e troca de segredo

Senhas de PFX são criptografadas com `ISS_INTERNAL_SECRET`. Se esse segredo mudar, versões antigas não podem ser descriptografadas. A API agora falha cedo com HTTP 409 e pede novo envio, em vez de escrever uma senha vazia na run.

Uma run histórica pode servir para diagnóstico, mas nunca imprima a senha ou o PFX. Valide apenas resultado, tamanho e hashes reduzidos.

## Evidência de 2026-07-13

Teste ampliado do Alan na run `20260707-150940-emitidas-20260601-20260630-cert-202607061735-ambos`:

- resultado final: `finalizado_parcial`;
- acumulado: 18 baixados, 0 erros, sendo 10 novos sobre a base inicial de 8;
- último lote: 6 itens com concorrência 1, seguido de 1 retry controlado após renovar a sessão anônima;
- método: XML e PDF com hCaptcha resolvido exclusivamente por Google Modo IA;
- solver final: `2026-07-13-google-ai-mode-v11-bundled-source`, rota direta, circuitos fechados;
- último item: 98 segundos entre início e `item_downloaded`.

O status parcial é esperado: `max_items` limitou o teste e 416 registros permaneceram fora dele. Nenhum desses pendentes foi contabilizado como erro.
