import os
import re
import json
import time
import base64
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
MAX_TENTATIVAS_SUPABASE = 5


def env(nome):
    valor = os.getenv(nome)

    if not valor:
        raise RuntimeError(
            f"Variável de ambiente ausente: {nome}"
        )

    return valor


def subtrair_meses(data, meses):
    total = data.year * 12 + data.month - 1 - meses
    return datetime(
        total // 12,
        total % 12 + 1,
        1,
    )


DATA_LIMITE = subtrair_meses(
    datetime(
        datetime.now().year,
        datetime.now().month,
        1,
    ),
    13,
)


USERNAME = env("IILEX_USERNAME")
PASSWORD = env("IILEX_PASSWORD")

credenciais = base64.b64encode(
    f"{USERNAME}:{PASSWORD}".encode()
).decode()


session = requests.Session()

session.headers.update(
    {
        "Authorization": f"Basic {credenciais}",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
    }
)


def normalizar_nome_campo(nome):
    nome = str(nome)

    nome = unicodedata.normalize(
        "NFKD",
        nome,
    )

    nome = (
        nome
        .encode("ASCII", "ignore")
        .decode("ASCII")
    )

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

    if nome and not nome[0].isalpha():
        nome = "col_" + nome

    return (nome or "column").lower()[:100]


def normalizar_colunas(colunas):
    usadas = {}
    resultado = []

    for coluna in colunas:
        base = normalizar_nome_campo(coluna)

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


def converter_data(valor):
    if valor is None:
        return None

    if isinstance(valor, str):
        valor = valor.strip()

        if not valor:
            return None

        try:
            dt = datetime.fromisoformat(
                valor.replace(
                    "Z",
                    "+00:00",
                )
            )

            if dt.tzinfo:
                dt = dt.replace(
                    tzinfo=None
                )

            return dt

        except ValueError:
            pass

        for formato in (
            "%Y-%m-%d",
            "%d/%m/%Y",
            "%d-%m-%Y",
            "%Y-%m-%d %H:%M:%S",
            "%d/%m/%Y %H:%M:%S",
        ):
            try:
                return datetime.strptime(
                    valor,
                    formato,
                )

            except ValueError:
                pass

    if isinstance(
        valor,
        (int, float),
    ):
        try:
            if valor > 1e10:
                valor /= 1000

            return datetime.fromtimestamp(
                valor
            )

        except Exception:
            pass

    return None


def obter_prazo(registro):
    for campo in (
        "Prazo",
        "prazo",
        "PRAZO",
    ):
        valor = registro.get(campo)

        if valor:
            return converter_data(
                valor
            )

    return None


ultimo_request = 0.0


def esperar_intervalo():
    global ultimo_request

    agora = time.monotonic()

    if ultimo_request:
        restante = (
            INTERVALO
            - (
                agora
                - ultimo_request
            )
        )

        if restante > 0:
            time.sleep(restante)

    ultimo_request = time.monotonic()


