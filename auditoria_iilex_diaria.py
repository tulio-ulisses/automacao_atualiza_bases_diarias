import json
import time
import hashlib
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser

import requests
from supabase import create_client

# Usa os Repository Secrets que ja existem nas outras automacoes deste repositorio.
# Para teste local, cred.py continua sendo opcional, mas NUNCA deve ser enviado ao GitHub.
import os
user = os.environ.get("IILEX_USERNAME") or os.environ.get("IILEX_USER", "")
senha = os.environ.get("IILEX_PASSWORD") or os.environ.get("IILEX_SENHA", "")
url_supabase = os.environ.get("SUPABASE_URL", "")
token_supabase = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
if not all((user, senha, url_supabase, token_supabase)):
    try:
        from cred import user as local_user, senha as local_senha, url_supabase as local_url, token_supabase as local_token
        user = user or local_user
        senha = senha or local_senha
        url_supabase = url_supabase or local_url
        token_supabase = token_supabase or local_token
    except ImportError:
        pass


VERSAO_SCRIPT = "2026-11-12-recuperacao-automatica-v2"

BASE_URL = "https://juscash.iilex.com.br/sistema"

URL_LOGIN = f"{BASE_URL}/controllers/login_p.php"
URL_AUDITORIA = f"{BASE_URL}/controllers/filtroauditoria_p.php"
URL_PAGINA_AUDITORIA = f"{BASE_URL}/modulos/filtroauditoria"

TABELA_LOGS = "logs_auditoria_raw"
TABELA_CARGAS = "logs_auditoria_cargas"

# O modo diario revisa automaticamente pendencias e dias recentes.
INICIO_HISTORICO = date(2026, 10, 6)
JANELA_REVISAO_PADRAO = 7
MAX_PENDENTES_PADRAO = 7
TENTATIVAS_COLETA_DIA = 2
RECONSULTAS_TOTAL_PAI = 2

LIMITE_REQUISICAO = 1000

PAUSA_ENTRE_AUDITORIAS = 30
PAUSA_ENTRE_DIAS = 60

LOTE_SUPABASE = 200

# Erros aparecem no console; o controle persistente fica em logs_auditoria_cargas.


class ErroAutenticacao(Exception):
    pass


class ErroServidor(Exception):
    pass


class DivergenciaAuditoria(Exception):
    """Diferenca entre total informado e registros obtidos por grupos."""
    pass


class ParserUsuarios(HTMLParser):
    def __init__(self):
        super().__init__()

        self.dentro_select = False
        self.valor = None
        self.texto = []
        self.usuarios = []

    def handle_starttag(self, tag, attrs):
        atributos = dict(attrs)

        if (
            tag == "select"
            and atributos.get("id") == "idusuario"
        ):
            self.dentro_select = True

        elif (
            tag == "option"
            and self.dentro_select
        ):
            self.valor = atributos.get(
                "value",
                "",
            )

            self.texto = []

    def handle_data(self, data):
        if self.valor is not None:
            self.texto.append(data)

    def handle_endtag(self, tag):
        if (
            tag == "option"
            and self.valor is not None
        ):
            valor = str(
                self.valor
            ).strip()

            nome = "".join(
                self.texto
            ).strip()

            if valor:
                self.usuarios.append(
                    {
                        "id": valor,
                        "nome": nome,
                    }
                )

            self.valor = None
            self.texto = []

        elif (
            tag == "select"
            and self.dentro_select
        ):
            self.dentro_select = False


def agora_utc():
    return datetime.now(
        timezone.utc
    ).isoformat()


def criar_sessao_http():
    sessao = requests.Session()

    sessao.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/151.0.0.0 "
                "Safari/537.36"
            ),
            "Accept": (
                "application/json, "
                "text/javascript, "
                "*/*; q=0.01"
            ),
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{BASE_URL}/",
        }
    )

    return sessao


