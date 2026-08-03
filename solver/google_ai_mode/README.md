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
O loop visual é restaurado antes do clique para que o próprio widget processe
a interação; o congelamento existe somente durante a análise do Modo IA.

Cookies, perfis, respostas, imagens e circuit breakers não pertencem ao Git.
Em produção, `GOOGLE_AI_STATE_DIR=/google-ai` aponta esse estado para um Volume
privado do Modal; o código é carregado da imagem em `/app`.

Teste mínimo antes do deploy:

```powershell
python -m py_compile solver\google_ai_mode\api_resolvedora_resolver.py solver\google_ai_mode\api_resolvedora_resolver_google_ia.py solver\google_ai_mode\google_ia_requests.py solver\google_ai_mode\detector_visual.py
modal deploy deploy\modal_portal_nacional_google_solver.py
```

O padrão é saída direta do Modal. A proxy do ThinkPad só pode ser ativada após
um probe real com autenticação de máquina no Cloudflare Access.
