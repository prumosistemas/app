---
title: Google Chrome Container
emoji: 🖥️
colorFrom: blue
colorTo: gray
sdk: gradio
sdk_version: 5.49.1
app_file: app.py
pinned: false
license: mit
short_description: Google Modo IA com Chrome oficial sob demanda
---

# Google Modo IA no Hugging Face

Google Chrome Stable oficial sob demanda para consultar o Google Modo IA. O pacote oficial da Google é baixado e extraído na inicialização, sem depender de privilégios de root.

## Operacao

- Google Chrome Stable oficial aberto somente quando a recuperacao visual exige;
- Gradio privado com API `test_google_ai`;
- uma analise por Space para evitar disputa pelo perfil;
- probe oculto `@spaces.GPU` apenas para compatibilidade com o hardware ZeroGPU.

O caminho normal de analise e CPU e nao reserva cota GPU. O desktop permanente
fica desativado porque Chrome + Xvfb + Openbox + x11vnc esgotavam o limite de
threads do Space.

## Persistência

O estado anonimo fica em `/tmp/google-ai-mode-state` e pode desaparecer quando o Space dormir, reiniciar ou for reconstruido.

## Hardware

O Google Chrome usa CPU. O probe ZeroGPU nao participa da navegacao nem da analise.

## Diagnóstico do Modo IA

A interface inclui um teste visual privado que envia uma imagem ao Google Modo IA usando o mesmo egress do Space. O resultado informa latência, quantidade de requisições e se houve bloqueio `unusual traffic`, sem expor cookies da sessão.