def validar_http(resposta):
    if resposta.status_code == 429:
        raise ErroServidor(
            "IILEX respondeu HTTP 429. "
            "Execução interrompida por segurança."
        )

    if resposta.status_code >= 500:
        raise ErroServidor(
            f"IILEX respondeu HTTP {resposta.status_code}. "
            "Execução interrompida por segurança."
        )

    if resposta.status_code in (401, 403):
        raise ErroAutenticacao(
            f"IILEX respondeu HTTP {resposta.status_code}. "
            "Execução interrompida."
        )

    if resposta.status_code >= 400:
        raise ErroServidor(
            f"IILEX respondeu HTTP {resposta.status_code}. "
            "Execução interrompida para análise."
        )


def fazer_login(sessao):
    device_identifier = {
        "userAgent": sessao.headers["User-Agent"],
        "language": "pt-BR",
        "platform": "Win32",
        "screenResolution": "1536x864",
        "timeZone": "America/Sao_Paulo",
    }

    try:
        resposta = sessao.post(
            URL_LOGIN,
            data={
                "origem": "login",
                "texto1": user,
                "texto2": senha,
                "deviceIdentifier": json.dumps(
                    device_identifier,
                    ensure_ascii=False,
                ),
            },
            timeout=60,
        )

    except requests.Timeout as erro:
        raise ErroServidor(
            f"Timeout durante o login: {erro}"
        )

    except requests.RequestException as erro:
        raise ErroServidor(
            f"Erro de comunicação durante o login: {erro}"
        )

    validar_http(resposta)

    try:
        dados = resposta.json()

    except Exception:
        raise RuntimeError(
            "Resposta do login não é JSON válido."
        )

    if dados.get("sucesso") is not True:
        raise ErroAutenticacao(
            "Login IILEX não realizado. Verifique usuario, senha e acesso."
        )

    sessao.headers["Referer"] = (
        URL_PAGINA_AUDITORIA
    )


def carregar_usuarios():
    sessao = criar_sessao_http()

    fazer_login(sessao)

    try:
        resposta = sessao.get(
            URL_PAGINA_AUDITORIA,
            timeout=60,
        )

        validar_http(resposta)

        parser = ParserUsuarios()

        parser.feed(
            resposta.text
        )

    except requests.RequestException as erro:
        raise ErroServidor(
            f"Erro ao carregar usuários: {erro}"
        )

    finally:
        sessao.close()

    usuarios = []
    ids_vistos = set()

    for item in parser.usuarios:
        usuario_id = item["id"]

        if usuario_id in ids_vistos:
            continue

        ids_vistos.add(
            usuario_id
        )

        usuarios.append(
            item
        )

    if not usuarios:
        raise RuntimeError(
            "Nenhum usuário encontrado "
            "no filtro da Auditoria."
        )

    return usuarios


def montar_payload(
    data_referencia,
    usuarios=None,
    limite=1,
):
    data_de = data_referencia.strftime(
        "%d/%m/%Y"
    )

    data_ate = (
        data_referencia
        + timedelta(days=1)
    ).strftime(
        "%d/%m/%Y"
    )

    payload = [
        (
            "dataDe",
            data_de,
        ),
        (
            "dataAte",
            data_ate,
        ),
        (
            "modulo",
            "",
        ),
        (
            "idregistro",
            "",
        ),
        (
            "acao",
            "",
        ),
        (
            "limite",
            str(limite),
        ),
    ]

    if usuarios:
        for item in usuarios:
            payload.append(
                (
                    "usuarios[]",
                    str(item["id"]),
                )
            )

    return payload


def interpretar_resposta(
    resposta,
):
    validar_http(
        resposta
    )

    try:
        dados = resposta.json()

    except Exception:
        raise RuntimeError(
            "Resposta da Auditoria "
            "não é JSON válido."
        )

    if dados.get("autenticado") is False:
        raise ErroAutenticacao(
            dados.get("mensagem")
            or "IILEX invalidou a sessão."
        )

    if "total" not in dados:
        raise RuntimeError(
            "Resposta inesperada da Auditoria: "
            f"{dados}"
        )

    resultados = dados.get(
        "resultados",
        [],
    )

    if resultados is None:
        resultados = []

    if not isinstance(
        resultados,
        list,
    ):
        raise RuntimeError(
            "Campo 'resultados' "
            "não é uma lista."
        )

    total_bruto = dados.get(
        "total"
    )

    if total_bruto in (
        None,
        "",
    ):
        if resultados:
            raise RuntimeError(
                "IILEX retornou total vazio "
                "mas trouxe resultados."
            )

        total = 0

    else:
        try:
            total = int(
                total_bruto
            )

        except (
            TypeError,
            ValueError,
        ):
            raise RuntimeError(
                "Total inválido retornado "
                f"pelo IILEX: {total_bruto}"
            )

    return (
        total,
        resultados,
    )


