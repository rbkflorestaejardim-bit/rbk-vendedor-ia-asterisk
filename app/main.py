import hmac
import os
import re
import json
import time as time_module
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any, Literal
from uuid import UUID, uuid4

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, Security, status as http_status
from fastapi.encoders import jsonable_encoder
from fastapi.security import APIKeyHeader
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, field_validator, model_validator
from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import VoiceResponse


DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
API_KEY = os.getenv("API_KEY", "").strip()
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "").strip()
TWILIO_BASE_URL = os.getenv("TWILIO_BASE_URL", "").strip().rstrip("/")


OLIST_API_BASE_URL = os.getenv(
    "OLIST_API_BASE_URL",
    "https://api.tiny.com.br/public-api/v3",
).strip().rstrip("/")
OLIST_AUTH_URL = os.getenv(
    "OLIST_AUTH_URL",
    "https://accounts.tiny.com.br/realms/tiny/protocol/openid-connect/auth",
).strip()
OLIST_TOKEN_URL = os.getenv(
    "OLIST_TOKEN_URL",
    "https://accounts.tiny.com.br/realms/tiny/protocol/openid-connect/token",
).strip()
OLIST_CLIENT_ID = os.getenv("OLIST_CLIENT_ID", "").strip()
OLIST_CLIENT_SECRET = os.getenv("OLIST_CLIENT_SECRET", "").strip()
OLIST_REDIRECT_URI = os.getenv("OLIST_REDIRECT_URI", "").strip()
OLIST_SCOPE = os.getenv("OLIST_SCOPE", "openid").strip() or "openid"
OLIST_TOKEN_CRYPTO_KEY = os.getenv(
    "OLIST_TOKEN_CRYPTO_KEY",
    "",
).strip()
OLIST_ID_LISTA_PRECO = os.getenv(
    "OLIST_ID_LISTA_PRECO",
    "",
).strip()
OLIST_TIMEOUT_SECONDS = int(
    os.getenv("OLIST_TIMEOUT_SECONDS", "25")
)

api_key_header = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    description="Chave privada de acesso à API Comercial RBK.",
)

STATUS_AGENDA = {
    "pendente",
    "em_execucao",
    "concluida",
    "reagendada",
    "cancelada",
    "sem_resposta",
}

STATUS_CHAMADA = {
    "iniciada",
    "em_andamento",
    "concluida",
    "nao_atendida",
    "ocupado",
    "falha",
    "cancelada",
}

STATUS_PENDENCIA_COMERCIAL = {
    "pendente",
    "em_analise",
    "aguardando_reposicao",
    "aguardando_catalogo",
    "resolvida",
    "cancelada",
}

FUSO_PROJETO = ZoneInfo("America/Sao_Paulo")

STATUS_TWILIO_PARA_INTERNO = {
    "queued": "iniciada",
    "initiated": "iniciada",
    "ringing": "em_andamento",
    "in-progress": "em_andamento",
    "completed": "concluida",
    "busy": "ocupado",
    "failed": "falha",
    "no-answer": "nao_atendida",
    "canceled": "cancelada",
}


def obter_conexao():
    if not DATABASE_URL:
        raise RuntimeError("A variável DATABASE_URL não foi configurada.")

    return psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row,
        connect_timeout=10,
    )


def validar_api_key(
    chave_recebida: str | None = Security(api_key_header),
) -> None:
    if not API_KEY:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="A API ainda não possui chave de acesso configurada.",
        )

    if not chave_recebida or not hmac.compare_digest(chave_recebida, API_KEY):
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="Chave de acesso inválida ou ausente.",
        )


def normalizar_documento(valor: str | None) -> str | None:
    if valor is None:
        return None
    documento = re.sub(r"\D", "", valor)
    return documento or None


def obter_vendedor_por_codigo(cursor, codigo: str) -> dict[str, Any]:
    cursor.execute(
        """
        SELECT id, codigo, nome, ativo, uf_principal
        FROM comercial.vendedores_ia
        WHERE codigo = %s;
        """,
        (codigo.upper(),),
    )
    vendedor = cursor.fetchone()

    if vendedor is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Vendedor não encontrado.",
        )

    if not vendedor["ativo"]:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="O vendedor informado está inativo.",
        )

    return vendedor


def obter_cliente_por_id(cursor, cliente_id: UUID) -> dict[str, Any]:
    cursor.execute(
        """
        SELECT
            c.*,
            v.codigo AS vendedor_codigo,
            v.nome_exibicao AS vendedor_nome
        FROM comercial.clientes c
        LEFT JOIN comercial.vendedores_ia v
            ON v.id = c.vendedor_id
        WHERE c.id = %s;
        """,
        (cliente_id,),
    )
    cliente = cursor.fetchone()

    if cliente is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Cliente não encontrado.",
        )

    return cliente


def obter_agenda_detalhada(cursor, agenda_id: UUID) -> dict[str, Any]:
    cursor.execute(
        """
        SELECT
            a.*,
            c.cpf_cnpj,
            c.razao_social,
            c.nome_fantasia,
            c.nome_contato,
            c.telefone,
            c.whatsapp,
            c.email,
            c.cidade,
            c.uf,
            c.status AS cliente_status,
            c.opt_out,
            c.bloqueado,
            c.dados_adicionais,
            v.codigo AS vendedor_codigo,
            v.nome_exibicao AS vendedor_nome
        FROM comercial.agendas_comerciais a
        JOIN comercial.clientes c
            ON c.id = a.cliente_id
        JOIN comercial.vendedores_ia v
            ON v.id = a.vendedor_id
        WHERE a.id = %s;
        """,
        (agenda_id,),
    )
    agenda = cursor.fetchone()

    if agenda is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Agenda não encontrada.",
        )

    return agenda


def obter_chamada_detalhada(cursor, chamada_id: UUID) -> dict[str, Any]:
    cursor.execute(
        """
        SELECT
            ch.*,
            a.data_agenda,
            a.status AS agenda_status,
            c.cpf_cnpj,
            c.razao_social,
            c.nome_fantasia,
            c.nome_contato,
            c.telefone,
            c.whatsapp,
            c.cidade,
            c.uf,
            v.codigo AS vendedor_codigo,
            v.nome_exibicao AS vendedor_nome
        FROM comercial.chamadas_ia ch
        LEFT JOIN comercial.agendas_comerciais a
            ON a.id = ch.agenda_id
        JOIN comercial.clientes c
            ON c.id = ch.cliente_id
        JOIN comercial.vendedores_ia v
            ON v.id = ch.vendedor_id
        WHERE ch.id = %s;
        """,
        (chamada_id,),
    )
    chamada = cursor.fetchone()

    if chamada is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Chamada não encontrada.",
        )

    return chamada


def validar_numero_e164(numero: str) -> str:
    numero_normalizado = numero.strip()

    if not re.fullmatch(r"\+[1-9]\d{7,14}", numero_normalizado):
        raise ValueError(
            "Use o número no formato internacional E.164, por exemplo +5541999999999."
        )

    return numero_normalizado


def mascarar_telefone(numero: str) -> str:
    if len(numero) <= 6:
        return "***"
    return f"{numero[:4]}{'*' * max(len(numero) - 8, 3)}{numero[-4:]}"


def obter_cliente_twilio() -> TwilioClient:
    return TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


async def validar_webhook_twilio(request: Request) -> dict[str, str]:
    assinatura = request.headers.get("X-Twilio-Signature", "")
    formulario = await request.form()
    dados = {str(chave): str(valor) for chave, valor in formulario.multi_items()}

    url_assinada = f"{TWILIO_BASE_URL}{request.url.path}"
    if request.url.query:
        url_assinada = f"{url_assinada}?{request.url.query}"

    validador = RequestValidator(TWILIO_AUTH_TOKEN)

    if not assinatura or not validador.validate(
        url_assinada,
        dados,
        assinatura,
    ):
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="Assinatura do webhook Twilio inválida.",
        )

    return dados


def validar_configuracao_olist(
    exigir_credenciais: bool = True,
) -> None:
    variaveis = {
        "OLIST_API_BASE_URL": OLIST_API_BASE_URL,
        "OLIST_AUTH_URL": OLIST_AUTH_URL,
        "OLIST_TOKEN_URL": OLIST_TOKEN_URL,
        "OLIST_REDIRECT_URI": OLIST_REDIRECT_URI,
        "OLIST_TOKEN_CRYPTO_KEY": OLIST_TOKEN_CRYPTO_KEY,
    }

    if exigir_credenciais:
        variaveis.update(
            {
                "OLIST_CLIENT_ID": OLIST_CLIENT_ID,
                "OLIST_CLIENT_SECRET": OLIST_CLIENT_SECRET,
            }
        )

    ausentes = [
        nome
        for nome, valor in variaveis.items()
        if not valor
    ]
    if ausentes:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Integração Olist incompleta. Variáveis ausentes: "
                + ", ".join(ausentes)
            ),
        )


def solicitar_token_olist(
    dados_formulario: dict[str, str],
) -> dict[str, Any]:
    validar_configuracao_olist()

    corpo = urllib.parse.urlencode(
        dados_formulario
    ).encode("utf-8")

    requisicao = urllib.request.Request(
        OLIST_TOKEN_URL,
        data=corpo,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "RBK-Vendedor-IA-API/0.9.3",
        },
    )

    try:
        with urllib.request.urlopen(
            requisicao,
            timeout=OLIST_TIMEOUT_SECONDS,
        ) as resposta:
            conteudo = resposta.read().decode(
                "utf-8",
                errors="replace",
            )
            retorno = json.loads(conteudo)
    except urllib.error.HTTPError as erro:
        corpo_erro = erro.read().decode(
            "utf-8",
            errors="replace",
        )
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"Olist OAuth retornou HTTP {erro.code}: "
                f"{corpo_erro[:1000]}"
            ),
        ) from erro
    except (
        urllib.error.URLError,
        TimeoutError,
        json.JSONDecodeError,
    ) as erro:
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail=f"Falha na comunicação OAuth com a Olist: {erro}",
        ) from erro

    if not retorno.get("access_token"):
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail="A Olist não retornou access_token.",
        )

    return retorno


def salvar_tokens_olist(
    cursor,
    retorno: dict[str, Any],
) -> dict[str, Any]:
    agora = datetime.now(timezone.utc)
    expira_em = agora + timedelta(
        seconds=max(int(retorno.get("expires_in", 14400)), 60)
    )
    refresh_expira_em = agora + timedelta(
        seconds=max(
            int(retorno.get("refresh_expires_in", 86400)),
            60,
        )
    )

    access_token = str(retorno["access_token"])
    refresh_token = str(retorno.get("refresh_token") or "")

    if not refresh_token:
        cursor.execute(
            """
            SELECT
                pgp_sym_decrypt(
                    refresh_token_cifrado,
                    %s
                ) AS refresh_token
            FROM comercial.olist_oauth_tokens
            WHERE id = 1;
            """,
            (OLIST_TOKEN_CRYPTO_KEY,),
        )
        existente = cursor.fetchone()
        if existente:
            refresh_token = existente["refresh_token"]

    if not refresh_token:
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail="A Olist não retornou refresh_token.",
        )

    cursor.execute(
        """
        INSERT INTO comercial.olist_oauth_tokens (
            id,
            access_token_cifrado,
            refresh_token_cifrado,
            token_type,
            scope,
            expira_em,
            refresh_expira_em,
            atualizado_em
        )
        VALUES (
            1,
            pgp_sym_encrypt(%s, %s),
            pgp_sym_encrypt(%s, %s),
            %s,
            %s,
            %s,
            %s,
            NOW()
        )
        ON CONFLICT (id) DO UPDATE
        SET
            access_token_cifrado = EXCLUDED.access_token_cifrado,
            refresh_token_cifrado = EXCLUDED.refresh_token_cifrado,
            token_type = EXCLUDED.token_type,
            scope = EXCLUDED.scope,
            expira_em = EXCLUDED.expira_em,
            refresh_expira_em = EXCLUDED.refresh_expira_em,
            atualizado_em = NOW();
        """,
        (
            access_token,
            OLIST_TOKEN_CRYPTO_KEY,
            refresh_token,
            OLIST_TOKEN_CRYPTO_KEY,
            str(retorno.get("token_type") or "Bearer"),
            str(retorno.get("scope") or OLIST_SCOPE),
            expira_em,
            refresh_expira_em,
        ),
    )

    return {
        "token_type": str(retorno.get("token_type") or "Bearer"),
        "scope": str(retorno.get("scope") or OLIST_SCOPE),
        "expira_em": expira_em,
        "refresh_expira_em": refresh_expira_em,
    }


def carregar_tokens_olist(cursor) -> dict[str, Any] | None:
    validar_configuracao_olist()

    cursor.execute(
        """
        SELECT
            pgp_sym_decrypt(
                access_token_cifrado,
                %s
            ) AS access_token,
            pgp_sym_decrypt(
                refresh_token_cifrado,
                %s
            ) AS refresh_token,
            token_type,
            scope,
            expira_em,
            refresh_expira_em,
            atualizado_em
        FROM comercial.olist_oauth_tokens
        WHERE id = 1;
        """,
        (
            OLIST_TOKEN_CRYPTO_KEY,
            OLIST_TOKEN_CRYPTO_KEY,
        ),
    )
    return cursor.fetchone()


def obter_access_token_olist(
    forcar_renovacao: bool = False,
) -> str:
    with obter_conexao() as conexao:
        with conexao.cursor() as cursor:
            tokens = carregar_tokens_olist(cursor)

            if tokens is None:
                raise HTTPException(
                    status_code=http_status.HTTP_409_CONFLICT,
                    detail=(
                        "A integração Olist ainda não foi autorizada. "
                        "Execute /olist/oauth/iniciar."
                    ),
                )

            agora = datetime.now(timezone.utc)
            margem = timedelta(seconds=90)

            if (
                not forcar_renovacao
                and tokens["expira_em"] > agora + margem
            ):
                return tokens["access_token"]

            if tokens["refresh_expira_em"] <= agora + margem:
                raise HTTPException(
                    status_code=http_status.HTTP_409_CONFLICT,
                    detail=(
                        "O refresh token da Olist expirou. "
                        "Autorize novamente em /olist/oauth/iniciar."
                    ),
                )

            retorno = solicitar_token_olist(
                {
                    "grant_type": "refresh_token",
                    "client_id": OLIST_CLIENT_ID,
                    "client_secret": OLIST_CLIENT_SECRET,
                    "refresh_token": tokens["refresh_token"],
                }
            )
            salvar_tokens_olist(cursor, retorno)
        conexao.commit()

    return str(retorno["access_token"])


def requisicao_get_olist(
    caminho: str,
    parametros: dict[str, Any] | None = None,
    repetir_apos_401: bool = True,
) -> tuple[Any, dict[str, str]]:
    token = obter_access_token_olist()
    url = f"{OLIST_API_BASE_URL}/{caminho.lstrip('/')}"

    parametros_limpos = {
        chave: valor
        for chave, valor in (parametros or {}).items()
        if valor not in (None, "")
    }
    if parametros_limpos:
        url += "?" + urllib.parse.urlencode(parametros_limpos)

    requisicao = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "RBK-Vendedor-IA-API/0.9.3",
        },
    )

    try:
        with urllib.request.urlopen(
            requisicao,
            timeout=OLIST_TIMEOUT_SECONDS,
        ) as resposta:
            conteudo = resposta.read().decode(
                "utf-8",
                errors="replace",
            )
            dados = json.loads(conteudo)
            limites = {
                "limite": resposta.headers.get(
                    "X-RateLimit-Limit",
                    "",
                ),
                "restante": resposta.headers.get(
                    "X-RateLimit-Remaining",
                    "",
                ),
                "reset_segundos": resposta.headers.get(
                    "X-RateLimit-Reset",
                    "",
                ),
            }
            return dados, limites

    except urllib.error.HTTPError as erro:
        corpo_erro = erro.read().decode(
            "utf-8",
            errors="replace",
        )

        if erro.code == 401 and repetir_apos_401:
            obter_access_token_olist(forcar_renovacao=True)
            return requisicao_get_olist(
                caminho,
                parametros,
                repetir_apos_401=False,
            )

        if erro.code == 429:
            raise HTTPException(
                status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    "Limite de requisições da Olist atingido. "
                    f"Retorno: {corpo_erro[:1000]}"
                ),
            ) from erro

        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"Olist API retornou HTTP {erro.code}: "
                f"{corpo_erro[:1200]}"
            ),
        ) from erro

    except (
        urllib.error.URLError,
        TimeoutError,
        json.JSONDecodeError,
    ) as erro:
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail=f"Falha na comunicação com a Olist: {erro}",
        ) from erro


