# RBK Vendedor IA — Asterisk Piloto

Servidor Asterisk isolado para testar um ramal SIP no Linphone sem custo de operadora.

## Ramal inicial

- Usuário: `7001`
- Porta SIP: `5160/UDP`
- Transporte: `UDP`
- Codecs: `PCMU/ulaw` e `PCMA/alaw`

## Testes

- Disque `600`: teste de eco bidirecional.
- Disque `601`: atende, reproduz um bip e encerra.

## Variáveis obrigatórias

```env
PUBLIC_IP=IP_PUBLICO_DA_VPS
SIP_PORT=5160
RAMAL_7001_SENHA=SENHA_FORTE_COM_PELO_MENOS_24_CARACTERES
```

## Portas UDP que precisam ser publicadas

- `5160` → `5160` — SIP
- `10000` até `10010` → mesmas portas — RTP

## Observação

Esta configuração é somente para o piloto. Antes de produção serão adicionados
TLS/SRTP, bloqueio de tentativas, política de IP, monitoramento e integração do
motor de voz.