class AuditoriaIILEX:
    def __init__(self):
        self.ultima_consulta = None
        self.numero_consultas = 0

    def aguardar(self):
        if self.ultima_consulta is None:
            return

        decorrido = (
            time.monotonic()
            - self.ultima_consulta
        )

        restante = (
            PAUSA_ENTRE_AUDITORIAS
            - decorrido
        )

        if restante > 0:
            print(
                f"Aguardando "
                f"{restante:.0f}s antes "
                "da próxima consulta..."
            )

            time.sleep(
                restante
            )

    def consultar(
        self,
        data_referencia,
        usuarios=None,
        limite=1,
        descricao="consulta",
    ):
        self.aguardar()

        sessao = criar_sessao_http()

        try:
            fazer_login(
                sessao
            )

            payload = montar_payload(
                data_referencia=(
                    data_referencia
                ),
                usuarios=usuarios,
                limite=limite,
            )

            print()
            print(
                f"Auditoria "
                f"#{self.numero_consultas + 1} "
                f"| {descricao}"
            )

            try:
                resposta = sessao.post(
                    URL_AUDITORIA,
                    data=payload,
                    timeout=90,
                )

            except requests.Timeout as erro:
                raise ErroServidor(
                    "Timeout ao consultar IILEX. "
                    "Execução interrompida. "
                    f"{erro}"
                )

            except requests.RequestException as erro:
                raise ErroServidor(
                    "Erro de comunicação com IILEX. "
                    "Execução interrompida. "
                    f"{erro}"
                )

            total, resultados = (
                interpretar_resposta(
                    resposta
                )
            )

            self.numero_consultas += 1

            print(
                f"Total informado: "
                f"{total}"
            )

            print(
                f"Registros recebidos: "
                f"{len(resultados)}"
            )

            return (
                total,
                resultados,
            )

        finally:
            self.ultima_consulta = (
                time.monotonic()
            )

            sessao.close()


def nome_grupo(
    usuarios,
):
    nomes = [
        item["nome"]
        for item in usuarios
    ]

    if len(nomes) <= 4:
        return ", ".join(
            nomes
        )

    return (
        f"{len(nomes)} usuários"
    )


