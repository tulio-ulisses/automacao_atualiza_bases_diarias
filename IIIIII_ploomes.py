import os
import io
import re
import json
import base64
import time
import unicodedata

import pandas as pd

from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from supabase import create_client


# ============================================================
# CONFIGURAÇÃO
# ============================================================

load_dotenv()

# Mesma pasta do Google Drive usada por IIIIIII_sij.py.
# Localiza o XLSX pelo nome a cada execucao; nao depende do ID do arquivo.
PASTA_PLOOMES_ID = "17c_h5S5DRNhjbFDab8mRCf4gGHoidWIj"
NOME_ARQUIVO_PLOOMES = "base_ploomes.xlsx"

TABELA_SUPABASE = "ploomes"

# Se ficar vazio, pega a PRIMEIRA aba do arquivo.
# Depois do primeiro teste veremos se é a aba correta.
ABA_PLOOMES = os.getenv(
    "PLOOMES_ABA",
    "",
).strip()

LOTE_SUPABASE = int(
    os.getenv(
        "PLOOMES_LOTE_SUPABASE",
        "200",
    )
)

MAX_TENTATIVAS = 4

MIME_XLSX = (
    "application/"
    "vnd.openxmlformats-officedocument."
    "spreadsheetml.sheet"
)

SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly"
]


# ============================================================
# ENV
# ============================================================

def env(*nomes):
    for nome in nomes:
        valor = os.getenv(nome)

        if valor:
            return valor

    raise RuntimeError(
        "Variável de ambiente não encontrada: "
        + " / ".join(nomes)
    )


# ============================================================
# GOOGLE
# ============================================================

def carregar_credencial_google():
    """
    Aceita:
    - GOOGLE_CREDENTIALS_JSON no GitHub Actions
    - google_credentials_json
    - GOOGLE_APPLICATION_CREDENTIALS apontando para arquivo local
    """

    conteudo = (
        os.getenv("GOOGLE_CREDENTIALS_JSON")
        or os.getenv("google_credentials_json")
    )

    if conteudo:
        try:
            info = json.loads(conteudo)

        except json.JSONDecodeError:
            # Também aceita secret em Base64.
            try:
                decodificado = base64.b64decode(
                    conteudo
                ).decode("utf-8")

                info = json.loads(
                    decodificado
                )

            except Exception as erro:
                raise RuntimeError(
                    "GOOGLE_CREDENTIALS_JSON não é "
                    "um JSON válido nem Base64 válido."
                ) from erro

        print(
            "Conta de serviço Google:",
            info.get(
                "client_email",
                "não identificada",
            ),
        )

        return (
            service_account
            .Credentials
            .from_service_account_info(
                info,
                scopes=SCOPES,
            )
        )

    caminho = os.getenv(
        "GOOGLE_APPLICATION_CREDENTIALS"
    )

    if caminho and os.path.exists(caminho):
        return (
            service_account
            .Credentials
            .from_service_account_file(
                caminho,
                scopes=SCOPES,
            )
        )

    raise RuntimeError(
        "Credencial Google não encontrada. "
        "Configure GOOGLE_CREDENTIALS_JSON "
        "ou GOOGLE_APPLICATION_CREDENTIALS."
    )


def criar_drive():
    credenciais = (
        carregar_credencial_google()
    )

    return build(
        "drive",
        "v3",
        credentials=credenciais,
        cache_discovery=False,
    )