def normalizar_texto_busca(valor: Any) -> str:
    texto = unicodedata.normalize(
        "NFKD",
        str(valor or ""),
    )
    texto = "".join(
        caractere
        for caractere in texto
        if not unicodedata.combining(caractere)
    )
    texto = texto.casefold()
    texto = re.sub(r"[^a-z0-9]+", " ", texto)
    return re.sub(r"\s+", " ", texto).strip()


def tokens_busca(valor: Any) -> list[str]:
    ignorar = {
        "a",
        "o",
        "as",
        "os",
        "de",
        "da",
        "do",
        "das",
        "dos",
        "para",
        "por",
        "com",
        "um",
        "uma",
        "uns",
        "umas",
        "preciso",
        "quero",
        "gostaria",
        "procuro",
        "procurando",
        "comprar",
        "tem",
        "teria",
        "voces",
        "vocês",
        "me",
        "ver",
        "favor",
        "produto",
        "peca",
        "peça",
        "epi",
    }
    return [
        token
        for token in normalizar_texto_busca(valor).split()
        if token not in ignorar
    ]


def aliases_marca(marca: str | None) -> set[str]:
    marca_normalizada = normalizar_texto_busca(marca)
    tokens = set(tokens_busca(marca))

    mapa_aliases: dict[str, set[str]] = {
        "stihl": {"stihl", "st"},
        "husqvarna": {"husqvarna", "hq", "husq"},
        "toyama": {"toyama", "ty"},
        "nakashi": {"nakashi", "nk"},
        "tekna": {"tekna", "tk"},
        "echo": {"echo"},
        "makita": {"makita"},
        "kawashima": {"kawashima"},
        "briggs stratton": {
            "briggs",
            "stratton",
            "briggsstratton",
            "bs",
        },
    }

    if marca_normalizada in mapa_aliases:
        tokens.update(mapa_aliases[marca_normalizada])

    for nome, aliases in mapa_aliases.items():
        if marca_normalizada in aliases:
            tokens.update(aliases)
            tokens.update(tokens_busca(nome))

    return {alias for alias in tokens if alias}


def componentes_modelo(
    modelo: str | None,
) -> dict[str, set[str]]:
    modelo_normalizado = normalizar_texto_busca(modelo)
    prefixos: set[str] = set()
    numeros: set[str] = set()

    for segmento in re.findall(
        r"[a-z]+|\d+[a-z]*",
        modelo_normalizado.replace(" ", ""),
    ):
        if segmento.isalpha():
            prefixos.add(segmento)
            continue

        numero = re.search(r"\d+", segmento)
        if numero:
            numeros.add(numero.group(0))
        prefixos.update(re.findall(r"[a-z]+", segmento))

    for token in modelo_normalizado.split():
        if token.isalpha():
            prefixos.add(token)
        numeros.update(re.findall(r"\d+", token))

    return {
        "prefixos": prefixos,
        "numeros": numeros,
    }


def normalizar_preco(
    valor: Any,
) -> float | None:
    if valor in (None, ""):
        return None

    try:
        if isinstance(valor, str):
            valor_limpo = valor.strip()
            if "," in valor_limpo:
                valor_limpo = (
                    valor_limpo
                    .replace(".", "")
                    .replace(",", ".")
                )
            numero = float(valor_limpo)
        else:
            numero = float(valor)
    except (TypeError, ValueError):
        return None

    return round(numero, 2) if numero > 0 else None


def normalizar_quantidade(
    valor: Any,
) -> float | None:
    if valor in (None, ""):
        return None

    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def preco_efetivo_item_olist(
    item: dict[str, Any],
) -> float | None:
    precos = item.get("precos")
    if not isinstance(precos, dict):
        precos = {}

    promocional = normalizar_preco(
        precos.get("precoPromocional")
    )
    normal = normalizar_preco(
        precos.get("preco")
    )
    return promocional or normal


def aguardar_rate_limit_olist(
    limites: dict[str, str],
) -> None:
    try:
        restante = int(limites.get("restante") or 999)
        reset = int(limites.get("reset_segundos") or 0)
    except (TypeError, ValueError):
        return

    if restante <= 2 and reset > 0:
        time_module.sleep(min(reset + 1, 65))


def sincronizar_catalogo_olist(
    max_paginas: int,
) -> dict[str, Any]:
    inicio = time_module.perf_counter()
    lote = uuid4()
    itens_catalogo: dict[int, dict[str, Any]] = {}
    limites_olist: dict[str, str] = {}
    offset = 0
    limite_pagina = 100
    paginas = 0
    total_informado = 0

    with obter_conexao() as conexao:
        with conexao.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO comercial.olist_catalogo_sincronizacoes (
                    id,
                    status,
                    inicio_em
                )
                VALUES (%s, 'em_andamento', NOW());
                """,
                (lote,),
            )
        conexao.commit()

    try:
        while paginas < max_paginas:
            retorno, limites_olist = requisicao_get_olist(
                "produtos",
                {
                    "situacao": "A",
                    "limit": limite_pagina,
                    "offset": offset,
                },
            )
            paginas += 1

            pagina_itens = retorno.get("itens") or []
            paginacao = retorno.get("paginacao") or {}
            total_informado = int(
                paginacao.get("total") or total_informado
            )

            for item in pagina_itens:
                produto_id = item.get("id")
                descricao = str(
                    item.get("descricao") or ""
                ).strip()

                if produto_id is None or not descricao:
                    continue

                descricao_normalizada = normalizar_texto_busca(
                    descricao
                )
                sku = str(item.get("sku") or "").strip()
                tokens = sorted(
                    set(
                        tokens_busca(descricao)
                        + tokens_busca(sku)
                    )
                )
                precos = item.get("precos")
                if not isinstance(precos, dict):
                    precos = {}

                preco = normalizar_preco(
                    precos.get("preco")
                )
                preco_promocional = normalizar_preco(
                    precos.get("precoPromocional")
                )
                preco_efetivo = (
                    preco_promocional or preco
                )

                itens_catalogo[int(produto_id)] = {
                    "id_olist": int(produto_id),
                    "sku": sku or None,
                    "descricao": descricao,
                    "descricao_normalizada": (
                        descricao_normalizada
                    ),
                    "tokens": tokens,
                    "unidade": item.get("unidade"),
                    "gtin": item.get("gtin"),
                    "situacao": "A",
                    "preco": preco,
                    "preco_promocional": preco_promocional,
                    "preco_efetivo": preco_efetivo,
                    "preco_disponivel": (
                        preco_efetivo is not None
                    ),
                    "localizacao": (
                        item.get("estoque") or {}
                    ).get("localizacao"),
                    "data_criacao_olist": item.get(
                        "dataCriacao"
                    ),
                    "data_alteracao_olist": item.get(
                        "dataAlteracao"
                    ),
                    "dados": item,
                }

            offset += limite_pagina
            aguardar_rate_limit_olist(limites_olist)

            if (
                not pagina_itens
                or (
                    total_informado > 0
                    and offset >= total_informado
                )
            ):
                break

        sincronizacao_completa = bool(
            total_informado == 0
            or len(itens_catalogo) >= total_informado
        )

        with obter_conexao() as conexao:
            with conexao.cursor() as cursor:
                for item in itens_catalogo.values():
                    cursor.execute(
                        """
                        INSERT INTO comercial.olist_catalogo_produtos (
                            id_olist,
                            sku,
                            descricao,
                            descricao_normalizada,
                            tokens,
                            unidade,
                            gtin,
                            situacao,
                            preco,
                            preco_promocional,
                            preco_efetivo,
                            preco_disponivel,
                            localizacao,
                            data_criacao_olist,
                            data_alteracao_olist,
                            dados,
                            ativo,
                            lote_sincronizacao,
                            sincronizado_em
                        )
                        VALUES (
                            %(id_olist)s,
                            %(sku)s,
                            %(descricao)s,
                            %(descricao_normalizada)s,
                            %(tokens)s,
                            %(unidade)s,
                            %(gtin)s,
                            %(situacao)s,
                            %(preco)s,
                            %(preco_promocional)s,
                            %(preco_efetivo)s,
                            %(preco_disponivel)s,
                            %(localizacao)s,
                            %(data_criacao_olist)s,
                            %(data_alteracao_olist)s,
                            %(dados)s,
                            TRUE,
                            %(lote)s,
                            NOW()
                        )
                        ON CONFLICT (id_olist) DO UPDATE
                        SET
                            sku = EXCLUDED.sku,
                            descricao = EXCLUDED.descricao,
                            descricao_normalizada = (
                                EXCLUDED.descricao_normalizada
                            ),
                            tokens = EXCLUDED.tokens,
                            unidade = EXCLUDED.unidade,
                            gtin = EXCLUDED.gtin,
                            situacao = EXCLUDED.situacao,
                            preco = EXCLUDED.preco,
                            preco_promocional = (
                                EXCLUDED.preco_promocional
                            ),
                            preco_efetivo = EXCLUDED.preco_efetivo,
                            preco_disponivel = (
                                EXCLUDED.preco_disponivel
                            ),
                            localizacao = EXCLUDED.localizacao,
                            data_criacao_olist = (
                                EXCLUDED.data_criacao_olist
                            ),
                            data_alteracao_olist = (
                                EXCLUDED.data_alteracao_olist
                            ),
                            dados = EXCLUDED.dados,
                            ativo = TRUE,
                            lote_sincronizacao = (
                                EXCLUDED.lote_sincronizacao
                            ),
                            sincronizado_em = NOW();
                        """,
                        {
                            **item,
                            "dados": Jsonb(item["dados"]),
                            "lote": lote,
                        },
                    )

                if sincronizacao_completa:
                    cursor.execute(
                        """
                        UPDATE comercial.olist_catalogo_produtos
                        SET
                            ativo = FALSE,
                            sincronizado_em = NOW()
                        WHERE ativo = TRUE
                          AND lote_sincronizacao <> %s;
                        """,
                        (lote,),
                    )

                duracao_ms = int(
                    (
                        time_module.perf_counter() - inicio
                    ) * 1000
                )
                cursor.execute(
                    """
                    UPDATE comercial.olist_catalogo_sincronizacoes
                    SET
                        status = %s,
                        fim_em = NOW(),
                        paginas = %s,
                        total_informado_olist = %s,
                        total_recebido = %s,
                        total_gravado = %s,
                        duracao_ms = %s,
                        rate_limit = %s
                    WHERE id = %s;
                    """,
                    (
                        (
                            "concluida"
                            if sincronizacao_completa
                            else "parcial"
                        ),
                        paginas,
                        total_informado,
                        len(itens_catalogo),
                        len(itens_catalogo),
                        duracao_ms,
                        Jsonb(limites_olist),
                        lote,
                    ),
                )
            conexao.commit()

        return {
            "sincronizacao_id": lote,
            "status": (
                "concluida"
                if sincronizacao_completa
                else "parcial"
            ),
            "paginas": paginas,
            "total_informado_olist": total_informado,
            "total_recebido": len(itens_catalogo),
            "total_gravado": len(itens_catalogo),
            "duracao_ms": duracao_ms,
            "rate_limit": limites_olist,
        }

    except Exception as erro:
        with obter_conexao() as conexao:
            with conexao.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE comercial.olist_catalogo_sincronizacoes
                    SET
                        status = 'erro',
                        fim_em = NOW(),
                        paginas = %s,
                        total_informado_olist = %s,
                        total_recebido = %s,
                        erro = %s,
                        duracao_ms = %s,
                        rate_limit = %s
                    WHERE id = %s;
                    """,
                    (
                        paginas,
                        total_informado,
                        len(itens_catalogo),
                        str(erro)[:4000],
                        int(
                            (
                                time_module.perf_counter()
                                - inicio
                            ) * 1000
                        ),
                        Jsonb(limites_olist),
                        lote,
                    ),
                )
            conexao.commit()
        raise


MATERIAIS_BUSCA_BASE = {
    "malha",
    "latex",
    "raspa",
    "vaqueta",
    "nitrilica",
    "nitrilo",
    "nylon",
    "couro",
    "algodao",
    "pvc",
}

PREFIXOS_MODELO_NAO_OBRIGATORIOS = {
    "ms",
    "fs",
    "st",
    "hq",
}


def palavra_chave_contida(
    descricao_normalizada: str,
    palavra: str,
) -> bool:
    palavra_normalizada = normalizar_texto_busca(palavra)
    if not palavra_normalizada:
        return True

    if palavra_normalizada in descricao_normalizada:
        return True

    descricao_compacta = descricao_normalizada.replace(" ", "")
    palavra_compacta = palavra_normalizada.replace(" ", "")
    return bool(
        palavra_compacta
        and palavra_compacta in descricao_compacta
    )


def identificar_palavras_chave_busca(
    termo: str | None,
    produto: str | None,
    marca: str | None,
    modelo: str | None,
) -> dict[str, list[str]]:
    termo_tokens = tokens_busca(termo)
    produto_tokens = tokens_busca(produto)
    marca_tokens = set(tokens_busca(marca))
    marca_tokens.update(aliases_marca(marca))

    modelo_partes = componentes_modelo(modelo)
    numeros_modelo = sorted(modelo_partes["numeros"])
    prefixos_modelo = set(modelo_partes["prefixos"])

    obrigatorias: list[str] = []
    vistas: set[str] = set()

    def adicionar_obrigatoria(valor: str) -> None:
        chave = normalizar_texto_busca(valor)
        if chave and chave not in vistas:
            vistas.add(chave)
            obrigatorias.append(chave)

    for token in produto_tokens:
        adicionar_obrigatoria(token)

    for numero in numeros_modelo:
        adicionar_obrigatoria(numero)

    # Números, medidas e cilindradas digitados pelo cliente funcionam como
    # palavras-chave do Olist: 170, 160, 43cc, 35cm etc.
    for token in termo_tokens:
        if any(caractere.isdigit() for caractere in token):
            adicionar_obrigatoria(token)

    # Quando o produto é amplo, o material é uma palavra-base útil.
    # Ex.: "luva malha", "luva raspa", "luva latex".
    if len(produto_tokens) <= 1:
        for token in termo_tokens:
            if token in MATERIAIS_BUSCA_BASE:
                adicionar_obrigatoria(token)

    # Se o LLM não separou o produto, usa as duas primeiras palavras úteis
    # do pedido como busca-base.
    if not obrigatorias:
        for token in termo_tokens[:2]:
            adicionar_obrigatoria(token)

    representadas = set(obrigatorias)
    representadas.update(marca_tokens)
    representadas.update(prefixos_modelo)
    representadas.update(PREFIXOS_MODELO_NAO_OBRIGATORIOS)

    preferenciais: list[str] = []
    preferencias_vistas: set[str] = set()

    for token in termo_tokens:
        chave = normalizar_texto_busca(token)
        if (
            not chave
            or chave in representadas
            or chave in preferencias_vistas
        ):
            continue

        preferencias_vistas.add(chave)
        preferenciais.append(chave)

    return {
        "obrigatorias": obrigatorias,
        "preferenciais": preferenciais,
        "aliases_marca": sorted(marca_tokens),
    }


def avaliar_correspondencia_catalogo(
    item: dict[str, Any],
    termo: str | None,
    produto: str | None,
    marca: str | None,
    modelo: str | None,
) -> dict[str, Any]:
    descricao = normalizar_texto_busca(
        item.get("descricao")
    )
    tokens_descricao = set(descricao.split())

    palavras = identificar_palavras_chave_busca(
        termo,
        produto,
        marca,
        modelo,
    )
    obrigatorias = palavras["obrigatorias"]
    preferenciais = palavras["preferenciais"]
    aliases = palavras["aliases_marca"]

    obrigatorias_encontradas = [
        palavra
        for palavra in obrigatorias
        if palavra_chave_contida(descricao, palavra)
    ]
    base_corresponde = bool(
        obrigatorias
        and len(obrigatorias_encontradas)
        == len(obrigatorias)
    )

    preferenciais_encontradas = [
        palavra
        for palavra in preferenciais
        if palavra_chave_contida(descricao, palavra)
    ]

    aliases_encontrados = [
        alias
        for alias in aliases
        if alias in tokens_descricao
    ]
    marca_corresponde = bool(
        not aliases or aliases_encontrados
    )

    produto_normalizado = normalizar_texto_busca(produto)
    frase_produto_corresponde = bool(
        produto_normalizado
        and produto_normalizado in descricao
    )

    pontuacao_preferencias = len(
        preferenciais_encontradas
    )
    pontuacao_relevancia = (
        len(obrigatorias_encontradas) * 100
        + pontuacao_preferencias * 40
        + (30 if aliases_encontrados else 0)
        + (20 if frase_produto_corresponde else 0)
    )

    return {
        "pontuacao": pontuacao_relevancia,
        "pontuacao_relevancia": pontuacao_relevancia,
        "correspondencia_exata": base_corresponde,
        "correspondencia_palavras": base_corresponde,
        "produto_corresponde": base_corresponde,
        "marca_corresponde": marca_corresponde,
        "modelo_corresponde": True,
        "palavras_chave_obrigatorias": obrigatorias,
        "palavras_chave_encontradas": (
            obrigatorias_encontradas
        ),
        "palavras_preferenciais": preferenciais,
        "palavras_preferenciais_encontradas": (
            preferenciais_encontradas
        ),
        "quantidade_preferenciais": len(preferenciais),
        "quantidade_preferenciais_encontradas": (
            pontuacao_preferencias
        ),
        "marca_aliases_encontrados": aliases_encontrados,
        "frase_produto_corresponde": (
            frase_produto_corresponde
        ),
    }