def coletar_grupo(auditoria, data_referencia, usuarios, nivel=0):
    """Coleta todos os registros, dividindo grupos que ultrapassam 1.000.

    Quando o total do pai difere da soma dos filhos, valida novamente o pai.
    NUNCA aceita uma divergencia persistente nem descarta registros.
    """
    indentacao = "    " * nivel
    total, resultados = auditoria.consultar(
        data_referencia=data_referencia,
        usuarios=usuarios,
        limite=LIMITE_REQUISICAO,
        descricao=f"grupo com {len(usuarios)} usuário(s)",
    )
    print(f"{indentacao}{nome_grupo(usuarios)} | total={total}", flush=True)

    if total < 0:
        raise DivergenciaAuditoria(f"Total negativo: {total}")
    if total == 0:
        if resultados:
            raise DivergenciaAuditoria("Grupo informou total zero, mas retornou registros.")
        return [], 0

    if total <= LIMITE_REQUISICAO:
        if len(resultados) != total:
            raise DivergenciaAuditoria(
                f"Grupo incompleto: total={total}, recebidos={len(resultados)}; "
                f"usuarios={[str(u['id']) for u in usuarios]}"
            )
        return resultados, total

    if len(usuarios) == 1:
        raise RuntimeError(
            f"Usuário '{usuarios[0]['nome']}' (id={usuarios[0]['id']}) "
            f"possui {total} registros no dia, acima do limite de "
            f"{LIMITE_REQUISICAO}. A API precisaria de paginacao/recorte "
            "adicional; nao sera feita gravacao incompleta."
        )

    print(f"{indentacao}Grupo acima de {LIMITE_REQUISICAO}. Dividindo automaticamente.")
    meio = len(usuarios) // 2
    esquerda, direita = usuarios[:meio], usuarios[meio:]
    registros_esquerda, total_esquerda = coletar_grupo(
        auditoria, data_referencia, esquerda, nivel + 1
    )
    registros_direita, total_direita = coletar_grupo(
        auditoria, data_referencia, direita, nivel + 1
    )
    total_filhos = total_esquerda + total_direita
    if len(registros_esquerda) + len(registros_direita) != total_filhos:
        raise DivergenciaAuditoria("Quantidade efetiva dos filhos nao corresponde aos totais.")

    if total_filhos != total:
        # Distinguir uma atualizacao retroativa do IILEX de inconsistencias
        # persistentes (filtros sobrepostos, total incorreto etc.).
        sobrepostos = sum((
            Counter(map(canonicalizar, registros_esquerda))
            & Counter(map(canonicalizar, registros_direita))
        ).values())
        print(
            f"{indentacao}ALERTA: pai={total}; filhos={total_filhos}; "
            f"registros identicos entre subgrupos={sobrepostos}. "
            "Reconsultando pai com pausa controlada.",
            flush=True,
        )
        confirmado = False
        historico_totais = [total]
        for tentativa in range(1, RECONSULTAS_TOTAL_PAI + 1):
            total_novo, _ = auditoria.consultar(
                data_referencia=data_referencia,
                usuarios=usuarios,
                limite=1,  # reconsulta apenas o contador, nao e paginacao
                descricao=f"reconferencia total pai {tentativa}/{RECONSULTAS_TOTAL_PAI}",
            )
            historico_totais.append(total_novo)
            if total_novo == total_filhos:
                confirmado = True
                print(
                    f"{indentacao}Total pai atualizado e reconciliado: "
                    f"{total} -> {total_novo}. Nenhum registro descartado.",
                    flush=True,
                )
                break
        if not confirmado:
            raise DivergenciaAuditoria(
                "Divisao do grupo nao fechou apos reconferencia. "
                f"Totais do pai={historico_totais}, filhos={total_filhos}, "
                f"sobrepostos={sobrepostos}, "
                f"ids_esquerda={[str(u['id']) for u in esquerda]}, "
                f"ids_direita={[str(u['id']) for u in direita]}."
            )

    return registros_esquerda + registros_direita, total_filhos


def canonicalizar(
    registro,
):
    return json.dumps(
        registro,
        ensure_ascii=False,
        sort_keys=True,
        separators=(
            ",",
            ":",
        ),
    )


def gerar_hash(
    canonical,
    ocorrencia,
):
    base = (
        f"{canonical}"
        f"|ocorrencia={ocorrencia}"
    )

    return hashlib.sha256(
        base.encode(
            "utf-8"
        )
    ).hexdigest()


# ---- Rotina diária e recuperação (complemento do arquivo recebido) ----
# O código recebido terminava no início de converter_inteiro().
# Esta implementacao valida hashes anteriores antes de inserir qualquer registro.
import argparse
from collections import Counter
from zoneinfo import ZoneInfo

FUSO = ZoneInfo("America/Recife")


def converter_inteiro(valor):
    if valor is None or valor == "":
        return None
    try:
        return int(valor)
    except (TypeError, ValueError):
        raise ValueError(f"Valor inteiro inválido: {valor!r}")


def obter_campo(registro, *nomes):
    for nome in nomes:
        if nome in registro and registro[nome] not in (None, ""):
            return registro[nome]
    return None


def obter_texto(registro, *nomes):
    v = obter_campo(registro, *nomes)
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    return str(v)


