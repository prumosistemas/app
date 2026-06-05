# Correcao: DAM via modal PIX

Data: 2026-06-05

## Problema observado

O fluxo DAM da DUDMA Engenharia (`20.194.719/0001-18`) continuava falhando mesmo depois da validacao contra arquivos `0 KB`.

Logs da run real:

```text
Browser fetch DAM falhou (FlowError), tentando save_as...
save_as DAM também falhou: save_as() gerou DAM inválido: Muito pequeno (0 bytes)
Nenhum DAM válido foi baixado.
```

## Prova encontrada

Foi aberta a screenshot da tentativa do Gabriel:

```text
/opt/prumo/data/empresas/HOpfZgXYdk0PMALbPUGCZg/colaboradores/btGJUFE5yf5Gtqp80e_JUw/runs/run_yH9h3sZY5UMgi27cbIjl5w/tentativa_2/dam/20194719000118 - DUDMA ENGENHARIA LTDA ME/erro_20260605_182749.png
```

A tela nao era um PDF. O portal abriu um modal:

```text
PAGAR COM PIX VIA QR CODE
DUDMA ENGENHARIA LTDA ME
R$ 2,01
Copiar Qr Code
```

Ou seja: para esse DAM, o portal nao entrega arquivo PDF por download tradicional. Ele renderiza uma cobranca PIX em modal HTML.

O fetch interno confirmou isso em execucao real:

```text
Fetch do DAM tipo=1 retornou conteúdo inválido:
Header inválido (esperado %PDF-, obteve b'<!DOCTYPE ')
content-type=text/html;charset=UTF-8, HTTP 200, size=95201
```

## Correcao aplicada

O fluxo DAM agora tem tres estrategias:

1. Fetch interno do formulario, igual ao padrao robusto usado em Certidao/Notas.
2. Fallback por `expect_download + save_as`, com validacao de `%PDF-` e tamanho.
3. Quando o portal abre o modal PIX, gerar um PDF valido da propria tela com `page.pdf()`.

O arquivo gerado pelo modal PIX tambem e validado:

- tamanho minimo;
- header `%PDF-`;
- remocao de arquivo invalido;
- log com tamanho final.

Tambem foi adicionada rotina para fechar o modal RichFaces `panelQrdCode` apos salvar o PDF, evitando que o overlay bloqueie o proximo tipo de DAM.

## Validacao real

Foi executado o DAM da DUDMA com a conta do Gabriel diretamente no container de producao, usando as credenciais salvas da API sem imprimir login/senha.

Resultado:

```text
DAM PIX salvo como PDF: DAM_tipo_1_20260605_184056.pdf — OK (106913 bytes)
DAM(s) baixado(s): 1
=== FIM (DAM OK) ===
```

Arquivo validado:

```text
/app/output/empresas/HOpfZgXYdk0PMALbPUGCZg/colaboradores/btGJUFE5yf5Gtqp80e_JUw/runs/debug_dudma_dam_v105_1780684820/tentativa_1/20194719000118 - DUDMA ENGENHARIA LTDA ME/dam/DAM_tipo_1_20260605_184056.pdf
size=106913
header=%PDF-
```

## Versao

- `v1.0.5`: adicionou geracao de PDF a partir do modal PIX.
- `v1.0.6`: reforcou o fechamento do modal RichFaces para nao bloquear tentativas seguintes.

## Observacao

O DAM tipo `0` pode nao existir para a competencia e o tipo `2` pode falhar sem invalidar a run, desde que pelo menos um DAM valido seja salvo. Para a DUDMA, o tipo valido observado foi `tipo=1`.
