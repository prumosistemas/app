# Correcao: DAM via PDF real `link-imprimir-dam`

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

A tela de confirmacao nao era um PDF. O portal abriu um modal:

```text
PAGAR COM PIX VIA QR CODE
DUDMA ENGENHARIA LTDA ME
R$ 2,01
Copiar Qr Code
```

Ou seja: o clique em confirmar pagamento nao entrega o PDF do DAM. Ele renderiza uma cobranca PIX em modal HTML.

O fetch interno confirmou isso em execucao real:

```text
Fetch do DAM tipo=1 retornou conteúdo inválido:
Header inválido (esperado %PDF-, obteve b'<!DOCTYPE ')
content-type=text/html;charset=UTF-8, HTTP 200, size=95201
```

## Solucao invalida descartada

Foi tentado gerar PDF da propria tela PIX com `page.pdf()`. Isso gerava um arquivo PDF tecnicamente valido, mas nao era o documento correto: o usuario precisa do PDF real do DAM, nao uma impressao da imagem/QR Code PIX.

Essa estrategia foi descartada.

## Correcao final

O request correto foi identificado pelo navegador:

```text
POST /grpfor/pages/escrituracao/emitirDam.seam
formEmitirDam=formEmitirDam
competenciaInputDate=05%2F2026
competenciaInputCurrentDate=05%2F2026
comboImposto=1
comboTipoDam=1
comboOutroFiltroSelecionado=
javax.faces.ViewState=j_id8
formEmitirDam%3Aj_idcl=link-imprimir-dam
```

Logo, o fluxo nao deve tentar baixar a confirmacao PIX. Ele deve postar o `formEmitirDam` com:

```text
formEmitirDam:j_idcl=link-imprimir-dam
```

Esse e o caminho que retorna o PDF real do DAM.

## Correcao aplicada no codigo

O fluxo DAM agora tem quatro estrategias:

1. Confirmar a emissao no `btnConfirma`.
2. Esperar o botao `btn_imprimir` (`Impressao DAM`) e baixar o PDF real pelo POST desse botao.
3. Fallback pelo `link-imprimir-dam`, caso o portal volte a expor o fluxo antigo.
4. Qualquer retorno HTML, PIX, vazio ou sem header `%PDF-` vira erro, nao arquivo final.

O arquivo do DAM real e validado:

- tamanho minimo;
- header `%PDF-`;
- remocao de arquivo invalido;
- log com tamanho final.

O modal RichFaces `panelQrdCode` continua sendo fechado quando aparecer, apenas para nao bloquear os proximos tipos. Ele nao e mais salvo como DAM.

## Validacao real anterior

Foi executado o DAM da DUDMA com a conta do Gabriel diretamente no container de producao, usando as credenciais salvas da API sem imprimir login/senha.

Resultado da tentativa intermediaria invalidada:

```text
DAM PIX salvo como PDF: DAM_tipo_1_20260605_184056.pdf — OK (106913 bytes)
DAM(s) baixado(s): 1
=== FIM (DAM OK) ===
```

Esse resultado provou que o modal PIX era a causa, mas nao era o documento correto. A versao `v1.0.7` substituiu essa abordagem pelo POST real `link-imprimir-dam`, mas a validacao em producao mostrou que o portal retornava HTML no fetch manual e que o link real era invisivel para clique comum.

A versao `v1.0.8` tentou chamar o submit JSF do proprio portal, que e o comportamento equivalente ao `onclick` do link invisivel:

```text
_JSFFormSubmit('link-imprimir-dam', 'formEmitirDam', null, {'formEmitirDam:j_idcl':'link-imprimir-dam'})
```

Tambem fecha o modal `mensagem_confirmar_emissao_dam_modal_panel` depois de falhas, evitando que um tipo de DAM bloqueie o proximo.

A validacao seguinte mostrou que o PDF real surge depois de confirmar a emissao: o portal renderiza o modal PIX, mas tambem adiciona o botao visivel `btn_imprimir` com valor `Impressao DAM`. O clique visual nesse botao e bloqueado pelo overlay `panelQrdCode`, mas o POST do proprio botao retorna:

```text
content-type=application/pdf
content-disposition=attachment; filename="DamISS.pdf"
header=%PDF-
```

Por isso a versao `v1.0.9` usa o `btn_imprimir` como caminho principal.

Arquivo da tentativa intermediaria:

```text
/app/output/empresas/HOpfZgXYdk0PMALbPUGCZg/colaboradores/btGJUFE5yf5Gtqp80e_JUw/runs/debug_dudma_dam_v105_1780684820/tentativa_1/20194719000118 - DUDMA ENGENHARIA LTDA ME/dam/DAM_tipo_1_20260605_184056.pdf
size=106913
header=%PDF-
```

## Versao

- `v1.0.5`: adicionou geracao de PDF a partir do modal PIX.
- `v1.0.6`: reforcou o fechamento do modal RichFaces para nao bloquear tentativas seguintes.
- `v1.0.7`: removeu a solucao de PDF da tela PIX e passou a baixar o PDF real via `formEmitirDam:j_idcl=link-imprimir-dam`.
- `v1.0.8`: usa o submit JSF real do `link-imprimir-dam` e limpa o modal de confirmacao quando uma tentativa falha.
- `v1.0.9`: confirma a emissao e baixa o PDF real via POST do botao `btn_imprimir` (`Impressao DAM`).

## Observacao

O DAM tipo `0` pode nao existir para a competencia e o tipo `2` pode falhar sem invalidar a run, desde que pelo menos um DAM real valido seja salvo. Para a DUDMA, o tipo valido observado foi `tipo=1`.
