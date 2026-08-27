import os
import re
import json
import time
import datetime
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from supabase import create_client


LOY_BASE_URL = "https://api.loylegal.com/v1"
LOY_INTERMEDIARIAS_URL = "https://web.2adv.com.br/api"

DATABASE_LOY = "20616170000102"

TABELA_CONTENCIOSO = "contencioso"
TABELA_LOY = "peticoes_loy"

LOY_MAX_WORKERS = 8
LOY_INTERVALO = 0.25
LOY_MAX_TENTATIVAS = 5

LOTE_LEITURA_SUPABASE = 1000
LOTE_ESCRITA_SUPABASE = 500

FUSO_BRASILIA = ZoneInfo(
    "America/Sao_Paulo"
)


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

        sufixo = (
            f"_{usadas[base]}"
        )

        resultado.append(
            base[
                :100 - len(sufixo)
            ]
            + sufixo
        )

    return resultado


def preparar_valor(valor):
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

    try:
        if pd.isna(valor):
            return None
    except (
        TypeError,
        ValueError,
    ):
        pass

    return str(valor)


def chave_processo(numero):
    return "".join(
        filter(
            str.isdigit,
            str(numero),
        )
    )


SUPABASE_URL = env(
    "SUPABASE_URL"
)

SUPABASE_KEY = env(
    "SUPABASE_SERVICE_ROLE_KEY"
)

LOY_TOKEN = env(
    "LOY_TOKEN_SERVICOS"
)


supabase = create_client(
    SUPABASE_URL,
    SUPABASE_KEY,
)


thread_local = threading.local()

loy_lock = threading.Lock()
loy_ultimo_request = 0.0


def get_session_loy():
    if not hasattr(
        thread_local,
        "session_loy",
    ):
        session = requests.Session()

        session.headers.update(
            {
                "Authorization":
                    f"Bearer {LOY_TOKEN}",
                "Accept":
                    "application/json",
                "User-Agent":
                    "Mozilla/5.0",
                "Connection":
                    "keep-alive",
            }
        )

        thread_local.session_loy = (
            session
        )

    return (
        thread_local.session_loy
    )


def esperar_loy():
    global loy_ultimo_request

    with loy_lock:
        agora = time.monotonic()

        restante = (
            LOY_INTERVALO
            - (
                agora
                - loy_ultimo_request
            )
        )

        if restante > 0:
            time.sleep(
                restante
            )

        loy_ultimo_request = (
            time.monotonic()
        )


def requisicao_loy(
    url,
    timeout=30,
):
    ultimo_erro = None

    for tentativa in range(
        1,
        LOY_MAX_TENTATIVAS + 1,
    ):
        esperar_loy()

        try:
            resposta = (
                get_session_loy()
                .get(
                    url,
                    timeout=timeout,
                )
            )

        except (
            requests.exceptions
            .RequestException
        ) as erro:
            ultimo_erro = erro

            if (
                tentativa
                == LOY_MAX_TENTATIVAS
            ):
                raise

            espera = min(
                60,
                5
                * 2
                ** (
                    tentativa - 1
                ),
            )

            time.sleep(
                espera
            )

            continue

        if resposta.status_code in (
            200,
            404,
        ):
            return resposta

        if (
            resposta.status_code
            == 429
        ):
            retry_after = (
                resposta.headers
                .get("Retry-After")
            )

            try:
                espera = int(
                    retry_after
                )
            except (
                TypeError,
                ValueError,
            ):
                espera = 60

            time.sleep(
                max(
                    espera,
                    30,
                )
            )

            continue

        if (
            resposta.status_code
            >= 500
        ):
            espera = min(
                60,
                5
                * 2
                ** (
                    tentativa - 1
                ),
            )

            time.sleep(
                espera
            )

            continue

        if resposta.status_code in (
            401,
            403,
        ):
            raise RuntimeError(
                f"LOY retornou HTTP "
                f"{resposta.status_code}. "
                f"Execução interrompida."
            )

        resposta.raise_for_status()

    if ultimo_erro:
        raise ultimo_erro

    raise RuntimeError(
        "Falha persistente "
        "na API LOY"
    )