def interpretar_data(valor):
    if valor is None or valor == "":
        return None
    if isinstance(valor, (int, float)):
        # Epoch em segundos / milissegundos
        epoch = valor / 1000 if valor > 10**11 else valor
        return datetime.fromtimestamp(epoch, FUSO).replace(tzinfo=None)
    if not isinstance(valor, str):
        raise ValueError(f"Formato de data inesperado: {type(valor).__name__}")
    valor = valor.strip()
    candidatos = ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y")
    try:
        parsed = datetime.fromisoformat(valor.replace("Z", "+00:00"))
    except ValueError:
        for formato in candidatos:
            try:
                parsed = datetime.strptime(valor, formato)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"Data de evento desconhecida: {valor!r}")
    if parsed.tzinfo:
        parsed = parsed.astimezone(FUSO).replace(tzinfo=None)
    return parsed


def construir_linha(registro, dia, hash_log):
    if not isinstance(registro, dict):
        raise ValueError(f"Log não é objeto JSON: {type(registro).__name__}")
    evento = interpretar_data(obter_campo(
        registro, "data_evento", "dataEvento", "dataevento", "data_hora",
        "dataHora", "datahora", "data", "date", "created_at", "datetime",
        "dataacao", "dataAcao", "data_cadastro"
    ))
    if evento is None:
        raise ValueError(
            "Não encontrei o campo da data do evento no retorno do IILEX. "
            f"Chaves disponíveis: {sorted(registro.keys())}. "
            "Não grave os dados antes de conferir o mapeamento."
        )
    if evento.date() != dia:
        raise ValueError(
            f"IILEX retornou evento de {evento.date()} ao consultar {dia}. "
            "Verifique os limites dataDe/dataAte antes de gravar."
        )
    return {
        "hash_log": hash_log,
        "data_referencia": dia.isoformat(),
        "id_original": obter_texto(registro, "id", "id_original", "idlog", "idLog"),
        "idregistro": converter_inteiro(obter_campo(registro, "idregistro", "idRegistro")),
        "idmodulo": converter_inteiro(obter_campo(registro, "idmodulo", "idModulo")),
        "idtabela": converter_inteiro(obter_campo(registro, "idtabela", "idTabela")),
        "data_evento": evento.isoformat(sep=" "),
        "usuario": obter_texto(registro, "usuario", "nome_usuario", "nomeUsuario", "usuario_nome"),
        "acao": obter_texto(registro, "acao", "ação", "tipoAcao"),
        "modulo": obter_texto(registro, "modulo", "módulo", "nomeModulo"),
        "dados": obter_texto(registro, "dados", "descricao", "descrição", "detalhes"),
        "payload_raw": registro,
        "carregado_em": agora_utc(),
    }


def ler_registros_dia(sb, dia):
    """Paginação explícita: a API normalmente limita cada resposta a 1.000 linhas."""
    todos = []
    inicio = 0
    while True:
        pagina = (sb.table(TABELA_LOGS)
                  .select("hash_log,payload_raw")
                  .eq("data_referencia", dia.isoformat())
                  .order("hash_log")
                  .range(inicio, inicio + 499)
                  .execute().data or [])
        todos.extend(pagina)
        if len(pagina) < 500:
            return todos
        inicio += 500


def detectar_inicio_ocorrencia(sb):
    """Confirma que hashes novos reproduzem a regra já usada no banco."""
    referencia = date(2026, 10, 7)
    linhas = ler_registros_dia(sb, referencia)
    if not linhas:
        raise RuntimeError("Faltam os registros de 07/10 para verificar a compatibilidade do hash.")
    multiplicidades = Counter(canonicalizar(l["payload_raw"]) for l in linhas)
    hashes_atuais = {l["hash_log"] for l in linhas}
    for inicio in (0, 1):
        esperados = {
            gerar_hash(canonical, ocorrencia)
            for canonical, qtde in multiplicidades.items()
            for ocorrencia in range(inicio, inicio + qtde)
        }
        if esperados == hashes_atuais:
            return inicio
    raise RuntimeError(
        "Hashes existentes não correspondem à fórmula enviada, nem com "
        "ocorrência iniciando em 0 ou 1. Interrompido para evitar duplicação. "
        "Precisamos conferir a parte final do script Python original."
    )


