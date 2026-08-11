# Rede e failover do ThinkPad

Atualizado em: **2026-08-11**

## Motivo

Em 11/08/2026 o ThinkPad permaneceu associado a
`AVANCAR CONTADORES ALARES_5G`, mas a rede deixou temporariamente de entregar
IPv4 e DNS. O NetworkManager mostrava `connected`, enquanto os tuneis
`browser` e `prumo-proxy` ficaram sem replicas e a API publica respondeu
Cloudflare 1033 / HTTP 530.

A ALARES continua sendo uma rede valida e primaria. A correcao nao a bloqueia:
ela distingue associacao Wi-Fi de internet funcional. Duas requisicoes HTTPS
IPv4 reais, separadas por cinco segundos, precisam falhar antes do failover.

## Ordem das redes

1. Ethernet funcional, quando conectada.
2. `AVANCAR CONTADORES ALARES_5G`.
3. `AVANÇAR_LINK_5G`.
4. `AVANÇAR_LINK_2G`.
5. `AVANCAR_CLARO`.

Quando a conexao ativa perde internet, o verificador tenta os perfis nessa
ordem e aceita somente aquele que passar no teste HTTPS. Uma rede que apenas
associa, mas continua sem internet, e ignorada naquele ciclo.

Senhas de Wi-Fi **nao ficam neste repositorio, na documentacao ou em comandos**.
Elas sao solicitadas interativamente uma unica vez e persistidas pelo
NetworkManager nos perfis protegidos do sistema.

## Instalacao ou recadastro

No console local do ThinkPad:

```bash
cd /home/server/prumo-src
git pull --ff-only
sudo bash deploy/install-network-failover.sh
```

O instalador ativa e valida cada perfil existente. Perfil com senha incorreta
ou segredo ausente e apagado e recadastrado antes da instalacao continuar.
Depois ele configura prioridades e instala `prumo-network-failover.timer`. O
verificador roda a cada minuto, nunca acessa senhas e reinicia somente o
`cloudflared` depois de uma troca bem-sucedida.

## Diagnostico

```bash
nmcli device status
nmcli -f NAME,TYPE,DEVICE,AUTOCONNECT,AUTOCONNECT-PRIORITY connection show
systemctl status prumo-network-failover.timer --no-pager
journalctl -t prumo-network-failover --since today --no-pager
curl -4 -fsS --max-time 7 https://cp.cloudflare.com/generate_204 -o /dev/null && echo internet_ok
```

Do PC operador:

```powershell
python -m ops.prumo_ops status
python -m ops.prumo_ops server status
```

O esperado na API Cloudflare e o tunel `browser` saudavel com quatro conexoes.
O tunel historico `main`, inativo desde maio de 2026, nao faz parte da rota de
producao atual.
