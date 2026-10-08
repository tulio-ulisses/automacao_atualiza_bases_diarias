import os
import io
import json
import base64
import time
from datetime import date, datetime, timedelta

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

SPREADSHEET_ID = "1r1RHLxkmTOt6uka1yE122R8Ic3WyYcec"

# IMPORTANTE:
# base_sij é a tabela física que recebe a carga.
# base_sij_tratada é uma VIEW calculada automaticamente a partir dela.
TABELA_SUPABASE = "base_sij"
VIEW_TRATADA = "base_sij_tratada"

ABA_SIJ = os.getenv("SIJ_ABA", "Negócios").strip() or "Negócios"
LOTE_SUPABASE = int(os.getenv("SIJ_LOTE_SUPABASE", "200"))
MAX_TENTATIVAS = 4
MIN_REGISTROS = int(os.getenv("SIJ_MIN_REGISTROS", "100"))

MIME_GOOGLE_SHEETS = "application/vnd.google-apps.spreadsheet"
MIME_XLSX = (
    "application/"
    "vnd.openxmlformats-officedocument."
    "spreadsheetml.sheet"
)

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# A primeira linha contém agrupadores; a segunda contém o cabeçalho real.
HEADER_EXCEL = 1

# Somente os 15 campos físicos de public.base_sij.
# Os demais campos de base_sij_tratada são calculados pela VIEW no PostgreSQL.
MAPEAMENTO = {
    "ID Negócio": "id",
    "Número do processo": "processo",
    "Nº cumprimento de sentença": "numero_cumprimento",
    "Data base da venda": "data_base_venda",
    "Data estimada de liquidação": "data_estimada_liquidacao",
    "Última data de recebimento": "data_recebimento",
    "Valor do contrato (R$)": "valor_contrato",
    "Total recebido (R$)": "valor_recebido_liquidacao",
    "Total depositado (R$)": "valor_deposito",
    "ID iiLex": "id_illex",
    "Tribunal": "tribunal",
    "Tipos de crédito": "tipo_credito",
    "Órgão": "orgao",
    "Vara": "vara",
    "Rito": "rito",
}

