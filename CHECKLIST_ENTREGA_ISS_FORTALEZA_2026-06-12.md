# Checklist de entrega - ISS Fortaleza

Data: 2026-06-12

## Correções feitas

- Login em guia anonima corrigido: o Worker agora devolve `session_token` e as paginas enviam `Authorization: Bearer`, sem depender apenas de cookie.
- Coluna "Uso" removida de "Usuarios da empresa" no painel do administrador da empresa.
- Aba de pagamento do administrador reorganizada com status mensal, ativo ate, QR Code, chave PIX, PIX copia e cola e historico de pagamentos.
- Painel master recebeu configuracao de PIX e lancamento flexivel de pagamentos por empresa, usuario opcional, mes inicial, quantidade de meses, valor, ativo ate manual e observacao.
- Master consegue ver alias, login e senha das contas ISS cadastradas por usuario nas telas de gestao.
- Logo do ISS Fortaleza trocada pela marca oficial da Prefeitura/SEFIN salva como `iss-fortaleza-logo.png`.
- Conjuntos receberam botao para baixar planilha modelo XLSX.
- Conjuntos receberam botao para exportar o conjunto atual em XLSX.
- Bug do input que desfocava a cada letra foi corrigido: a tabela nao e mais redesenhada a cada digitacao.
- Modal do editor de conjunto recebeu scroll vertical para acessar os botoes inferiores.
- Regras de run foram reforcadas: avisos agora consideram o conjunto inteiro e alertam sobre DAM/Escrituracao e falta de Codigo Dominio.
- Quando usar Codigo Dominio nas pastas e a empresa nao tiver codigo, Notas usa fallback `cnpj - empresa` sem quebrar.
- Com "reabrir escrituracao se estiver fechada" desmarcado, a escrituracao fechada vira parada controlada, informa o motivo e permite DAM seguir se estiver selecionado.
- Cada run recebeu botao para baixar PDF de relatorio simples com resumo do que rodou.
- Downloads protegidos foram ajustados para usar token tambem em modo anonimo.

## Verificacoes realizadas

- `node --check cloudflare/worker.js`
- `python -m compileall server`
- `python -m pytest -q` com 23 testes passando
- Checagem `node --check` dos scripts embutidos em:
  - `login.html`
  - `index.html`
  - `iss-fortaleza.html`
  - `admin.html`
  - `master.html`
  - `master-company.html`

## Observacoes

- A cobranca continua manual por design: o master marca o pagamento e define excecoes sem alterar codigo.
- A logo usada veio da pagina oficial da SEFIN/Prefeitura de Fortaleza.
- As pastas sem Codigo Dominio preservam o padrao seguro `cnpj - empresa`.