def buscar_candidatos_catalogo(
    termo: str | None,
    produto: str | None,
    marca: str | None,
    modelo: str | None,
    limite_candidatos: int = 2000,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    palavras = identificar_palavras_chave_busca(
        termo,
        produto,
        marca,
        modelo,
    )
    obrigatorias = palavras["obrigatorias"]

    if not obrigatorias:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Não foi possível extrair palavras-chave "
                "para pesquisar o catálogo."
            ),
        )

    # A palavra mais longa reduz o volume inicial. As demais são validadas
    # por substring em Python, reproduzindo o modo "palavras-chave" do ERP.
    ancora = max(obrigatorias, key=len)

    with obter_conexao() as conexao:
        with conexao.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS total
                FROM comercial.olist_catalogo_produtos
                WHERE ativo = TRUE;
                """
            )
            total_catalogo = cursor.fetchone()["total"]

            cursor.execute(
                """
                SELECT
                    MAX(sincronizado_em) AS sincronizado_em
                FROM comercial.olist_catalogo_produtos
                WHERE ativo = TRUE;
                """
            )
            sincronizado_em = cursor.fetchone()[
                "sincronizado_em"
            ]

            if total_catalogo == 0:
                raise HTTPException(
                    status_code=http_status.HTTP_409_CONFLICT,
                    detail=(
                        "O catálogo local da Olist está vazio. "
                        "Execute POST /olist/catalogo/sincronizar."
                    ),
                )

            cursor.execute(
                """
                SELECT
                    id_olist AS id,
                    sku,
                    descricao,
                    descricao_normalizada,
                    tokens,
                    unidade,
                    gtin,
                    preco,
                    preco_promocional,
                    preco_efetivo,
                    preco_disponivel,
                    localizacao,
                    sincronizado_em
                FROM comercial.olist_catalogo_produtos
                WHERE ativo = TRUE
                  AND descricao_normalizada LIKE %s
                ORDER BY
                    preco_disponivel DESC,
                    descricao
                LIMIT %s;
                """,
                (
                    f"%{ancora}%",
                    limite_candidatos,
                ),
            )
            candidatos_iniciais = cursor.fetchall()

    candidatos = [
        item
        for item in candidatos_iniciais
        if all(
            palavra_chave_contida(
                item["descricao_normalizada"],
                palavra,
            )
            for palavra in obrigatorias
        )
    ]

    return candidatos, {
        "total_catalogo_ativo": total_catalogo,
        "catalogo_sincronizado_em": sincronizado_em,
        "modo_correspondencia": (
            "todas_as_palavras_base_como_substring"
        ),
        "palavra_ancora": ancora,
        "palavras_chave_obrigatorias": obrigatorias,
        "palavras_preferenciais": palavras[
            "preferenciais"
        ],
        "aliases_marca": palavras["aliases_marca"],
        "quantidade_candidatos_iniciais": len(
            candidatos_iniciais
        ),
        "quantidade_candidatos_filtrados": len(
            candidatos
        ),
    }


def enriquecer_estoque_catalogo(
    item: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    retorno, limites = requisicao_get_olist(
        f"estoque/{item['id']}",
    )
    estoque = (
        retorno.get("estoque")
        if isinstance(retorno, dict)
        and isinstance(retorno.get("estoque"), dict)
        else retorno
    )
    if not isinstance(estoque, dict):
        estoque = {}

    saldo = normalizar_quantidade(
        estoque.get("saldo")
    )
    reservado = normalizar_quantidade(
        estoque.get("reservado")
    )
    disponivel = normalizar_quantidade(
        estoque.get("disponivel")
    )

    preco = normalizar_preco(item.get("preco"))
    preco_promocional = normalizar_preco(
        item.get("preco_promocional")
    )
    preco_efetivo = (
        preco_promocional
        or normalizar_preco(item.get("preco_efetivo"))
        or preco
    )
    tem_preco = preco_efetivo is not None
    tem_estoque = bool(
        disponivel is not None and disponivel > 0
    )

    if tem_preco and tem_estoque:
        prioridade = 3
        situacao = "preco_e_estoque"
    elif tem_preco:
        prioridade = 2
        situacao = "somente_preco"
    elif tem_estoque:
        prioridade = 1
        situacao = "somente_estoque"
    else:
        prioridade = 0
        situacao = "sem_preco_e_sem_estoque"

    return (
        {
            **item,
            "preco": preco,
            "preco_promocional": preco_promocional,
            "preco_efetivo": preco_efetivo,
            "preco_disponivel": tem_preco,
            "tem_estoque": tem_estoque,
            "prioridade_comercial": prioridade,
            "situacao_comercial": situacao,
            "estoque": {
                "saldo": saldo,
                "reservado": reservado,
                "disponivel": disponivel,
                "localizacao": (
                    estoque.get("localizacao")
                    or item.get("localizacao")
                ),
                "status": (
                    "disponivel"
                    if tem_estoque
                    else (
                        "sem_estoque"
                        if disponivel is not None
                        else "nao_informado"
                    )
                ),
                "depositos": estoque.get("depositos") or [],
            },
        },
        limites,
    )


def pesquisar_produtos_olist(
    termo: str | None,
    produto: str | None,
    marca: str | None,
    modelo: str | None,
    limite: int,
) -> dict[str, Any]:
    inicio = time_module.perf_counter()
    candidatos, catalogo_info = buscar_candidatos_catalogo(
        termo,
        produto,
        marca,
        modelo,
    )

    avaliados: list[dict[str, Any]] = []

    for item in candidatos:
        correspondencia = avaliar_correspondencia_catalogo(
            item,
            termo,
            produto,
            marca,
            modelo,
        )
        if not correspondencia["correspondencia_palavras"]:
            continue

        avaliados.append(
            {
                **item,
                "pontuacao": correspondencia[
                    "pontuacao_relevancia"
                ],
                "correspondencia": correspondencia,
            }
        )

    if avaliados:
        melhor_chave = max(
            (
                item["correspondencia"][
                    "quantidade_preferenciais_encontradas"
                ],
                bool(
                    item["correspondencia"][
                        "marca_aliases_encontrados"
                    ]
                ),
                item["correspondencia"][
                    "pontuacao_relevancia"
                ],
            )
            for item in avaliados
        )
    else:
        melhor_chave = None

    for item in avaliados:
        chave_item = (
            item["correspondencia"][
                "quantidade_preferenciais_encontradas"
            ],
            bool(
                item["correspondencia"][
                    "marca_aliases_encontrados"
                ]
            ),
            item["correspondencia"][
                "pontuacao_relevancia"
            ],
        )
        item["melhor_correspondencia"] = bool(
            melhor_chave is not None
            and chave_item == melhor_chave
        )

    avaliados.sort(
        key=lambda item: (
            item["melhor_correspondencia"],
            item["correspondencia"][
                "quantidade_preferenciais_encontradas"
            ],
            bool(
                item["correspondencia"][
                    "marca_aliases_encontrados"
                ]
            ),
            item.get("preco_disponivel") or False,
            item["pontuacao"],
            normalizar_texto_busca(item["descricao"]),
        ),
        reverse=True,
    )

    # O estoque é consultado somente para os candidatos mais relevantes.
    quantidade_enriquecer = min(
        max(limite * 2, 8),
        12,
    )
    candidatos_estoque = avaliados[:quantidade_enriquecer]

    resultados_enriquecidos: list[dict[str, Any]] = []
    limites_olist: dict[str, str] = {}

    for item in candidatos_estoque:
        enriquecido, limites_olist = (
            enriquecer_estoque_catalogo(item)
        )
        resultados_enriquecidos.append(enriquecido)
        aguardar_rate_limit_olist(limites_olist)

    # Relevância vem antes da disponibilidade. Assim, uma luva preta não
    # substitui uma luva branca apenas porque existe em estoque.
    resultados_enriquecidos.sort(
        key=lambda item: (
            item["melhor_correspondencia"],
            item["correspondencia"][
                "quantidade_preferenciais_encontradas"
            ],
            bool(
                item["correspondencia"][
                    "marca_aliases_encontrados"
                ]
            ),
            item["prioridade_comercial"],
            item["pontuacao"],
            item["estoque"]["disponivel"] or 0,
            normalizar_texto_busca(item["descricao"]),
        ),
        reverse=True,
    )

    resultados = resultados_enriquecidos[:limite]

    if len(resultados) == 0:
        status_resultado = "nao_encontrado"
    elif len(resultados) == 1:
        status_resultado = "encontrado"
    else:
        status_resultado = "multiplos_resultados"

    quantidade_melhores = sum(
        1
        for item in avaliados
        if item["melhor_correspondencia"]
    )

    return {
        "status": status_resultado,
        "consulta": {
            "termo": termo,
            "produto": produto,
            "marca": marca,
            "modelo": modelo,
            "modo_busca": (
                "palavras_chave_olist_substring"
            ),
            "ordenacao": (
                "melhor_correspondencia, preço+estoque, relevância"
            ),
            **catalogo_info,
        },
        "quantidade_resultados": len(resultados),
        "quantidade_compativeis_localizados": len(avaliados),
        "quantidade_melhor_correspondencia": (
            quantidade_melhores
        ),
        "resultados": resultados,
        "rate_limit": limites_olist,
        "duracao_ms": int(
            (
                time_module.perf_counter() - inicio
            ) * 1000
        ),
    }


class ClienteCriar(BaseModel):
    crm_origem_id: str | None = Field(default=None, max_length=200)
    olist_id: str | None = Field(default=None, max_length=200)
    tipo_pessoa: Literal["CPF", "CNPJ"] | None = None
    cpf_cnpj: str | None = Field(default=None, max_length=20)
    razao_social: str | None = Field(default=None, max_length=200)
    nome_fantasia: str | None = Field(default=None, max_length=200)
    nome_contato: str | None = Field(default=None, max_length=150)
    telefone: str | None = Field(default=None, max_length=30)
    whatsapp: str | None = Field(default=None, max_length=30)
    email: str | None = Field(default=None, max_length=180)
    cidade: str | None = Field(default=None, max_length=120)
    uf: str | None = Field(default=None, min_length=2, max_length=2)
    vendedor_codigo: str | None = Field(default=None, max_length=40)
    status: str = Field(default="novo", max_length=40)
    origem: str = Field(default="crm_ligacoes", max_length=40)
    dados_adicionais: dict[str, Any] = Field(default_factory=dict)

    @field_validator("cpf_cnpj")
    @classmethod
    def validar_documento(cls, valor: str | None) -> str | None:
        return normalizar_documento(valor)

    @field_validator("uf")
    @classmethod
    def validar_uf(cls, valor: str | None) -> str | None:
        return valor.upper() if valor else None

    @field_validator("vendedor_codigo")
    @classmethod
    def validar_codigo_vendedor(cls, valor: str | None) -> str | None:
        return valor.upper() if valor else None

    @model_validator(mode="after")
    def validar_identificacao(self):
        if not any([self.razao_social, self.nome_fantasia, self.nome_contato]):
            raise ValueError(
                "Informe ao menos razão social, nome fantasia ou nome do contato."
            )
        return self


class ClienteAtualizar(BaseModel):
    crm_origem_id: str | None = Field(default=None, max_length=200)
    olist_id: str | None = Field(default=None, max_length=200)
    tipo_pessoa: Literal["CPF", "CNPJ"] | None = None
    cpf_cnpj: str | None = Field(default=None, max_length=20)
    razao_social: str | None = Field(default=None, max_length=200)
    nome_fantasia: str | None = Field(default=None, max_length=200)
    nome_contato: str | None = Field(default=None, max_length=150)
    telefone: str | None = Field(default=None, max_length=30)
    whatsapp: str | None = Field(default=None, max_length=30)
    email: str | None = Field(default=None, max_length=180)
    cidade: str | None = Field(default=None, max_length=120)
    uf: str | None = Field(default=None, min_length=2, max_length=2)
    vendedor_codigo: str | None = Field(default=None, max_length=40)
    status: str | None = Field(default=None, max_length=40)
    origem: str | None = Field(default=None, max_length=40)
    opt_out: bool | None = None
    bloqueado: bool | None = None
    proxima_acao_em: datetime | None = None
    dados_adicionais: dict[str, Any] | None = None

    @field_validator("cpf_cnpj")
    @classmethod
    def validar_documento(cls, valor: str | None) -> str | None:
        return normalizar_documento(valor)

    @field_validator("uf")
    @classmethod
    def validar_uf(cls, valor: str | None) -> str | None:
        return valor.upper() if valor else None

    @field_validator("vendedor_codigo")
    @classmethod
    def validar_codigo_vendedor(cls, valor: str | None) -> str | None:
        return valor.upper() if valor else None


class AgendaCriar(BaseModel):
    cliente_id: UUID
    vendedor_codigo: str = Field(max_length=40)
    data_agenda: date
    horario_previsto: time | None = None
    prioridade: int = Field(default=3, ge=1, le=5)
    objetivo: str | None = Field(default=None, max_length=255)
    canal_preferencial: Literal["telefone", "whatsapp", "email"] = "telefone"
    maximo_tentativas: int = Field(default=3, ge=1, le=10)
    observacao: str | None = None

    @field_validator("vendedor_codigo")
    @classmethod
    def validar_codigo_vendedor(cls, valor: str) -> str:
        return valor.upper()


class AgendaAtualizar(BaseModel):
    data_agenda: date | None = None
    horario_previsto: time | None = None
    prioridade: int | None = Field(default=None, ge=1, le=5)
    objetivo: str | None = Field(default=None, max_length=255)
    canal_preferencial: Literal["telefone", "whatsapp", "email"] | None = None
    status: Literal[
        "pendente",
        "em_execucao",
        "concluida",
        "reagendada",
        "cancelada",
        "sem_resposta",
    ] | None = None
    numero_tentativas: int | None = Field(default=None, ge=0, le=100)
    maximo_tentativas: int | None = Field(default=None, ge=1, le=10)
    ultima_tentativa_em: datetime | None = None
    proxima_tentativa_em: datetime | None = None
    resultado: str | None = Field(default=None, max_length=80)
    observacao: str | None = None


class AssumirProximaAgenda(BaseModel):
    vendedor_codigo: str = Field(max_length=40)
    data_agenda: date | None = None

    @field_validator("vendedor_codigo")
    @classmethod
    def validar_codigo_vendedor(cls, valor: str) -> str:
        return valor.upper()


class ChamadaIniciar(BaseModel):
    agenda_id: UUID
    provedor: str = Field(max_length=60)
    chamada_externa_id: str | None = Field(default=None, max_length=150)
    numero_origem: str | None = Field(default=None, max_length=30)
    numero_destino: str | None = Field(default=None, max_length=30)
    status: Literal["iniciada", "em_andamento"] = "iniciada"


class ChamadaFinalizar(BaseModel):
    status: Literal[
        "concluida",
        "nao_atendida",
        "ocupado",
        "falha",
        "cancelada",
    ]
    atendida: bool = False
    fim_em: datetime | None = None
    duracao_segundos: int | None = Field(default=None, ge=0)
    gravacao_url: str | None = None
    transcricao: str | None = None
    resumo: str | None = None
    sentimento: str | None = Field(default=None, max_length=40)
    intencao: str | None = Field(default=None, max_length=80)
    resultado: str | None = Field(default=None, max_length=80)
    custo_telefonia: float = Field(default=0, ge=0)
    custo_ia: float = Field(default=0, ge=0)
    dados_extraidos: dict[str, Any] = Field(default_factory=dict)
    agenda_status: Literal[
        "concluida",
        "reagendada",
        "cancelada",
        "sem_resposta",
    ]
    proxima_tentativa_em: datetime | None = None
    observacao_agenda: str | None = None
    cliente_status: str | None = Field(default=None, max_length=40)
    proxima_acao_em: datetime | None = None

    @model_validator(mode="after")
    def validar_reagendamento(self):
        if self.agenda_status == "reagendada" and self.proxima_tentativa_em is None:
            raise ValueError(
                "Informe proxima_tentativa_em quando a agenda for reagendada."
            )
        return self


class TurnoConversaVoz(BaseModel):
    numero: int = Field(ge=1, le=100)
    cliente: str = Field(min_length=1, max_length=5000)
    agente: str | None = Field(default=None, max_length=5000)


class ConversaVozRegistrar(BaseModel):
    cliente_id: UUID
    vendedor_codigo: str = Field(max_length=40)
    agenda_id: UUID | None = None
    provedor: str = Field(
        default="asterisk_audiosocket",
        max_length=60,
    )
    chamada_externa_id: str = Field(min_length=8, max_length=150)
    numero_origem: str | None = Field(default=None, max_length=30)
    numero_destino: str | None = Field(default=None, max_length=30)
    direcao: Literal["entrada", "saida"] = "saida"
    inicio_em: datetime
    fim_em: datetime
    duracao_segundos: int = Field(ge=0)
    resumo: str | None = Field(default=None, max_length=4000)
    sentimento: str | None = Field(default=None, max_length=40)
    intencao: str = Field(
        default="consulta_peca",
        max_length=80,
    )
    resultado: str = Field(max_length=80)
    levantamento_completo: bool = False
    motivo_encerramento: str | None = Field(
        default=None,
        max_length=200,
    )
    estado_comercial: dict[str, Any] = Field(default_factory=dict)
    turnos: list[TurnoConversaVoz] = Field(
        default_factory=list,
        max_length=20,
    )
    modelos: dict[str, Any] = Field(default_factory=dict)
    dados_extraidos: dict[str, Any] = Field(default_factory=dict)

    @field_validator("vendedor_codigo")
    @classmethod
    def validar_codigo_vendedor(cls, valor: str) -> str:
        return valor.upper()

    @model_validator(mode="after")
    def validar_periodo(self):
        if self.fim_em < self.inicio_em:
            raise ValueError(
                "fim_em não pode ser anterior a inicio_em."
            )
        return self


class PendenciaComercialAtualizar(BaseModel):
    status: Literal[
        "pendente",
        "em_analise",
        "aguardando_reposicao",
        "aguardando_catalogo",
        "resolvida",
        "cancelada",
    ] | None = None
    responsavel: str | None = Field(
        default=None,
        max_length=150,
    )
    previsao_retorno: datetime | None = None
    resolucao: str | None = Field(
        default=None,
        max_length=4000,
    )



class TwilioTesteChamada(BaseModel):
    numero_destino: str = Field(
        description="Número verificado na Twilio, no padrão E.164.",
        examples=["+5541999999999"],
    )
    timeout_segundos: int = Field(default=25, ge=10, le=60)

    @field_validator("numero_destino")
    @classmethod
    def validar_destino(cls, valor: str) -> str:
        return validar_numero_e164(valor)


class TwilioTesteInterativo(BaseModel):
    numero_destino: str = Field(
        description="Número verificado na Twilio, no padrão E.164.",
        examples=["+5541999999999"],
    )
    timeout_segundos: int = Field(default=25, ge=10, le=60)

    @field_validator("numero_destino")
    @classmethod
    def validar_destino(cls, valor: str) -> str:
        return validar_numero_e164(valor)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not DATABASE_URL:
        raise RuntimeError("A variável DATABASE_URL não foi configurada.")

    if not API_KEY:
        raise RuntimeError("A variável API_KEY não foi configurada.")

    variaveis_twilio = {
        "TWILIO_ACCOUNT_SID": TWILIO_ACCOUNT_SID,
        "TWILIO_AUTH_TOKEN": TWILIO_AUTH_TOKEN,
        "TWILIO_PHONE_NUMBER": TWILIO_PHONE_NUMBER,
        "TWILIO_BASE_URL": TWILIO_BASE_URL,
    }
    ausentes = [
        nome
        for nome, valor in variaveis_twilio.items()
        if not valor
    ]

    if ausentes:
        raise RuntimeError(
            "Variáveis Twilio não configuradas: " + ", ".join(ausentes)
        )

    if not TWILIO_ACCOUNT_SID.startswith("AC"):
        raise RuntimeError("TWILIO_ACCOUNT_SID inválido.")

    try:
        validar_numero_e164(TWILIO_PHONE_NUMBER)
    except ValueError as erro:
        raise RuntimeError("TWILIO_PHONE_NUMBER inválido.") from erro

    if not TWILIO_BASE_URL.startswith("https://"):
        raise RuntimeError("TWILIO_BASE_URL deve usar HTTPS.")

    with obter_conexao() as conexao:
        with conexao.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    current_database() AS banco,
                    to_regclass('comercial.vendedores_ia') AS tabela_vendedores,
                    to_regclass('comercial.clientes') AS tabela_clientes,
                    to_regclass('comercial.agendas_comerciais') AS tabela_agendas,
                    to_regclass('comercial.chamadas_ia') AS tabela_chamadas,
                    to_regclass('comercial.interacoes') AS tabela_interacoes,
                    to_regclass('comercial.acoes_agente') AS tabela_acoes,
                    to_regclass('comercial.configuracoes') AS tabela_configuracoes,
                    to_regclass('comercial.olist_oauth_tokens') AS tabela_olist_tokens,
                    to_regclass('comercial.olist_oauth_states') AS tabela_olist_states,
                    to_regclass('comercial.consultas_olist') AS tabela_consultas_olist,
                    to_regclass('comercial.olist_catalogo_produtos') AS tabela_catalogo_olist,
                    to_regclass('comercial.olist_catalogo_sincronizacoes') AS tabela_catalogo_sync,
                    to_regclass('comercial.pendencias_comerciais') AS tabela_pendencias_comerciais;
                """
            )
            resultado = cursor.fetchone()

            if resultado is None:
                raise RuntimeError(
                    "Não foi possível validar a estrutura do banco de dados."
                )

            tabelas = [
                resultado["tabela_vendedores"],
                resultado["tabela_clientes"],
                resultado["tabela_agendas"],
                resultado["tabela_chamadas"],
                resultado["tabela_interacoes"],
                resultado["tabela_acoes"],
                resultado["tabela_configuracoes"],
                resultado["tabela_olist_tokens"],
                resultado["tabela_olist_states"],
                resultado["tabela_consultas_olist"],
                resultado["tabela_catalogo_olist"],
                resultado["tabela_catalogo_sync"],
                resultado["tabela_pendencias_comerciais"],
            ]

            if any(tabela is None for tabela in tabelas):
                raise RuntimeError(
                    f"Estrutura comercial incompleta no banco {resultado['banco']}."
                )

    yield


