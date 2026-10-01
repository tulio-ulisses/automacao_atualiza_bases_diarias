import os
import io
import re
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
TABELA_SUPABASE = "base_sij_tratada"

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

# A planilha possui uma linha de agrupadores e a segunda linha é o cabeçalho real.
HEADER_EXCEL = 1

MAPEAMENTO = {
    "ID Negócio": "id",
    "Número do processo": "processo",
    "Nº cumprimento de sentença": "numero_cumprimento",
    "Data base da venda": "data_base_venda",
    "Data estimada de liquidação": "data_estimada_liquidacao",
    "Última data de recebimento": "data_recebimento",
    "Valor do contrato (R$)": "valor_contrato",
    "Total depositado (R$)": "valor_deposito",
    "Total recebido (R$)": "valor_recebido_liquidacao",
    "Receita final (R$)": "receita",
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
    "processo_normalizado",
    "numero_cumprimento",
    "numero_cumprimento_normalizado",
    "data_base_venda",
    "data_estimada_liquidacao",
    "data_recebimento",
    "valor_contrato",
    "valor_deposito",
    "valor_recebido_liquidacao",
    "receita",
    "recebido",
    "mes_venda",
    "mes_estimado_liquidacao",
    "mes_recebimento",
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


def normalizar_numero_processo(valor):
    valor = texto(valor)
    if valor is None:
        return None

    digitos = re.sub(r"\D", "", valor)
    return digitos or None


def converter_numero(valor):
    if vazio(valor):
        return None

    if isinstance(valor, bool):
        return float(valor)

    if isinstance(valor, (int, float)):
        numero = float(valor)
        if pd.isna(numero):
            return None
        return numero

    s = str(valor).strip()
    s = s.replace("R$", "").replace(" ", "")

    if s in {"", "-"}:
        return None

    # 1.234,56 -> 1234.56 | 1,234.56 -> 1234.56
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(".", "").replace(",", ".")

    s = re.sub(r"[^0-9+\-.]", "", s)

    if s in {"", "+", "-", ".", "+.", "-."}:
        return None

    try:
        return float(s)
    except ValueError:
        return None


def converter_data(valor):
    if vazio(valor):
        return None

    if isinstance(valor, pd.Timestamp):
        return valor.date().isoformat()

    if isinstance(valor, datetime):
        return valor.date().isoformat()

    if isinstance(valor, date):
        return valor.isoformat()

    # Excel serial date, caso venha como número cru.
    if isinstance(valor, (int, float)):
        numero = float(valor)
        if 1 <= numero <= 100000:
            data_excel = datetime(1899, 12, 30) + timedelta(days=numero)
            return data_excel.date().isoformat()

    s = str(valor).strip()

    # Serial do Excel vindo como texto.
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        try:
            numero = float(s)
            if 1 <= numero <= 100000:
                data_excel = datetime(1899, 12, 30) + timedelta(days=numero)
                return data_excel.date().isoformat()
        except ValueError:
            pass

    data_convertida = pd.to_datetime(
        s,
        errors="coerce",
        dayfirst=True,
    )

    if pd.isna(data_convertida):
        return None

    return data_convertida.date().isoformat()


def primeiro_dia_mes(data_iso):
    if not data_iso:
        return None
    return f"{data_iso[:7]}-01"


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

    # Mantém exclusivamente as colunas usadas pela base_sij_tratada.
    df = df[list(MAPEAMENTO.keys())].copy()
    df = df.rename(columns=MAPEAMENTO)

    # Textos/IDs.
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

    # Datas.
    for coluna in [
        "data_base_venda",
        "data_estimada_liquidacao",
        "data_recebimento",
    ]:
        df[coluna] = df[coluna].map(converter_data)

    # Valores.
    for coluna in [
        "valor_contrato",
        "valor_deposito",
        "valor_recebido_liquidacao",
        "receita",
    ]:
        df[coluna] = df[coluna].map(converter_numero)

    # Campos derivados já existentes em public.base_sij_tratada.
    df["processo_normalizado"] = df["processo"].map(
        normalizar_numero_processo
    )
    df["numero_cumprimento_normalizado"] = df[
        "numero_cumprimento"
    ].map(normalizar_numero_processo)

    df["recebido"] = df["data_recebimento"].notna()

    df["mes_venda"] = df["data_base_venda"].map(primeiro_dia_mes)
    df["mes_estimado_liquidacao"] = df[
        "data_estimada_liquidacao"
    ].map(primeiro_dia_mes)
    df["mes_recebimento"] = df["data_recebimento"].map(
        primeiro_dia_mes
    )

    df = df[COLUNAS_DESTINO]

    # Segurança: a origem atual possui milhares de registros. Não limpamos o
    # Supabase se a leitura vier vazia/quebrada por alteração do arquivo.
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
    print("Mapeamento SIJ -> base_sij_tratada")
    print("------------------------------")
    for origem, destino in MAPEAMENTO.items():
        print(f"{origem} -> {destino}")

    print()
    print("Registros encontrados:", f"{len(df):,}")
    print("Quantidade de colunas:", len(df.columns))
    print("Recebidos:", f"{int(df['recebido'].sum()):,}")

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

    # PostgREST exige filtro em DELETE. A primeira chamada remove IDs não nulos;
    # a segunda garante a remoção de eventual linha antiga com id NULL.
    (
        supabase.table(TABELA_SUPABASE)
        .delete()
        .neq("id", "__ID_QUE_NAO_EXISTE__")
        .execute()
    )

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


def validar_carga(supabase, total_esperado):
    resposta = (
        supabase.table(TABELA_SUPABASE)
        .select("*", count="exact")
        .limit(1)
        .execute()
    )

    total_supabase = resposta.count

    if total_supabase is None:
        raise RuntimeError(
            "Não foi possível validar a quantidade no Supabase."
        )

    if total_supabase != total_esperado:
        raise RuntimeError(
            "Validação da SIJ falhou. "
            f"Planilha: {total_esperado:,} | "
            f"Supabase: {total_supabase:,}"
        )

    print()
    print("Validação OK:", f"{total_supabase:,}", "registros")


def enviar_supabase(df):
    supabase = criar_supabase()

    # Só limpa depois de toda a leitura, validação de colunas, transformação e
    # checagens de segurança terem terminado com sucesso.
    limpar_tabela(supabase)

    registros = df.to_dict(orient="records")
    total = len(registros)

    print()
    print("Enviando ao Supabase...")

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
