import os
import io
import json
import base64
import time
import argparse
import unicodedata
from datetime import date, datetime, timedelta

import pandas as pd
from dotenv import load_dotenv


# ============================================================
# CONFIGURAÇÃO
# ============================================================

load_dotenv()

SPREADSHEET_ID = "1r1RHLxkmTOt6uka1yE122R8Ic3WyYcec"

# base_sij é a tabela física que recebe a carga.
# base_sij_tratada é uma VIEW calculada automaticamente a partir dela.
TABELA_SUPABASE = "base_sij"
VIEW_TRATADA = "base_sij_tratada"

ABA_SIJ = os.getenv("SIJ_ABA", "Negócios").strip() or "Negócios"
ABA_MOVIMENTACOES = (
    os.getenv("SIJ_ABA_MOVIMENTACOES", "Movimentações").strip()
    or "Movimentações"
)

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

# Campos lidos diretamente da aba Negócios.
# data_recebimento, valor_recebido_liquidacao e valor_deposito são tratados
# separadamente porque os totais da aba Negócios são fórmulas SUMIFS que podem
# chegar no XLSX sem valor calculado em cache.
MAPEAMENTO_NEGOCIOS = {
    "ID Negócio": "id",
    "Número do processo": "processo",
    "Nº cumprimento de sentença": "numero_cumprimento",
    "Data base da venda": "data_base_venda",
    "Data estimada de liquidação": "data_estimada_liquidacao",
    "Prazo liquidação (meses)": "prazo_liquidacao_meses",
    "Última data de recebimento": "data_recebimento_planilha",
    "Valor do contrato (R$)": "valor_contrato",
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
    "prazo_liquidacao_meses",
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

COLUNAS_MOVIMENTACOES = [
    "ID Negócio",
    "Tipo",
    "Data",
    "Valor (R$)",
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
# GOOGLE DRIVE / ARQUIVO LOCAL
# ============================================================


def carregar_credencial_google():
    """Aceita JSON, JSON em Base64 ou arquivo local de credenciais."""

    from google.oauth2 import service_account

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
        "GOOGLE_CREDENTIALS_JSON ou GOOGLE_APPLICATION_CREDENTIALS. "
        "Para testar localmente sem credencial Google, use "
        "--arquivo-local CAMINHO_DO_XLSX --somente-validar."
    )


def criar_drive():
    from googleapiclient.discovery import build

    return build(
        "drive",
        "v3",
        credentials=carregar_credencial_google(),
        cache_discovery=False,
    )


def abrir_arquivo_sij(caminho_local=None):
    """Retorna o XLSX em memória, vindo do caminho local ou do Google Drive."""

    if caminho_local:
        caminho_local = os.path.abspath(os.path.expanduser(caminho_local))
        if not os.path.exists(caminho_local):
            raise RuntimeError(
                f"Arquivo local SIJ não encontrado: {caminho_local}"
            )

        print()
        print("Arquivo local utilizado")
        print("------------------------------")
        print("Caminho:", caminho_local)
        print()

        with open(caminho_local, "rb") as arquivo:
            conteudo = arquivo.read()

        return io.BytesIO(conteudo)

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

    from googleapiclient.http import MediaIoBaseDownload

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

    return texto(valor)


def data_para_timestamp(valor):
    """Converte data do Excel/texto para Timestamp somente para cálculos locais."""

    if vazio(valor):
        return pd.NaT

    if isinstance(valor, pd.Timestamp):
        return valor.normalize()

    if isinstance(valor, datetime):
        return pd.Timestamp(valor.date())

    if isinstance(valor, date):
        return pd.Timestamp(valor)

    if isinstance(valor, (int, float)) and not isinstance(valor, bool):
        numero = float(valor)
        if 1 <= numero <= 100000:
            return pd.Timestamp("1899-12-30") + pd.to_timedelta(
                numero, unit="D"
            )

    return pd.to_datetime(
        str(valor).strip(),
        dayfirst=True,
        errors="coerce",
    )


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


def normalizar_tipo(valor):
    """Normaliza Tipo da movimentação para comparação robusta."""

    s = texto(valor)
    if not s:
        return None

    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return " ".join(s.casefold().split())


def agregar_movimentacoes(excel):
    if ABA_MOVIMENTACOES not in excel.sheet_names:
        raise RuntimeError(
            f"A aba '{ABA_MOVIMENTACOES}' não existe. "
            f"Disponíveis: {excel.sheet_names}"
        )

    mov = pd.read_excel(
        excel,
        sheet_name=ABA_MOVIMENTACOES,
        header=HEADER_EXCEL,
        dtype=object,
    )
    mov = mov.dropna(how="all")

    faltantes = [
        coluna
        for coluna in COLUNAS_MOVIMENTACOES
        if coluna not in mov.columns
    ]
    if faltantes:
        raise RuntimeError(
            "Colunas obrigatórias ausentes na aba Movimentações: "
            + ", ".join(faltantes)
        )

    mov = mov[COLUNAS_MOVIMENTACOES].copy()
    mov["id"] = mov["ID Negócio"].map(texto)
    mov["tipo_norm"] = mov["Tipo"].map(normalizar_tipo)
    mov["valor_num"] = mov["Valor (R$)"].map(numero_para_float)
    mov["data_ts"] = mov["Data"].map(data_para_timestamp)

    mov = mov.loc[mov["id"].notna()].copy()

    receb = mov.loc[mov["tipo_norm"] == "recebimento"].copy()
    depositos = mov.loc[mov["tipo_norm"] == "deposito"].copy()

    if receb.empty:
        raise RuntimeError(
            "Carga abortada: nenhuma movimentação do tipo Recebimento "
            "foi encontrada."
        )

    if depositos.empty:
        raise RuntimeError(
            "Carga abortada: nenhuma movimentação do tipo Depósito "
            "foi encontrada."
        )

    receb_sem_valor = int(receb["valor_num"].isna().sum())
    receb_sem_data = int(receb["data_ts"].isna().sum())
    dep_sem_valor = int(depositos["valor_num"].isna().sum())

    receb_ag = (
        receb.groupby("id", as_index=False)
        .agg(
            valor_recebido_calc=("valor_num", "sum"),
            data_recebimento_mov=("data_ts", "max"),
            qtd_mov_recebimento=("id", "size"),
        )
    )

    deposito_ag = (
        depositos.groupby("id", as_index=False)
        .agg(
            valor_deposito_calc=("valor_num", "sum"),
            qtd_mov_deposito=("id", "size"),
        )
    )

    print()
    print("Auditoria da aba Movimentações")
    print("------------------------------")
    print("Movimentações totais:", f"{len(mov):,}")
    print("Movimentações do tipo Recebimento:", f"{len(receb):,}")
    print("IDs distintos com Recebimento:", f"{receb['id'].nunique():,}")
    print(
        "Soma bruta dos Recebimentos:",
        f"R$ {receb['valor_num'].fillna(0).sum():,.2f}",
    )
    print("Recebimentos sem valor:", f"{receb_sem_valor:,}")
    print("Recebimentos sem data:", f"{receb_sem_data:,}")
    print("Movimentações do tipo Depósito:", f"{len(depositos):,}")
    print("IDs distintos com Depósito:", f"{depositos['id'].nunique():,}")
    print(
        "Soma bruta dos Depósitos:",
        f"R$ {depositos['valor_num'].fillna(0).sum():,.2f}",
    )
    print("Depósitos sem valor:", f"{dep_sem_valor:,}")

    if receb["valor_num"].fillna(0).sum() <= 0:
        raise RuntimeError(
            "Carga abortada: a soma das movimentações de Recebimento "
            "não é positiva."
        )

    return receb_ag, deposito_ag


def ler_sij(caminho_local=None):
    arquivo = abrir_arquivo_sij(caminho_local)
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
        for coluna in MAPEAMENTO_NEGOCIOS
        if coluna not in df.columns
    ]
    if faltantes:
        raise RuntimeError(
            "Colunas obrigatórias ausentes no arquivo SIJ: "
            + ", ".join(faltantes)
        )

    # Lê somente os campos necessários da aba Negócios.
    df = df[list(MAPEAMENTO_NEGOCIOS.keys())].copy()
    df = df.rename(columns=MAPEAMENTO_NEGOCIOS)

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

    # Datas da aba Negócios.
    for coluna in [
        "data_base_venda",
        "data_estimada_liquidacao",
        "data_recebimento_planilha",
    ]:
        df[coluna] = df[coluna].map(texto_data)

    # Prazo e valor do contrato permanecem TEXT na base_sij.
    for coluna in [
        "prazo_liquidacao_meses",
        "valor_contrato",
    ]:
        df[coluna] = df[coluna].map(texto_numero)

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

    receb_ag, deposito_ag = agregar_movimentacoes(excel)

    df = df.merge(receb_ag, on="id", how="left")
    df = df.merge(deposito_ag, on="id", how="left")

    # Total depositado replica a lógica SUMIFS da planilha:
    # sem movimentação de depósito = zero.
    df["valor_deposito_calc"] = pd.to_numeric(
        df["valor_deposito_calc"], errors="coerce"
    ).fillna(0.0)
    df["valor_deposito"] = df["valor_deposito_calc"].map(texto_numero)

    # Total recebido também replica SUMIFS para negócios com venda:
    # sem recebimento = zero.
    df["valor_recebido_calc"] = pd.to_numeric(
        df["valor_recebido_calc"], errors="coerce"
    ).fillna(0.0)

    # A data preferencial é a última data das movimentações de Recebimento.
    # Se por algum problema uma movimentação estiver sem data, usamos a data já
    # registrada na aba Negócios como fallback, mantendo auditoria abaixo.
    data_mov_texto = df["data_recebimento_mov"].map(texto_data)
    df["data_recebimento"] = data_mov_texto.where(
        data_mov_texto.notna(),
        df["data_recebimento_planilha"],
    )

    tem_venda = df["data_base_venda"].notna()

    # Regra de negócio solicitada:
    # sem Data base da venda, data de recebimento e total recebido DEVEM ser
    # NULL, mesmo que a planilha/movimentações tragam valores preenchidos.
    receb_excluidos_sem_venda = (
        (~tem_venda)
        & (
            df["data_recebimento"].notna()
            | (df["valor_recebido_calc"] != 0)
        )
    )
    qtd_receb_excluidos_sem_venda = int(receb_excluidos_sem_venda.sum())
    valor_receb_excluido_sem_venda = float(
        df.loc[
            receb_excluidos_sem_venda,
            "valor_recebido_calc",
        ].sum()
    )

    df["valor_recebido_liquidacao"] = df["valor_recebido_calc"].map(
        texto_numero
    )
    df.loc[~tem_venda, "data_recebimento"] = None
    df.loc[~tem_venda, "valor_recebido_liquidacao"] = None

    # Auditoria: quando ambas as datas existem, a aba Negócios deve concordar
    # com o máximo calculado na aba Movimentações.
    data_planilha_ts = df["data_recebimento_planilha"].map(
        data_para_timestamp
    )
    data_mov_ts = pd.to_datetime(
        df["data_recebimento_mov"], errors="coerce"
    )
    datas_comparaveis = (
        tem_venda
        & data_planilha_ts.notna()
        & data_mov_ts.notna()
    )
    divergencias_data = int(
        (
            data_planilha_ts.loc[datas_comparaveis].dt.normalize()
            != data_mov_ts.loc[datas_comparaveis].dt.normalize()
        ).sum()
    )

    # Limpa colunas auxiliares antes de enviar ao Supabase.
    df = df[COLUNAS_DESTINO]
    df = df.astype(object).where(pd.notna(df), None)

    qtd_com_data_recebimento = int(df["data_recebimento"].notna().sum())
    valores_recebidos_num = df["valor_recebido_liquidacao"].map(
        numero_para_float
    )
    qtd_com_valor_recebido_positivo = int(
        (pd.to_numeric(valores_recebidos_num, errors="coerce") > 0).sum()
    )

    sem_venda_com_data = int(
        (
            df["data_base_venda"].isna()
            & df["data_recebimento"].notna()
        ).sum()
    )
    sem_venda_com_valor = int(
        (
            df["data_base_venda"].isna()
            & df["valor_recebido_liquidacao"].notna()
        ).sum()
    )

    print()
    print("Regra Data base da venda")
    print("------------------------------")
    print(
        "Negócios sem Data base da venda:",
        f"{int(df['data_base_venda'].isna().sum()):,}",
    )
    print(
        "Recebimentos anulados por falta de Data base da venda:",
        f"{qtd_receb_excluidos_sem_venda:,}",
    )
    print(
        "Valor de recebimentos anulados por essa regra:",
        f"R$ {valor_receb_excluido_sem_venda:,.2f}",
    )
    print(
        "Divergências entre Última data de recebimento e Movimentações:",
        f"{divergencias_data:,}",
    )

    print()
    print("Mapeamento SIJ -> public.base_sij")
    print("------------------------------")
    for origem, destino in MAPEAMENTO_NEGOCIOS.items():
        if destino != "data_recebimento_planilha":
            print(f"{origem} -> {destino}")
    print(
        "Movimentações[Tipo=Recebimento] -> "
        "valor_recebido_liquidacao + data_recebimento"
    )
    print("Movimentações[Tipo=Depósito] -> valor_deposito")

    print()
    print("Registros encontrados:", f"{len(df):,}")
    print("Quantidade de colunas físicas:", len(df.columns))
    print(
        "Registros finais com data de recebimento:",
        f"{qtd_com_data_recebimento:,}",
    )
    print(
        "Registros finais com valor recebido > 0:",
        f"{qtd_com_valor_recebido_positivo:,}",
    )

    # Validações que evitam apagar a tabela com uma carga financeira quebrada.
    if qtd_com_data_recebimento == 0:
        raise RuntimeError(
            "Carga abortada: nenhum registro final ficou com data de "
            "recebimento."
        )

    if qtd_com_valor_recebido_positivo == 0:
        raise RuntimeError(
            "Carga abortada: nenhum registro final ficou com "
            "valor_recebido_liquidacao positivo."
        )

    if sem_venda_com_data != 0 or sem_venda_com_valor != 0:
        raise RuntimeError(
            "Carga abortada: a regra de Data base da venda falhou. "
            f"Sem venda com data={sem_venda_com_data}; "
            f"sem venda com valor={sem_venda_com_valor}."
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
    from supabase import create_client

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

    recebidos_com_valor = (
        supabase.table(VIEW_TRATADA)
        .select("id", count="exact")
        .gt("valor_recebido_liquidacao", 0)
        .limit(1)
        .execute()
    )

    qtd_recebidos_com_valor = recebidos_com_valor.count or 0
    if qtd_recebidos_com_valor <= 0:
        raise RuntimeError(
            "Validação financeira falhou: base_sij_tratada ficou sem "
            "valor_recebido_liquidacao positivo."
        )

    amostra = (
        supabase.table(VIEW_TRATADA)
        .select(
            "id,processo_normalizado,data_base_venda,data_recebimento,"
            "valor_contrato,valor_recebido_liquidacao,valor_deposito,"
            "receita,recebido,mes_venda,prazo_liquidacao_meses"
        )
        .gt("valor_recebido_liquidacao", 0)
        .limit(5)
        .execute()
    )

    print()
    print("Validação OK")
    print("------------------------------")
    print("base_sij:", f"{total_base:,}", "registros")
    print("base_sij_tratada:", f"{total_view:,}", "registros")
    print(
        "Registros com valor recebido > 0 na view tratada:",
        f"{qtd_recebidos_com_valor:,}",
    )
    print(
        "Amostra financeira da view tratada lida com sucesso:",
        len(amostra.data),
    )


def enviar_supabase(df):
    supabase = criar_supabase()

    # A leitura e TODAS as validações locais acontecem antes do DELETE.
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
            print(f"Supabase SIJ: {fim:,}/{total:,}")

    validar_carga(supabase, total)


# ============================================================
# MAIN
# ============================================================


def argumentos():
    parser = argparse.ArgumentParser(
        description="Atualiza a base SIJ no Supabase."
    )
    parser.add_argument(
        "--arquivo-local",
        default=os.getenv("SIJ_ARQUIVO_LOCAL"),
        help=(
            "XLSX local para teste. Quando omitido, baixa o arquivo "
            "configurado no Google Drive."
        ),
    )
    parser.add_argument(
        "--somente-validar",
        action="store_true",
        help=(
            "Lê, calcula e valida a base sem alterar o Supabase. "
            "Útil para teste local."
        ),
    )
    return parser.parse_args()


def main():
    args = argumentos()

    print()
    print("======================================")
    print("ATUALIZAÇÃO SIJ -> SUPABASE")
    print("======================================")

    df = ler_sij(args.arquivo_local)

    if args.somente_validar:
        print()
        print("======================================")
        print("VALIDAÇÃO LOCAL CONCLUÍDA COM SUCESSO")
        print("Supabase NÃO foi alterado.")
        print("======================================")
        return

    enviar_supabase(df)

    print()
    print("======================================")
    print("SIJ ATUALIZADA COM SUCESSO")
    print("======================================")


if __name__ == "__main__":
    main()