def buscar_pagina(pagina):
    global ultimo_request

    params = {
        "idmodulo": MODULO_AGENDA,
        "pagina": pagina,
        "linhas": LINHAS_POR_PAGINA,
    }

    tentativa = 1

    while tentativa <= MAX_TENTATIVAS:
        esperar_intervalo()

        try:
            response = session.get(
                f"{BASE_URL}/api/public/v1/dados",
                params=params,
                timeout=45,
            )

            if response.status_code == 200:
                data = response.json()

                registros = (
                    data
                    .get(
                        "registros",
                        {},
                    )
                    .get(
                        "registro",
                        [],
                    )
                )

                if not registros:
                    return (
                        None,
                        data.get(
                            "paginacao",
                            {},
                        ),
                    )

                if not isinstance(
                    registros,
                    list,
                ):
                    registros = [
                        registros
                    ]

                return (
                    registros,
                    data.get(
                        "paginacao",
                        {},
                    ),
                )

            if response.status_code == 429:
                print(
                    f"Rate limit na página "
                    f"{pagina}. "
                    f"Aguardando "
                    f"{ESPERA_429}s."
                )

                time.sleep(
                    ESPERA_429
                )

                ultimo_request = 0

                continue

            if response.status_code in (
                403,
                404,
            ):
                try:
                    erro = (
                        response
                        .json()
                        .get(
                            "erro",
                            {},
                        )
                    )

                    mensagem = str(
                        erro.get(
                            "mensagem",
                            "",
                        )
                    )

                except Exception:
                    mensagem = ""

                if (
                    "Nenhum registro"
                    in mensagem
                ):
                    return None, {}

                raise RuntimeError(
                    f"HTTP "
                    f"{response.status_code}: "
                    f"{response.text}"
                )

            if response.status_code >= 500:
                espera = min(
                    60,
                    5
                    * 2
                    ** (
                        tentativa - 1
                    ),
                )

                print(
                    f"HTTP "
                    f"{response.status_code} "
                    f"na página {pagina}. "
                    f"Nova tentativa em "
                    f"{espera}s."
                )

                time.sleep(
                    espera
                )

                tentativa += 1

                continue

            raise RuntimeError(
                f"HTTP "
                f"{response.status_code}: "
                f"{response.text}"
            )

        except requests.exceptions.Timeout:
            espera = min(
                60,
                5
                * 2
                ** (
                    tentativa - 1
                ),
            )

            print(
                f"Timeout na página "
                f"{pagina}. "
                f"Nova tentativa em "
                f"{espera}s."
            )

            time.sleep(
                espera
            )

            tentativa += 1

        except requests.exceptions.RequestException as erro:
            espera = min(
                60,
                5
                * 2
                ** (
                    tentativa - 1
                ),
            )

            print(
                f"Erro na página "
                f"{pagina}: "
                f"{erro}. "
                f"Nova tentativa em "
                f"{espera}s."
            )

            time.sleep(
                espera
            )

            tentativa += 1

    raise RuntimeError(
        f"Falha ao obter a página "
        f"{pagina} após "
        f"{MAX_TENTATIVAS} "
        f"tentativas."
    )


def buscar_agenda():
    pagina = 1
    registros_finais = []

    paginas_antigas = 0
    total_baixado = 0
    total_api = None
    total_paginas = None

    inicio = time.time()

    print(
        f"Agenda: início | "
        f"corte "
        f"{DATA_LIMITE.strftime('%d/%m/%Y')}"
    )

    while True:
        registros, paginacao = (
            buscar_pagina(
                pagina
            )
        )

        if registros is None:
            break

        if total_api is None:
            total_api = (
                paginacao.get(
                    "total"
                )
            )

            total_paginas = (
                paginacao.get(
                    "total_de_paginas"
                )
            )

            retornados = (
                paginacao.get(
                    "total_por_pagina"
                )
            )

            if (
                retornados
                and int(retornados)
                != LINHAS_POR_PAGINA
            ):
                raise RuntimeError(
                    f"A API não aplicou "
                    f"linhas="
                    f"{LINHAS_POR_PAGINA}. "
                    f"Retornou "
                    f"{retornados} "
                    f"registros por página."
                )

        total_baixado += len(
            registros
        )

        datas_pagina = []

        for registro in registros:
            prazo = obter_prazo(
                registro
            )

            if prazo is None:
                continue

            datas_pagina.append(
                prazo
            )

            if prazo >= DATA_LIMITE:
                registros_finais.append(
                    registro
                )

        if (
            datas_pagina
            and max(
                datas_pagina
            )
            < DATA_LIMITE
        ):
            paginas_antigas += 1

        else:
            paginas_antigas = 0

        if (
            pagina == 1
            or pagina % 20 == 0
        ):
            decorrido = (
                time.time()
                - inicio
            ) / 60

            if datas_pagina:
                menor = min(
                    datas_pagina
                ).strftime(
                    "%d/%m/%Y"
                )

                maior = max(
                    datas_pagina
                ).strftime(
                    "%d/%m/%Y"
                )

                faixa = (
                    f"{menor} "
                    f"a {maior}"
                )

            else:
                faixa = (
                    "sem datas"
                )

            print(
                f"Página {pagina} | "
                f"Baixados: "
                f"{total_baixado:,} | "
                f"Mantidos: "
                f"{len(registros_finais):,} | "
                f"Prazo: {faixa} | "
                f"{decorrido:.1f} min"
            )

        if (
            paginas_antigas
            >= PAGINAS_ANTIGAS_PARA_PARAR
        ):
            break

        if (
            total_paginas
            and pagina
            >= int(
                total_paginas
            )
        ):
            break

        pagina += 1

    if not registros_finais:
        raise RuntimeError(
            "Nenhum registro "
            "da Agenda foi encontrado"
        )

    print(
        f"Agenda capturada: "
        f"{len(registros_finais):,} "
        f"registros"
    )

    return registros_finais


