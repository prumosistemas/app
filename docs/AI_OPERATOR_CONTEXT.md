# Contexto para operador de IA - Prumo

Versao do app: **1.0.101**
Atualizado em: **2026-08-24**

Este e o ponto de entrada para uma IA operar a Prumo sem receber, ler ou
imprimir credenciais. Os comandos abaixo usam aliases e um cofre local
criptografado pelo Windows DPAPI.

## Regra principal

Nao abra nem leia arquivos de credencial. Nao solicite ao usuario que cole um
token no chat e nao coloque segredo em argumento, log, commit, `.env` ou
documentacao. Use somente:

```powershell
cd C:\Users\ryang\Desktop\projetosv2\projeto
python -m ops.prumo_ops <area> <acao>
```

Se `requests` nao estiver instalado em uma maquina nova:

```powershell
python -m pip install -r ops\requirements.txt
```

O cofre fica em
`%LOCALAPPDATA%\Prumo\operator-secrets.dpapi.json`, fora do Git. Os valores so
podem ser abertos pelo mesmo usuario do Windows. `secrets status` mostra nomes,
nunca valores.

Os tokens HF usam `HUGGINGFACE_PRIMARY_TOKEN` e
`HUGGINGFACE_SECONDARY_TOKEN`. A conta secundaria ainda nao possui compute;
consulte `docs/HUGGINGFACE_CONTEXT.md`. Tokens nunca ficam fisicamente no Git.

## Ordem de leitura

1. `AGENTS.md`
2. este arquivo
3. `README.md`
4. `docs/SERVER_CONTEXT.md` para detalhes do ThinkPad e dados persistentes
5. `docs/OPERACAO_PRUMO_DETALHADO.md` para os fluxos
6. `docs/C4.md` para a arquitetura

Este arquivo substitui comandos antigos com `wrangler`, `modal profile` ou
variaveis secretas escritos em snapshots historicos. Os detalhes arquiteturais
dos documentos antigos continuam validos, mas a interface operacional canonica
e `ops.prumo_ops`.

Na cobrança, nunca trate `billing_disabled` como decisão manual do administrador.
Pagamento confirmado limpa somente o bloqueio financeiro; `manual_disabled`
permanece até o administrador reativar explicitamente o colaborador.

## Topologia atual

| Parte | Estado e responsabilidade | Fonte local |
| --- | --- | --- |
| GitHub | `prumosistemas/app`, branch `main`; dispara o Netlify | repositorio atual |
| Cloudflare | Worker `morning-credit-8a59`, D1 `db`, auth, telas criticas e proxy da API | `cloudflare/worker.js`, `cloudflare/wrangler.toml` |
| Netlify | site `appprumo`; publicacao complementar automatica pelo GitHub | HTMLs raiz, `netlify.toml` |
| ThinkPad | API `prumo-api`, dados em `/opt/prumo/data`, codigo em `/home/server/prumo-src` | `server/`, `deploy/docker-compose.yml` |
| Modal principal | Browserless ISS e solver Google Modo IA principal | `deploy/modal_browserless.py`, `deploy/modal_portal_nacional_google_solver.py` |
| Modal fallback | Browserless ISS de contingência e segundo solver Google Modo IA | mesmos arquivos de deploy |
| Modal terceira | Browserless ISS e terceiro solver Google Modo IA (`prumo-sistema`) | mesmos arquivos de deploy |
| Hugging Face | Dois Spaces privados ativos e uma segunda conta preparada no cofre; somente análise visual efêmera | `deploy/huggingface/navegador-headless/`, `solver/google_ai_mode/`, `docs/HUGGINGFACE_CONTEXT.md` |
| App publico | login, master, ISS Fortaleza e Portal Nacional | HTMLs raiz |

Cloudflare e a porta publica de autenticacao. A API Python fica atras do Worker
e valida `X-Internal-Secret`; o servico no host esta ligado a `127.0.0.1:8000`.
O ISS usa um pool Browserless direto nas três contas Modal, ponderado 18/4/8.
Falha de quota/workspace abre cooldown e desvia para as contas saudáveis. O
servidor volta a sondar a principal automaticamente. No Portal, o hCaptcha roda nas três contas
Modal; a análise Google Modo IA tenta os dois Spaces privados HF, depois o
egress da conta Modal que hospeda o navegador. O ThinkPad permanece como
último fallback residencial. A API espelha no ThinkPad imagens-resumo, MP4 e
eventos de auditoria dos Volumes Modal, com retenção de sete dias.