def ler_tabela_supabase(
    tabela,
    colunas,
):
    registros = []
    inicio = 0

    selecao = ",".join(
        colunas
    )

    while True:
        fim = (
            inicio
            + LOTE_LEITURA_SUPABASE
            - 1
        )

        resposta = (
            supabase
            .table(tabela)
            .select(selecao)
            .range(
                inicio,
                fim,
            )
            .execute()
        )

        lote = (
            resposta.data
            or []
        )

        registros.extend(
            lote
        )

        if (
            len(lote)
            < LOTE_LEITURA_SUPABASE
        ):
            break

        inicio += (
            LOTE_LEITURA_SUPABASE
        )

    return registros


def buscar_processos_contencioso():
    registros = (
        ler_tabela_supabase(
            TABELA_CONTENCIOSO,
            ["processo"],
        )
    )

    processos = []

    vistos = set()

    for registro in registros:
        processo = registro.get(
            "processo"
        )

        chave = chave_processo(
            processo
        )

        if (
            not chave
            or chave in vistos
        ):
            continue

        vistos.add(
            chave
        )

        processos.append(
            processo
        )

    if not processos:
        raise RuntimeError(
            "Nenhum processo encontrado "
            "na tabela contencioso."
        )

    return processos


def carregar_cache_loy():
    try:
        registros = (
            ler_tabela_supabase(
                TABELA_LOY,
                [
                    "processo",
                    "id_processo",
                    "titulo",
                    "situacao",
                    "is_worked",
                ],
            )
        )

    except Exception:
        return {}

    cache = {}

    for registro in registros:
        processo = registro.get(
            "processo"
        )

        chave = chave_processo(
            processo
        )

        if chave:
            cache[chave] = (
                registro
            )

    return cache


def buscar_capa_processo(
    numero_processo,
):
    numero_limpo = (
        chave_processo(
            numero_processo
        )
    )

    if not numero_limpo:
        return None

    resposta = requisicao_loy(
        f"{LOY_BASE_URL}/"
        f"{DATABASE_LOY}/"
        f"process/"
        f"{numero_limpo}",
        timeout=30,
    )

    if (
        resposta.status_code
        == 404
    ):
        return None

    return (
        resposta
        .json()
        .get(
            "data",
            {},
        )
    )


def buscar_movimentacoes(
    id_processo,
):
    data_tentativa = (
        datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat()
    )

    resposta = requisicao_loy(
        f"{LOY_BASE_URL}/"
        f"{DATABASE_LOY}/"
        f"movements/"
        f"{id_processo}",
        timeout=30,
    )

    if (
        resposta.status_code
        == 404
    ):
        return {
            "status":
                "FAIL",
            "motivo":
                "HTTP 404",
            "data_tentativa":
                data_tentativa,
            "movs":
                [],
        }

    return {
        "status":
            "SUCCESS",
        "motivo":
            "",
        "data_tentativa":
            data_tentativa,
        "movs":
            (
                resposta
                .json()
                .get(
                    "data",
                    [],
                )
            ),
    }


def buscar_intermediarias_por_data(
    data_inicio,
    data_fim,
    limit=300,
):
    data_inicio_encoded = quote(
        data_inicio,
        safe="",
    )

    data_fim_encoded = quote(
        data_fim,
        safe="",
    )

    todas = []

    ids_encontrados = set()

    pagina = 1

    while True:
        url = (
            f"{LOY_INTERMEDIARIAS_URL}"
            f"/workloads?"
            f"limit={limit}&"
            f"page={pagina}&"
            f"params[kind]="
            f"Petição+Intermediária&"
            f"params[success]="
            f"Sucesso&"
            f"params[dateStart]="
            f"{data_inicio_encoded}&"
            f"params[dateEnd]="
            f"{data_fim_encoded}&"
            f"token={LOY_TOKEN}"
        )

        resposta = requisicao_loy(
            url,
            timeout=60,
        )

        if (
            resposta.status_code
            == 404
        ):
            break

        workloads = (
            resposta
            .json()
            .get(
                "workloads",
                [],
            )
        )

        if not workloads:
            break

        novos = []

        for item in workloads:
            item_id = item.get(
                "_id"
            )

            if item_id:
                if (
                    item_id
                    in ids_encontrados
                ):
                    continue

                ids_encontrados.add(
                    item_id
                )

            novos.append(
                item
            )

        if not novos:
            break

        todas.extend(
            novos
        )

        if (
            len(workloads)
            < limit
        ):
            break

        pagina += 1

    return todas