app = FastAPI(
    title="RBK Vendedor IA API",
    description="API comercial do projeto piloto RBK Vendedor IA.",
    version="0.9.3",
    lifespan=lifespan,
)


@app.get("/saude", tags=["Sistema"])
def saude() -> dict[str, str]:
    return {
        "status": "ok",
        "servico": "api-comercial",
        "projeto": "RBK Vendedor IA",
        "versao": "0.9.3",
    }


@app.get(
    "/olist/status",
    tags=["Olist"],
    dependencies=[Depends(validar_api_key)],
)
def status_olist() -> dict[str, Any]:
    configuracao = {
        "api_base_url": OLIST_API_BASE_URL,
        "redirect_uri": OLIST_REDIRECT_URI or None,
        "client_id_configurado": bool(OLIST_CLIENT_ID),
        "client_secret_configurado": bool(OLIST_CLIENT_SECRET),
        "chave_criptografia_configurada": bool(
            OLIST_TOKEN_CRYPTO_KEY
        ),
        "id_lista_preco": (
            int(OLIST_ID_LISTA_PRECO)
            if OLIST_ID_LISTA_PRECO.isdigit()
            else None
        ),
    }

    token = None
    if OLIST_TOKEN_CRYPTO_KEY:
        with obter_conexao() as conexao:
            with conexao.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        token_type,
                        scope,
                        expira_em,
                        refresh_expira_em,
                        atualizado_em
                    FROM comercial.olist_oauth_tokens
                    WHERE id = 1;
                    """
                )
                token = cursor.fetchone()

    agora = datetime.now(timezone.utc)
    return {
        "configuracao": configuracao,
        "autorizado": token is not None,
        "access_token_valido": bool(
            token and token["expira_em"] > agora
        ),
        "refresh_token_valido": bool(
            token and token["refresh_expira_em"] > agora
        ),
        "token": token,
    }


@app.post(
    "/olist/oauth/iniciar",
    tags=["Olist"],
    dependencies=[Depends(validar_api_key)],
)
def iniciar_oauth_olist() -> dict[str, Any]:
    validar_configuracao_olist()

    state = uuid4()
    expira_em = datetime.now(timezone.utc) + timedelta(
        minutes=15
    )

    with obter_conexao() as conexao:
        with conexao.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM comercial.olist_oauth_states
                WHERE expira_em < NOW()
                   OR usado_em IS NOT NULL;
                """
            )
            cursor.execute(
                """
                INSERT INTO comercial.olist_oauth_states (
                    state,
                    expira_em
                )
                VALUES (%s, %s);
                """,
                (state, expira_em),
            )
        conexao.commit()

    parametros = urllib.parse.urlencode(
        {
            "client_id": OLIST_CLIENT_ID,
            "redirect_uri": OLIST_REDIRECT_URI,
            "scope": OLIST_SCOPE,
            "response_type": "code",
            "state": str(state),
        }
    )
    return {
        "authorization_url": f"{OLIST_AUTH_URL}?{parametros}",
        "state": state,
        "expira_em": expira_em,
    }


@app.get(
    "/olist/oauth/callback",
    tags=["Olist"],
)
def callback_oauth_olist(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
) -> Response:
    if error:
        return Response(
            content=(
                "<h1>Autorização Olist não concluída</h1>"
                f"<p>{error}: {error_description or ''}</p>"
            ),
            status_code=400,
            media_type="text/html",
        )

    if not code or not state:
        return Response(
            content=(
                "<h1>Parâmetros OAuth ausentes</h1>"
                "<p>Não foram recebidos code e state.</p>"
            ),
            status_code=400,
            media_type="text/html",
        )

    try:
        state_uuid = UUID(state)
    except ValueError:
        return Response(
            content="<h1>State OAuth inválido</h1>",
            status_code=400,
            media_type="text/html",
        )

    with obter_conexao() as conexao:
        with conexao.cursor() as cursor:
            cursor.execute(
                """
                SELECT state
                FROM comercial.olist_oauth_states
                WHERE state = %s
                  AND usado_em IS NULL
                  AND expira_em > NOW()
                FOR UPDATE;
                """,
                (state_uuid,),
            )
            registro_state = cursor.fetchone()

            if registro_state is None:
                return Response(
                    content=(
                        "<h1>Autorização expirada ou inválida</h1>"
                        "<p>Inicie novamente pela API Comercial.</p>"
                    ),
                    status_code=400,
                    media_type="text/html",
                )

            retorno = solicitar_token_olist(
                {
                    "grant_type": "authorization_code",
                    "client_id": OLIST_CLIENT_ID,
                    "client_secret": OLIST_CLIENT_SECRET,
                    "redirect_uri": OLIST_REDIRECT_URI,
                    "code": code,
                }
            )
            token_info = salvar_tokens_olist(
                cursor,
                retorno,
            )
            cursor.execute(
                """
                UPDATE comercial.olist_oauth_states
                SET usado_em = NOW()
                WHERE state = %s;
                """,
                (state_uuid,),
            )

        conexao.commit()

    return Response(
        content=(
            "<h1>Olist conectada com sucesso</h1>"
            "<p>Os tokens foram armazenados de forma criptografada "
            "no PostgreSQL.</p>"
            f"<p>Access token válido até: "
            f"{token_info['expira_em'].isoformat()}</p>"
            "<p>Você pode fechar esta página.</p>"
        ),
        media_type="text/html",
    )


@app.post(
    "/olist/catalogo/sincronizar",
    tags=["Olist"],
    dependencies=[Depends(validar_api_key)],
)
def sincronizar_catalogo(
    max_paginas: int = Query(
        default=200,
        ge=1,
        le=1000,
    ),
) -> dict[str, Any]:
    return sincronizar_catalogo_olist(max_paginas)


@app.get(
    "/olist/catalogo/status",
    tags=["Olist"],
    dependencies=[Depends(validar_api_key)],
)
def status_catalogo_olist() -> dict[str, Any]:
    with obter_conexao() as conexao:
        with conexao.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    COUNT(*) FILTER (
                        WHERE ativo = TRUE
                    ) AS produtos_ativos,
                    COUNT(*) FILTER (
                        WHERE ativo = TRUE
                          AND preco_disponivel = TRUE
                    ) AS produtos_com_preco,
                    MAX(sincronizado_em) FILTER (
                        WHERE ativo = TRUE
                    ) AS ultima_atualizacao
                FROM comercial.olist_catalogo_produtos;
                """
            )
            resumo = cursor.fetchone()

            cursor.execute(
                """
                SELECT
                    id,
                    status,
                    inicio_em,
                    fim_em,
                    paginas,
                    total_informado_olist,
                    total_recebido,
                    total_gravado,
                    duracao_ms,
                    erro,
                    rate_limit
                FROM comercial.olist_catalogo_sincronizacoes
                ORDER BY inicio_em DESC
                LIMIT 1;
                """
            )
            ultima = cursor.fetchone()

    return {
        **resumo,
        "ultima_sincronizacao": ultima,
    }


@app.get(
    "/olist/catalogo/produto/{codigo}",
    tags=["Olist"],
    dependencies=[Depends(validar_api_key)],
)
def obter_produto_catalogo_por_codigo(
    codigo: str,
) -> dict[str, Any]:
    codigo_limpo = codigo.strip()

    with obter_conexao() as conexao:
        with conexao.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    id_olist AS id,
                    sku,
                    descricao,
                    unidade,
                    gtin,
                    preco,
                    preco_promocional,
                    preco_efetivo,
                    preco_disponivel,
                    localizacao,
                    ativo,
                    sincronizado_em
                FROM comercial.olist_catalogo_produtos
                WHERE sku = %s
                   OR id_olist::text = %s
                ORDER BY ativo DESC
                LIMIT 1;
                """,
                (codigo_limpo, codigo_limpo),
            )
            produto_catalogo = cursor.fetchone()

    if produto_catalogo is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Produto não localizado no catálogo sincronizado.",
        )

    return produto_catalogo


