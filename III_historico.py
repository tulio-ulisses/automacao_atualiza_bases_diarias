import os
import re
import json
import time
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from supabase import create_client


IILEX_URL = "https://juscash.iilex.com.br/sistema"

TABELA_HISTORICO = "historico"
TABELA_HISTORICO_120 = "historico_120"

MAX_PAGINAS = 5000
MAX_PAGINAS_VAZIAS = 5
MAX_TENTATIVAS = 5

INTERVALO = 1.5
ESPERA_403 = 30
ESPERA_429 = 65

LOTE_SUPABASE = 500
MAX_TENTATIVAS_SUPABASE = 5

AGORA = datetime.now(
    ZoneInfo("America/Sao_Paulo")
).replace(tzinfo=None)

DATA_FIM = AGORA
DATA_INICIO = AGORA - timedelta(days=120)


def env(nome):
    valor = os.getenv(nome)

    if not valor:
        raise RuntimeError(
            f"Variável de ambiente ausente: {nome}"
        )

    return valor


def normalizar_nome(nome):
    nome = str(nome)

    nome = unicodedata.normalize(
        "NFKD",
        nome,
    ).encode(
        "ASCII",
        "ignore",
    ).decode()

    nome = re.sub(
        r"[^a-zA-Z0-9_]",
        "_",
        nome,
    )

    nome = re.sub(
        r"_+",
        "_",
        nome,
    ).strip("_")

    if not nome:
        nome = "column"

    if not nome[0].isalpha():
        nome = f"col_{nome}"

    return nome.lower()[:100]


def normalizar_colunas(colunas):
    usadas = {}
    resultado = []

    for coluna in colunas:
        base = normalizar_nome(coluna)

        if base not in usadas:
            usadas[base] = 1
            resultado.append(base)
            continue

        usadas[base] += 1

        sufixo = f"_{usadas[base]}"

        resultado.append(
            base[:100 - len(sufixo)]
            + sufixo
        )

    return resultado


def normalizar_texto(texto):
    texto = str(texto)

    texto = unicodedata.normalize(
        "NFKD",
        texto,
    ).encode(
        "ASCII",
        "ignore",
    ).decode()

    return texto.lower()


def resposta_sem_registros(resposta):
    textos = [resposta.text]

    try:
        textos.append(
            json.dumps(
                resposta.json(),
                ensure_ascii=False,
            )
        )
    except Exception:
        pass

    texto = normalizar_texto(
        " ".join(textos)
    )

    return "nenhum registro" in texto


def preparar_valor(valor):
    if valor is None:
        return None

    if isinstance(valor, (dict, list)):
        return json.dumps(
            valor,
            ensure_ascii=False,
        )

    if isinstance(
        valor,
        (pd.Timestamp, datetime),
    ):
        return valor.isoformat()

    try:
        if pd.isna(valor):
            return None
    except (TypeError, ValueError):
        pass

    return str(valor)


def extrair_todos_os_dados(data):
    dados = []

    if isinstance(data, dict):
        dados.append(data)

        for valor in data.values():
            if isinstance(
                valor,
                (dict, list),
            ):
                dados.extend(
                    extrair_todos_os_dados(
                        valor
                    )
                )

    elif isinstance(data, list):
        for item in data:
            dados.extend(
                extrair_todos_os_dados(
                    item
                )
            )

    return dados


def criar_sessao_iilex():
    session = requests.Session()

    session.auth = (
        env("IILEX_USERNAME"),
        env("IILEX_PASSWORD"),
    )

    session.headers.update(
        {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
            "Connection": "keep-alive",
        }
    )

    return session


def buscar_pagina(session, pagina):
    for tentativa in range(
        1,
        MAX_TENTATIVAS + 1,
    ):
        try:
            resposta = session.get(
                f"{IILEX_URL}/api/public/v1/dados",
                params={
                    "idmodulo": 1,
                    "submodulos": "Histórico",
                    "pagina": pagina,
                },
                timeout=60,
            )

        except requests.exceptions.RequestException:
            if tentativa == MAX_TENTATIVAS:
                raise

            espera = min(
                60,
                5 * 2 ** (tentativa - 1),
            )

            time.sleep(espera)
            continue

        if resposta.status_code == 200:
            return resposta

        if resposta.status_code in (403, 404):
            if resposta_sem_registros(resposta):
                return None

            if resposta.status_code == 404:
                return None

            if tentativa == MAX_TENTATIVAS:
                raise RuntimeError(
                    f"IILEX retornou HTTP 403 "
                    f"repetidamente na página "
                    f"{pagina}"
                )

            time.sleep(ESPERA_403)
            continue

        if resposta.status_code == 429:
            if tentativa == MAX_TENTATIVAS:
                resposta.raise_for_status()

            time.sleep(ESPERA_429)
            continue

        if resposta.status_code >= 500:
            if tentativa == MAX_TENTATIVAS:
                resposta.raise_for_status()

            espera = min(
                60,
                5 * 2 ** (tentativa - 1),
            )

            time.sleep(espera)
            continue

        resposta.raise_for_status()

    raise RuntimeError(
        f"Falha ao consultar página {pagina}"
    )