def localizar_arquivo_ploomes(drive):
    """Localiza exatamente um base_ploomes.xlsx na pasta usada pela SIJ."""
    arquivos = []
    pagina = None

    while True:
        try:
            resposta = drive.files().list(
                q=(
                    f"'{PASTA_PLOOMES_ID}' in parents "
                    f"and name = '{NOME_ARQUIVO_PLOOMES}' "
                    "and trashed = false"
                ),
                fields=(
                    "nextPageToken,"
                    "files(id,name,mimeType,modifiedTime)"
                ),
                pageSize=100,
                pageToken=pagina,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
        except Exception as erro:
            raise RuntimeError(
                "Nao foi possivel consultar a pasta da Ploomes "
                "no Google Drive. Confira o acesso da conta de servico "
                f"a pasta {PASTA_PLOOMES_ID}."
            ) from erro

        arquivos.extend(resposta.get("files", []))
        pagina = resposta.get("nextPageToken")
        if not pagina:
            break

    if len(arquivos) != 1:
        raise RuntimeError(
            f"Esperado exatamente 1 arquivo '{NOME_ARQUIVO_PLOOMES}' "
            f"na pasta {PASTA_PLOOMES_ID}. "
            f"Encontrados: {len(arquivos)}. "
            "Carga cancelada para evitar substituir a tabela "
            "por um arquivo errado."
        )

    arquivo = arquivos[0]
    if arquivo.get("mimeType") != MIME_XLSX:
        raise RuntimeError(
            f"'{NOME_ARQUIVO_PLOOMES}' foi encontrado, mas nao "
            "e um arquivo Excel XLSX. Carga cancelada."
        )

    print()
    print("Arquivo Ploomes encontrado")
    print("------------------------------")
    print("Nome:", arquivo["name"])
    print("ID atual:", arquivo["id"])
    print("MIME:", arquivo["mimeType"])
    print()
    return arquivo


def baixar_arquivo_ploomes():
    drive = criar_drive()
    arquivo = localizar_arquivo_ploomes(drive)

    try:
        request = drive.files().get_media(fileId=arquivo["id"])
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(
            buffer,
            request,
            chunksize=10 * 1024 * 1024,
        )

        concluido = False
        while not concluido:
            status, concluido = downloader.next_chunk()
            if status:
                print("Download:", f"{status.progress() * 100:.1f}%")
    except Exception as erro:
        raise RuntimeError(
            f"Falha ao baixar '{NOME_ARQUIVO_PLOOMES}' "
            f"(ID atual: {arquivo['id']})."
        ) from erro

    if buffer.getbuffer().nbytes == 0:
        raise RuntimeError(
            f"O download de '{NOME_ARQUIVO_PLOOMES}' veio vazio. "
            "Carga cancelada."
        )

    buffer.seek(0)
    return buffer


# ============================================================
# DATAFRAME
# ============================================================

def normalizar_nome_coluna(valor):
    valor = str(valor).strip()

    valor = unicodedata.normalize(
        "NFKD",
        valor,
    )

    valor = (
        valor
        .encode(
            "ascii",
            "ignore",
        )
        .decode("ascii")
    )

    valor = valor.lower()

    valor = re.sub(
        r"[^a-z0-9]+",
        "_",
        valor,
    )

    valor = valor.strip("_")

    if not valor:
        valor = "coluna"

    # PostgreSQL limita identificadores.
    return valor[:55]


def normalizar_colunas(colunas):
    resultado = []
    usados = {}

    for indice, coluna in enumerate(
        colunas,
        start=1,
    ):
        base = normalizar_nome_coluna(
            coluna
        )

        if base == "coluna":
            base = f"coluna_{indice}"

        numero = usados.get(
            base,
            0,
        )

        usados[base] = numero + 1

        if numero == 0:
            nome = base

        else:
            sufixo = f"_{numero + 1}"

            nome = (
                base[
                    : 55 - len(sufixo)
                ]
                + sufixo
            )

        resultado.append(nome)

    return resultado


def ler_ploomes():
    arquivo = baixar_arquivo_ploomes()

    excel = pd.ExcelFile(
        arquivo
    )

    print(
        "Abas encontradas:",
        excel.sheet_names,
    )

    if not excel.sheet_names:
        raise RuntimeError(
            "O arquivo não possui nenhuma aba."
        )

    if ABA_PLOOMES:

        if ABA_PLOOMES not in excel.sheet_names:
            raise RuntimeError(
                f"A aba '{ABA_PLOOMES}' não existe. "
                f"Disponíveis: {excel.sheet_names}"
            )

        aba = ABA_PLOOMES

    else:
        aba = excel.sheet_names[0]

    print(
        "Aba utilizada:",
        aba,
    )

    df = pd.read_excel(
        excel,
        sheet_name=aba,
        dtype=str,
    )

    # Remove linhas 100% vazias.
    df = df.dropna(
        how="all"
    )

    # Remove colunas 100% vazias.
    df = df.dropna(
        axis=1,
        how="all",
    )

    if df.empty:
        raise RuntimeError(
            "A planilha Ploomes está vazia."
        )

    colunas_originais = list(
        df.columns
    )

    colunas_novas = (
        normalizar_colunas(
            colunas_originais
        )
    )

    df.columns = colunas_novas

    print()
    print("Colunas")
    print("------------------------------")

    for antiga, nova in zip(
        colunas_originais,
        colunas_novas,
    ):
        print(
            f"{antiga} -> {nova}"
        )

    # Substitui NaN por None.
    df = df.astype(object)

    df = df.where(
        pd.notna(df),
        None,
    )

    # Tudo vai para o Supabase como texto.
    for coluna in df.columns:
        df[coluna] = df[coluna].map(
            lambda x: (
                None
                if x is None
                else str(x).strip()
            )
        )

    print()
    print(
        "Registros encontrados:",
        f"{len(df):,}",
    )

    print(
        "Quantidade de colunas:",
        len(df.columns),
    )

    return df


# ============================================================
# SUPABASE
# ============================================================

def criar_supabase():
    return create_client(
        env(
            "SUPABASE_URL",
            "supabase_url",
        ),
        env(
            "SUPABASE_SERVICE_ROLE_KEY",
            "supabase_service_role_key",
        ),
    )


def preparar_tabela(
    supabase,
    df,
):
    print()
    print(
        "Preparando public.ploomes..."
    )

    (
        supabase
        .rpc(
            "preparar_carga_ploomes",
            {
                "p_colunas":
                    df.columns.tolist()
            },
        )
        .execute()
    )


def inserir_lote(
    supabase,
    lote,
):
    ultimo_erro = None

    for tentativa in range(
        1,
        MAX_TENTATIVAS + 1,
    ):
        try:
            (
                supabase
                .table(
                    TABELA_SUPABASE
                )
                .insert(
                    lote
                )
                .execute()
            )

            return

        except Exception as erro:
            ultimo_erro = erro

            print(
                f"Tentativa {tentativa}/"
                f"{MAX_TENTATIVAS} falhou:"
            )

            print(
                str(erro)[:1000]
            )

            if tentativa < MAX_TENTATIVAS:
                time.sleep(
                    tentativa * 3
                )

    raise ultimo_erro


def validar_carga(
    supabase,
    total_esperado,
):
    resposta = (
        supabase
        .table(
            TABELA_SUPABASE
        )
        .select(
            "*",
            count="exact",
        )
        .limit(1)
        .execute()
    )

    total_supabase = (
        resposta.count
    )

    if total_supabase is None:
        raise RuntimeError(
            "Não foi possível validar "
            "a quantidade no Supabase."
        )

    if total_supabase != total_esperado:
        raise RuntimeError(
            "Validação da Ploomes falhou. "
            f"Planilha: {total_esperado:,} | "
            f"Supabase: {total_supabase:,}"
        )

    print()
    print(
        "Validação OK:",
        f"{total_supabase:,}",
        "registros",
    )


def enviar_supabase(df):
    supabase = criar_supabase()

    preparar_tabela(
        supabase,
        df,
    )

    registros = df.to_dict(
        orient="records"
    )

    total = len(
        registros
    )

    print()
    print(
        "Enviando ao Supabase..."
    )

    for inicio in range(
        0,
        total,
        LOTE_SUPABASE,
    ):
        fim = min(
            inicio + LOTE_SUPABASE,
            total,
        )

        lote = registros[
            inicio:fim
        ]

        inserir_lote(
            supabase,
            lote,
        )

        if (
            fim == total
            or fim % 2000 == 0
        ):
            print(
                f"Supabase Ploomes: "
                f"{fim:,}/{total:,}"
            )

    validar_carga(
        supabase,
        total,
    )


# ============================================================
# MAIN
# ============================================================

def main():
    print()
    print(
        "======================================"
    )
    print(
        "ATUALIZAÇÃO PLOOMES -> SUPABASE"
    )
    print(
        "======================================"
    )

    df = ler_ploomes()

    enviar_supabase(
        df
    )

    print()
    print(
        "======================================"
    )
    print(
        "PLOOMES ATUALIZADA COM SUCESSO"
    )
    print(
        "======================================"
    )


if __name__ == "__main__":
    main()