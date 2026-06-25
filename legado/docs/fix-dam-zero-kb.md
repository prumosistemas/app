# Investigacao: DAM com PDF 0 KB

Data: 2026-06-05

## Escopo

Investigar por que PDFs do fluxo DAM foram salvos com 0 KB, principalmente no colaborador `btGJUFE5yf5Gtqp80e_JUw` (Gabriel) e tambem em outros colaboradores.

Nao foi feita alteracao de codigo nesta investigacao.

## Estado da versao

- Producao API: `ryang20/prumo-api:1.0.2`
- Commit versionado: `85c5b6e fix run log retrieval and portal login timeouts`
- Tag criada no Git: `v1.0.2`

## Evidencias no servidor

Comando base usado:

```bash
ssh -o ProxyCommand="cloudflared access ssh --hostname ssh.prumosistemas.com.br" server@localhost
```

Container em producao:

```text
prumo-api   ryang20/prumo-api:1.0.2   Up 2 days
browserless 57d19e414d9f              Up 2 days
```

Foram encontrados 11 PDFs DAM em `/opt/prumo/data/empresas`, todos com tamanho `0`.

Resumo por colaborador:

```text
btGJUFE5yf5Gtqp80e_JUw: 6 DAMs zerados, todos DAM_tipo_1
FsW0e9o-ijdnXSp6OvuCNQ: 5 DAMs zerados, sendo 3 DAM_tipo_1 e 2 DAM_tipo_0
Total: 11 PDFs DAM, 11 com 0 bytes, nenhum DAM nao-zero encontrado
```

Exemplos confirmados:

```text
btGJU... tentativa_1 20194719000118 DAM_tipo_1_20260603_121458.pdf size=0
btGJU... tentativa_1 57706960000199 DAM_tipo_1_20260603_122033.pdf size=0
btGJU... tentativa_3 31894807000149 DAM_tipo_1_20260603_133930.pdf size=0
FsW0...  tentativa_2 16527529000106 DAM_tipo_0_20260603_135242.pdf size=0
```

O `runs_state` marcou esses itens como sucesso:

```text
res_status=ok
erro_code=None
arquivo=/app/output/.../dam/<cnpj - empresa>/dam
```

Nos logs da tentativa, o fluxo DAM tambem marcou sucesso mesmo quando o arquivo salvo estava vazio:

```text
[INFO] ... flow=dam cnpj=20194719000118 ... DAM baixado: DAM_tipo_1_20260603_121458.pdf
[INFO] ... flow=dam cnpj=20194719000118 ... DAM(s) baixado(s): 1
[FLOW_END] ... === FIM (DAM OK) ===
[ITEM_OK] flow=dam cnpj=20194719000118 conta=ISS DANIEL
```

Em outro caso, ficou ainda mais claro que o fluxo encerra OK mesmo sem DAM valido:

```text
[INFO] ... flow=dam cnpj=16527529000106 ... DAM baixado: DAM_tipo_0_20260603_135242.pdf
[INFO] ... flow=dam cnpj=16527529000106 ... DAM(s) baixado(s): 0
[FLOW_END] ... === FIM (DAM OK) ===
[ITEM_OK] flow=dam cnpj=16527529000106 conta=Iss Daniel
```

Browserless, no intervalo analisado, nao mostrou erro direto de download, OOM, crash, 429 ou timeout global. O periodo teve alto paralelismo (`maxConcurrent=15`), mas sem erro registrado pelo Browserless. Isso aponta mais para validacao ausente no fluxo DAM do que para falha explicita do container.

## Causa confirmada

O fluxo DAM aceita o download sem validar o arquivo salvo.

Em `server/flow_dam.py`, `_emitir_dam_tipo()` faz:

```python
async with page.expect_download(timeout=30_000) as dl:
    await page.click("input#btnConfirma")
download = await dl.value
await download.save_as(caminho)
await log_flow(ctx, f"DAM baixado: {nome}", event="INFO")
return True
```

