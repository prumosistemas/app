# Contexto Hugging Face da Prumo

Atualizado em: **2026-08-12**
Versao da Prumo: **1.0.93**

## Segredos

Os tokens nao ficam no repositorio. Eles estao no cofre DPAPI do usuario
Windows, em `%LOCALAPPDATA%\Prumo\operator-secrets.dpapi.json`, com estes
aliases:

- `HUGGINGFACE_PRIMARY_TOKEN`: conta `ryanzinprot`;
- `HUGGINGFACE_SECONDARY_TOKEN`: conta `jorjoinho`;
- `HUGGINGFACE_TOKEN`: alias legado da conta primaria, mantido por compatibilidade.

Os arquivos `hf_write_token.txt` e `hf_write_token - 2.txt` foram removidos de
Downloads depois de os tokens serem validados e protegidos. Nunca documente o
valor, abra o cofre ou recrie esses TXT.

```powershell
cd C:\Users\ryang\Desktop\projetosv2\projeto
python -m ops.prumo_ops secrets status
python -m ops.prumo_ops hf status --account primary
python -m ops.prumo_ops hf status --account secondary
```

## Spaces e limite gratuito

Ativos na conta primaria:

- `ryanzinprot/navegador-headless` — privado, `zero-a10g`;
- `ryanzinprot/navegador-headless-2` — privado, `zero-a10g`.

A conta secundaria tinha apenas o Space estatico descartavel
`space-teste-0807`, removido durante a limpeza. Tentativas reais de criar
compute com ZeroGPU e CPU Basic receberam HTTP 402. A API informou que a conta
nova deve aguardar 30 dias ou assinar PRO para ZeroGPU; pela regra publica
atual, criacao Gradio/Docker requer plano pago e a excecao gratuita e de ate
dois Gradio ZeroGPU para conta pessoal elegivel. Nenhum auxiliar parcial ficou
ativo e nenhum hardware pago foi selecionado.

Segundo a documentacao oficial atual, CPU Basic nao tem preco horario, mas a
criacao de Space Gradio/Docker exige plano pago. Uma conta pessoal gratuita em
boa situacao pode manter ate dois Spaces Gradio ZeroGPU:

- https://huggingface.co/docs/hub/spaces-overview
- https://huggingface.co/pricing

O endpoint da Prumo usa somente CPU, mas estes dois Spaces existentes estao no
plano ZeroGPU gratuito e o runtime exige ao menos uma funcao `@spaces.GPU`.
Por isso `test_google_ai` conserva o decorador de compatibilidade; o Chrome e o
Modo IA continuam executando em CPU. Remover esse decorador causa
`No @spaces.GPU function detected during startup`. O hardware gratuito pode
dormir e ter cold start. Nao selecionar hardware pago sem autorizacao.

## Criacao futura da conta secundaria

Fonte operacional atual do Space:

```text
C:\Users\ryang\Downloads\navegador-headless-hf
```

Quando a conta ficar elegivel:

```powershell
python -m ops.prumo_ops hf deploy --account secondary `
  --source-dir "C:\Users\ryang\Downloads\navegador-headless-hf" `
  --space-name navegador-headless-prumo `
  --space-name navegador-headless-prumo-2
```

Depois de ambos ficarem `RUNNING`, adicione os IDs ao pool, associe o alias
`HUGGINGFACE_SECONDARY_TOKEN` ao proprietario e sincronize o Secret Modal nas
duas contas. O cliente ja aceita token privado por proprietario. Ate la, o
token secundario permanece somente no cofre local e nao e enviado ao Modal.

## Politica de desempenho

- Cada Space gratuito processa uma analise por vez.
- Os dois Spaces primarios recebem primeiro as imagens efemeras dos captchas.
- A espera HF termina em 30 segundos; fila excedente segue ao Modo IA direto
  do Modal, evitando prender quatro trabalhadores em dois Spaces.
- O ThinkPad permanece no ultimo fallback.
- PFX, senha, cookies e arquivos fiscais nunca seguem ao Hugging Face.

Em 12/08/2026, os dois Spaces falhavam antes da analise com `can't start new
thread`: o desktop permanente mantinha Chrome, Xvfb, Openbox, x11vnc e noVNC
ativos e esgotava PIDs/threads. A fonte passou a deixar o Chrome real iniciar
sob demanda e a fazer uma tentativa por egress. Depois do ajuste, os testes
reais do Modo IA responderam com sucesso em aproximadamente 28 s e 14 s.