@app.get(
    "/olist/produtos/pesquisar",
    tags=["Olist"],
    dependencies=[Depends(validar_api_key)],
)
def pesquisar_produtos(
    termo: str | None = Query(
        default=None,
        min_length=2,
        max_length=200,
    ),
    produto: str | None = Query(
        default=None,
        min_length=2,
        max_length=120,
    ),
    marca: str | None = Query(
        default=None,
        min_length=1,
        max_length=80,
    ),
    modelo: str | None = Query(
        default=None,
        min_length=1,
        max_length=80,
    ),
    limite: int = Query(default=5, ge=1, le=10),
) -> dict[str, Any]:
    resultado = pesquisar_produtos_olist(
        termo=termo,
        produto=produto,
        marca=marca,
        modelo=modelo,
        limite=limite,
    )

    with obter_conexao() as conexao:
        with conexao.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO comercial.consultas_olist (
                    termo,
                    produto,
                    marca,
                    modelo,
                    status,
                    quantidade_resultados,
                    duracao_ms,
                    resposta
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s
                )
                RETURNING id;
                """,
                (
                    termo,
                    produto,
                    marca,
                    modelo,
                    resultado["status"],
                    resultado["quantidade_resultados"],
                    resultado["duracao_ms"],
                    Jsonb(jsonable_encoder(resultado)),
                ),
            )
            consulta_id = cursor.fetchone()["id"]
        conexao.commit()

    return {
        "consulta_id": consulta_id,
        **resultado,
    }


@app.get(
    "/vendedores",
    tags=["Vendedores"],
    dependencies=[Depends(validar_api_key)],
)
def listar_vendedores() -> list[dict[str, Any]]:
    consulta = """
        SELECT
            v.id,
            v.nome,
            v.nome_exibicao,
            v.codigo,
            v.ativo,
            v.uf_principal,
            v.meta_contatos_dia,
            v.tipo_telefonia,
            v.horario_inicio,
            v.horario_fim,
            v.timezone,
            COALESCE(
                jsonb_agg(
                    DISTINCT jsonb_build_object(
                        'uf', t.uf,
                        'ativo', t.ativo,
                        'cidades', t.cidades
                    )
                ) FILTER (WHERE t.id IS NOT NULL),
                '[]'::jsonb
            ) AS territorios
        FROM comercial.vendedores_ia v
        LEFT JOIN comercial.territorios_vendedor t
            ON t.vendedor_id = v.id
        GROUP BY v.id
        ORDER BY v.nome;
    """

    with obter_conexao() as conexao:
        with conexao.cursor() as cursor:
            cursor.execute(consulta)
            return cursor.fetchall()


@app.get(
    "/vendedores/{codigo}",
    tags=["Vendedores"],
    dependencies=[Depends(validar_api_key)],
)
def buscar_vendedor(codigo: str) -> dict[str, Any]:
    consulta = """
        SELECT
            v.id,
            v.nome,
            v.nome_exibicao,
            v.codigo,
            v.ativo,
            v.uf_principal,
            v.meta_contatos_dia,
            v.limite_chamadas_simultaneas,
            v.tipo_telefonia,
            v.horario_inicio,
            v.horario_fim,
            v.timezone,
            COALESCE(
                jsonb_agg(
                    DISTINCT jsonb_build_object(
                        'uf', t.uf,
                        'ativo', t.ativo,
                        'cidades', t.cidades
                    )
                ) FILTER (WHERE t.id IS NOT NULL),
                '[]'::jsonb
            ) AS territorios
        FROM comercial.vendedores_ia v
        LEFT JOIN comercial.territorios_vendedor t
            ON t.vendedor_id = v.id
        WHERE v.codigo = %s
        GROUP BY v.id;
    """

    with obter_conexao() as conexao:
        with conexao.cursor() as cursor:
            cursor.execute(consulta, (codigo.upper(),))
            vendedor = cursor.fetchone()

    if vendedor is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Vendedor não encontrado.",
        )

    return vendedor


@app.get(
    "/configuracoes/{chave}",
    tags=["Configurações"],
    dependencies=[Depends(validar_api_key)],
)
def buscar_configuracao(chave: str) -> dict[str, Any]:
    consulta = """
        SELECT chave, valor, descricao, atualizado_em
        FROM comercial.configuracoes
        WHERE chave = %s;
    """

    with obter_conexao() as conexao:
        with conexao.cursor() as cursor:
            cursor.execute(consulta, (chave,))
            configuracao = cursor.fetchone()

    if configuracao is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Configuração não encontrada.",
        )

    return configuracao


@app.post(
    "/clientes",
    tags=["Clientes"],
    status_code=http_status.HTTP_201_CREATED,
    dependencies=[Depends(validar_api_key)],
)
def criar_cliente(dados: ClienteCriar) -> dict[str, Any]:
    vendedor_id = None

    try:
        with obter_conexao() as conexao:
            with conexao.cursor() as cursor:
                if dados.vendedor_codigo:
                    vendedor = obter_vendedor_por_codigo(
                        cursor,
                        dados.vendedor_codigo,
                    )
                    vendedor_id = vendedor["id"]

                cursor.execute(
                    """
                    INSERT INTO comercial.clientes (
                        crm_origem_id,
                        olist_id,
                        tipo_pessoa,
                        cpf_cnpj,
                        razao_social,
                        nome_fantasia,
                        nome_contato,
                        telefone,
                        whatsapp,
                        email,
                        cidade,
                        uf,
                        vendedor_id,
                        status,
                        origem,
                        dados_adicionais
                    )
                    VALUES (
                        %(crm_origem_id)s,
                        %(olist_id)s,
                        %(tipo_pessoa)s,
                        %(cpf_cnpj)s,
                        %(razao_social)s,
                        %(nome_fantasia)s,
                        %(nome_contato)s,
                        %(telefone)s,
                        %(whatsapp)s,
                        %(email)s,
                        %(cidade)s,
                        %(uf)s,
                        %(vendedor_id)s,
                        %(status)s,
                        %(origem)s,
                        %(dados_adicionais)s
                    )
                    RETURNING id;
                    """,
                    {
                        **dados.model_dump(exclude={"vendedor_codigo", "dados_adicionais"}),
                        "vendedor_id": vendedor_id,
                        "dados_adicionais": Jsonb(dados.dados_adicionais),
                    },
                )
                cliente_id = cursor.fetchone()["id"]
                cliente = obter_cliente_por_id(cursor, cliente_id)

            conexao.commit()
            return cliente

    except UniqueViolation as erro:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Já existe um cliente com este CPF/CNPJ.",
        ) from erro


@app.get(
    "/clientes",
    tags=["Clientes"],
    dependencies=[Depends(validar_api_key)],
)
def listar_clientes(
    uf: str | None = Query(default=None, min_length=2, max_length=2),
    status_cliente: str | None = Query(default=None, alias="status"),
    vendedor_codigo: str | None = Query(default=None),
    limite: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    filtros = []
    parametros: list[Any] = []

    if uf:
        filtros.append("c.uf = %s")
        parametros.append(uf.upper())

    if status_cliente:
        filtros.append("c.status = %s")
        parametros.append(status_cliente)

    if vendedor_codigo:
        filtros.append("v.codigo = %s")
        parametros.append(vendedor_codigo.upper())

    where_sql = f"WHERE {' AND '.join(filtros)}" if filtros else ""

    consulta_total = f"""
        SELECT COUNT(*) AS total
        FROM comercial.clientes c
        LEFT JOIN comercial.vendedores_ia v
            ON v.id = c.vendedor_id
        {where_sql};
    """

    consulta_itens = f"""
        SELECT
            c.*,
            v.codigo AS vendedor_codigo,
            v.nome_exibicao AS vendedor_nome
        FROM comercial.clientes c
        LEFT JOIN comercial.vendedores_ia v
            ON v.id = c.vendedor_id
        {where_sql}
        ORDER BY c.criado_em DESC
        LIMIT %s OFFSET %s;
    """

    with obter_conexao() as conexao:
        with conexao.cursor() as cursor:
            cursor.execute(consulta_total, parametros)
            total = cursor.fetchone()["total"]

            cursor.execute(
                consulta_itens,
                [*parametros, limite, offset],
            )
            itens = cursor.fetchall()

    return {
        "total": total,
        "limite": limite,
        "offset": offset,
        "itens": itens,
    }


@app.get(
    "/clientes/{cliente_id}",
    tags=["Clientes"],
    dependencies=[Depends(validar_api_key)],
)
def buscar_cliente(cliente_id: UUID) -> dict[str, Any]:
    with obter_conexao() as conexao:
        with conexao.cursor() as cursor:
            return obter_cliente_por_id(cursor, cliente_id)


@app.patch(
    "/clientes/{cliente_id}",
    tags=["Clientes"],
    dependencies=[Depends(validar_api_key)],
)
def atualizar_cliente(
    cliente_id: UUID,
    dados: ClienteAtualizar,
) -> dict[str, Any]:
    campos = dados.model_dump(exclude_unset=True)
    vendedor_codigo_informado = "vendedor_codigo" in campos
    vendedor_codigo = campos.pop("vendedor_codigo", None)

    if "dados_adicionais" in campos and campos["dados_adicionais"] is not None:
        campos["dados_adicionais"] = Jsonb(campos["dados_adicionais"])

    if not campos and not vendedor_codigo_informado:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="Nenhum campo foi informado para atualização.",
        )

    try:
        with obter_conexao() as conexao:
            with conexao.cursor() as cursor:
                obter_cliente_por_id(cursor, cliente_id)

                if vendedor_codigo_informado:
                    if vendedor_codigo is None:
                        campos["vendedor_id"] = None
                    else:
                        vendedor = obter_vendedor_por_codigo(
                            cursor,
                            vendedor_codigo,
                        )
                        campos["vendedor_id"] = vendedor["id"]

                atribuicoes = [
                    f"{campo} = %s"
                    for campo in campos.keys()
                ]
                valores = list(campos.values())

                atribuicoes.append("atualizado_em = NOW()")

                cursor.execute(
                    f"""
                    UPDATE comercial.clientes
                    SET {", ".join(atribuicoes)}
                    WHERE id = %s;
                    """,
                    [*valores, cliente_id],
                )

                cliente = obter_cliente_por_id(cursor, cliente_id)

            conexao.commit()
            return cliente

    except UniqueViolation as erro:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Já existe um cliente com este CPF/CNPJ.",
        ) from erro


@app.post(
    "/agendas",
    tags=["Agenda"],
    status_code=http_status.HTTP_201_CREATED,
    dependencies=[Depends(validar_api_key)],
)
def criar_agenda(dados: AgendaCriar) -> dict[str, Any]:
    with obter_conexao() as conexao:
        with conexao.cursor() as cursor:
            cliente = obter_cliente_por_id(cursor, dados.cliente_id)
            vendedor = obter_vendedor_por_codigo(
                cursor,
                dados.vendedor_codigo,
            )

            if cliente["bloqueado"] or cliente["opt_out"]:
                raise HTTPException(
                    status_code=http_status.HTTP_409_CONFLICT,
                    detail="O cliente está bloqueado ou solicitou opt-out.",
                )

            cursor.execute(
                """
                INSERT INTO comercial.agendas_comerciais (
                    cliente_id,
                    vendedor_id,
                    data_agenda,
                    horario_previsto,
                    prioridade,
                    objetivo,
                    canal_preferencial,
                    maximo_tentativas,
                    observacao
                )
                VALUES (
                    %(cliente_id)s,
                    %(vendedor_id)s,
                    %(data_agenda)s,
                    %(horario_previsto)s,
                    %(prioridade)s,
                    %(objetivo)s,
                    %(canal_preferencial)s,
                    %(maximo_tentativas)s,
                    %(observacao)s
                )
                RETURNING id;
                """,
                {
                    **dados.model_dump(exclude={"vendedor_codigo"}),
                    "vendedor_id": vendedor["id"],
                },
            )
            agenda_id = cursor.fetchone()["id"]

            cursor.execute(
                """
                SELECT
                    a.*,
                    c.razao_social,
                    c.nome_fantasia,
                    c.nome_contato,
                    c.telefone,
                    c.whatsapp,
                    c.cidade,
                    c.uf,
                    v.codigo AS vendedor_codigo,
                    v.nome_exibicao AS vendedor_nome
                FROM comercial.agendas_comerciais a
                JOIN comercial.clientes c
                    ON c.id = a.cliente_id
                JOIN comercial.vendedores_ia v
                    ON v.id = a.vendedor_id
                WHERE a.id = %s;
                """,
                (agenda_id,),
            )
            agenda = cursor.fetchone()

        conexao.commit()
        return agenda


@app.get(
    "/agendas/proxima",
    tags=["Agenda"],
    dependencies=[Depends(validar_api_key)],
)
def buscar_proxima_agenda(
    vendedor_codigo: str,
    data_agenda: date | None = None,
) -> dict[str, Any]:
    data_consulta = data_agenda or datetime.now(FUSO_PROJETO).date()

    with obter_conexao() as conexao:
        with conexao.cursor() as cursor:
            vendedor = obter_vendedor_por_codigo(
                cursor,
                vendedor_codigo,
            )

            cursor.execute(
                """
                SELECT
                    a.*,
                    c.razao_social,
                    c.nome_fantasia,
                    c.nome_contato,
                    c.telefone,
                    c.whatsapp,
                    c.cidade,
                    c.uf,
                    c.cpf_cnpj,
                    c.dados_adicionais,
                    v.codigo AS vendedor_codigo,
                    v.nome_exibicao AS vendedor_nome
                FROM comercial.agendas_comerciais a
                JOIN comercial.clientes c
                    ON c.id = a.cliente_id
                JOIN comercial.vendedores_ia v
                    ON v.id = a.vendedor_id
                WHERE a.vendedor_id = %s
                  AND a.data_agenda = %s
                  AND a.status = 'pendente'
                  AND c.bloqueado = FALSE
                  AND c.opt_out = FALSE
                ORDER BY
                    a.prioridade ASC,
                    a.horario_previsto NULLS LAST,
                    a.criado_em ASC
                LIMIT 1;
                """,
                (vendedor["id"], data_consulta),
            )
            agenda = cursor.fetchone()

    if agenda is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Nenhuma agenda pendente encontrada para o vendedor e data informados.",
        )

    return agenda


@app.get(
    "/agendas",
    tags=["Agenda"],
    dependencies=[Depends(validar_api_key)],
)
def listar_agendas(
    data_agenda: date | None = None,
    vendedor_codigo: str | None = None,
    status_agenda: str | None = Query(default=None, alias="status"),
    limite: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    if status_agenda and status_agenda not in STATUS_AGENDA:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Status inválido. Use um destes: {', '.join(sorted(STATUS_AGENDA))}.",
        )

    filtros = []
    parametros: list[Any] = []

    if data_agenda:
        filtros.append("a.data_agenda = %s")
        parametros.append(data_agenda)

    if vendedor_codigo:
        filtros.append("v.codigo = %s")
        parametros.append(vendedor_codigo.upper())

    if status_agenda:
        filtros.append("a.status = %s")
        parametros.append(status_agenda)

    where_sql = f"WHERE {' AND '.join(filtros)}" if filtros else ""

    consulta_total = f"""
        SELECT COUNT(*) AS total
        FROM comercial.agendas_comerciais a
        JOIN comercial.vendedores_ia v
            ON v.id = a.vendedor_id
        {where_sql};
    """

    consulta_itens = f"""
        SELECT
            a.*,
            c.razao_social,
            c.nome_fantasia,
            c.nome_contato,
            c.telefone,
            c.whatsapp,
            c.cidade,
            c.uf,
            v.codigo AS vendedor_codigo,
            v.nome_exibicao AS vendedor_nome
        FROM comercial.agendas_comerciais a
        JOIN comercial.clientes c
            ON c.id = a.cliente_id
        JOIN comercial.vendedores_ia v
            ON v.id = a.vendedor_id
        {where_sql}
        ORDER BY
            a.data_agenda ASC,
            a.prioridade ASC,
            a.horario_previsto NULLS LAST,
            a.criado_em ASC
        LIMIT %s OFFSET %s;
    """

    with obter_conexao() as conexao:
        with conexao.cursor() as cursor:
            cursor.execute(consulta_total, parametros)
            total = cursor.fetchone()["total"]

            cursor.execute(
                consulta_itens,
                [*parametros, limite, offset],
            )
            itens = cursor.fetchall()

    return {
        "total": total,
        "limite": limite,
        "offset": offset,
        "itens": itens,
    }


@app.patch(
    "/agendas/{agenda_id}",
    tags=["Agenda"],
    dependencies=[Depends(validar_api_key)],
)
def atualizar_agenda(
    agenda_id: UUID,
    dados: AgendaAtualizar,
) -> dict[str, Any]:
    campos = dados.model_dump(exclude_unset=True)

    if not campos:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="Nenhum campo foi informado para atualização.",
        )

    with obter_conexao() as conexao:
        with conexao.cursor() as cursor:
            cursor.execute(
                """
                SELECT id
                FROM comercial.agendas_comerciais
                WHERE id = %s;
                """,
                (agenda_id,),
            )
            if cursor.fetchone() is None:
                raise HTTPException(
                    status_code=http_status.HTTP_404_NOT_FOUND,
                    detail="Agenda não encontrada.",
                )

            atribuicoes = [
                f"{campo} = %s"
                for campo in campos.keys()
            ]
            valores = list(campos.values())
            atribuicoes.append("atualizado_em = NOW()")

            cursor.execute(
                f"""
                UPDATE comercial.agendas_comerciais
                SET {", ".join(atribuicoes)}
                WHERE id = %s;
                """,
                [*valores, agenda_id],
            )

            cursor.execute(
                """
                SELECT
                    a.*,
                    c.razao_social,
                    c.nome_fantasia,
                    c.nome_contato,
                    c.telefone,
                    c.whatsapp,
                    c.cidade,
                    c.uf,
                    v.codigo AS vendedor_codigo,
                    v.nome_exibicao AS vendedor_nome
                FROM comercial.agendas_comerciais a
                JOIN comercial.clientes c
                    ON c.id = a.cliente_id
                JOIN comercial.vendedores_ia v
                    ON v.id = a.vendedor_id
                WHERE a.id = %s;
                """,
                (agenda_id,),
            )
            agenda = cursor.fetchone()

        conexao.commit()
        return agenda


@app.post(
    "/agendas/assumir-proxima",
    tags=["Agenda"],
    dependencies=[Depends(validar_api_key)],
)
def assumir_proxima_agenda(
    dados: AssumirProximaAgenda,
) -> dict[str, Any]:
    data_consulta = dados.data_agenda or datetime.now(FUSO_PROJETO).date()

    with obter_conexao() as conexao:
        with conexao.cursor() as cursor:
            vendedor = obter_vendedor_por_codigo(
                cursor,
                dados.vendedor_codigo,
            )

            cursor.execute(
                """
                SELECT a.id
                FROM comercial.agendas_comerciais a
                JOIN comercial.clientes c
                    ON c.id = a.cliente_id
                WHERE a.vendedor_id = %s
                  AND a.data_agenda = %s
                  AND a.status = 'pendente'
                  AND a.numero_tentativas < a.maximo_tentativas
                  AND c.bloqueado = FALSE
                  AND c.opt_out = FALSE
                ORDER BY
                    a.prioridade ASC,
                    a.horario_previsto NULLS LAST,
                    a.criado_em ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1;
                """,
                (vendedor["id"], data_consulta),
            )
            selecionada = cursor.fetchone()

            if selecionada is None:
                raise HTTPException(
                    status_code=http_status.HTTP_404_NOT_FOUND,
                    detail=(
                        "Nenhuma agenda pendente disponível para o vendedor "
                        "e data informados."
                    ),
                )

            agenda_id = selecionada["id"]

            cursor.execute(
                """
                UPDATE comercial.agendas_comerciais
                SET
                    status = 'em_execucao',
                    numero_tentativas = numero_tentativas + 1,
                    ultima_tentativa_em = NOW(),
                    atualizado_em = NOW()
                WHERE id = %s;
                """,
                (agenda_id,),
            )

            cursor.execute(
                """
                INSERT INTO comercial.acoes_agente (
                    vendedor_id,
                    cliente_id,
                    tipo_acao,
                    origem,
                    entrada,
                    saida,
                    sucesso
                )
                SELECT
                    a.vendedor_id,
                    a.cliente_id,
                    'assumir_agenda',
                    'api_comercial',
                    %s,
                    %s,
                    TRUE
                FROM comercial.agendas_comerciais a
                WHERE a.id = %s;
                """,
                (
                    Jsonb(
                        {
                            "vendedor_codigo": dados.vendedor_codigo,
                            "data_agenda": data_consulta.isoformat(),
                        }
                    ),
                    Jsonb(
                        {
                            "agenda_id": str(agenda_id),
                            "status": "em_execucao",
                        }
                    ),
                    agenda_id,
                ),
            )

            agenda = obter_agenda_detalhada(cursor, agenda_id)

        conexao.commit()
        return agenda


@app.post(
    "/agendas/{agenda_id}/liberar",
    tags=["Agenda"],
    dependencies=[Depends(validar_api_key)],
)
def liberar_agenda(
    agenda_id: UUID,
) -> dict[str, Any]:
    with obter_conexao() as conexao:
        with conexao.cursor() as cursor:
            agenda = obter_agenda_detalhada(cursor, agenda_id)

            if agenda["status"] != "em_execucao":
                raise HTTPException(
                    status_code=http_status.HTTP_409_CONFLICT,
                    detail="Somente agendas em execução podem ser liberadas.",
                )

            cursor.execute(
                """
                UPDATE comercial.agendas_comerciais
                SET
                    status = 'pendente',
                    atualizado_em = NOW()
                WHERE id = %s;
                """,
                (agenda_id,),
            )

            cursor.execute(
                """
                INSERT INTO comercial.acoes_agente (
                    vendedor_id,
                    cliente_id,
                    tipo_acao,
                    origem,
                    entrada,
                    saida,
                    sucesso
                )
                VALUES (
                    %s,
                    %s,
                    'liberar_agenda',
                    'api_comercial',
                    %s,
                    %s,
                    TRUE
                );
                """,
                (
                    agenda["vendedor_id"],
                    agenda["cliente_id"],
                    Jsonb({"agenda_id": str(agenda_id)}),
                    Jsonb({"status": "pendente"}),
                ),
            )

            agenda_atualizada = obter_agenda_detalhada(cursor, agenda_id)

        conexao.commit()
        return agenda_atualizada


@app.post(
    "/chamadas",
    tags=["Chamadas"],
    status_code=http_status.HTTP_201_CREATED,
    dependencies=[Depends(validar_api_key)],
)
def iniciar_chamada(
    dados: ChamadaIniciar,
) -> dict[str, Any]:
    with obter_conexao() as conexao:
        with conexao.cursor() as cursor:
            agenda = obter_agenda_detalhada(cursor, dados.agenda_id)

            if agenda["status"] != "em_execucao":
                raise HTTPException(
                    status_code=http_status.HTTP_409_CONFLICT,
                    detail=(
                        "A agenda precisa estar em execução antes de iniciar "
                        "a chamada."
                    ),
                )

            numero_destino = dados.numero_destino or agenda["telefone"]

            if not numero_destino:
                raise HTTPException(
                    status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="O cliente não possui telefone para a chamada.",
                )

            cursor.execute(
                """
                INSERT INTO comercial.chamadas_ia (
                    agenda_id,
                    cliente_id,
                    vendedor_id,
                    provedor,
                    chamada_externa_id,
                    numero_origem,
                    numero_destino,
                    direcao,
                    status,
                    inicio_em
                )
                VALUES (
                    %(agenda_id)s,
                    %(cliente_id)s,
                    %(vendedor_id)s,
                    %(provedor)s,
                    %(chamada_externa_id)s,
                    %(numero_origem)s,
                    %(numero_destino)s,
                    'saida',
                    %(status)s,
                    NOW()
                )
                RETURNING id;
                """,
                {
                    "agenda_id": dados.agenda_id,
                    "cliente_id": agenda["cliente_id"],
                    "vendedor_id": agenda["vendedor_id"],
                    "provedor": dados.provedor,
                    "chamada_externa_id": dados.chamada_externa_id,
                    "numero_origem": dados.numero_origem,
                    "numero_destino": numero_destino,
                    "status": dados.status,
                },
            )
            chamada_id = cursor.fetchone()["id"]

            cursor.execute(
                """
                INSERT INTO comercial.acoes_agente (
                    vendedor_id,
                    cliente_id,
                    tipo_acao,
                    origem,
                    entrada,
                    saida,
                    sucesso
                )
                VALUES (
                    %s,
                    %s,
                    'iniciar_chamada',
                    'api_comercial',
                    %s,
                    %s,
                    TRUE
                );
                """,
                (
                    agenda["vendedor_id"],
                    agenda["cliente_id"],
                    Jsonb(
                        {
                            "agenda_id": str(dados.agenda_id),
                            "provedor": dados.provedor,
                            "numero_destino": numero_destino,
                        }
                    ),
                    Jsonb({"chamada_id": str(chamada_id)}),
                ),
            )

            chamada = obter_chamada_detalhada(cursor, chamada_id)

        conexao.commit()
        return chamada


@app.get(
    "/chamadas",
    tags=["Chamadas"],
    dependencies=[Depends(validar_api_key)],
)
def listar_chamadas(
    vendedor_codigo: str | None = None,
    cliente_id: UUID | None = None,
    status_chamada: str | None = Query(default=None, alias="status"),
    data_inicio: date | None = None,
    data_fim: date | None = None,
    limite: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    if status_chamada and status_chamada not in STATUS_CHAMADA:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Status inválido. Use um destes: {', '.join(sorted(STATUS_CHAMADA))}.",
        )

    filtros: list[str] = []
    parametros: list[Any] = []

    if vendedor_codigo:
        filtros.append("v.codigo = %s")
        parametros.append(vendedor_codigo.upper())

    if cliente_id:
        filtros.append("ch.cliente_id = %s")
        parametros.append(cliente_id)

    if status_chamada:
        filtros.append("ch.status = %s")
        parametros.append(status_chamada)

    if data_inicio:
        filtros.append("ch.criado_em::date >= %s")
        parametros.append(data_inicio)

    if data_fim:
        filtros.append("ch.criado_em::date <= %s")
        parametros.append(data_fim)

    where_sql = f"WHERE {' AND '.join(filtros)}" if filtros else ""

    consulta_total = f"""
        SELECT COUNT(*) AS total
        FROM comercial.chamadas_ia ch
        JOIN comercial.vendedores_ia v
            ON v.id = ch.vendedor_id
        {where_sql};
    """

    consulta_itens = f"""
        SELECT
            ch.*,
            a.data_agenda,
            a.status AS agenda_status,
            c.razao_social,
            c.nome_fantasia,
            c.nome_contato,
            c.telefone,
            c.cidade,
            c.uf,
            v.codigo AS vendedor_codigo,
            v.nome_exibicao AS vendedor_nome
        FROM comercial.chamadas_ia ch
        LEFT JOIN comercial.agendas_comerciais a
            ON a.id = ch.agenda_id
        JOIN comercial.clientes c
            ON c.id = ch.cliente_id
        JOIN comercial.vendedores_ia v
            ON v.id = ch.vendedor_id
        {where_sql}
        ORDER BY ch.criado_em DESC
        LIMIT %s OFFSET %s;
    """

    with obter_conexao() as conexao:
        with conexao.cursor() as cursor:
            cursor.execute(consulta_total, parametros)
            total = cursor.fetchone()["total"]

            cursor.execute(
                consulta_itens,
                [*parametros, limite, offset],
            )
            itens = cursor.fetchall()

    return {
        "total": total,
        "limite": limite,
        "offset": offset,
        "itens": itens,
    }


def montar_resumo_conversa_voz(
    estado: dict[str, Any],
    levantamento_completo: bool,
    resultado: str,
) -> str:
    partes: list[str] = []

    campos = [
        ("Cliente", estado.get("nome_cliente")),
        ("Produto", estado.get("produto")),
        ("Marca", estado.get("marca_maquina")),
        ("Modelo", estado.get("modelo_maquina")),
        ("Quantidade", estado.get("quantidade")),
    ]

    for rotulo, valor in campos:
        if valor not in (None, "", [], {}):
            partes.append(f"{rotulo}: {valor}")

    dados_tecnicos = estado.get("dados_tecnicos")
    if isinstance(dados_tecnicos, dict) and dados_tecnicos:
        dados_formatados = ", ".join(
            f"{chave}: {valor}"
            for chave, valor in dados_tecnicos.items()
            if valor not in (None, "", [], {})
        )
        if dados_formatados:
            partes.append(f"Dados técnicos: {dados_formatados}")

    partes.append(
        "Levantamento técnico completo"
        if levantamento_completo
        else "Levantamento técnico incompleto"
    )
    partes.append(f"Resultado: {resultado}")

    return "; ".join(partes)[:4000]


def criar_venda_futura_da_chamada(
    cursor,
    chamada_id: UUID,
    dados: ConversaVozRegistrar,
    vendedor: dict[str, Any],
    resumo: str,
) -> dict[str, Any] | None:
    configuracoes = {
        "sem_opcao_comercializavel": {
            "tipo": "verificar_disponibilidade",
            "etapa": "aguardando_disponibilidade",
            "prioridade": 1,
            "probabilidade": 45,
            "acao": (
                "Verificar preço, estoque e previsão de reposição; "
                "retornar ao cliente."
            ),
        },
        "aguardando_disponibilidade": {
            "tipo": "verificar_disponibilidade",
            "etapa": "aguardando_disponibilidade",
            "prioridade": 1,
            "probabilidade": 45,
            "acao": (
                "Verificar preço, estoque e previsão de reposição; "
                "retornar ao cliente."
            ),
        },
        "produto_nao_encontrado": {
            "tipo": "revisar_catalogo",
            "etapa": "revisao_catalogo",
            "prioridade": 2,
            "probabilidade": 30,
            "acao": (
                "Revisar descrição, código e possíveis equivalências "
                "no catálogo; retornar ao cliente."
            ),
        },
        "revisao_catalogo": {
            "tipo": "revisar_catalogo",
            "etapa": "revisao_catalogo",
            "prioridade": 2,
            "probabilidade": 30,
            "acao": (
                "Revisar descrição, código e possíveis equivalências "
                "no catálogo; retornar ao cliente."
            ),
        },
        "falha_consulta_catalogo": {
            "tipo": "revisar_integracao",
            "etapa": "revisao_integracao",
            "prioridade": 1,
            "probabilidade": 35,
            "acao": (
                "Reprocessar a consulta ao catálogo e retornar ao cliente."
            ),
        },
        "revisao_integracao": {
            "tipo": "revisar_integracao",
            "etapa": "revisao_integracao",
            "prioridade": 1,
            "probabilidade": 35,
            "acao": (
                "Reprocessar a consulta ao catálogo e retornar ao cliente."
            ),
        },
    }

    configuracao = configuracoes.get(dados.resultado)
    if configuracao is None:
        return None

    estado = dados.estado_comercial or {}
    termo_busca = (
        estado.get("termo_busca")
        or estado.get("descricao_solicitada")
        or estado.get("produto")
        or "Produto solicitado pelo cliente"
    )
    ultima_consulta = estado.get("ultima_consulta_catalogo")
    if not isinstance(ultima_consulta, dict):
        ultima_consulta = {}

    opcoes = (
        ultima_consulta.get("opcoes_descartadas")
        or ultima_consulta.get("opcoes_comercializaveis")
        or ultima_consulta.get("opcoes")
        or []
    )
    if not isinstance(opcoes, list):
        opcoes = []

    prazo_retorno = dados.fim_em + timedelta(hours=4)
    titulo = f"Venda futura: {str(termo_busca)[:150]}"
    descricao = (
        f"{resumo}. Próxima ação: {configuracao['acao']}"
    )[:4000]

    cursor.execute(
        """
        INSERT INTO comercial.oportunidades (
            cliente_id,
            vendedor_id,
            origem,
            etapa,
            titulo,
            descricao,
            probabilidade,
            produtos,
            proxima_acao,
            proxima_acao_em
        )
        VALUES (
            %s,
            %s,
            'agente_voz',
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s
        )
        RETURNING id;
        """,
        (
            dados.cliente_id,
            vendedor["id"],
            configuracao["etapa"],
            titulo,
            descricao,
            configuracao["probabilidade"],
            Jsonb(opcoes),
            configuracao["acao"],
            prazo_retorno,
        ),
    )
    oportunidade_id = cursor.fetchone()["id"]

    cursor.execute(
        """
        INSERT INTO comercial.pendencias_comerciais (
            oportunidade_id,
            chamada_id,
            cliente_id,
            vendedor_id,
            tipo,
            prioridade,
            status,
            destinatario,
            titulo,
            descricao,
            termo_busca,
            estado_comercial,
            opcoes_catalogo,
            prazo_em
        )
        VALUES (
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            'pendente',
            'gerente_ou_humano',
            %s,
            %s,
            %s,
            %s,
            %s,
            %s
        )
        ON CONFLICT (chamada_id, tipo) DO UPDATE
        SET
            atualizado_em = NOW()
        RETURNING
            id,
            oportunidade_id,
            tipo,
            prioridade,
            status,
            destinatario,
            prazo_em,
            criado_em;
        """,
        (
            oportunidade_id,
            chamada_id,
            dados.cliente_id,
            vendedor["id"],
            configuracao["tipo"],
            configuracao["prioridade"],
            titulo,
            descricao,
            str(termo_busca)[:300],
            Jsonb(estado),
            Jsonb(opcoes),
            prazo_retorno,
        ),
    )
    pendencia = cursor.fetchone()

    cursor.execute(
        """
        UPDATE comercial.clientes
        SET
            status = CASE
                WHEN status IN ('novo', 'teste_voz')
                THEN 'oportunidade'
                ELSE status
            END,
            proxima_acao_em = CASE
                WHEN proxima_acao_em IS NULL
                THEN %s
                ELSE LEAST(proxima_acao_em, %s)
            END,
            atualizado_em = NOW()
        WHERE id = %s;
        """,
        (
            prazo_retorno,
            prazo_retorno,
            dados.cliente_id,
        ),
    )

    cursor.execute(
        """
        INSERT INTO comercial.interacoes (
            cliente_id,
            vendedor_id,
            canal,
            direcao,
            tipo,
            resumo,
            intencao,
            mensagem_externa_id
        )
        VALUES (
            %s,
            %s,
            'sistema',
            'saida',
            'pendencia_venda_futura',
            %s,
            'retorno_comercial',
            %s
        );
        """,
        (
            dados.cliente_id,
            vendedor["id"],
            descricao,
            f"{dados.chamada_externa_id}:pendencia",
        ),
    )

    cursor.execute(
        """
        INSERT INTO comercial.acoes_agente (
            vendedor_id,
            cliente_id,
            tipo_acao,
            origem,
            entrada,
            saida,
            sucesso
        )
        VALUES (
            %s,
            %s,
            'criar_pendencia_venda_futura',
            'persistencia_chamada_voz',
            %s,
            %s,
            TRUE
        );
        """,
        (
            vendedor["id"],
            dados.cliente_id,
            Jsonb(
                {
                    "chamada_id": str(chamada_id),
                    "resultado": dados.resultado,
                    "termo_busca": termo_busca,
                }
            ),
            Jsonb(
                {
                    "pendencia_id": str(pendencia["id"]),
                    "oportunidade_id": str(oportunidade_id),
                    "prazo_em": prazo_retorno.isoformat(),
                }
            ),
        ),
    )

    return {
        **pendencia,
        "oportunidade_id": oportunidade_id,
        "titulo": titulo,
        "proxima_acao": configuracao["acao"],
    }


@app.post(
    "/chamadas/registrar-conversa-voz",
    tags=["Chamadas"],
    status_code=http_status.HTTP_201_CREATED,
    dependencies=[Depends(validar_api_key)],
)
def registrar_conversa_voz(
    dados: ConversaVozRegistrar,
) -> dict[str, Any]:
    with obter_conexao() as conexao:
        with conexao.cursor() as cursor:
            cursor.execute(
                """
                SELECT id
                FROM comercial.chamadas_ia
                WHERE provedor = %s
                  AND chamada_externa_id = %s
                ORDER BY criado_em DESC
                LIMIT 1;
                """,
                (
                    dados.provedor,
                    dados.chamada_externa_id,
                ),
            )
            chamada_existente = cursor.fetchone()

            if chamada_existente is not None:
                cursor.execute(
                    """
                    SELECT
                        id,
                        oportunidade_id,
                        tipo,
                        prioridade,
                        status,
                        destinatario,
                        prazo_em,
                        criado_em
                    FROM comercial.pendencias_comerciais
                    WHERE chamada_id = %s
                    ORDER BY criado_em DESC
                    LIMIT 1;
                    """,
                    (chamada_existente["id"],),
                )
                pendencia_existente = cursor.fetchone()

                return {
                    "criada": False,
                    "idempotente": True,
                    "interacoes_registradas": 0,
                    "chamada": obter_chamada_detalhada(
                        cursor,
                        chamada_existente["id"],
                    ),
                    "venda_futura": pendencia_existente,
                }

            vendedor = obter_vendedor_por_codigo(
                cursor,
                dados.vendedor_codigo,
            )
            cliente = obter_cliente_por_id(
                cursor,
                dados.cliente_id,
            )

            if (
                cliente["vendedor_id"] is not None
                and cliente["vendedor_id"] != vendedor["id"]
            ):
                raise HTTPException(
                    status_code=http_status.HTTP_409_CONFLICT,
                    detail=(
                        "O cliente está vinculado a outro vendedor. "
                        "Não foi possível registrar a conversa para "
                        f"{dados.vendedor_codigo}."
                    ),
                )

            agenda = None
            if dados.agenda_id is not None:
                agenda = obter_agenda_detalhada(
                    cursor,
                    dados.agenda_id,
                )

                if agenda["cliente_id"] != dados.cliente_id:
                    raise HTTPException(
                        status_code=http_status.HTTP_409_CONFLICT,
                        detail=(
                            "A agenda informada pertence a outro cliente."
                        ),
                    )

                if agenda["vendedor_id"] != vendedor["id"]:
                    raise HTTPException(
                        status_code=http_status.HTTP_409_CONFLICT,
                        detail=(
                            "A agenda informada pertence a outro vendedor."
                        ),
                    )

            transcricao_linhas: list[str] = []
            for turno in dados.turnos:
                transcricao_linhas.append(
                    f"Cliente: {turno.cliente.strip()}"
                )
                if turno.agente:
                    transcricao_linhas.append(
                        f"{vendedor['nome']}: {turno.agente.strip()}"
                    )

            transcricao_completa = "\n".join(transcricao_linhas)
            resumo = dados.resumo or montar_resumo_conversa_voz(
                dados.estado_comercial,
                dados.levantamento_completo,
                dados.resultado,
            )

            dados_extraidos = {
                **dados.dados_extraidos,
                "estado_comercial": dados.estado_comercial,
                "levantamento_completo": dados.levantamento_completo,
                "motivo_encerramento": dados.motivo_encerramento,
                "turnos": [
                    turno.model_dump()
                    for turno in dados.turnos
                ],
                "modelos": dados.modelos,
                "origem_integracao": "gateway_voz",
            }

            cursor.execute(
                """
                INSERT INTO comercial.chamadas_ia (
                    agenda_id,
                    cliente_id,
                    vendedor_id,
                    provedor,
                    chamada_externa_id,
                    numero_origem,
                    numero_destino,
                    direcao,
                    status,
                    inicio_em,
                    fim_em,
                    duracao_segundos,
                    atendida,
                    transcricao,
                    resumo,
                    sentimento,
                    intencao,
                    resultado,
                    custo_telefonia,
                    custo_ia,
                    custo_total,
                    dados_extraidos
                )
                VALUES (
                    %(agenda_id)s,
                    %(cliente_id)s,
                    %(vendedor_id)s,
                    %(provedor)s,
                    %(chamada_externa_id)s,
                    %(numero_origem)s,
                    %(numero_destino)s,
                    %(direcao)s,
                    'concluida',
                    %(inicio_em)s,
                    %(fim_em)s,
                    %(duracao_segundos)s,
                    TRUE,
                    %(transcricao)s,
                    %(resumo)s,
                    %(sentimento)s,
                    %(intencao)s,
                    %(resultado)s,
                    0,
                    0,
                    0,
                    %(dados_extraidos)s
                )
                RETURNING id;
                """,
                {
                    "agenda_id": dados.agenda_id,
                    "cliente_id": dados.cliente_id,
                    "vendedor_id": vendedor["id"],
                    "provedor": dados.provedor,
                    "chamada_externa_id": dados.chamada_externa_id,
                    "numero_origem": dados.numero_origem,
                    "numero_destino": dados.numero_destino,
                    "direcao": dados.direcao,
                    "inicio_em": dados.inicio_em,
                    "fim_em": dados.fim_em,
                    "duracao_segundos": dados.duracao_segundos,
                    "transcricao": transcricao_completa or None,
                    "resumo": resumo,
                    "sentimento": dados.sentimento,
                    "intencao": dados.intencao,
                    "resultado": dados.resultado,
                    "dados_extraidos": Jsonb(dados_extraidos),
                },
            )
            chamada_id = cursor.fetchone()["id"]

            interacoes_registradas = 0

            for turno in dados.turnos:
                cursor.execute(
                    """
                    INSERT INTO comercial.interacoes (
                        cliente_id,
                        vendedor_id,
                        canal,
                        direcao,
                        tipo,
                        mensagem,
                        intencao,
                        mensagem_externa_id
                    )
                    VALUES (
                        %s,
                        %s,
                        'telefone',
                        'entrada',
                        'fala_cliente_ia',
                        %s,
                        %s,
                        %s
                    );
                    """,
                    (
                        dados.cliente_id,
                        vendedor["id"],
                        turno.cliente,
                        dados.intencao,
                        (
                            f"{dados.chamada_externa_id}:"
                            f"cliente:{turno.numero}"
                        ),
                    ),
                )
                interacoes_registradas += 1

                if turno.agente:
                    cursor.execute(
                        """
                        INSERT INTO comercial.interacoes (
                            cliente_id,
                            vendedor_id,
                            canal,
                            direcao,
                            tipo,
                            mensagem,
                            intencao,
                            mensagem_externa_id
                        )
                        VALUES (
                            %s,
                            %s,
                            'telefone',
                            'saida',
                            'resposta_vendedor_ia',
                            %s,
                            %s,
                            %s
                        );
                        """,
                        (
                            dados.cliente_id,
                            vendedor["id"],
                            turno.agente,
                            dados.intencao,
                            (
                                f"{dados.chamada_externa_id}:"
                                f"agente:{turno.numero}"
                            ),
                        ),
                    )
                    interacoes_registradas += 1

            cursor.execute(
                """
                INSERT INTO comercial.interacoes (
                    cliente_id,
                    vendedor_id,
                    canal,
                    direcao,
                    tipo,
                    resumo,
                    intencao,
                    mensagem_externa_id
                )
                VALUES (
                    %s,
                    %s,
                    'telefone',
                    'saida',
                    'resumo_chamada_ia',
                    %s,
                    %s,
                    %s
                );
                """,
                (
                    dados.cliente_id,
                    vendedor["id"],
                    resumo,
                    dados.intencao,
                    f"{dados.chamada_externa_id}:resumo",
                ),
            )
            interacoes_registradas += 1

            snapshot_triagem = {
                "chamada_id": str(chamada_id),
                "chamada_externa_id": dados.chamada_externa_id,
                "vendedor_codigo": vendedor["codigo"],
                "resultado": dados.resultado,
                "levantamento_completo": dados.levantamento_completo,
                "estado_comercial": dados.estado_comercial,
                "resumo": resumo,
                "registrado_em": dados.fim_em.isoformat(),
            }

            cursor.execute(
                """
                UPDATE comercial.clientes
                SET
                    vendedor_id = COALESCE(vendedor_id, %s),
                    ultima_interacao_em = %s,
                    dados_adicionais = COALESCE(
                        dados_adicionais,
                        '{}'::jsonb
                    ) || jsonb_build_object(
                        'ultima_triagem_ia',
                        %s
                    ),
                    atualizado_em = NOW()
                WHERE id = %s;
                """,
                (
                    vendedor["id"],
                    dados.fim_em,
                    Jsonb(snapshot_triagem),
                    dados.cliente_id,
                ),
            )

            if agenda is not None:
                cursor.execute(
                    """
                    UPDATE comercial.agendas_comerciais
                    SET
                        status = 'concluida',
                        resultado = %s,
                        observacao = COALESCE(
                            NULLIF(observacao, ''),
                            %s
                        ),
                        atualizado_em = NOW()
                    WHERE id = %s;
                    """,
                    (
                        dados.resultado,
                        resumo,
                        dados.agenda_id,
                    ),
                )

            venda_futura = criar_venda_futura_da_chamada(
                cursor,
                chamada_id,
                dados,
                vendedor,
                resumo,
            )

            cursor.execute(
                """
                INSERT INTO comercial.acoes_agente (
                    vendedor_id,
                    cliente_id,
                    tipo_acao,
                    origem,
                    entrada,
                    saida,
                    sucesso,
                    duracao_ms
                )
                VALUES (
                    %s,
                    %s,
                    'registrar_conversa_voz',
                    'gateway_voz',
                    %s,
                    %s,
                    TRUE,
                    %s
                );
                """,
                (
                    vendedor["id"],
                    dados.cliente_id,
                    Jsonb(
                        {
                            "chamada_externa_id": (
                                dados.chamada_externa_id
                            ),
                            "agenda_id": (
                                str(dados.agenda_id)
                                if dados.agenda_id
                                else None
                            ),
                            "quantidade_turnos": len(dados.turnos),
                        }
                    ),
                    Jsonb(
                        {
                            "chamada_id": str(chamada_id),
                            "resultado": dados.resultado,
                            "levantamento_completo": (
                                dados.levantamento_completo
                            ),
                            "interacoes_registradas": (
                                interacoes_registradas
                            ),
                            "pendencia_venda_futura_id": (
                                str(venda_futura["id"])
                                if venda_futura
                                else None
                            ),
                        }
                    ),
                    dados.duracao_segundos * 1000,
                ),
            )

            chamada = obter_chamada_detalhada(
                cursor,
                chamada_id,
            )

        conexao.commit()

    return {
        "criada": True,
        "idempotente": False,
        "interacoes_registradas": interacoes_registradas,
        "chamada": chamada,
        "venda_futura": venda_futura,
    }


@app.get(
    "/pendencias-comerciais",
    tags=["Gestão Comercial"],
    dependencies=[Depends(validar_api_key)],
)
def listar_pendencias_comerciais(
    status_pendencia: str | None = Query(
        default="pendente",
        alias="status",
    ),
    tipo: str | None = Query(default=None),
    vendedor_codigo: str | None = Query(default=None),
    limite: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    if (
        status_pendencia
        and status_pendencia not in STATUS_PENDENCIA_COMERCIAL
    ):
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Status de pendência inválido.",
        )

    filtros: list[str] = []
    parametros: list[Any] = []

    if status_pendencia:
        filtros.append("p.status = %s")
        parametros.append(status_pendencia)

    if tipo:
        filtros.append("p.tipo = %s")
        parametros.append(tipo)

    if vendedor_codigo:
        filtros.append("v.codigo = %s")
        parametros.append(vendedor_codigo.upper())

    where_sql = (
        "WHERE " + " AND ".join(filtros)
        if filtros
        else ""
    )

    with obter_conexao() as conexao:
        with conexao.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT
                    p.id,
                    p.oportunidade_id,
                    p.chamada_id,
                    p.tipo,
                    p.prioridade,
                    p.status,
                    p.destinatario,
                    p.responsavel,
                    p.titulo,
                    p.descricao,
                    p.termo_busca,
                    p.estado_comercial,
                    p.opcoes_catalogo,
                    p.prazo_em,
                    p.previsao_retorno,
                    p.resolucao,
                    p.criado_em,
                    p.atualizado_em,
                    c.nome_contato,
                    c.telefone,
                    c.whatsapp,
                    c.uf,
                    v.codigo AS vendedor_codigo,
                    v.nome_exibicao AS vendedor_nome
                FROM comercial.pendencias_comerciais p
                JOIN comercial.clientes c
                    ON c.id = p.cliente_id
                JOIN comercial.vendedores_ia v
                    ON v.id = p.vendedor_id
                {where_sql}
                ORDER BY
                    p.prioridade ASC,
                    p.prazo_em ASC NULLS LAST,
                    p.criado_em ASC
                LIMIT %s;
                """,
                [*parametros, limite],
            )
            itens = cursor.fetchall()

    return {
        "quantidade": len(itens),
        "itens": itens,
    }