def criar_mapa_intermediarias():
    hoje = datetime.datetime.now(
        FUSO_BRASILIA
    )

    data_fim = hoje.strftime(
        "%d/%m/%Y"
    )

    data_inicio = (
        hoje
        - datetime.timedelta(
            days=60
        )
    ).strftime(
        "%d/%m/%Y"
    )

    intermediarias = (
        buscar_intermediarias_por_data(
            data_inicio,
            data_fim,
            limit=300,
        )
    )

    mapa = {}

    for intermediaria in intermediarias:
        justice = (
            intermediaria
            .get(
                "justice",
                {},
            )
            or {}
        )

        justice_id = (
            justice.get(
                "_id"
            )
        )

        if not justice_id:
            continue

        novo = {
            "intermediate_id":
                intermediaria.get(
                    "_id"
                ),

            "intermediate_situation":
                intermediaria.get(
                    "situation",
                    "",
                ),

            "intermediate_success":
                intermediaria.get(
                    "success",
                    "",
                ),

            "intermediate_message":
                intermediaria.get(
                    "message",
                    "",
                ),

            "intermediate_receipt":
                (
                    "PRESENTE"
                    if intermediaria.get(
                        "receipt"
                    )
                    else "AUSENTE"
                ),

            "intermediate_isWorked":
                intermediaria.get(
                    "isWorked",
                    "",
                ),

            "intermediate_createdAt":
                intermediaria.get(
                    "createdAt",
                    "",
                ),
        }

        atual = mapa.get(
            justice_id
        )

        if not atual:
            mapa[
                justice_id
            ] = novo
            continue

        data_atual = str(
            atual.get(
                "intermediate_createdAt",
                "",
            )
        )

        data_nova = str(
            novo.get(
                "intermediate_createdAt",
                "",
            )
        )

        if (
            data_nova
            >= data_atual
        ):
            mapa[
                justice_id
            ] = novo

    return mapa


def montar_resultado(
    numero_processo,
    id_processo,
    titulo,
    situacao,
    is_worked,
    mov_result,
    mapa_intermediarias,
):
    movimentacoes = (
        mov_result["movs"]
    )

    ultima_movimentacao = (
        max(
            movimentacoes,
            key=lambda x: x.get(
                "dateTime",
                "",
            ),
        )
        if movimentacoes
        else {}
    )

    intermediaria = (
        mapa_intermediarias.get(
            id_processo,
            {},
        )
    )

    return {
        "processo":
            numero_processo,

        "id_processo":
            id_processo,

        "titulo":
            titulo or "",

        "situacao":
            situacao or "",

        "is_worked":
            is_worked,

        "captura_status":
            mov_result[
                "status"
            ],

        "captura_motivo":
            mov_result[
                "motivo"
            ],

        "captura_data":
            mov_result[
                "data_tentativa"
            ],

        "data_ultima_movimentacao":
            ultima_movimentacao.get(
                "dateTime",
                "",
            ),

        "descricao_ultima_movimentacao":
            ultima_movimentacao.get(
                "description",
                "",
            ),

        "origem_ultima_movimentacao":
            ultima_movimentacao.get(
                "origin",
                "",
            ),

        "intermediate_id":
            intermediaria.get(
                "intermediate_id",
                "",
            ),

        "intermediate_situation":
            intermediaria.get(
                "intermediate_situation",
                "",
            ),

        "intermediate_success":
            intermediaria.get(
                "intermediate_success",
                "",
            ),

        "intermediate_message":
            intermediaria.get(
                "intermediate_message",
                "",
            ),

        "intermediate_receipt":
            intermediaria.get(
                "intermediate_receipt",
                "",
            ),

        "intermediate_isworked":
            intermediaria.get(
                "intermediate_isWorked",
                "",
            ),

        "intermediate_createdat":
            intermediaria.get(
                "intermediate_createdAt",
                "",
            ),
    }


