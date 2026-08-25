import os
import re
import json
import datetime
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from supabase import create_client


IILEX_BASE_URL = "https://juscash.iilex.com.br/sistema"
LOY_BASE_URL = "https://api.loylegal.com/v1"
LOY_INTERMEDIARIAS_URL = "https://web.2adv.com.br/api"

DATABASE_LOY = "20616170000102"
MODULO_CONTENCIOSO = 1

TABELA = "peticoes_loy"

MAX_WORKERS = 5
LOTE_SUPABASE = 500


def env(nome):
    valor = os.getenv(nome)

    if not valor:
        raise RuntimeError(f"Variável de ambiente ausente: {nome}")

    return valor


def criar_sessao():
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
    session.mount("https://", HTTPAdapter(max_retries=retry))

    return session


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


IILEX_USERNAME = env("IILEX_USERNAME")
IILEX_PASSWORD = env("IILEX_PASSWORD")
LOY_TOKEN = env("LOY_TOKEN_SERVICOS")

session_iilex = criar_sessao()
session_iilex.auth = (IILEX_USERNAME, IILEX_PASSWORD)
session_iilex.headers.update({"Accept": "application/json"})

session_loy = criar_sessao()
session_loy.headers.update(
    {
        "Authorization": f"Bearer {LOY_TOKEN}",
        "Accept": "application/json",
    }
)


def buscar_processos_iilex(max_paginas=5000):
    processos = []

    for pagina in range(1, max_paginas + 1):
        resposta = session_iilex.get(
            f"{IILEX_BASE_URL}/api/public/v1/dados",
            params={
                "idmodulo": MODULO_CONTENCIOSO,
                "pagina": pagina,
            },
            timeout=60,
        )

        resposta.raise_for_status()

        dados = resposta.json()
        registros = dados.get("registros", {}).get("registro", [])

        if not registros:
            break

        if not isinstance(registros, list):
            registros = [registros]

        for item in registros:
            processo = item.get("Processo")

            if processo:
                processos.append(processo)

    if not processos:
        raise RuntimeError("Nenhum processo retornado pelo Contencioso do IILEX")

    return list(dict.fromkeys(processos))


def buscar_capa_processo(numero_processo):
    numero_limpo = "".join(filter(str.isalnum, str(numero_processo)))

    resposta = session_loy.get(
        f"{LOY_BASE_URL}/{DATABASE_LOY}/process/{numero_limpo}",
        timeout=30,
    )

    if resposta.status_code == 404:
        return None

    resposta.raise_for_status()

    return resposta.json().get("data", {})


def buscar_movimentacoes(id_processo):
    data_tentativa = datetime.datetime.now(
        datetime.timezone.utc
    ).isoformat()

    resposta = session_loy.get(
        f"{LOY_BASE_URL}/{DATABASE_LOY}/movements/{id_processo}",
        timeout=30,
    )

    if resposta.status_code != 200:
        return {
            "status": "FAIL",
            "motivo": f"HTTP {resposta.status_code}",
            "data_tentativa": data_tentativa,
            "movs": [],
        }

    return {
        "status": "SUCCESS",
        "motivo": "",
        "data_tentativa": data_tentativa,
        "movs": resposta.json().get("data", []),
    }


def buscar_intermediarias_por_data(data_inicio, data_fim, limit=300):
    data_inicio_encoded = quote(data_inicio, safe="")
    data_fim_encoded = quote(data_fim, safe="")

    url = (
        f"{LOY_INTERMEDIARIAS_URL}/workloads?"
        f"limit={limit}&page=1&params[kind]=Petição+Intermediária&"
        f"params[success]=Sucesso&params[dateStart]={data_inicio_encoded}&"
        f"params[dateEnd]={data_fim_encoded}&token={LOY_TOKEN}"
    )

    resposta = session_loy.get(url, timeout=60)
    resposta.raise_for_status()

    return resposta.json().get("workloads", [])


