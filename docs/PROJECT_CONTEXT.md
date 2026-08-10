# Contexto do projeto Prumo

## Objetivo

O Prumo centraliza automações fiscais para ISS Fortaleza e Portal Nacional de NFS-e. O frontend é estático, a borda/autenticação roda em Cloudflare Worker e a API Python executa no servidor com dados persistentes por empresa e colaborador.

## Onde está cada coisa

| Área | Caminho |
|---|---|
| Frontend | `iss-fortaleza.html`, `portal-nacional.html` |
| Worker de borda | `cloudflare/worker.js` |
| API e filas | `server/main.py`, `server/run_queue.py` |
| ISS Fortaleza | `server/flow_*.py` |
| Portal Nacional | `server/portal_nacional.py`, `server/portal_nacional_automation.py` |
| Deploy Modal | `deploy/` |
| Testes | `tests/` |
| Operação | `docs/SERVER_CONTEXT.md`, `docs/OPERACAO_PRUMO_DETALHADO.md` |

## Estado validado em 2026-07-18

### Estabilidade e latência do login em 2026-08-10

- O código de suporte `Lr_8bPQTpimKraUHpYYFHQ` foi localizado no Workers Logs: a exceção veio do D1 em `checkRateLimit`, dentro das duas gravações que o login executava em `Promise.all`. O segundo código informado não havia sido retido pela amostragem antiga de 25%.
- O login não inicia mais a limpeza periódica do D1. Essa limpeza já pertence ao cron de um minuto e, antes desta correção, podia concorrer com o rate limit no caminho crítico.
- Os limites por IP e email agora são avaliados em um único `db.batch`, sequencial e transacional. No login aceito, limpeza dos limites, atualização dos horários, poda de sessões e criação da nova sessão também usam um único batch idempotente.
- Falhas transitórias documentadas pelo D1 recebem no máximo três tentativas, com backoff exponencial curto e jitter. Erros definitivos continuam falhando imediatamente, sem esconder defeitos nem reduzir PBKDF2 ou os limites de segurança.
- A observabilidade do Worker passou a 100% e registra rota e etapa sem corpo, senha, cookie ou token. Após o deploy, dois logins sintéticos simultâneos responderam JSON 401 em 931-959 ms, sem erro interno e sem retry; a suíte completa passou com 163 testes.
- O D1 `db` recebeu replicação global de leitura em modo `auto`. Cada requisição da API usa a Sessions API com `first-primary`: a primeira consulta confirma o estado mais recente de autenticação e as seguintes podem aproveitar réplicas mantendo consistência sequencial. Escritas continuam no primário; a replicação não tem custo adicional e não altera as cotas do plano Free.

### Captura automática do Portal em 2026-08-10

- A versão 1.0.81 adiciona `Notas automático` por colaborador e certificado. A primeira execução cobre 123 dias; as seguintes usam checkpoint com dois dias de sobreposição e permanecem disponíveis por 123 dias.
- As configurações habilitadas são distribuídas uniformemente nas 24 horas. O agendador inicia apenas uma captura automática por vez e espera o Portal ficar sem runs ativas, reduzindo colisões entre empresas e consumo simultâneo de solver.
- `Capturar agora` permite antecipar uma configuração. A run continua usando o mesmo isolamento por empresa/colaborador e o mesmo fluxo idempotente de recebidas/emitidas.
- O certificado agora pode ser editado por clique na lista. A API nunca devolve o nome original do PFX; arquivo e senha existentes são mantidos quando não forem substituídos.
- A tela ganhou `Voltar` acima de `Sair`, `Parar run` ficou restrito à aba de notas e a listagem deixou de varrer todos os arquivos das runs em cada atualização.
- Validação local: 168 testes, compilação Python e validação sintática do JavaScript inline.

### Atualização de cobrança em 2026-08-03

