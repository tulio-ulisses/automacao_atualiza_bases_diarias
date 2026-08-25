import os
import re
import json
import time
import unicodedata
from datetime import datetime, timedelta

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from supabase import create_client


IILEX_URL = "https://juscash.iilex.com.br/sistema"

TABELA_HISTORICO = "historico"
TABELA_HISTORICO_120 = "historico_120"

LOTE_SUPABASE = 500
MAX_PAGINAS = 5000
MAX_PAGINAS_VAZIAS = 5

DATA_FIM = datetime.now()
DATA_INICIO = DATA_FIM - timedelta(days=120)


def env(nome):
    valor = os.getenv(nome)

    if not valor:
        raise RuntimeError(f"Variável de ambiente ausente: {nome}")

    return valor


def normalizar_nome(nome):
    nome = str(nome)
    nome = unicodedata.normalize("NFKD", nome).encode("ASCII", "ignore").decode()
    nome = re.sub(r"[^a-zA-Z0-9_]", "_", nome)
    nome = re.sub(r"_+", "_", nome).strip("_")

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
        resultado.append(base[: 100 - len(sufixo)] + sufixo)

    return resultado


def preparar_valor(valor):
    if valor is None:
        return None

    if isinstance(valor, (dict, list)):
        return json.dumps(valor, ensure_ascii=False)

    if isinstance(valor, (pd.Timestamp, datetime)):
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
            if isinstance(valor, (dict, list)):
                dados.extend(extrair_todos_os_dados(valor))

    elif isinstance(data, list):
        for item in data:
            dados.extend(extrair_todos_os_dados(item))

    return dados


def criar_sessao_iilex():
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        status=5,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )

    session = requests.Session()

    session.auth = (
        env("IILEX_USERNAME"),
        env("IILEX_PASSWORD"),
    )

    session.headers.update(
        {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
        }
    )

    session.mount(
        "https://",
        HTTPAdapter(max_retries=retry),
    )

    return session


def extrair_historico():
    session = criar_sessao_iilex()

    todos = []
    pagina = 1
    paginas_vazias = 0
    erros_403 = 0

    while pagina <= MAX_PAGINAS and paginas_vazias < MAX_PAGINAS_VAZIAS:
        resposta = session.get(
            f"{IILEX_URL}/api/public/v1/dados",
            params={
                "idmodulo": 1,
                "submodulos": "Histórico",
                "pagina": pagina,
            },
            timeout=60,
        )

        if resposta.status_code == 403:
            erros_403 += 1

            if erros_403 >= 5:
                raise RuntimeError(
                    f"IILEX retornou HTTP 403 repetidamente na página {pagina}"
                )

            time.sleep(15)
            continue

        if resposta.status_code == 404:
            break

        resposta.raise_for_status()
        erros_403 = 0

        dados = extrair_todos_os_dados(resposta.json())

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

        pagina += 1
        time.sleep(1.5)

    if not todos:
        raise RuntimeError("Nenhum registro de Histórico foi retornado pelo IILEX")

    return todos


def criar_dataframe_historico(registros):
    df = pd.DataFrame(registros)
    df.columns = normalizar_colunas(df.columns)

    return df


def criar_historico_120(df):
    if "data_do_historico" not in df.columns:
        raise RuntimeError("Coluna data_do_historico não encontrada")

    if "idprocesso" not in df.columns:
        raise RuntimeError("Coluna idprocesso não encontrada")

    df_120 = df.copy()

    df_120["data_do_historico"] = pd.to_datetime(
        df_120["data_do_historico"],
        errors="coerce",
    )

    df_120 = df_120[
        (df_120["data_do_historico"] >= DATA_INICIO)
        & (df_120["data_do_historico"] <= DATA_FIM)
    ]

    df_120 = df_120.dropna(
        subset=[
            "idprocesso",
            "data_do_historico",
        ]
    )

    if df_120.empty:
        raise RuntimeError(
            "Nenhum registro encontrado para historico_120"
        )

    indices = (
        df_120
        .groupby("idprocesso")["data_do_historico"]
        .idxmax()
    )

    return df_120.loc[indices].reset_index(drop=True)


def preparar_dataframe_supabase(df):
    df = df.copy()

    for coluna in df.columns:
        df[coluna] = df[coluna].map(preparar_valor)

    return df


def inserir_lote(supabase, tabela, lote, tentativas=5):
    ultimo_erro = None

    for tentativa in range(1, tentativas + 1):
        try:
            supabase.table(tabela).insert(lote).execute()
            return

        except Exception as erro:
            ultimo_erro = erro

            if tentativa < tentativas:
                time.sleep(tentativa * 2)

    raise ultimo_erro


def enviar_supabase(supabase, tabela, df):
    df = preparar_dataframe_supabase(df)

    supabase.rpc(
        "preparar_carga_iilex",
        {
            "p_tabela": tabela,
            "p_colunas": df.columns.tolist(),
        },
    ).execute()

    registros = df.to_dict(orient="records")

    for inicio in range(0, len(registros), LOTE_SUPABASE):
        lote = registros[
            inicio : inicio + LOTE_SUPABASE
        ]

        inserir_lote(
            supabase,
            tabela,
            lote,
        )


def main():
    registros = extrair_historico()

    df_historico = criar_dataframe_historico(registros)
    df_historico_120 = criar_historico_120(df_historico)

    supabase = create_client(
        env("SUPABASE_URL"),
        env("SUPABASE_SERVICE_ROLE_KEY"),
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
        f"Historico atualizado: {len(df_historico)} registros | "
        f"Historico 120: {len(df_historico_120)} registros"
    )


if __name__ == "__main__":
    main()