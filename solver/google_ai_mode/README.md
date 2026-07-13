# Google Modo IA do Portal Nacional

Este diretório contém a cópia versionada e reproduzível do resolvedor validado
no projeto organizado. O deploy oficial é `deploy/modal_portal_nacional_google_solver.py`.

Arquivos:

- `api_resolvedora_resolver.py`: núcleo de navegador e hCaptcha, sem provedor legado configurado.
- `api_resolvedora_resolver_google_ia.py`: integração exclusiva com Google Modo IA.
- `google_ia_requests.py`: cliente anônimo do Modo IA.
- `detector_visual.py`: caixas e coordenadas visuais.

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