No Portal Nacional, `Notas automático` persiste somente configuração e IDs em
`automatic.json`, dentro do escopo do colaborador. O agendador do contêiner
distribui os horários ao longo do dia, garante uma tentativa por dia mesmo após
rebalanceamento, não inicia captura automática enquanto
outra run do Portal estiver ativa e retém apenas runs automáticas por 123 dias.
A captura diária sempre baixa XML+PDF e sua primeira janela começa na data
escolhida na tela.
Certificados, senhas e sessões continuam fora dos comandos e da documentação.

Na 1.0.93, o solver não consulta o Google durante cold start e cada análise
direta faz uma única tentativa interna. Um bloqueio explícito do egress Modal
é atribuído somente à tentativa/container observado: o endpoint balanceado
volta ao pool em 10 s, 5xx genérico em 15 s e o probe global cresce até 120 s.
Um sucesso confirmado zera a penalidade. Isso não usa nem recomenda conta
Google pessoal: autenticar uma conta para contornar `unusual traffic` pode
associar o bloqueio à conta.
Quando a recuperação por Chrome já identifica `/sorry/index`, ela devolve o
controle imediatamente ao failover e não abre outros perfis no mesmo egress.
O Modal principal continua preferencial enquanto saudável e a conta reserva é
usada durante a quarentena, sem alternância 2+2 desnecessária. `404 workspace
disabled` abre cooldown compartilhado de 30 minutos; `unusual` continua curto
porque pode pertencer apenas a um contêiner. Threads que aguardaram vaga
reavaliam o cooldown antes de consultar. As runs publicam
heartbeat e resumo do índice a cada 10 s; o monitor interno separa runtimes ISS
e Portal para não classificar um processo vivo como travado.

Na 1.0.97, o cooldown de falta de crédito não depende do dia de renovação. Ao
expirar, a próxima atividade sonda o Modal principal antes da reserva. Novo 404
renova a quarentena; sucesso limpa a penalidade e restaura automaticamente o
principal. `PORTAL_MODAL_DISABLED_RECHECK_SECONDS` permite ajustar o intervalo
entre 300 e 21600 segundos, com padrão de 1800.

Na 1.0.100, uma indisponibilidade simultanea dos resolvedores abre um portao por
run: os trabalhos ja iniciados terminam, uma unica nota passa a sondar a cadeia
com backoff de 10/20/30/60/90/120 segundos e os quatro downloads so reabrem
apos sucesso confirmado. Um keepalive HTTP preserva a sessao do Portal durante
esperas longas. Cenas visuais estaticas usam um quadro coerente, congelado ate
o clique; desafios temporais continuam animados e mantem captura propria. O
resolvedor residencial segue com um navegador e ultimo na ordem, agora sob um
supervisor com reinicio limitado, sem afetar os slots Browserless do ISS.
Runs manuais permanecem nesse probe ate a rota voltar ou o usuario parar. Runs
automaticas salvam o checkpoint apos dez minutos, cedem a vaga e ficam
`aguardando_solver`; retomam em 15 minutos com um unico probe. Capturas diarias
ainda nao iniciadas passam antes de retries deferidos, impedindo que os
primeiros certificados monopolizem a agenda durante uma pane longa.

Na 1.0.101, cenas de animais que parecem estaticas mas mudam enquanto a IA
responde pausam o relogio virtual do iframe ate o clique. O ThinkPad respeita o
`retry_after` do proprio circuito e deixa de ser sondado em cada nota durante
bloqueio residencial. Rejeicoes normais de circuito tambem deixaram de aparecer
na auditoria como `request_ended_early`.

Na 1.0.96, o Compose usa `init: true`, o Chrome nasce em grupo de processos e
breakpad/crash reporter ficam desativados. Isso corrige o incidente de
21/08/2026, quando 8.398 processos zumbis (`chrome`/`chrome_crashpad`) levaram o
contêiner ao limite de PIDs e causaram `can't start new thread`/conexões
encerradas. O solver residencial fica em uma vaga por padrão e continua sendo
o último fallback. Nos Spaces, a análise é serializada, threads numéricas são
limitadas e o processo Python adota e recolhe descendentes do Chrome.