- O Worker separa bloqueio financeiro (`billing_disabled`) de desativação feita pelo administrador (`manual_disabled`). Pagamento pendente bloqueia os colaboradores e informa o email do administrador somente depois de a senha correta ser validada; a confirmação do pagamento remove apenas o bloqueio financeiro.
- O painel da empresa mostra aviso vermelho persistente, identifica colaboradores como `Pagamento pendente`, oculta `Reativar` durante a pendência e abriu diretamente em `Usuários`; a antiga `Visão geral` foi removida. A aba de pagamento orienta `Contate o responsável`.
- O painel master mostra a empresa como `Pendente` quando `active_until` expirou, sem confundir cobrança com a ação manual `Desativar empresa`. A suíte local passou com 98 testes Pytest e 52 testes Unittest antes da publicação.

### Correção ISS/login em 2026-08-03

- A criação de conta do ISS usa a rota canônica `POST /py/api/accounts`, sem barra final. O proxy também normaliza barras finais antes de chamar o FastAPI. Isso elimina o `307` que apontava para `http://api.prumosistemas.com.br/api/accounts` e fazia o Worker responder `A API interna não respondeu.` ao tentar reutilizar o corpo do POST.
- `login-farol.png` e `iss-fortaleza-logo.png` estavam no Netlify, mas eram interceptados pelos padrões do Worker `/login*` e `/iss-fortaleza*`. O Worker agora encaminha apenas esses dois caminhos exatos para a origem estática; ambos foram validados em produção com HTTP 200 e `image/png`.
- A troca obrigatória de senha conserva somente o aviso fixo `Sua conta exige troca de senha antes de continuar.`; os dois avisos dinâmicos redundantes foram removidos.
- Worker publicado sem alterar bindings, segredos, rotas ou cron. API e container 1.0.58 permaneceram saudáveis. Validação local: 102 testes Pytest e 52 testes Unittest.

### Importação XLSX e autofill em 2026-08-03

- Campos operacionais do ISS não aceitam mais preenchimento automático de navegador ou gerenciadores de senha. Os campos de credencial de conta ISS nas telas `iss-fortaleza.html` e `admin.html` começam somente-leitura e são liberados apenas na interação real do usuário; o login da Prumo continua com autofill normal.
- A importação XLS/XLSX lê simultaneamente a apresentação formatada e o valor bruto da célula. Quando o Excel apresenta um CNPJ em notação científica, como `1.45915E+13`, o importador usa o inteiro completo armazenado no arquivo, sem alterar campos textuais formatados como código `0012`.
- A proteção vale no carregamento automático, no editor avançado e nas linhas dinâmicas do conjunto. Worker publicado preservando bindings e rotas. Validação local: 106 testes Pytest e 52 testes Unittest.

### Privacidade do Portal e estado do editor em 2026-08-03

- Os campos de alias e senha do certificado no Portal Nacional usam a mesma proteção contra autofill dos campos operacionais do ISS: começam somente-leitura e são liberados pela interação do usuário. O formulário e os campos também sinalizam aos navegadores e gerenciadores de senha que não são credenciais de login.
- A interface do Portal não exibe o nome original do arquivo `.pfx`, nem na seleção/arraste nem na lista de certificados. Depois da escolha, mostra apenas `Arquivo selecionado`; depois do upload, mostra somente o alias cadastrado.
- O editor avançado de conjuntos persiste por usuário e por conjunto a aba da planilha, linhas de cabeçalho/início/limite, mapeamentos de colunas e conta aplicada a todos. Salvar, aplicar, fechar pelo botão ou pelo fundo preserva o estado; `Limpar` remove-o intencionalmente. Um arquivo local precisa ser selecionado novamente após recarregar a página por restrição de segurança do navegador, e então as configurações salvas são reaplicadas.
- Validação local desta atualização: 110 testes Pytest, verificação sintática do Worker e montagem de deploy sem mudança de rotas ou bindings.

### Competências do Portal Nacional em 2026-08-03