def processar_processo(
    numero_processo,
    cache,
    mapa_intermediarias,
    atualizar_capa,
):
    chave = chave_processo(
        numero_processo
    )

    registro_cache = (
        cache.get(
            chave,
            {},
        )
    )

    id_cache = (
        registro_cache
        .get(
            "id_processo"
        )
    )

    usar_cache = (
        bool(id_cache)
        and not atualizar_capa
    )

    if usar_cache:
        id_processo = (
            id_cache
        )

        titulo = (
            registro_cache
            .get(
                "titulo",
                "",
            )
        )

        situacao = (
            registro_cache
            .get(
                "situacao",
                "",
            )
        )

        is_worked = (
            registro_cache
            .get(
                "is_worked",
                "",
            )
        )

        mov_result = (
            buscar_movimentacoes(
                id_processo
            )
        )

        if (
            mov_result[
                "motivo"
            ]
            != "HTTP 404"
        ):
            return montar_resultado(
                numero_processo,
                id_processo,
                titulo,
                situacao,
                is_worked,
                mov_result,
                mapa_intermediarias,
            )

    capa = buscar_capa_processo(
        numero_processo
    )

    if not capa:
        return None

    id_processo = (
        capa.get("_id")
    )

    if not id_processo:
        return None

    mov_result = (
        buscar_movimentacoes(
            id_processo
        )
    )

    return montar_resultado(
        numero_processo,
        id_processo,
        capa.get(
            "title",
            "",
        ),
        capa.get(
            "situation",
            "",
        ),
        capa.get(
            "isWorked",
            False,
        ),
        mov_result,
        mapa_intermediarias,
    )


def criar_dataframe(
    resultados,
):
    if not resultados:
        raise RuntimeError(
            "Nenhum dado do LOY "
            "foi processado."
        )

    df = pd.DataFrame(
        resultados
    )

    df.columns = (
        normalizar_colunas(
            df.columns
        )
    )

    df = (
        df
        .drop_duplicates(
            subset=[
                "processo"
            ],
            keep="last",
        )
        .reset_index(
            drop=True
        )
    )

    df.insert(
        0,
        "id_unico",
        range(
            1,
            len(df) + 1,
        ),
    )

    for coluna in df.columns:
        df[coluna] = (
            df[coluna]
            .map(
                preparar_valor
            )
        )

    return df


def inserir_lote(
    lote,
    tentativas=5,
):
    ultimo_erro = None

    for tentativa in range(
        1,
        tentativas + 1,
    ):
        try:
            (
                supabase
                .table(
                    TABELA_LOY
                )
                .insert(
                    lote
                )
                .execute()
            )

            return

        except Exception as erro:
            ultimo_erro = erro

            if (
                tentativa
                < tentativas
            ):
                time.sleep(
                    min(
                        30,
                        tentativa * 3,
                    )
                )

    raise ultimo_erro


def validar_quantidade(
    esperado,
):
    resposta = (
        supabase
        .table(
            TABELA_LOY
        )
        .select(
            "*",
            count="exact",
        )
        .limit(1)
        .execute()
    )

    encontrado = (
        resposta.count
    )

    if encontrado is None:
        return

    if (
        encontrado
        != esperado
    ):
        raise RuntimeError(
            f"Validação LOY falhou. "
            f"Esperado: "
            f"{esperado:,} | "
            f"Supabase: "
            f"{encontrado:,}"
        )