@app.patch(
    "/pendencias-comerciais/{pendencia_id}",
    tags=["Gestão Comercial"],
    dependencies=[Depends(validar_api_key)],
)
def atualizar_pendencia_comercial(
    pendencia_id: UUID,
    dados: PendenciaComercialAtualizar,
) -> dict[str, Any]:
    atualizacoes: list[str] = []
    valores: list[Any] = []

    if dados.status is not None:
        atualizacoes.append("status = %s")
        valores.append(dados.status)

        if dados.status in {"resolvida", "cancelada"}:
            atualizacoes.append("resolvido_em = NOW()")

    if dados.responsavel is not None:
        atualizacoes.append("responsavel = %s")
        valores.append(dados.responsavel)

    if dados.previsao_retorno is not None:
        atualizacoes.append("previsao_retorno = %s")
        valores.append(dados.previsao_retorno)

    if dados.resolucao is not None:
        atualizacoes.append("resolucao = %s")
        valores.append(dados.resolucao)

    if not atualizacoes:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Nenhuma alteração foi informada.",
        )

    atualizacoes.append("atualizado_em = NOW()")
    valores.append(pendencia_id)

    with obter_conexao() as conexao:
        with conexao.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE comercial.pendencias_comerciais
                SET {", ".join(atualizacoes)}
                WHERE id = %s
                RETURNING *;
                """,
                valores,
            )
            pendencia = cursor.fetchone()

            if pendencia is None:
                raise HTTPException(
                    status_code=http_status.HTTP_404_NOT_FOUND,
                    detail="Pendência comercial não encontrada.",
                )

        conexao.commit()

    return pendencia


@app.get(
    "/chamadas/{chamada_id}",
    tags=["Chamadas"],
    dependencies=[Depends(validar_api_key)],
)
def buscar_chamada(
    chamada_id: UUID,
) -> dict[str, Any]:
    with obter_conexao() as conexao:
        with conexao.cursor() as cursor:
            return obter_chamada_detalhada(cursor, chamada_id)


@app.patch(
    "/chamadas/{chamada_id}/finalizar",
    tags=["Chamadas"],
    dependencies=[Depends(validar_api_key)],
)
def finalizar_chamada(
    chamada_id: UUID,
    dados: ChamadaFinalizar,
) -> dict[str, Any]:
    with obter_conexao() as conexao:
        with conexao.cursor() as cursor:
            chamada = obter_chamada_detalhada(cursor, chamada_id)

            if chamada["status"] in {
                "concluida",
                "nao_atendida",
                "ocupado",
                "falha",
                "cancelada",
            }:
                raise HTTPException(
                    status_code=http_status.HTTP_409_CONFLICT,
                    detail="Esta chamada já foi finalizada.",
                )

            fim_em = dados.fim_em or datetime.now(FUSO_PROJETO)
            custo_total = round(dados.custo_telefonia + dados.custo_ia, 4)

            cursor.execute(
                """
                UPDATE comercial.chamadas_ia
                SET
                    status = %(status)s,
                    atendida = %(atendida)s,
                    fim_em = %(fim_em)s,
                    duracao_segundos = %(duracao_segundos)s,
                    gravacao_url = %(gravacao_url)s,
                    transcricao = %(transcricao)s,
                    resumo = %(resumo)s,
                    sentimento = %(sentimento)s,
                    intencao = %(intencao)s,
                    resultado = %(resultado)s,
                    custo_telefonia = %(custo_telefonia)s,
                    custo_ia = %(custo_ia)s,
                    custo_total = %(custo_total)s,
                    dados_extraidos = %(dados_extraidos)s
                WHERE id = %(chamada_id)s;
                """,
                {
                    "status": dados.status,
                    "atendida": dados.atendida,
                    "fim_em": fim_em,
                    "duracao_segundos": dados.duracao_segundos,
                    "gravacao_url": dados.gravacao_url,
                    "transcricao": dados.transcricao,
                    "resumo": dados.resumo,
                    "sentimento": dados.sentimento,
                    "intencao": dados.intencao,
                    "resultado": dados.resultado,
                    "custo_telefonia": dados.custo_telefonia,
                    "custo_ia": dados.custo_ia,
                    "custo_total": custo_total,
                    "dados_extraidos": Jsonb(dados.dados_extraidos),
                    "chamada_id": chamada_id,
                },
            )

            cursor.execute(
                """
                UPDATE comercial.agendas_comerciais
                SET
                    status = %s,
                    resultado = %s,
                    proxima_tentativa_em = %s,
                    observacao = COALESCE(%s, observacao),
                    atualizado_em = NOW()
                WHERE id = %s;
                """,
                (
                    dados.agenda_status,
                    dados.resultado,
                    dados.proxima_tentativa_em,
                    dados.observacao_agenda,
                    chamada["agenda_id"],
                ),
            )

            cliente_atualizacoes = [
                "ultima_interacao_em = NOW()",
                "atualizado_em = NOW()",
            ]
            cliente_valores: list[Any] = []

            if dados.cliente_status is not None:
                cliente_atualizacoes.append("status = %s")
                cliente_valores.append(dados.cliente_status)

            if dados.proxima_acao_em is not None:
                cliente_atualizacoes.append("proxima_acao_em = %s")
                cliente_valores.append(dados.proxima_acao_em)

            cursor.execute(
                f"""
                UPDATE comercial.clientes
                SET {", ".join(cliente_atualizacoes)}
                WHERE id = %s;
                """,
                [*cliente_valores, chamada["cliente_id"]],
            )

            cursor.execute(
                """
                INSERT INTO comercial.interacoes (
                    cliente_id,
                    vendedor_id,
                    canal,
                    direcao,
                    tipo,
                    mensagem,
                    resumo,
                    intencao,
                    anexos
                )
                VALUES (
                    %s,
                    %s,
                    'telefone',
                    'saida',
                    'chamada_ia',
                    %s,
                    %s,
                    %s,
                    %s
                );
                """,
                (
                    chamada["cliente_id"],
                    chamada["vendedor_id"],
                    dados.transcricao,
                    dados.resumo,
                    dados.intencao,
                    Jsonb(
                        [
                            {
                                "tipo": "gravacao",
                                "url": dados.gravacao_url,
                            }
                        ]
                        if dados.gravacao_url
                        else []
                    ),
                ),
            )

            cursor.execute(
                """
                INSERT INTO comercial.acoes_agente (
                    vendedor_id,
                    cliente_id,
                    tipo_acao,
                    origem,
                    entrada,
                    saida,
                    sucesso,
                    custo
                )
                VALUES (
                    %s,
                    %s,
                    'finalizar_chamada',
                    'api_comercial',
                    %s,
                    %s,
                    TRUE,
                    %s
                );
                """,
                (
                    chamada["vendedor_id"],
                    chamada["cliente_id"],
                    Jsonb(
                        {
                            "chamada_id": str(chamada_id),
                            "agenda_status": dados.agenda_status,
                        }
                    ),
                    Jsonb(
                        {
                            "status": dados.status,
                            "atendida": dados.atendida,
                            "resultado": dados.resultado,
                        }
                    ),
                    custo_total,
                ),
            )

            chamada_finalizada = obter_chamada_detalhada(cursor, chamada_id)

        conexao.commit()
        return chamada_finalizada


@app.post(
    "/telefonia/twilio/teste",
    tags=["Telefonia Twilio"],
    status_code=http_status.HTTP_201_CREATED,
    dependencies=[Depends(validar_api_key)],
)
def iniciar_teste_twilio(
    dados: TwilioTesteChamada,
) -> dict[str, Any]:
    template_trial = (
        "https://webhooks.twilio.com/v1/Voice/Template/"
        "voice_text_to_speech"
    )
    callback_url = f"{TWILIO_BASE_URL}/webhooks/twilio/status-chamada"
    telefone_mascarado = mascarar_telefone(dados.numero_destino)

    try:
        chamada = obter_cliente_twilio().calls.create(
            to=dados.numero_destino,
            from_=TWILIO_PHONE_NUMBER,
            url=template_trial,
            method="POST",
            status_callback=callback_url,
            status_callback_event=[
                "initiated",
                "ringing",
                "answered",
                "completed",
            ],
            status_callback_method="POST",
            timeout=dados.timeout_segundos,
        )
    except TwilioRestException as erro:
        with obter_conexao() as conexao:
            with conexao.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO comercial.acoes_agente (
                        tipo_acao,
                        origem,
                        entrada,
                        saida,
                        sucesso,
                        erro
                    )
                    VALUES (
                        'twilio_teste_chamada',
                        'api_comercial',
                        %s,
                        %s,
                        FALSE,
                        %s
                    );
                    """,
                    (
                        Jsonb(
                            {
                                "numero_destino": telefone_mascarado,
                                "timeout_segundos": dados.timeout_segundos,
                                "template": "voice_text_to_speech",
                            }
                        ),
                        Jsonb(
                            {
                                "codigo_twilio": erro.code,
                                "status_http": erro.status,
                            }
                        ),
                        str(erro),
                    ),
                )
            conexao.commit()

        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail={
                "mensagem": "A Twilio recusou a criação da chamada.",
                "codigo_twilio": erro.code,
                "status_http": erro.status,
                "detalhe_twilio": erro.msg,
            },
        ) from erro

    with obter_conexao() as conexao:
        with conexao.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO comercial.acoes_agente (
                    tipo_acao,
                    origem,
                    entrada,
                    saida,
                    sucesso
                )
                VALUES (
                    'twilio_teste_chamada',
                    'api_comercial',
                    %s,
                    %s,
                    TRUE
                );
                """,
                (
                    Jsonb(
                        {
                            "numero_destino": telefone_mascarado,
                            "timeout_segundos": dados.timeout_segundos,
                            "template": "voice_text_to_speech",
                        }
                    ),
                    Jsonb(
                        {
                            "call_sid": chamada.sid,
                            "status": chamada.status,
                            "numero_origem": TWILIO_PHONE_NUMBER,
                        }
                    ),
                ),
            )
        conexao.commit()

    return {
        "mensagem": "Chamada de teste solicitada à Twilio.",
        "call_sid": chamada.sid,
        "status": chamada.status,
        "numero_origem": TWILIO_PHONE_NUMBER,
        "numero_destino": telefone_mascarado,
        "template_trial": "voice_text_to_speech",
        "status_callback": callback_url,
    }


@app.get(
    "/telefonia/twilio/chamadas/{call_sid}",
    tags=["Telefonia Twilio"],
    dependencies=[Depends(validar_api_key)],
)
def consultar_chamada_twilio(
    call_sid: str,
) -> dict[str, Any]:
    if not re.fullmatch(r"CA[0-9a-fA-F]{32}", call_sid):
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Call SID da Twilio inválido.",
        )

    try:
        chamada = obter_cliente_twilio().calls(call_sid).fetch()
    except TwilioRestException as erro:
        codigo_http = (
            http_status.HTTP_404_NOT_FOUND
            if erro.status == 404
            else http_status.HTTP_502_BAD_GATEWAY
        )
        raise HTTPException(
            status_code=codigo_http,
            detail={
                "mensagem": "Não foi possível consultar a chamada na Twilio.",
                "codigo_twilio": erro.code,
                "status_http": erro.status,
                "detalhe_twilio": erro.msg,
            },
        ) from erro

    return {
        "call_sid": chamada.sid,
        "status": chamada.status,
        "direcao": chamada.direction,
        "numero_origem": chamada.from_,
        "numero_destino": mascarar_telefone(chamada.to),
        "duracao_segundos": chamada.duration,
        "preco": chamada.price,
        "moeda": chamada.price_unit,
        "inicio_em": chamada.start_time,
        "fim_em": chamada.end_time,
    }


@app.post(
    "/webhooks/twilio/status-chamada",
    include_in_schema=False,
)
async def receber_status_chamada_twilio(
    request: Request,
) -> dict[str, str]:
    dados = await validar_webhook_twilio(request)

    call_sid = dados.get("CallSid")
    status_twilio = dados.get("CallStatus", "")
    status_interno = STATUS_TWILIO_PARA_INTERNO.get(status_twilio)
    duracao = dados.get("CallDuration")

    with obter_conexao() as conexao:
        with conexao.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO comercial.acoes_agente (
                    tipo_acao,
                    origem,
                    entrada,
                    saida,
                    sucesso
                )
                VALUES (
                    'twilio_status_chamada',
                    'twilio_webhook',
                    %s,
                    %s,
                    TRUE
                );
                """,
                (
                    Jsonb(dados),
                    Jsonb(
                        {
                            "call_sid": call_sid,
                            "status_twilio": status_twilio,
                            "status_interno": status_interno,
                        }
                    ),
                ),
            )

            if call_sid and status_interno:
                cursor.execute(
                    """
                    UPDATE comercial.chamadas_ia
                    SET
                        status = %s,
                        duracao_segundos = COALESCE(%s, duracao_segundos),
                        atendida = CASE
                            WHEN %s IN ('em_andamento', 'concluida') THEN TRUE
                            ELSE atendida
                        END,
                        fim_em = CASE
                            WHEN %s IN (
                                'concluida',
                                'nao_atendida',
                                'ocupado',
                                'falha',
                                'cancelada'
                            )
                            THEN COALESCE(fim_em, NOW())
                            ELSE fim_em
                        END
                    WHERE chamada_externa_id = %s;
                    """,
                    (
                        status_interno,
                        int(duracao) if duracao and duracao.isdigit() else None,
                        status_interno,
                        status_interno,
                        call_sid,
                    ),
                )

        conexao.commit()

    return {"status": "recebido"}