- A API 1.0.59 extrai `dCompet` do XML de cada NFS-e e agrega as quantidades por competência sem alterar o filtro de emissão do Portal. Assim, notas retroativas ou futuras encontradas no período continuam no índice e ficam visíveis separadamente.
- O detalhe da run não exibe mais a lista extensa de arquivos. Em seu lugar, mostra seletores com `MM/AAAA`, total de notas e quantidade pronta; é possível baixar uma, várias ou todas as competências.
- O ZIP contém somente XML/PDF das notas selecionadas. Quando existem várias competências, cria pastas `MM-AAAA/XML` e `MM-AAAA/PDF`; logs, `run.json` e `indice.json` não entram no pacote de notas.
- Validação local: 115 testes Pytest, compilação dos módulos Python, validação sintática do HTML e dry-run do Worker sem mudança de rotas ou bindings.

### Indisponibilidade HTTP 503 do Portal em 2026-08-03

- A prova Loquicenter entrou pelo certificado na segunda tentativa, mas a primeira janela de recebidas recebeu HTTP 503 no endpoint oficial. A causa foi classificada como `portal_indisponivel_temporario`, distinta de certificado, captcha e erro de nota.
- A API 1.0.60 repete a indexação para HTTP 429/500/502/503/504 e falhas de rede em até oito tentativas, com intervalos crescentes de 15, 30, 60, 120, 240 e até 300 segundos. A run permanece ativa e a tela informa a causa e a próxima espera.