def linhas_do_iilex(registros, dia, inicio_ocorrencia):
    contadores = defaultdict(int)
    linhas = []
    for registro in registros:
        canonical = canonicalizar(registro)
        ocorrencia = inicio_ocorrencia + contadores[canonical]
        contadores[canonical] += 1
        h = gerar_hash(canonical, ocorrencia)
        linhas.append(construir_linha(registro, dia, h))
    if len({linha["hash_log"] for linha in linhas}) != len(linhas):
        raise RuntimeError("Foram gerados hashes repetidos: gravação interrompida.")
    return linhas


def marcar_carga(sb, dia, status, total, mensagem, iniciado, concluido=None):
    """Conserva o formato ja utilizado na tabela logs_auditoria_cargas."""
    dados = {
        "data_referencia": dia.isoformat(),
        "status": status,
        "total_informado": total,
        "total_coletado": total,
        "limite_consulta": LIMITE_REQUISICAO,
        "mensagem": mensagem[:900],
        "iniciado_em": iniciado,
        "concluido_em": concluido,
        "atualizado_em": agora_utc(),
        "carregado_em": agora_utc(),
    }
    sb.table(TABELA_CARGAS).upsert(dados, on_conflict="data_referencia").execute()


def ler_status_cargas(sb, ultimo_dia):
    """Busca todos os dias da tabela de controle, com paginacao explicita."""
    statuses = {}
    inicio = 0
    while True:
        pagina = (
            sb.table(TABELA_CARGAS)
            .select("data_referencia,status")
            .gte("data_referencia", INICIO_HISTORICO.isoformat())
            .lte("data_referencia", ultimo_dia.isoformat())
            .order("data_referencia")
            .range(inicio, inicio + 499)
            .execute().data or []
        )
        for item in pagina:
            if item.get("data_referencia"):
                statuses[str(item["data_referencia"])[:10]] = str(item.get("status") or "").lower()
        if len(pagina) < 500:
            return statuses
        inicio += 500


def planejar_execucao_diaria(ontem, statuses, janela_revisao=JANELA_REVISAO_PADRAO,
                            max_pendentes=MAX_PENDENTES_PADRAO):
    """Retorna (datas, pendentes_adiados, motivos) sem consultar o IILEX.

    Ordem: ontem, pendencias da janela recente, pendencias historicas mais
    antigas (limitadas), revisao dos concluidos recentes. Todo dia nao
    concluido, ausente na tabela ou 'processando' entra como pendente.
    """
    if janela_revisao < 0 or max_pendentes < 0:
        raise ValueError("janela_revisao e max_pendentes devem ser >= 0")
    if ontem < INICIO_HISTORICO:
        return [], [], {}

    todos = [INICIO_HISTORICO + timedelta(days=i)
             for i in range((ontem - INICIO_HISTORICO).days + 1)]
    recentes = {ontem - timedelta(days=i) for i in range(janela_revisao)}
    recentes = {dia for dia in recentes if INICIO_HISTORICO <= dia <= ontem}

    def concluido(dia):
        return statuses.get(dia.isoformat()) == "concluido"

    # O ontem entra obrigatoriamente, mesmo se houver registro concluido.
    datas = [ontem]
    motivos = {ontem: "coleta de ontem"}
    recentes_pendentes = sorted(
        [d for d in recentes if d != ontem and not concluido(d)], reverse=True
    )
    historicos_pendentes = sorted(
        d for d in todos if d != ontem and d not in recentes and not concluido(d)
    )
    # O limite recai apenas sobre as pendencias historicas; recentes jamais
    # sao esquecidos por excesso de backlog antigo.
    escolhidos_historicos = historicos_pendentes[:max_pendentes]
    adiados = historicos_pendentes[max_pendentes:]

    for dia in recentes_pendentes:
        datas.append(dia)
        motivos[dia] = "recuperacao recente"
    for dia in escolhidos_historicos:
        datas.append(dia)
        motivos[dia] = "recuperacao historica"
    for dia in sorted(recentes, reverse=True):
        if dia != ontem and concluido(dia):
            datas.append(dia)
            motivos[dia] = "revisao de 7 dias" if janela_revisao == 7 else "revisao recente"

    return datas, adiados, motivos


