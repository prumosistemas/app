# Google Modo IA do Portal Nacional

Este diretório contém a cópia versionada e reproduzível do resolvedor validado
no projeto organizado. O deploy oficial é `deploy/modal_portal_nacional_google_solver.py`.

Arquivos:

- `api_resolvedora_resolver.py`: núcleo de navegador e hCaptcha, sem provedor legado configurado.
- `api_resolvedora_resolver_google_ia.py`: integração exclusiva com Google Modo IA.
- `google_ia_requests.py`: cliente anônimo do Modo IA.
- `detector_visual.py`: caixas e coordenadas visuais.

O fluxo visual classifica cada cena como estática, temporal completa,
trajetória ou desconhecida. Perguntas que dependem do ciclo inteiro (por exemplo,
"nunca"/"sempre") observam até 80 quadros/~17,4 s e só encerram antes depois
de movimento comprovado, ao menos dez segundos e retorno relativo ao estado
inicial. A IA continua recebendo uma única sobreposição de permanência na mesma
geometria do clique (passagem fraca, permanência forte), enquanto os quadros
extras são processados no próprio container; a montagem cronológica continua
salva para depuração. Uma cena
complexa repetida além do limite é trocada pelo botão de atualizar do hCaptcha.
O iframe do hCaptcha permanece com animação e temporizadores nativos durante
toda a análise. A evidência enviada à IA já está salva, e o alvo estático é
relocalizado no quadro atual imediatamente antes do clique.

Na v43, a página oficial da NFS-e é mantida como contexto do widget visível.
Isso evita que uma resposta visual correta seja recusada por ter sido emitida
numa página `127.0.0.1`. O script é carregado explicitamente depois da troca do
documento, com CSP liberada somente nessa página isolada do solver; resets
reinjetam o mesmo widget sem perder a origem.
A URL recebida pelo solver é limitada aos hosts oficiais; `PORTAL_SOLVER_PRESERVE_ORIGIN=0` existe
somente como compatibilidade de rollback para o documento local antigo.
O canvas visual é reconhecido por sua estrutura e dimensões, sem depender do
texto de acessibilidade. A classificação temporal reconhece as perguntas em
português e inglês, inclusive `nunca/never` e `sempre/always`.
Quando o hCaptcha aprova o checkbox sem exibir grade, o token é recolhido e
devolvido imediatamente, sem recarregar o widget nem consumir uma análise visual.
Se o clique no iframe pai não abrir a grade, o solver tenta imediatamente um
clique CDP dentro do iframe do checkbox, sem esperar o timeout antigo de 30 s.
O widget é renderizado em modo visível. O checkbox e a API oficial
`hcaptcha.execute` são tentados, com cliques CDP como recuperação. Estados que
existem, mas não contêm grade, canvas, tarefas nem conclusão, renovam o navegador
após duas leituras. A cena conhecida da abelha pode repetir o mesmo fundo por
até oito etapas, pois a trajetória muda em cada ciclo; desafios desconhecidos
continuam sendo trocados cedo. O teto de 360 s evita pagar principal, reserva e
fallback por sequências corretas que foram interrompidas no meio.

Cookies, perfis, respostas, imagens e circuit breakers não pertencem ao Git.
Em produção, `GOOGLE_AI_STATE_DIR=/google-ai` aponta esse estado para um Volume
privado do Modal; o código é carregado da imagem em `/app`.

Containers que não conseguem formar uma sessão do Modo IA abrem o circuito
após três falhas consecutivas. O servidor então aplica cooldown de cinco
minutos àquela conta e segue para a próxima rota, evitando até 30 recuperações
caras dentro da mesma nota.

Teste mínimo antes do deploy:

```powershell
python -m py_compile solver\google_ai_mode\api_resolvedora_resolver.py solver\google_ai_mode\api_resolvedora_resolver_google_ia.py solver\google_ai_mode\google_ia_requests.py solver\google_ai_mode\detector_visual.py
modal deploy deploy\modal_portal_nacional_google_solver.py
```

O padrão é saída direta do Modal. A proxy do ThinkPad só pode ser ativada após
um probe real com autenticação de máquina no Cloudflare Access.
