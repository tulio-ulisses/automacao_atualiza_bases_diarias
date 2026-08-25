import os
import re
import json
import time
import unicodedata

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from supabase import create_client


BASE_URL = "https://juscash.iilex.com.br/sistema"
MODULO = 1
TABELA = "contencioso"

LINHAS_POR_PAGINA = 100
LOTE_SUPABASE = 500


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

    try:
        if pd.isna(valor):
            return None
    except (TypeError, ValueError):
        pass

    return str(valor)


def extrair_contencioso():
    usuario = env("IILEX_USERNAME")
    senha = env("IILEX_PASSWORD")

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
    session.auth = (usuario, senha)
    session.headers.update({"Accept": "application/json"})
    session.mount("https://", HTTPAdapter(max_retries=retry))

    registros_totais = []
    pagina = 0

    while True:
        resposta = session.get(
            f"{BASE_URL}/api/public/v1/dados",
            params={
                "idmodulo": MODULO,
                "pagina": pagina,
                "linhas": LINHAS_POR_PAGINA,
            },
            timeout=60,
        )

        resposta.raise_for_status()

        dados = resposta.json()

        if "registros" not in dados:
            raise RuntimeError(
                f"Resposta inesperada do IILEX na página {pagina}"
            )

        registros = dados.get("registros", {}).get("registro", [])

        if not registros:
            break

        if not isinstance(registros, list):
            registros = [registros]

        registros_totais.extend(registros)
        pagina += 1

    if not registros_totais:
        raise RuntimeError("Nenhum registro retornado pelo Contencioso do IILEX")

    return registros_totais


def criar_dataframe(registros):
    df = pd.DataFrame(registros)

    df.columns = normalizar_colunas(df.columns)

    df.insert(
        0,
        "id_unico",
        range(1, len(df) + 1),
    )

    for coluna in df.columns:
        df[coluna] = df[coluna].map(preparar_valor)

    return df


def enviar_supabase(df):
    supabase = create_client(
        env("SUPABASE_URL"),
        env("SUPABASE_SERVICE_ROLE_KEY"),
    )

    supabase.rpc(
        "preparar_carga_iilex",
        {
            "p_tabela": TABELA,
            "p_colunas": df.columns.tolist(),
        },
    ).execute()

    time.sleep(1)

    registros = df.to_dict(orient="records")

    for inicio in range(0, len(registros), LOTE_SUPABASE):
        lote = registros[inicio : inicio + LOTE_SUPABASE]

        supabase.table(TABELA).insert(lote).execute()


def main():
    registros = extrair_contencioso()
    df = criar_dataframe(registros)
    enviar_supabase(df)

    print(f"Contencioso atualizado: {len(df)} registros")


if __name__ == "__main__":
    main()