def criar_mapa_intermediarias():
    hoje = datetime.datetime.now()

    data_fim = hoje.strftime("%d/%m/%Y")
    data_inicio = (
        hoje - datetime.timedelta(days=60)
    ).strftime("%d/%m/%Y")

    intermediarias = buscar_intermediarias_por_data(
        data_inicio,
        data_fim,
        limit=300,
    )

    mapa = {}

    for intermediaria in intermediarias:
        justice = intermediaria.get("justice", {})
        justice_id = justice.get("_id")

        if not justice_id:
            continue

        mapa[justice_id] = {
            "intermediate_id": intermediaria.get("_id"),
            "intermediate_situation": intermediaria.get("situation", ""),
            "intermediate_success": intermediaria.get("success", ""),
            "intermediate_message": intermediaria.get("message", ""),
            "intermediate_receipt": (
                "PRESENTE"
                if intermediaria.get("receipt")
                else "AUSENTE"
            ),
            "intermediate_isWorked": intermediaria.get("isWorked", ""),
            "intermediate_createdAt": intermediaria.get("createdAt", ""),
        }

    return mapa


def processar_processo(numero_processo, mapa_intermediarias):
    try:
        capa = buscar_capa_processo(numero_processo)

        if not capa:
            return None

        id_processo = capa.get("_id")

        if not id_processo:
            return None

        mov_result = buscar_movimentacoes(id_processo)
        movimentacoes = mov_result["movs"]

        ultima_movimentacao = (
            max(
                movimentacoes,
                key=lambda x: x.get("dateTime", ""),
            )
            if movimentacoes
            else {}
        )

        intermediaria = mapa_intermediarias.get(id_processo, {})

        return {
            "processo": numero_processo,
            "id_processo": id_processo,
            "titulo": capa.get("title", ""),
            "situacao": capa.get("situation", ""),
            "is_worked": capa.get("isWorked", False),
            "captura_status": mov_result["status"],
            "captura_motivo": mov_result["motivo"],
            "captura_data": mov_result["data_tentativa"],
            "data_ultima_movimentacao": ultima_movimentacao.get(
                "dateTime", ""
            ),
            "descricao_ultima_movimentacao": ultima_movimentacao.get(
                "description", ""
            ),
            "origem_ultima_movimentacao": ultima_movimentacao.get(
                "origin", ""
            ),
            "intermediate_id": intermediaria.get(
                "intermediate_id", ""
            ),
            "intermediate_situation": intermediaria.get(
                "intermediate_situation", ""
            ),
            "intermediate_success": intermediaria.get(
                "intermediate_success", ""
            ),
            "intermediate_message": intermediaria.get(
                "intermediate_message", ""
            ),
            "intermediate_receipt": intermediaria.get(
                "intermediate_receipt", ""
            ),
            "intermediate_isworked": intermediaria.get(
                "intermediate_isWorked", ""
            ),
            "intermediate_createdat": intermediaria.get(
                "intermediate_createdAt", ""
            ),
        }

    except Exception:
        return None


def criar_dataframe(resultados):
    if not resultados:
        raise RuntimeError("Nenhum dado do LOY foi processado")

    df = pd.DataFrame(resultados)
    df.columns = normalizar_colunas(df.columns)

    df.insert(
        0,
        "id_unico",
        range(1, len(df) + 1),
    )

    for coluna in df.columns:
        df[coluna] = df[coluna].map(preparar_valor)

    return df


def inserir_lote(supabase, lote, tentativas=5):
    ultimo_erro = None

    for tentativa in range(1, tentativas + 1):
        try:
            supabase.table(TABELA).insert(lote).execute()
            return
        except Exception as erro:
            ultimo_erro = erro

            if tentativa < tentativas:
                import time
                time.sleep(tentativa * 2)

    raise ultimo_erro


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

    registros = df.to_dict(orient="records")

    for inicio in range(0, len(registros), LOTE_SUPABASE):
        lote = registros[inicio : inicio + LOTE_SUPABASE]
        inserir_lote(supabase, lote)


def main():
    mapa_intermediarias = criar_mapa_intermediarias()
    processos = buscar_processos_iilex()

    resultados = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [
            executor.submit(
                processar_processo,
                processo,
                mapa_intermediarias,
            )
            for processo in processos
        ]

        for future in as_completed(futures):
            resultado = future.result()

            if resultado:
                resultados.append(resultado)

    df = criar_dataframe(resultados)
    enviar_supabase(df)

    print(f"LOY atualizado: {len(df)} registros")


if __name__ == "__main__":
    main()