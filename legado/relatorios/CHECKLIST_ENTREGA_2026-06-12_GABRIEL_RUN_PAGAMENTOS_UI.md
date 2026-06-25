# Checklist de entrega - 2026-06-12

## Contexto verificado

- Login de teste usado para conferir a ultima execucao do Gabriel: `gabriel@avancar.com`.
- Run analisada: `run_0-GKphDDFwhMdf7Vn3QIEQ`.
- Sintoma confirmado: Escrituração ficou como `interrompida` com `ESCRITURACAO_FECHADA_REABERTURA_DESATIVADA`, enquanto o DAM rodou `ok`.

## Correcoes realizadas

- Escrituração fechada com reabertura desmarcada agora termina como `ok` com aviso, nao como `interrompida`.
- O DAM continua podendo rodar depois desse aviso da Escrituração.
- ZIP de run sem arquivos gerados nao retorna erro; baixa um ZIP com `SEM_ARQUIVOS_GERADOS.txt`.
- Relatorio da run agora e HTML organizado, com cards de resumo, tabela de resultados, avisos e arquivos.
- Notas sem Codigo Dominio agora usam:
  - `notas/prestadas/CNPJ - EMPRESA`
  - `notas/tomadas/CNPJ - EMPRESA`
- Notas com Codigo Dominio continuam usando:
  - `notas/prestadas/CODIGO - EMPRESA`
  - `notas/tomadas/CODIGO - EMPRESA`
- Adicionada busca em:
  - lista de runs;
  - conjuntos;
  - run selecionada;
  - modal de nova run.
- Lista master de empresas foi aliviada para nao montar detalhes pesados de todos os usuarios de uma vez.
- Modelo de conjunto agora usa empresas, CNPJs e conta ficticios.
- Botoes do editor de conjunto foram agrupados para ficar mais organizado.
- Aba de pagamento do administrador da empresa agora mostra apenas:
  - status;
  - ativo ate;
  - QR Code PIX;
  - chave PIX;
  - PIX copia e cola.
- Painel master removeu `Mensalidade exibida`.
- Lancamento de pagamento no master agora e sempre por empresa inteira.
- Regra de pagamento agora e aplicada no login e nas listagens:
  - admin da empresa nao e desativado por vencimento;
  - colaboradores sao bloqueados quando o pagamento vence;
  - admin nao consegue reativar colaborador enquanto a empresa esta vencida;
  - pagamento reativa os colaboradores que nao estavam desativados manualmente;
  - desativacao manual continua separada de exclusao.
- `master-company.html` agora mostra tabela simples de usuarios da empresa:
  - email;
  - tipo;
  - status;
  - ultimo login.
- Detalhes do usuario foram movidos para modal individual.
- No modal individual do usuario master ve:
  - dados do usuario;
  - contas ISS com login e senha;
  - conjuntos;
  - exportacao CSV dos conjuntos.
- Novo design de login aplicado com imagem `login-farol.png`, mantendo a logica de autenticacao existente.

## Verificacoes executadas

- `node --check cloudflare/worker.js`
- `python -m py_compile server/main.py server/domain.py server/flow_notas.py server/run_queue.py`
- Teste local da pasta de Notas sem Codigo Dominio:
  - esperado e obtido: `prestadas/47276980000113 - JARS DRONES E COMUNICACAO VISUAL LTDA`
  - esperado e obtido: `tomadas/47276980000113 - JARS DRONES E COMUNICACAO VISUAL LTDA`
- Teste local da pasta de Notas com Codigo Dominio:
  - esperado e obtido: `prestadas/206 - JARS DRONES E COMUNICACAO VISUAL LTDA`
  - esperado e obtido: `tomadas/206 - JARS DRONES E COMUNICACAO VISUAL LTDA`

## Observacoes

- A versao da API foi atualizada para `1.0.11`.
- A pasta `LOGIN novo/` foi usada como referencia local; somente o ativo necessario foi incorporado como `login-farol.png`.
