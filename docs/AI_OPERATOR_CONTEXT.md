# Contexto para operador de IA - Prumo

Versao do app: **1.0.52**
Atualizado em: **2026-07-16**

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

## Topologia atual

| Parte | Estado e responsabilidade | Fonte local |
| --- | --- | --- |
| GitHub | `prumosistemas/app`, branch `main`; dispara o Netlify | repositorio atual |
| Cloudflare | Worker `morning-credit-8a59`, D1 `db`, auth, telas criticas e proxy da API | `cloudflare/worker.js`, `cloudflare/wrangler.toml` |
| Netlify | site `appprumo`; publicacao complementar automatica pelo GitHub | HTMLs raiz, `netlify.toml` |
| ThinkPad | API `prumo-api`, dados em `/opt/prumo/data`, codigo em `/home/server/prumo-src` | `server/`, `deploy/docker-compose.yml` |
| Modal principal | Browserless ISS e solver Google Modo IA principal | `deploy/modal_browserless.py`, `deploy/modal_portal_nacional_google_solver.py` |
| Modal fallback | segundo solver Google Modo IA, escala a zero quando ocioso | mesmo arquivo de deploy do Portal |
| App publico | login, master, ISS Fortaleza e Portal Nacional | HTMLs raiz |

Cloudflare e a porta publica de autenticacao. A API Python fica atras do Worker
e valida `X-Internal-Secret`; o servico no host esta ligado a `127.0.0.1:8000`.
O ISS usa Browserless Modal direto. O Portal usa Google Modo IA no Modal, conta
principal e fallback; o ThinkPad e apenas o ultimo fallback residencial.

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

## Modal sem trocar perfil

```powershell
python -m ops.prumo_ops modal billing --account primary
python -m ops.prumo_ops modal billing --account fallback
python -m ops.prumo_ops modal deploy --account primary --target iss
python -m ops.prumo_ops modal deploy --account primary --target portal
python -m ops.prumo_ops modal deploy --account fallback --target portal
```

A CLI injeta `MODAL_TOKEN_ID` e `MODAL_TOKEN_SECRET` somente no ambiente do
processo filho e redige qualquer ocorrencia acidental na saida. Nao use
`modal profile activate`; isso altera estado global e pode publicar na conta
errada.

## ThinkPad

```powershell
python -m ops.prumo_ops server status
python -m ops.prumo_ops server logs --lines 300
python -m ops.prumo_ops server deploy
python -m ops.prumo_ops server deploy --apply
```

O acesso usa Cloudflare Access SSH. O deploy remoto e fixo: `git pull
--ff-only`, build da imagem indicada no Compose, recriacao do `prumo-api` e
health check. Dados persistentes em `/opt/prumo/data` nao sao apagados.

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