def preparar_dataframe(
    registros,
):
    df = pd.DataFrame(
        registros
    )

    if df.empty:
        raise RuntimeError(
            "Nenhum registro encontrado."
        )

    df.columns = normalizar_colunas(
        df.columns
    )

    if "prazo" in df.columns:
        datas = pd.to_datetime(
            df["prazo"],
            errors="coerce",
        )

        df = df.loc[
            datas
            >= pd.Timestamp(
                DATA_LIMITE
            )
        ].copy()

    if df.empty:
        raise RuntimeError(
            "Nenhum registro permaneceu "
            "após o filtro da Agenda."
        )

    return df


def preparar_valor_supabase(
    valor,
):
    if valor is None:
        return None

    if isinstance(
        valor,
        (dict, list),
    ):
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
    except (
        TypeError,
        ValueError,
    ):
        pass

    return str(valor)


def preparar_dataframe_supabase(
    df,
):
    df = df.copy()

    for coluna in df.columns:
        df[coluna] = (
            df[coluna]
            .map(
                preparar_valor_supabase
            )
        )

    return df


def inserir_lote(
    supabase,
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
                .table(TABELA)
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


def validar_supabase(
    supabase,
    quantidade_esperada,
):
    resposta = (
        supabase
        .table(TABELA)
        .select(
            "*",
            count="exact",
        )
        .limit(1)
        .execute()
    )

    quantidade_supabase = (
        resposta.count
    )

    if quantidade_supabase is None:
        return

    if (
        quantidade_supabase
        != quantidade_esperada
    ):
        raise RuntimeError(
            f"Validação da Agenda falhou. "
            f"DataFrame: "
            f"{quantidade_esperada:,} | "
            f"Supabase: "
            f"{quantidade_supabase:,}"
        )


def enviar_supabase(df):
    supabase = create_client(
        env("SUPABASE_URL"),
        env(
            "SUPABASE_SERVICE_ROLE_KEY"
        ),
    )

    df = (
        preparar_dataframe_supabase(
            df
        )
    )

    supabase.rpc(
        "preparar_carga_iilex",
        {
            "p_tabela":
                TABELA,

            "p_colunas":
                df.columns.tolist(),
        },
    ).execute()

    time.sleep(2)

    registros = df.to_dict(
        orient="records"
    )

    total = len(
        registros
    )

    for inicio in range(
        0,
        total,
        LOTE_SUPABASE,
    ):
        lote = registros[
            inicio:
            inicio
            + LOTE_SUPABASE
        ]

        inserir_lote(
            supabase,
            lote,
        )

        enviados = min(
            inicio
            + LOTE_SUPABASE,
            total,
        )

        if (
            enviados == total
            or enviados % 5000 == 0
        ):
            print(
                f"Supabase Agenda: "
                f"{enviados:,}/"
                f"{total:,}"
            )

    validar_supabase(
        supabase,
        total,
    )


def main():
    registros = buscar_agenda()

    df = preparar_dataframe(
        registros
    )

    enviar_supabase(
        df
    )

    print(
        f"Agenda atualizada: "
        f"{len(df):,} registros"
    )


if __name__ == "__main__":
    main()