@app.post(
    "/telefonia/twilio/teste-interativo",
    tags=["Telefonia Twilio"],
    status_code=http_status.HTTP_201_CREATED,
    dependencies=[Depends(validar_api_key)],
)
def iniciar_teste_interativo_twilio(
    dados: TwilioTesteInterativo,
) -> dict[str, Any]:
    url_voz = f"{TWILIO_BASE_URL}/webhooks/twilio/voz-interativa"
    callback_url = f"{TWILIO_BASE_URL}/webhooks/twilio/status-chamada"
    telefone_mascarado = mascarar_telefone(dados.numero_destino)

    try:
        chamada = obter_cliente_twilio().calls.create(
            to=dados.numero_destino,
            from_=TWILIO_PHONE_NUMBER,
            url=url_voz,
            method="POST",
            status_callback=callback_url,
            status_callback_event=[
                "initiated",
                "ringing",
                "answered",
                "completed",
            ],
            status_callback_method="POST",
            timeout=dados.timeout_segundos,
        )
    except TwilioRestException as erro:
        with obter_conexao() as conexao:
            with conexao.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO comercial.acoes_agente (
                        tipo_acao,
                        origem,
                        entrada,
                        saida,
                        sucesso,
                        erro
                    )
                    VALUES (
                        'twilio_teste_interativo',
                        'api_comercial',
                        %s,
                        %s,
                        FALSE,
                        %s
                    );
                    """,
                    (
                        Jsonb(
                            {
                                "numero_destino": telefone_mascarado,
                                "timeout_segundos": dados.timeout_segundos,
                            }
                        ),
                        Jsonb(
                            {
                                "codigo_twilio": erro.code,
                                "status_http": erro.status,
                            }
                        ),
                        str(erro),
                    ),
                )
            conexao.commit()

        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail={
                "mensagem": "A Twilio recusou o teste interativo.",
                "codigo_twilio": erro.code,
                "status_http": erro.status,
                "detalhe_twilio": erro.msg,
            },
        ) from erro

    with obter_conexao() as conexao:
        with conexao.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO comercial.acoes_agente (
                    tipo_acao,
                    origem,
                    entrada,
                    saida,
                    sucesso
                )
                VALUES (
                    'twilio_teste_interativo',
                    'api_comercial',
                    %s,
                    %s,
                    TRUE
                );
                """,
                (
                    Jsonb(
                        {
                            "numero_destino": telefone_mascarado,
                            "timeout_segundos": dados.timeout_segundos,
                        }
                    ),
                    Jsonb(
                        {
                            "call_sid": chamada.sid,
                            "status": chamada.status,
                            "url_voz": url_voz,
                        }
                    ),
                ),
            )
        conexao.commit()

    return {
        "mensagem": "Teste interativo solicitado à Twilio.",
        "call_sid": chamada.sid,
        "status": chamada.status,
        "numero_origem": TWILIO_PHONE_NUMBER,
        "numero_destino": telefone_mascarado,
        "url_voz": url_voz,
        "status_callback": callback_url,
    }