No ISS Fortaleza, `Checar encerramento` usa `server/iss_closure_scan.py`. A API
abre sessões HTTP diretamente no ThinkPad, limita o conjunto a seis sessões de
rede globais e quatro por conta e não consome navegadores Modal. O estado fica
no SQLite sob uma chave da empresa, com no máximo cinco verificações visíveis
a todos os seus usuários. Cada run mantém o email do executor; somente IDs e
aliases de contas são persistidos, nunca login ou senha.

## Preparacao do cofre

Migrar silenciosamente os tokens locais conhecidos de Cloudflare, Netlify e
Modal:

```powershell
python -m ops.prumo_ops secrets migrate-local
python -m ops.prumo_ops secrets status
```

O migrador testa os tokens antes de escolher. Nesta maquina a migracao inicial
ja foi concluida e os antigos `AccountID.txt`/`token.txt` em texto puro foram
removidos da raiz. Cloudflare e os dois perfis Modal foram validados em
2026-07-16. Os tres tokens encontrados no cache local do Netlify retornaram
`401` e o token invalido nao ficou no cofre; cadastre um PAT novo quando o
Netlify precisar de operacao direta:

```powershell
python -m ops.prumo_ops secrets set NETLIFY_API_TOKEN
```

O prompt nao ecoa o valor. Para trocar o token Cloudflare sem editar arquivo:

```powershell
python -m ops.prumo_ops secrets set CLOUDFLARE_ACCOUNT_ID
python -m ops.prumo_ops secrets set CLOUDFLARE_API_TOKEN
```

Cadastrar logins por alias, em uma sessao humana local:

```powershell
python -m ops.prumo_ops secrets set-login --alias master
python -m ops.prumo_ops secrets set-login --alias laryssa
python -m ops.prumo_ops secrets set-login --alias alan
```

Emails e senhas diferentes ficam isolados por alias. Nunca suponha que usuarios
compartilham senha; cada alias deve ser cadastrado e testado separadamente.

## Diagnostico rapido

```powershell
python -m ops.prumo_ops status
python -m ops.prumo_ops cloudflare status
python -m ops.prumo_ops modal status --account primary
python -m ops.prumo_ops modal status --account fallback
python -m ops.prumo_ops hf status --account primary
python -m ops.prumo_ops hf status --account secondary
python -m ops.prumo_ops server status
python -m ops.prumo_ops app login-smoke --alias master
```

O primeiro comando mede app, API e solver e mostra o estado do Git. O login
smoke autentica, consulta `/api/me` e encerra a sessao sem imprimir email,
senha, cookie, CSRF ou token de sessao.

## Cloudflare sem Wrangler

Validar o bundle e o plano, sem publicar:

```powershell
python -m ops.prumo_ops cloudflare deploy
```

Publicar somente depois de revisar o Git e receber autorizacao:

```powershell
python -m ops.prumo_ops cloudflare deploy --apply
python -m ops.prumo_ops cloudflare status
```

A CLI chama a API REST com token em memoria, incorpora os HTMLs no modulo,
valida o JavaScript com Node, preserva bindings existentes por `inherit` e
exige que `ISS_INTERNAL_SECRET` ja exista. O deploy do script nao altera rotas
nem cron. Nunca substitua esse fluxo por um comando que coloque o token na linha
de comando.

## Netlify

O caminho normal e commit + push no GitHub; o Netlify observa o repositorio.
Como ele e complementar e pode ficar sem creditos, falha de deploy Netlify nao
significa que as telas criticas do Worker cairam.

```powershell
python -m ops.prumo_ops netlify status
python -m ops.prumo_ops netlify deploy
python -m ops.prumo_ops netlify deploy --apply
```

O deploy direto e fallback. Ele envia um ZIP atomico contendo apenas HTML,
PNG, ICO, `_redirects` e `_headers`; codigo, documentos, tokens e dados do
servidor ficam fora do pacote.

## Hugging Face

A fonte especifica dos Spaces fica em `deploy/huggingface/navegador-headless/`.
O deploy monta um bundle temporario e injeta o resolvedor canonico
`solver/google_ai_mode/google_ia_requests.py`; nao use uma copia em Downloads.

```powershell
python -m ops.prumo_ops hf status --account primary
python -m ops.prumo_ops hf deploy --account primary --space-name navegador-headless --space-name navegador-headless-2
```

## Modal sem trocar perfil