def coletar_dia_com_retencao_de_erros(auditoria, usuarios, dia):
    """Refaz a coleta integral se a conferencia de um grupo for inconsistente."""
    for tentativa in range(1, TENTATIVAS_COLETA_DIA + 1):
        try:
            registros, total = coletar_grupo(auditoria, dia, usuarios)
            if len(registros) != total:
                raise DivergenciaAuditoria(
                    f"Coleta incompleta: total={total}; recebidos={len(registros)}"
                )
            return registros, total
        except DivergenciaAuditoria as erro:
            print(
                f"ALERTA dia {dia}: tentativa {tentativa}/{TENTATIVAS_COLETA_DIA} "
                f"nao passou na verificacao: {erro}", flush=True
            )
            if tentativa == TENTATIVAS_COLETA_DIA:
                raise
            # A classe AuditoriaIILEX sempre respeita 30s entre consultas.
    raise AssertionError("Inatingivel")


def processar_dia(sb, auditoria, usuarios, dia, inicio_ocorrencia, gravar):
    print(f"\n===== {dia} | {'GRAVAÇÃO' if gravar else 'SIMULAÇÃO'} =====", flush=True)
    iniciado = agora_utc()
    total = 0
    if gravar:
        # Antes a carga so era registrada apos a coleta completa. Agora
        # uma falha durante a coleta tambem fica rastreavel para recuperacao.
        marcar_carga(sb, dia, "processando", 0, "Coleta iniciada.", iniciado)
    try:
        registros, total = coletar_dia_com_retencao_de_erros(auditoria, usuarios, dia)
        novos = linhas_do_iilex(registros, dia, inicio_ocorrencia)
        antigos = ler_registros_dia(sb, dia)
        hashes_antigos = {item["hash_log"] for item in antigos}
        hashes_novos = {item["hash_log"] for item in novos}
        faltando = hashes_novos - hashes_antigos
        extras = hashes_antigos - hashes_novos
        print(f"IILEX={total} | banco={len(antigos)} | inserir={len(faltando)} | extras={len(extras)}")
        if extras:
            raise RuntimeError(
                f"Ha {len(extras)} registros no banco ausentes da coleta atual. "
                "Nenhuma exclusao automatica; investigar diferenca."
            )
        if not gravar:
            return
        inserir = [l for l in novos if l["hash_log"] in faltando]
        for i in range(0, len(inserir), LOTE_SUPABASE):
            sb.table(TABELA_LOGS).upsert(
                inserir[i:i + LOTE_SUPABASE],
                on_conflict="hash_log", ignore_duplicates=True
            ).execute()
        depois = ler_registros_dia(sb, dia)
        if len(depois) != total or {r["hash_log"] for r in depois} != hashes_novos:
            raise RuntimeError(
                f"Conferencia pos-gravacao falhou: esperado {total}, encontrado {len(depois)}."
            )
        marcar_carga(
            sb, dia, "concluido", total,
            "Carga integral conferida contra o total IILEX.", iniciado, agora_utc()
        )
        print(f"Dia {dia} CONCLUIDO, {total} logs conferidos.", flush=True)
    except Exception as exc:
        if gravar:
            try:
                # Preserva os registros antigos, mas deixa a data pendente.
                marcar_carga(sb, dia, "erro", total, str(exc), iniciado)
            except Exception as falha_controle:
                print(f"FALHA AO REGISTRAR ERRO DO DIA {dia}: {falha_controle}", flush=True)
        raise