@app.post(
    "/webhooks/twilio/voz-interativa",
    include_in_schema=False,
)
async def fornecer_voz_interativa_twilio(
    request: Request,
):
    await validar_webhook_twilio(request)

    resposta = VoiceResponse()
    coleta = resposta.gather(
        input="speech",
        action=f"{TWILIO_BASE_URL}/webhooks/twilio/resposta-interativa",
        method="POST",
        language="pt-BR",
        speech_timeout="auto",
        timeout=5,
        action_on_empty_result=True,
    )
    coleta.say(
        (
            "Olá. Aqui é o Carlos, assistente virtual da RBK Distribuidora. "
            "Esta é uma ligação de teste. "
            "Depois do sinal, diga seu nome e uma peça que gostaria de consultar."
        ),
        language="pt-BR",
    )
    resposta.say(
        "Não consegui receber sua resposta. O teste será encerrado.",
        language="pt-BR",
    )
    resposta.hangup()

    return Response(
        content=str(resposta),
        media_type="application/xml",
    )


@app.post(
    "/webhooks/twilio/resposta-interativa",
    include_in_schema=False,
)
async def receber_resposta_interativa_twilio(
    request: Request,
):
    dados = await validar_webhook_twilio(request)

    call_sid = dados.get("CallSid")
    fala = (dados.get("SpeechResult") or "").strip()
    confianca = dados.get("Confidence")

    if len(fala) > 500:
        fala = fala[:500]

    with obter_conexao() as conexao:
        with conexao.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO comercial.acoes_agente (
                    tipo_acao,
                    origem,
                    entrada,
                    saida,
                    sucesso
                )
                VALUES (
                    'twilio_resposta_interativa',
                    'twilio_webhook',
                    %s,
                    %s,
                    TRUE
                );
                """,
                (
                    Jsonb(
                        {
                            "CallSid": call_sid,
                            "SpeechResult": fala,
                            "Confidence": confianca,
                        }
                    ),
                    Jsonb(
                        {
                            "fala_recebida": bool(fala),
                            "tamanho": len(fala),
                        }
                    ),
                ),
            )
        conexao.commit()

    resposta = VoiceResponse()

    if fala:
        resposta.say(
            (
                "Entendi sua resposta. "
                "O reconhecimento de voz do projeto foi validado com sucesso. "
                "Obrigado."
            ),
            language="pt-BR",
        )
    else:
        resposta.say(
            (
                "Não consegui entender sua resposta. "
                "O teste de telefonia foi concluído, mas o reconhecimento "
                "de voz precisa ser repetido."
            ),
            language="pt-BR",
        )

    resposta.hangup()

    return Response(
        content=str(resposta),
        media_type="application/xml",
    )


@app.get(
    "/telefonia/twilio/teste-interativo/{call_sid}",
    tags=["Telefonia Twilio"],
    dependencies=[Depends(validar_api_key)],
)
def consultar_teste_interativo_twilio(
    call_sid: str,
) -> dict[str, Any]:
    if not re.fullmatch(r"CA[0-9a-fA-F]{32}", call_sid):
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Call SID da Twilio inválido.",
        )

    with obter_conexao() as conexao:
        with conexao.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    id,
                    entrada,
                    saida,
                    criado_em
                FROM comercial.acoes_agente
                WHERE tipo_acao = 'twilio_resposta_interativa'
                  AND entrada ->> 'CallSid' = %s
                ORDER BY criado_em DESC
                LIMIT 1;
                """,
                (call_sid,),
            )
            resultado = cursor.fetchone()

    if resultado is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Ainda não existe resposta reconhecida para esta chamada.",
        )

    return {
        "call_sid": call_sid,
        "fala_reconhecida": resultado["entrada"].get("SpeechResult"),
        "confianca": resultado["entrada"].get("Confidence"),
        "fala_recebida": resultado["saida"].get("fala_recebida"),
        "registrado_em": resultado["criado_em"],
    }