def extrair_historico():
    session = criar_sessao_iilex()

    todos = []

    pagina = 1
    paginas_vazias = 0
    total_paginas = None

    while (
        pagina <= MAX_PAGINAS
        and paginas_vazias < MAX_PAGINAS_VAZIAS
    ):
        resposta = buscar_pagina(
            session,
            pagina,
        )

        if resposta is None:
            break

        dados_json = resposta.json()

        if total_paginas is None:
            paginacao = (
                dados_json.get(
                    "paginacao",
                    {},
                )
                or {}
            )

            try:
                total_paginas = int(
                    paginacao.get(
                        "total_de_paginas"
                    )
                )
            except (TypeError, ValueError):
                total_paginas = None

        dados = extrair_todos_os_dados(
            dados_json
        )

        registros = [
            item
            for item in dados
            if isinstance(item, dict)
            and "Data_do_histórico" in item
        ]

        if registros:
            paginas_vazias = 0
            todos.extend(registros)
        else:
            paginas_vazias += 1

        if (
            pagina == 1
            or pagina % 20 == 0
        ):
            print(
                f"Historico: página {pagina} | "
                f"{len(todos):,} registros"
            )

        if (
            total_paginas
            and pagina >= total_paginas
        ):
            break

        pagina += 1

        time.sleep(INTERVALO)

    if not todos:
        raise RuntimeError(
            "Nenhum registro de Histórico "
            "foi retornado pelo IILEX"
        )

    return todos


def criar_dataframe_historico(registros):
    df = pd.DataFrame(registros)

    if df.empty:
        raise RuntimeError(
            "DataFrame do Histórico vazio"
        )

    df.columns = normalizar_colunas(
        df.columns
    )

    return df


def criar_historico_120(df):
    if (
        "data_do_historico"
        not in df.columns
    ):
        raise RuntimeError(
            "Coluna data_do_historico "
            "não encontrada"
        )

    if "idprocesso" not in df.columns:
        raise RuntimeError(
            "Coluna idprocesso "
            "não encontrada"
        )

    df_120 = df.copy()

    df_120[
        "data_do_historico"
    ] = pd.to_datetime(
        df_120[
            "data_do_historico"
        ],
        errors="coerce",
        dayfirst=True,
    )

    df_120 = df_120[
        (
            df_120["data_do_historico"]
            >= pd.Timestamp(DATA_INICIO)
        )
        &
        (
            df_120["data_do_historico"]
            <= pd.Timestamp(DATA_FIM)
        )
    ]

    df_120 = df_120.dropna(
        subset=[
            "idprocesso",
            "data_do_historico",
        ]
    )

    if df_120.empty:
        raise RuntimeError(
            "Nenhum registro encontrado "
            "para historico_120"
        )

    indices = (
        df_120
        .groupby(
            "idprocesso"
        )[
            "data_do_historico"
        ]
        .idxmax()
    )

    return (
        df_120
        .loc[indices]
        .reset_index(drop=True)
    )


def preparar_dataframe_supabase(df):
    df = df.copy()

    for coluna in df.columns:
        df[coluna] = (
            df[coluna]
            .map(preparar_valor)
        )

    return df


def inserir_lote(
    supabase,
    tabela,
    lote,
):
    ultimo_erro = None

    for tentativa in range(
        1,
        MAX_TENTATIVAS_SUPABASE + 1,
    ):
        try:
            (
                supabase
                .table(tabela)
                .insert(lote)
                .execute()
            )

            return

        except Exception as erro:
            ultimo_erro = erro

            if (
                tentativa
                < MAX_TENTATIVAS_SUPABASE
            ):
                time.sleep(
                    min(
                        30,
                        tentativa * 3,
                    )
                )

    raise ultimo_erro


def validar_quantidade(
    supabase,
    tabela,
    esperado,
):
    resposta = (
        supabase
        .table(tabela)
        .select(
            "*",
            count="exact",
        )
        .limit(1)
        .execute()
    )

    encontrado = resposta.count

    if encontrado is None:
        return

    if encontrado != esperado:
        raise RuntimeError(
            f"{tabela}: Supabase possui "
            f"{encontrado:,} registros, "
            f"mas eram esperados "
            f"{esperado:,}"
        )


def enviar_supabase(
    supabase,
    tabela,
    df,
):
    df = preparar_dataframe_supabase(
        df
    )

    supabase.rpc(
        "preparar_carga_iilex",
        {
            "p_tabela": tabela,
            "p_colunas":
                df.columns.tolist(),
        },
    ).execute()

    time.sleep(2)

    registros = df.to_dict(
        orient="records"
    )

    total = len(registros)

    for inicio in range(
        0,
        total,
        LOTE_SUPABASE,
    ):
        lote = registros[
            inicio:
            inicio + LOTE_SUPABASE
        ]

        inserir_lote(
            supabase,
            tabela,
            lote,
        )

        enviados = min(
            inicio + LOTE_SUPABASE,
            total,
        )

        if (
            enviados == total
            or enviados % 5000 == 0
        ):
            print(
                f"Supabase {tabela}: "
                f"{enviados:,}/{total:,}"
            )

    validar_quantidade(
        supabase,
        tabela,
        total,
    )


def main():
    registros = extrair_historico()

    df_historico = (
        criar_dataframe_historico(
            registros
        )
    )

    df_historico_120 = (
        criar_historico_120(
            df_historico
        )
    )

    print(
        f"Historico capturado: "
        f"{len(df_historico):,} registros | "
        f"Historico 120: "
        f"{len(df_historico_120):,}"
    )

    supabase = create_client(
        env("SUPABASE_URL"),
        env(
            "SUPABASE_SERVICE_ROLE_KEY"
        ),
    )

    enviar_supabase(
        supabase,
        TABELA_HISTORICO,
        df_historico,
    )

    enviar_supabase(
        supabase,
        TABELA_HISTORICO_120,
        df_historico_120,
    )

    print(
        f"Historico atualizado: "
        f"{len(df_historico):,} registros | "
        f"Historico 120: "
        f"{len(df_historico_120):,} registros"
    )


if __name__ == "__main__":
    main()