COLUNAS_DESTINO = [
    "id",
    "processo",
    "numero_cumprimento",
    "data_base_venda",
    "data_estimada_liquidacao",
    "data_recebimento",
    "valor_contrato",
    "valor_recebido_liquidacao",
    "valor_deposito",
    "id_illex",
    "tribunal",
    "tipo_credito",
    "orgao",
    "vara",
    "rito",
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
# GOOGLE DRIVE
# ============================================================


def carregar_credencial_google():
    """Aceita JSON, JSON em Base64 ou arquivo local de credenciais."""

    conteudo = (
        os.getenv("GOOGLE_CREDENTIALS_JSON")
        or os.getenv("google_credentials_json")
    )

    if conteudo:
        try:
            info = json.loads(conteudo)
        except json.JSONDecodeError:
            try:
                decodificado = base64.b64decode(conteudo).decode("utf-8")
                info = json.loads(decodificado)
            except Exception as erro:
                raise RuntimeError(
                    "GOOGLE_CREDENTIALS_JSON não é um JSON válido "
                    "nem Base64 válido."
                ) from erro

        print(
            "Conta de serviço Google:",
            info.get("client_email", "não identificada"),
        )

        return service_account.Credentials.from_service_account_info(
            info,
            scopes=SCOPES,
        )

    caminho = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

    if caminho and os.path.exists(caminho):
        return service_account.Credentials.from_service_account_file(
            caminho,
            scopes=SCOPES,
        )

    raise RuntimeError(
        "Credencial Google não encontrada. Configure "
        "GOOGLE_CREDENTIALS_JSON ou GOOGLE_APPLICATION_CREDENTIALS."
    )


def criar_drive():
    return build(
        "drive",
        "v3",
        credentials=carregar_credencial_google(),
        cache_discovery=False,
    )


def baixar_arquivo_sij():
    drive = criar_drive()

    try:
        metadata = (
            drive.files()
            .get(
                fileId=SPREADSHEET_ID,
                fields="id,name,mimeType,capabilities(canDownload)",
                supportsAllDrives=True,
            )
            .execute()
        )
    except Exception as erro:
        raise RuntimeError(
            "\nNão foi possível acessar o arquivo SIJ no Drive.\n"
            "Compartilhe o arquivo com o e-mail da conta de serviço "
            "mostrado acima.\n"
            f"ID: {SPREADSHEET_ID}"
        ) from erro

    nome = metadata.get("name", "SIJ")
    mime_type = metadata.get("mimeType")

    print()
    print("Arquivo encontrado")
    print("------------------------------")
    print("Nome:", nome)
    print("MIME:", mime_type)
    print()

    buffer = io.BytesIO()

    if mime_type == MIME_GOOGLE_SHEETS:
        request = drive.files().export_media(
            fileId=SPREADSHEET_ID,
            mimeType=MIME_XLSX,
        )
    else:
        request = drive.files().get_media(fileId=SPREADSHEET_ID)

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

    buffer.seek(0)
    return buffer


# ============================================================
# TRATAMENTO
# ============================================================


def vazio(valor):
    if valor is None:
        return True

    try:
        if pd.isna(valor):
            return True
    except Exception:
        pass

    if isinstance(valor, str) and valor.strip().lower() in {
        "",
        "nan",
        "none",
        "null",
        "-",
        "nat",
        "<na>",
    }:
        return True

    return False


def texto(valor):
    if vazio(valor):
        return None

    if isinstance(valor, bool):
        return str(valor)

    if isinstance(valor, int):
        return str(valor)

    if isinstance(valor, float) and valor.is_integer():
        return str(int(valor))

    resultado = str(valor).strip()
    return resultado or None


def texto_data(valor):
    """Entrega data em texto pt-BR, formato aceito por parse_data_ptbr."""

    if vazio(valor):
        return None

    if isinstance(valor, pd.Timestamp):
        return valor.strftime("%d/%m/%Y")

    if isinstance(valor, datetime):
        return valor.strftime("%d/%m/%Y")

    if isinstance(valor, date):
        return valor.strftime("%d/%m/%Y")

    # Serial numérico do Excel.
    if isinstance(valor, (int, float)) and not isinstance(valor, bool):
        numero = float(valor)
        if 1 <= numero <= 100000:
            data_excel = datetime(1899, 12, 30) + timedelta(days=numero)
            return data_excel.strftime("%d/%m/%Y")

    # Se o Excel trouxe a data como texto, preservamos o formato original.
    return texto(valor)


def numero_para_float(valor):
    if vazio(valor):
        return None

    if isinstance(valor, bool):
        return float(valor)

    if isinstance(valor, (int, float)):
        numero = float(valor)
        return None if pd.isna(numero) else numero

    s = str(valor).strip()
    s = s.replace("R$", "").replace(" ", "")

    if not s or s == "-":
        return None

    # 1.234,56 -> 1234.56
    # 1,234.56 -> 1234.56
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(".", "").replace(",", ".")

    s = "".join(ch for ch in s if ch in "0123456789+-.eE")

    if s in {"", "+", "-", ".", "+.", "-."}:
        return None

    try:
        return float(s)
    except ValueError:
        return None


def texto_numero(valor):
    """Normaliza números para texto pt-BR sem separador de milhar."""

    numero = numero_para_float(valor)
    if numero is None:
        return None

    s = f"{numero:.10f}".rstrip("0").rstrip(".")
    if s == "-0":
        s = "0"

    return s.replace(".", ",")


def ler_sij():
    arquivo = baixar_arquivo_sij()
    excel = pd.ExcelFile(arquivo)

    print("Abas encontradas:", excel.sheet_names)

    if ABA_SIJ not in excel.sheet_names:
        raise RuntimeError(
            f"A aba '{ABA_SIJ}' não existe. "
            f"Disponíveis: {excel.sheet_names}"
        )

    print("Aba utilizada:", ABA_SIJ)

    df = pd.read_excel(
        excel,
        sheet_name=ABA_SIJ,
        header=HEADER_EXCEL,
        dtype=object,
    )

    df = df.dropna(how="all")

    faltantes = [
        coluna
        for coluna in MAPEAMENTO
        if coluna not in df.columns
    ]

    if faltantes:
        raise RuntimeError(
            "Colunas obrigatórias ausentes no arquivo SIJ: "
            + ", ".join(faltantes)
        )

    # Mantém exclusivamente os 15 campos físicos usados por public.base_sij.
    df = df[list(MAPEAMENTO.keys())].copy()
    df = df.rename(columns=MAPEAMENTO)

    # IDs e textos.
    for coluna in [
        "id",
        "processo",
        "numero_cumprimento",
        "id_illex",
        "tribunal",
        "tipo_credito",
        "orgao",
        "vara",
        "rito",
    ]:
        df[coluna] = df[coluna].map(texto)

    # As três datas permanecem TEXT na base_sij.
    # A view base_sij_tratada chama parse_data_ptbr() sobre elas.
    for coluna in [
        "data_base_venda",
        "data_estimada_liquidacao",
        "data_recebimento",
    ]:
        df[coluna] = df[coluna].map(texto_data)

    # Os três valores também permanecem TEXT na base_sij.
    # A view chama parse_numero() e calcula receita = contrato - depósito.
    for coluna in [
        "valor_contrato",
        "valor_recebido_liquidacao",
        "valor_deposito",
    ]:
        df[coluna] = df[coluna].map(texto_numero)

    df = df[COLUNAS_DESTINO]

    # Segurança antes de tocar no Supabase.
    if len(df) < MIN_REGISTROS:
        raise RuntimeError(
            f"Carga abortada: somente {len(df):,} registros encontrados. "
            f"Mínimo de segurança: {MIN_REGISTROS:,}."
        )

    ids_vazios = int(df["id"].isna().sum())
    if ids_vazios:
        raise RuntimeError(
            f"Carga abortada: {ids_vazios:,} registros sem ID Negócio."
        )

    duplicados = df["id"].duplicated(keep=False)
    if duplicados.any():
        exemplos = df.loc[duplicados, "id"].head(10).tolist()
        raise RuntimeError(
            "Carga abortada: existem IDs de negócio duplicados. "
            f"Exemplos: {exemplos}"
        )

    df = df.astype(object).where(pd.notna(df), None)

    print()
    print("Mapeamento SIJ -> public.base_sij")
    print("------------------------------")
    for origem, destino in MAPEAMENTO.items():
        print(f"{origem} -> {destino}")

    print()
    print("Registros encontrados:", f"{len(df):,}")
    print("Quantidade de colunas físicas:", len(df.columns))
    print(
        "Registros com data de recebimento:",
        f"{int(df['data_recebimento'].notna().sum()):,}",
    )
    print()
    print(
        "Observação: processo_normalizado, numero_cumprimento_normalizado, "
        "receita, recebido e campos mes_* serão calculados pela view "
        "public.base_sij_tratada."
    )

    return df


# ============================================================
# SUPABASE
# ============================================================


def criar_supabase():
    return create_client(
        env("SUPABASE_URL", "supabase_url"),
        env(
            "SUPABASE_SERVICE_ROLE_KEY",
            "supabase_service_role_key",
        ),
    )


def limpar_tabela(supabase):
    print()
    print(f"Limpando public.{TABELA_SUPABASE}...")

    # PostgREST exige filtro em DELETE.
    (
        supabase.table(TABELA_SUPABASE)
        .delete()
        .neq("id", "__ID_QUE_NAO_EXISTE__")
        .execute()
    )

    # Segurança para eventual linha antiga com id NULL.
    (
        supabase.table(TABELA_SUPABASE)
        .delete()
        .is_("id", "null")
        .execute()
    )


def inserir_lote(supabase, lote):
    ultimo_erro = None

    for tentativa in range(1, MAX_TENTATIVAS + 1):
        try:
            (
                supabase.table(TABELA_SUPABASE)
                .insert(lote)
                .execute()
            )
            return
        except Exception as erro:
            ultimo_erro = erro
            print(
                f"Tentativa {tentativa}/{MAX_TENTATIVAS} falhou:"
            )
            print(str(erro)[:1000])

            if tentativa < MAX_TENTATIVAS:
                time.sleep(tentativa * 3)

    raise ultimo_erro


def contar(supabase, tabela):
    resposta = (
        supabase.table(tabela)
        .select("*", count="exact")
        .limit(1)
        .execute()
    )

    if resposta.count is None:
        raise RuntimeError(
            f"Não foi possível contar os registros de public.{tabela}."
        )

    return resposta.count


def validar_carga(supabase, total_esperado):
    total_base = contar(supabase, TABELA_SUPABASE)
    total_view = contar(supabase, VIEW_TRATADA)

    if total_base != total_esperado:
        raise RuntimeError(
            "Validação da tabela física falhou. "
            f"Planilha: {total_esperado:,} | "
            f"base_sij: {total_base:,}"
        )

    if total_view != total_esperado:
        raise RuntimeError(
            "Validação da view tratada falhou. "
            f"Planilha: {total_esperado:,} | "
            f"base_sij_tratada: {total_view:,}"
        )

    # Validação de leitura dos campos calculados da view.
    amostra = (
        supabase.table(VIEW_TRATADA)
        .select(
            "id,processo_normalizado,data_base_venda,"
            "valor_contrato,receita,recebido,mes_venda"
        )
        .limit(5)
        .execute()
    )

    print()
    print("Validação OK")
    print("------------------------------")
    print("base_sij:", f"{total_base:,}", "registros")
    print("base_sij_tratada:", f"{total_view:,}", "registros")
    print("Amostra da view tratada lida com sucesso:", len(amostra.data))


def enviar_supabase(df):
    supabase = criar_supabase()

    # A leitura e todas as validações locais acontecem antes do DELETE.
    limpar_tabela(supabase)

    registros = df.to_dict(orient="records")
    total = len(registros)

    print()
    print(f"Enviando para public.{TABELA_SUPABASE}...")

    for inicio in range(0, total, LOTE_SUPABASE):
        fim = min(inicio + LOTE_SUPABASE, total)
        lote = registros[inicio:fim]

        inserir_lote(supabase, lote)

        if fim == total or fim % 2000 == 0:
            print(
                f"Supabase SIJ: {fim:,}/{total:,}"
            )

    validar_carga(supabase, total)


# ============================================================
# MAIN
# ============================================================


def main():
    print()
    print("======================================")
    print("ATUALIZAÇÃO SIJ -> SUPABASE")
    print("======================================")

    df = ler_sij()
    enviar_supabase(df)

    print()
    print("======================================")
    print("SIJ ATUALIZADA COM SUCESSO")
    print("======================================")


if __name__ == "__main__":
    main()