def enviar_supabase(df):
    supabase.rpc(
        "preparar_carga_iilex",
        {
            "p_tabela":
                TABELA_LOY,

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
        LOTE_ESCRITA_SUPABASE,
    ):
        lote = registros[
            inicio:
            inicio
            + LOTE_ESCRITA_SUPABASE
        ]

        inserir_lote(
            lote
        )

        enviados = min(
            inicio
            + LOTE_ESCRITA_SUPABASE,
            total,
        )

        if (
            enviados == total
            or enviados % 5000 == 0
        ):
            print(
                f"Supabase LOY: "
                f"{enviados:,}/"
                f"{total:,}"
            )

    validar_quantidade(
        total
    )


def executar_processos(
    processos,
    cache,
    mapa_intermediarias,
    atualizar_capa,
):
    resultados = []
    falhas = []

    total = len(
        processos
    )

    inicio = time.time()

    with ThreadPoolExecutor(
        max_workers=LOY_MAX_WORKERS
    ) as executor:

        futures = {
            executor.submit(
                processar_processo,
                processo,
                cache,
                mapa_intermediarias,
                atualizar_capa,
            ): processo
            for processo in processos
        }

        concluidos = 0

        for future in as_completed(
            futures
        ):
            processo = (
                futures[
                    future
                ]
            )

            try:
                resultado = (
                    future.result()
                )

                if resultado:
                    resultados.append(
                        resultado
                    )

            except Exception as erro:
                falhas.append(
                    (
                        processo,
                        str(erro),
                    )
                )

            concluidos += 1

            if (
                concluidos % 500 == 0
                or concluidos == total
            ):
                minutos = (
                    time.time()
                    - inicio
                ) / 60

                print(
                    f"LOY: "
                    f"{concluidos:,}/"
                    f"{total:,} | "
                    f"{minutos:.1f} min | "
                    f"falhas: "
                    f"{len(falhas):,}"
                )

    return (
        resultados,
        falhas,
    )


def repetir_falhas(
    falhas,
    cache,
    mapa_intermediarias,
    atualizar_capa,
):
    if not falhas:
        return [], []

    print(
        f"Repetindo "
        f"{len(falhas):,} "
        f"processos com falha"
    )

    time.sleep(30)

    resultados = []
    falhas_finais = []

    processos = [
        processo
        for processo, _
        in falhas
    ]

    with ThreadPoolExecutor(
        max_workers=4
    ) as executor:

        futures = {
            executor.submit(
                processar_processo,
                processo,
                cache,
                mapa_intermediarias,
                atualizar_capa,
            ): processo
            for processo
            in processos
        }

        for future in as_completed(
            futures
        ):
            processo = (
                futures[
                    future
                ]
            )

            try:
                resultado = (
                    future.result()
                )

                if resultado:
                    resultados.append(
                        resultado
                    )

            except Exception as erro:
                falhas_finais.append(
                    (
                        processo,
                        str(erro),
                    )
                )

    return (
        resultados,
        falhas_finais,
    )


def main():
    hoje = datetime.datetime.now(
        FUSO_BRASILIA
    )

    atualizar_capa = (
        hoje.weekday() == 6
    )

    if atualizar_capa:
        print(
            "LOY: domingo - "
            "atualização completa "
            "das capas"
        )
    else:
        print(
            "LOY: modo diário rápido - "
            "reutilizando IDs existentes"
        )

    processos = (
        buscar_processos_contencioso()
    )

    cache = (
        carregar_cache_loy()
    )

    com_cache = sum(
        1
        for processo
        in processos
        if (
            cache.get(
                chave_processo(
                    processo
                ),
                {},
            ).get(
                "id_processo"
            )
        )
    )

    novos = (
        len(processos)
        - com_cache
    )

    print(
        f"Contencioso: "
        f"{len(processos):,} processos | "
        f"IDs reutilizáveis: "
        f"{com_cache:,} | "
        f"sem ID: "
        f"{novos:,}"
    )

    mapa_intermediarias = (
        criar_mapa_intermediarias()
    )

    print(
        f"Petições intermediárias: "
        f"{len(mapa_intermediarias):,} "
        f"processos"
    )

    resultados, falhas = (
        executar_processos(
            processos,
            cache,
            mapa_intermediarias,
            atualizar_capa,
        )
    )

    novos_resultados, falhas_finais = (
        repetir_falhas(
            falhas,
            cache,
            mapa_intermediarias,
            atualizar_capa,
        )
    )

    resultados.extend(
        novos_resultados
    )

    if falhas_finais:
        exemplos = "; ".join(
            f"{processo}: "
            f"{erro[:120]}"
            for processo, erro
            in falhas_finais[:5]
        )

        raise RuntimeError(
            f"{len(falhas_finais):,} "
            f"processos continuaram "
            f"com falha. "
            f"A tabela LOY não será "
            f"substituída. "
            f"Exemplos: {exemplos}"
        )

    df = criar_dataframe(
        resultados
    )

    enviar_supabase(
        df
    )

    print(
        f"LOY atualizado: "
        f"{len(df):,} registros"
    )


if __name__ == "__main__":
    main()