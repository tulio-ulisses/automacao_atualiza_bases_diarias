import os
import re
import json
import time
import unicodedata
from datetime import datetime

import pandas as pd
import requests
from supabase import create_client


BASE_URL = "https://juscash.iilex.com.br/sistema"
MODULO_AGENDA = 41
TABELA = "agenda"

LINHAS_POR_PAGINA = 50
INTERVALO = 2.5
ESPERA_429 = 65
MAX_TENTATIVAS = 5
PAGINAS_ANTIGAS_PARA_PARAR = 10
LOTE_SUPABASE = 500


def env(nome):
    valor = os.getenv(nome)

    if not valor:
        raise RuntimeError(f"Variável de ambiente ausente: {nome}")

    return valor


def subtrair_meses(data, meses):
    total = data.year * 12 + data.month - 1 - meses
    return datetime(total // 12, total % 12 + 1, 1)


DATA_LIMITE = subtrair_meses(
    datetime(datetime.now().year, datetime.now().month, 1),
    13,
)


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
        resultado.append(base[:100 - len(sufixo)] + sufixo)

    return resultado


def converter_data(valor):
    if valor is None:
        return None

    if isinstance(valor, str):
        valor = valor.strip()

        if not valor:
            return None

        try:
            dt = datetime.fromisoformat(
                valor.replace("Z", "+00:00")
            )

            if dt.tzinfo:
                dt = dt.replace(tzinfo=None)

            return dt

        except ValueError:
            pass

        formatos = (
            "%Y-%m-%d",
            "%d/%m/%Y",
            "%d-%m-%Y",
            "%Y-%m-%d %H:%M:%S",
            "%d/%m/%Y %H:%M:%S",
        )

        for formato in formatos:
            try:
                return datetime.strptime(valor, formato)
            except ValueError:
                pass

    if isinstance(valor, (int, float)):
        try:
            if valor > 1e10:
                valor /= 1000

            return datetime.fromtimestamp(valor)

        except Exception:
            pass

    return None


def obter_prazo(registro):
    for campo in ("Prazo", "prazo", "PRAZO"):
        valor = registro.get(campo)

        if valor:
            return converter_data(valor)

    return None


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

ultimo_request = 0.0


def esperar_intervalo():
    global ultimo_request

    agora = time.monotonic()

    if ultimo_request:
        restante = INTERVALO - (agora - ultimo_request)

        if restante > 0:
            time.sleep(restante)

    ultimo_request = time.monotonic()


def buscar_pagina(pagina):
    global ultimo_request

    for tentativa in range(1, MAX_TENTATIVAS + 1):
        esperar_intervalo()

        try:
            resposta = session.get(
                f"{BASE_URL}/api/public/v1/dados",
                params={
                    "idmodulo": MODULO_AGENDA,
                    "pagina": pagina,
                    "linhas": LINHAS_POR_PAGINA,
                },
                timeout=60,
            )

            if resposta.status_code == 200:
                dados = resposta.json()

                registros = (
                    dados
                    .get("registros", {})
                    .get("registro", [])
                )

                if not registros:
                    return None, dados.get("paginacao", {})

                if not isinstance(registros, list):
                    registros = [registros]

                return registros, dados.get("paginacao", {})

            if resposta.status_code == 429:
                time.sleep(ESPERA_429)
                ultimo_request = 0
                continue

            if resposta.status_code in (403, 404):
                try:
                    mensagem = str(
                        resposta.json()
                        .get("erro", {})
                        .get("mensagem", "")
                    )
                except Exception:
                    mensagem = ""

                if "Nenhum registro" in mensagem:
                    return None, {}

                resposta.raise_for_status()

            if resposta.status_code >= 500:
                time.sleep(
                    min(60, 5 * 2 ** (tentativa - 1))
                )
                continue

            resposta.raise_for_status()

        except requests.exceptions.RequestException:
            if tentativa == MAX_TENTATIVAS:
                raise

            time.sleep(
                min(60, 5 * 2 ** (tentativa - 1))
            )

    raise RuntimeError(
        f"Falha ao obter a página {pagina}"
    )


def buscar_agenda():
    pagina = 1
    registros_finais = []
    paginas_antigas = 0
    total_paginas = None

    while True:
        registros, paginacao = buscar_pagina(pagina)

        if registros is None:
            break

        if total_paginas is None:
            total_paginas = paginacao.get(
                "total_de_paginas"
            )

            retornados = paginacao.get(
                "total_por_pagina"
            )

            if (
                retornados
                and int(retornados) != LINHAS_POR_PAGINA
            ):
                raise RuntimeError(
                    "A API não respeitou a quantidade "
                    "de registros por página."
                )

        datas_pagina = []

        for registro in registros:
            prazo = obter_prazo(registro)

            if prazo is None:
                continue

            datas_pagina.append(prazo)

            if prazo >= DATA_LIMITE:
                registros_finais.append(registro)

        if (
            datas_pagina
            and max(datas_pagina) < DATA_LIMITE
        ):
            paginas_antigas += 1
        else:
            paginas_antigas = 0

        if pagina % 20 == 0:
            print(
                f"Agenda: página {pagina} | "
                f"{len(registros_finais)} registros mantidos"
            )

        if (
            paginas_antigas
            >= PAGINAS_ANTIGAS_PARA_PARAR
        ):
            break

        if (
            total_paginas
            and pagina >= int(total_paginas)
        ):
            break

        pagina += 1

    if not registros_finais:
        raise RuntimeError(
            "Nenhum registro da Agenda foi encontrado"
        )

    return registros_finais


def criar_dataframe(registros):
    df = pd.DataFrame(registros)

    if df.empty:
        raise RuntimeError(
            "DataFrame da Agenda está vazio"
        )

    df.columns = normalizar_colunas(df.columns)

    if "prazo" in df.columns:
        datas = pd.to_datetime(
            df["prazo"],
            errors="coerce",
        )

        df = df.loc[
            datas >= pd.Timestamp(DATA_LIMITE)
        ].copy()

    for coluna in df.columns:
        df[coluna] = df[coluna].map(
            preparar_valor
        )

    return df


def inserir_lote(
    supabase,
    lote,
    tentativas=5,
):
    ultimo_erro = None

    for tentativa in range(1, tentativas + 1):
        try:
            (
                supabase
                .table(TABELA)
                .insert(lote)
                .execute()
            )
            return

        except Exception as erro:
            ultimo_erro = erro

            if tentativa < tentativas:
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

    registros = df.to_dict(
        orient="records"
    )

    for inicio in range(
        0,
        len(registros),
        LOTE_SUPABASE,
    ):
        lote = registros[
            inicio:inicio + LOTE_SUPABASE
        ]

        inserir_lote(
            supabase,
            lote,
        )


def main():
    registros = buscar_agenda()
    df = criar_dataframe(registros)

    enviar_supabase(df)

    print(
        f"Agenda atualizada: "
        f"{len(df)} registros"
    )


if __name__ == "__main__":
    main()