```powershell
python -m ops.prumo_ops modal billing --account primary
python -m ops.prumo_ops modal billing --account fallback
python -m ops.prumo_ops modal sync-hf-secret --account primary --hf-mode prefer
python -m ops.prumo_ops modal sync-hf-secret --account fallback --hf-mode prefer
python -m ops.prumo_ops modal deploy --account primary --target iss
python -m ops.prumo_ops modal deploy --account primary --target portal
python -m ops.prumo_ops modal deploy --account fallback --target portal
python -m ops.prumo_ops modal sync-iss-secret --account fallback --target iss
python -m ops.prumo_ops modal deploy --account fallback --target iss
python -m ops.prumo_ops modal smoke-iss --account fallback --target iss
python -m ops.prumo_ops server configure-iss-pool --apply
python -m ops.prumo_ops server smoke-iss
python -m ops.prumo_ops server metrics
python -m ops.prumo_ops server configure-tertiary --apply
```

A CLI injeta `MODAL_TOKEN_ID` e `MODAL_TOKEN_SECRET` somente no ambiente do
processo filho. `sync-hf-secret` cria o Secret Modal `prumo-huggingface` por
arquivo temporario apagado ao final; o valor vem do cofre DPAPI e e redigido da
saida. Nao use `modal profile activate`; isso altera estado global e pode
publicar na conta errada.

## ThinkPad

```powershell
python -m ops.prumo_ops server status
python -m ops.prumo_ops server runs
python -m ops.prumo_ops server logs --lines 300
python -m ops.prumo_ops server deploy
python -m ops.prumo_ops server deploy --apply
```

O acesso usa Cloudflare Access SSH. O deploy remoto e fixo: `git pull
--ff-only`, build da imagem indicada no Compose, recriacao do `prumo-api` e
health check. Dados persistentes em `/opt/prumo/data` nao sao apagados.

O failover de internet do host esta em `docs/NETWORK_FAILOVER.md`. A ALARES
continua primaria, mas so e aceita enquanto houver conectividade HTTPS real.
O cadastro inicial dos perfis Wi-Fi exige `sudo` no console do ThinkPad para
que as senhas sejam solicitadas diretamente pelo NetworkManager e nunca passem
por comando, prompt de IA, documentacao ou Git.

`server runs` e somente leitura e mostra metadados sanitizados das runs Portal
e das verificacoes de encerramento, incluindo progresso e resumo de erros, sem
senha, certificado, cookie ou resultado fiscal detalhado. `server status`
inclui o mesmo snapshot para auditorias curtas. Recuperacoes excepcionais podem
ser descritas em `.ops-server-recovery.json` apenas com IDs, emails e aliases;
o arquivo exige `"apply": true`, e apagado depois de uma aplicacao bem-sucedida
e nunca deve conter credenciais.

## Fluxo de mudanca recomendado

1. Verificar `git status --short --branch` e ler o codigo afetado.
2. Fazer mudanca minima e executar testes locais.
3. Rodar o dry-run do destino.
4. Commitar e enviar `main` quando autorizado; isso cobre GitHub/Netlify.
5. Publicar Cloudflare, Modal ou servidor apenas se os arquivos daquele destino
   mudaram.
6. Repetir `status` e um teste funcional por alias.
7. Separar resultado por canal: GitHub, Worker, Netlify, servidor e cada conta
   Modal podem ter estados diferentes.

## Limites e recuperacao

- `401` Cloudflare: rode `secrets set CLOUDFLARE_API_TOKEN`; nao abra o cofre.
- `401` Netlify: gere PAT novo na conta que possui `appprumo` e rode `secrets
  set NETLIFY_API_TOKEN`.
- Modal errado: confira `--account`; nao troque perfil global.
- SSH indisponivel: confirme a autenticacao Cloudflare Access do usuario; nao
  copie credenciais para o comando.
- Login falha: recadastre apenas o alias e repita `login-smoke`.
- Um child retry bem-sucedido nao muda o resultado historico do root run; ao
  analisar runs, relate ambos.

## Proibicoes

- Nao executar `Get-Content`/`type` em arquivos de credencial.
- Nao imprimir `.env`, `.modal.toml`, cache Netlify, cofre DPAPI, cookies,
  certificados ou blobs de conta.
- Nao colocar senha em URL, argumento, variavel persistente ou commit.
- Nao executar deploy destrutivo, apagar D1, `/opt/prumo/data`, empresa ou run
  sem pedido explicito e backup adequado.
- Nao tratar dados locais em `server/output/` como prova da producao.