Nao existe verificacao de:

- `download.failure()`
- tamanho minimo
- header `%PDF-`
- remocao do arquivo invalido
- erro quando todos os tipos retornam arquivo invalido

Por isso, quando o portal/browserless gera um arquivo vazio, a automacao registra `DAM baixado`, considera o tipo como sucesso e a run termina `ITEM_OK`.

Essa e a diferenca exata em relacao ao fluxo de certidao. Em `server/flow_certidao.py`, o download ja tem validacao:

- `_validar_pdf_bytes()`: rejeita bytes vazios, arquivo menor que 100 bytes e header diferente de `%PDF-`
- `_validar_pdf_salvo()`: valida tamanho e header no disco
- `baixar_pdf_com_fallback()`: tenta fetch interno primeiro, fallback com `expect_download`, remove arquivo invalido e levanta erro
- logs de certidao incluem tamanho: `OK (49736 bytes)`

Os logs de certidao dos mesmos CNPJs mostram PDFs validos com tamanho real, enquanto os DAMs do mesmo conjunto ficaram `0 bytes`. Isso elimina permissao de disco como causa principal e confirma que o problema esta no tratamento do download DAM.

## Por que aconteceu com Gabriel e outros

Nao e exclusivo do Gabriel. Gabriel (`btGJU...`) concentrou 6 dos 11 casos, mas Thais (`FsW0...`) tambem teve 5.

O padrao e por fluxo/tipo de download, nao por usuario:

- todos os DAMs encontrados estao zerados;
- os mesmos runs tinham certidoes salvas corretamente;
- o codigo DAM nao valida o arquivo;
- o `runs_state` grava sucesso mesmo quando o arquivo DAM tem 0 bytes.

## Metodo recomendado para corrigir

Proximo patch deve fazer o DAM seguir a mesma regra da certidao.

1. Extrair validacao comum de PDF para `flow_core.py` ou reutilizar a logica da certidao:
   - minimo 100 bytes;
   - header `%PDF-`;
   - remover arquivo invalido do disco;
   - logar tamanho quando OK.

2. Alterar `_emitir_dam_tipo()`:
   - chamar `download.failure()` antes do `save_as`;
   - depois do `save_as`, validar arquivo;
   - se invalidar, remover o arquivo e retornar erro controlado, nao sucesso;
   - logar `DAM salvo: <nome> - OK (<bytes> bytes)` quando valido.

3. Preferir o mesmo modelo robusto da certidao:
   - tentar fetch interno do formulario/botao `input#btnConfirma`;
   - validar bytes retornados;
   - usar `expect_download + save_as` apenas como fallback;
   - validar o fallback tambem.

4. Corrigir regra final de `emitir_dams()` / `job_dam()`:
   - contar como baixado somente PDF validado;
   - se todos os tipos falharem por arquivo invalido, levantar `FlowError` retryable, por exemplo `DAM_DOWNLOAD_INVALID`;
   - nao encerrar `DAM OK` quando `tipos_ok` estiver vazio por falha de download.

5. Testes obrigatorios:
   - DAM `save_as()` gera arquivo 0 bytes => arquivo removido e resultado nao e `ok`;
   - DAM gera arquivo sem header `%PDF-` => erro retryable;
   - DAM valido => status `ok`, log com tamanho e arquivo aparece no modal/zip;
   - quando tipo 0 falha e tipo 1 valido, run fica `ok` com apenas tipo 1;
   - quando todos falham, run fica `erro`, nao `ok`.

## Conclusao

Tenho certeza suficiente para o proximo patch: o problema nao e "Gabriel" em si, nem permissao de pasta, nem falta de arquivo no ZIP. O arquivo DAM esta sendo criado com 0 bytes e o fluxo DAM trata isso como sucesso porque nao valida o download.

A correcao deve ser implementar validacao/fallback de PDF no DAM, igual ao que ja foi resolvido na certidao, e impedir `ITEM_OK` quando nenhum DAM valido foi salvo.