def executar():
    print(f"Auditoria IILEX | versao {VERSAO_SCRIPT}", flush=True)
    parser = argparse.ArgumentParser(
        description="Auditoria IILEX com recuperacao automatica de dias pendentes"
    )
    parser.add_argument("--modo", choices=("diario", "recuperar"), required=True)
    parser.add_argument("--inicio", default=INICIO_HISTORICO.isoformat(),
                        help="Inicio inclusivo na recuperacao manual (AAAA-MM-DD)")
    parser.add_argument("--fim", default=None,
                        help="Fim inclusivo da recuperacao manual; padrao: ontem")
    parser.add_argument("--gravar", action="store_true",
                        help="Escreve no Supabase; sem esta flag apenas compara")
    parser.add_argument("--janela-revisao", type=int, default=JANELA_REVISAO_PADRAO,
                        help="Quantidade de dias recentes para revalidar (padrao: 7)")
    parser.add_argument("--max-pendentes", type=int, default=MAX_PENDENTES_PADRAO,
                        help="Limite de pendencias historicas por execucao (padrao: 7)")
    args = parser.parse_args()
    if args.janela_revisao < 0 or args.max_pendentes < 0:
        parser.error("--janela-revisao e --max-pendentes devem ser >= 0")

    ontem = datetime.now(FUSO).date() - timedelta(days=1)
    if args.modo == "diario":
        # Planejamento exige ler as cargas, sem consultar IILEX antes disso.
        if ontem < INICIO_HISTORICO:
            print("Nenhum dia a processar desde o inicio do historico.")
            return
    else:
        inicio = date.fromisoformat(args.inicio)
        fim = date.fromisoformat(args.fim) if args.fim else ontem
        if fim > ontem:
            raise ValueError("Nao e permitido coletar o dia atual ou futuro.")
        if inicio > fim:
            raise ValueError("Inicio maior que fim.")
        datas = [inicio + timedelta(days=i) for i in range((fim - inicio).days + 1)]

    if not all((user, senha, url_supabase, token_supabase)):
        raise RuntimeError(
            "Credenciais ausentes: confira os Repository Secrets ou cred.py local."
        )
    sb = create_client(url_supabase, token_supabase)
    print("Validando compatibilidade de hash com 07/10/2026...", flush=True)
    inicio_ocorrencia = detectar_inicio_ocorrencia(sb)
    print(f"Compatibilidade OK. Ocorrencias comecam em {inicio_ocorrencia}.", flush=True)

    adiados = []
    motivos = {}
    if args.modo == "diario":
        statuses = ler_status_cargas(sb, ontem)
        datas, adiados, motivos = planejar_execucao_diaria(
            ontem, statuses, args.janela_revisao, args.max_pendentes
        )
        print(
            f"Planejamento: {len(datas)} dia(s) nesta execucao; "
            f"{len(adiados)} pendencia(s) historica(s) para proximas execucoes.",
            flush=True,
        )
        for d in datas:
            print(f"  - {d}: {motivos[d]}", flush=True)
    else:
        print(f"Recuperacao manual: {datas[0]} ate {datas[-1]}", flush=True)

    usuarios = carregar_usuarios()
    print(f"{len(usuarios)} usuarios IILEX.", flush=True)
    auditoria = AuditoriaIILEX()
    erros = []
    for indice, dia in enumerate(datas):
        try:
            processar_dia(sb, auditoria, usuarios, dia, inicio_ocorrencia, args.gravar)
        except (ErroAutenticacao, ErroServidor) as exc:
            # 401/403/429/5xx: parar imediatamente e evitar trafego adicional.
            print(f"FALHA CRITICA em {dia}: {exc}", flush=True)
            raise
        except Exception as exc:
            # Demais dias continuam, e a falha persiste na tabela de controle.
            print(f"ERRO em {dia}: {exc}", flush=True)
            erros.append((dia.isoformat(), str(exc)))
        if indice < len(datas) - 1:
            print(f"Pausa entre dias: {PAUSA_ENTRE_DIAS}s", flush=True)
            time.sleep(PAUSA_ENTRE_DIAS)
    if adiados:
        print(
            "Pendencias historicas adiadas para futuras execucoes:",
            [d.isoformat() for d in adiados], flush=True,
        )
    if erros:
        print("Datas com erro (serao tentadas novamente):", erros, flush=True)
        raise SystemExit(1)
    print("Execucao terminada sem divergencias nas datas processadas.", flush=True)


if __name__ == "__main__":
    executar()