- API alvo atual: 1.0.81, preservando autenticação direta no ThinkPad e seleção adaptativa entre as rotas de resolução antes do fallback residencial.
- Portal 1.0.72: a ação visível `Continuar` retoma emitidas e recebidas incompletas em sequência, preserva índice e arquivos válidos e mantém indisponibilidades transitórias em espera. Outage do solver não consome tentativas da nota; após falha, a concorrência cai para um probe e o Modal com sucesso recente passa a ser preferido. Respostas HTTP 503 agora preservam `reason/error` do JSON; `google_ai_request_failed`, `unusual traffic` e `/sorry/index` aplicam cooldown de 300 segundos somente ao endpoint Modal explicitamente bloqueado, enquanto 5xx genérico mantém cooldown curto para aproveitar outros containers do pool.
- Portal 1.0.73: a recuperação de sessão do Modo IA cria um grupo de processos próprio no Linux e encerra toda a árvore do Chromium ao terminar. Isso impede o acúmulo observado na Loquicenter (8.438 tarefas no contêiner), que degradava o solver residencial até `Resource temporarily unavailable`.
- Portal Alan/Loquicenter em 07/08/2026: recebidas preservadas em 72 XML/72 PDFs; emitidas retomadas somente em XML, sem reindexar e preservando 274 PDFs existentes. Backup local anterior à retomada em `Downloads/Prumo-Alan-Loquicenter-20260807`, contendo arquivos e os quatro JSONs de índice/estado, sem certificado, senha ou sessão.
- Portal 1.0.78: a captura temporal caiu de uma mediana anterior de aproximadamente 26 s para 11-13 s nas amostras normais, mantendo 30 quadros em 8,7 s. A evidência de ocupação continua síncrona para acertividade; montagem/overlay redundantes deixaram de ser gerados. Como até uma thread de MP4 no Modal elevou a captura seguinte a 37,6 s, os quadros agora são espelhados e o vídeo é montado pelo ThinkPad, limitado a quatro por ciclo, sem usar CPU do solver. Retomadas sem parâmetros preservam XML/PDF da run original, e canvas temporal vazio troca de cena/faz failover cedo.
- Portal 1.0.79: depois de confirmar o MP4 local, o espelho do ThinkPad remove somente os `quadro-*` redundantes daquela pasta. MP4, heatmap, imagens-resumo, JSONs e cliques permanecem por sete dias; os quadros brutos continuam no Volume Modal durante a mesma retenção. XML, PDF, índices, certificados e artefatos sem vídeo não entram nessa limpeza.
- Portal 1.0.80: o download de uma run lógica com recebidas e emitidas gera um único ZIP. A árvore separa primeiro `Recebidas`/`Emitidas`, depois `MM-AAAA` e então `XML`/`PDF`; o seletor de competências continua filtrando as duas partes juntas. Downloads individuais antigos permanecem compatíveis.
- Prova real em 08/08/2026: o lote mais recente finalizou 118/118 recebidas e 415/415 emitidas em XML. Nas 400 capturas recentes, as medianas ficaram em 12,04-12,27 s e P90 em 12,43-12,71 s, com 30 quadros e zero codificação no solver. A conta Modal primária encontrou dois eventos de unusual; o fallback assumiu 187 resoluções, sem unusual, e o ThinkPad não foi usado. Alan e outro colaborador executaram simultaneamente em raízes distintas, sem mistura de arquivos.
- Portal Nacional: o período é dividido por mês em janelas inclusivas de até 30 dias, cada janela é validada contra o total informado pelo Portal e os IDs são unidos sem duplicação. O período não filtra a competência: notas retroativas continuam incluídas. Para a SIM7, o Portal informou 169 emitidas em 01/06-30/06 e 205 em 01/07-17/07.
- Portal Alan/SIM7: a prova completa de 01/06 a 17/07 finalizou 374/374, com janelas 169/169 e 205/205, zero duplicata e zero erro final. Foram removidos 62 XMLs órfãos de tentativas antigas depois de validar fisicamente os 374 XMLs e 374 PDFs referenciados; nenhum arquivo válido foi removido.
- Solver Portal: Google Modo IA unificado, sem Florence/Cohere. Quatro trabalhos são distribuídos exatamente 2+2 entre as contas Modal; cada trabalho usa contêiner e Chromium isolados, evitando contenção CDP e mistura de sessão. O `requests.Session` do Modo IA permanece vivo entre etapas temporais, pois apenas restaurar cookies passou a invalidar o quadro seguinte. Widget que não abre e sessão Google inválida falham cedo; o ThinkPad residencial permanece último fallback. Capturas temporais mantêm heatmap/montagem e uma única imagem completa final, com retenção de sete dias.
- Concorrência e isolamento do Portal: o backend fixa quatro tarefas por colaborador; o HTML não permite escolher navegadores. Runtime, sessão, certificados, índices e arquivos são separados por empresa/colaborador. A prova de produção Alan/Gabriel encontrou zero IDs de run em comum e acesso cruzado retornou 404.
- ISS Laryssa: a prova real `run_OY1xfaaUUenSaIS_pgioDw` concluiu Notas na primeira tentativa em 6min56s, com 242 prestadas/25 páginas e 4 tomadas/1 página, 26 XMLs novos e zero erro.
- ISS Gabriel: a run real mais recente validada concluiu 12/12 fluxos. A raiz histórica anterior continua mostrando 12 erros corretamente, mas retentativas de bloqueios definitivos deixaram de ser agendadas.
- ISS padrão: Modal direto. O proxy continua no ThinkPad, mas não deve ser ativado no Modal sem autenticação de máquina no Cloudflare Access.
- Token do Browserless rotacionado em 2026-07-12; deploy Modal e handshake WebSocket 101 validados após a rotação.
- Login Firefox: Bearer atual tem precedência sobre cookie antigo, as páginas autenticadas usam mesma origem e login/admin/master são entregues pelo Worker com `Cache-Control: no-store`.
- Login/Worker: o incidente `1101` de 2026-07-17 revelou rejeições assíncronas escapando do `try/catch` porque os handlers eram retornados sem `await`. Todas as rotas assíncronas agora são aguardadas dentro da barreira de erro; respostas HTML de infraestrutura são reduzidas a uma mensagem segura com código de suporte, sem inserir o documento da Cloudflare no formulário.
- Monitor do ThinkPad: segredo sincronizado, arquivo de ambiente em modo `600` e `/api/internal/runtime-metrics` respondendo 200.
- Imagem alvo do servidor: `ryang20/prumo-api:1.0.81`; a imagem em produção imediatamente anterior deve ser mantida como rollback local até a validação do deploy.
- Cloudflare: Worker `morning-credit-8a59` no deploy `b8dd0650-6555-41d1-bdac-aa34bda09e35`; bundle local validado em dry-run com 119,98 KiB gzip e zero vulnerabilidades no `npm audit`.
- Modal: somente `ryangurgell20` e `fabriciofarofa5` permanecem como solvers Portal ativos. O app Florence e os apps Prumo da conta desabilitada `jorhinhogames` foram parados em 2026-07-15; `prumo-browserless` foi migrado para `ryangurgell20` e validado por handshake real.
- Servidor: Docker, cloudflared, monitor e Fail2ban ativos; 23% do disco usado, 72 GiB livres e artefatos do solver em 3,0 GiB após a primeira compactacao.
- Testes locais: a suíte inclui preservação do motivo JSON em HTTP 503, cooldown longo para bloqueio Google explícito, janela vazia legítima, backoff persistente, preferência adaptativa de Modal, retomada sem reindexar, checkpoint parcial, temporal multi-etapas, encerramento da árvore do Chrome, failover e isolamento por colaborador.
- Prova isolada pós-deploy: o solver residencial v19 abriu o hCaptcha real após recovery, atravessou quatro etapas visuais e devolveu token; ao final havia 0/4 navegadores locais ativos.
- Prova controlada Gabriel pós-1.0.57: duas notas pendentes concluíram 2/2; a run ficou com 9 baixadas, 23 pendentes por limite de teste e zero item em execução. O desafio residencial longo avançou por 13 respostas visuais válidas dentro da janela de 240 s.
- Billing consultado em 2026-07-18: o app Portal atualmente ativo acumulava aproximadamente US$ 7,34 na conta principal e US$ 5,10 na fallback no intervalo retornado pela API; o painel master continua sendo a referência operacional de crédito estimado.
- Fechamento 1.0.58: API e imagem Docker em 1.0.58; solvers principal, reserva e residencial em v21, todos com circuito fechado e zero navegador ativo. Login, Portal e ISS responderam 200 pelo Worker; login inválido retornou JSON 400 sem HTML/1101 e os dois HTMLs de automação não expõem seletor de navegadores. Git local, `origin/main` e servidor estavam no mesmo commit.
- Host no fechamento: `docker`, `cloudflared`, `fail2ban` e `prumo-monitor.service` ativos; somente `prumo-api` entre os containers Prumo; 30% do disco usado e 66 GiB livres.

