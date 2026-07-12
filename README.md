# RBK Vendedor IA — Asterisk (última configuração validada)

Este pacote recompõe o repositório Asterisk no último estado validado no
piloto do RBK Vendedor IA.

## Componentes recuperados

- Dockerfile: revisão `1.0.2`
- `config/pjsip.conf.template`: revisão `1.0.4`
- `config/extensions.conf`: revisão `1.4.0`

## Ramal SIP

- Usuário: `7001`
- Contexto: `ramais`
- Porta SIP: `5160/UDP`
- RTP: `10000-10010/UDP`
- Codecs: `ulaw` e `alaw`

## Ramais de teste

- `600`: eco local do Asterisk
- `601`: bip e encerramento
- `602`: AudioSocket / eco pelo gateway-voz
- `603`: Groq STT
- `604`: STT + LLM + Piper TTS
- `605`: vendedor IA multi-turno com memória, catálogo e persistência

## Variáveis obrigatórias

```env
PUBLIC_IP=129.121.37.172
SIP_PORT=5160
RAMAL_7001_SENHA=GERAR_UMA_SENHA_FORTE_COM_32_OU_MAIS_CARACTERES
```

## Gateway interno

O `extensions.conf` utiliza:

```text
rbk-vendedor-ia_gateway-voz:9019
```

Não coloque chaves de API ou senhas reais no repositório.