## Regras operacionais

- Estado local em `server/output/` não prova produção; confirme por SSH.
- Não exiba segredos, senhas, cookies, PFX ou blobs completos do banco.
- Uma tentativa filha bem-sucedida não altera o resultado histórico da run raiz no ISS.
- Teste Portal/ISS com lote mínimo antes de ampliar o período ou a quantidade de empresas. A concorrência do Portal é automática e não é informada pelo navegador.
- GitHub e a fonte dos HTMLs. Login, master, admin, ISS, Portal e raiz são entregues diretamente pelo Worker; o fluxo automático GitHub para Netlify fica como publicação complementar quando a conta tiver créditos.
- Mudanças no Worker Cloudflare são separadas do deploy estático e devem preservar rotas internas bloqueadas.

## Pendências externas

- O deploy automático Netlify pode ser ignorado por limite de créditos da conta. As telas críticas atualizadas continuam ao vivo pelas rotas do Worker Cloudflare, sem deploy manual obrigatório.
- Debug visual fica por sete dias. Após 15 minutos, conteúdo textual é gzipado e PNG vira WebP lossless; o compose limita logs Docker a 3 x 10 MiB.
- O registro Docker externo não é necessário no caminho normal: a imagem 1.0.72 pode ser construída diretamente no ThinkPad após `git pull`. Manter a 1.0.70 como rollback local.
- O resolvedor anterior foi removido. O único caminho permitido para hCaptcha é o Google Modo IA versionado em `solver/google_ai_mode`, direto pelo Modal. A proxy do servidor só poderá ser ativada após autenticação de máquina no Cloudflare Access.
