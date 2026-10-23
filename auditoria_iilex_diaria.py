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


BASE_URL = "https://juscash.iilex.com.br/sistema"

URL_LOGIN = f"{BASE_URL}/controllers/login_p.php"
URL_AUDITORIA = f"{BASE_URL}/controllers/filtroauditoria_p.php"
URL_PAGINA_AUDITORIA = f"{BASE_URL}/modulos/filtroauditoria"

TABELA_LOGS = "logs_auditoria_raw"
TABELA_CARGAS = "logs_auditoria_cargas"

# As datas são definidas pelos modos --modo diario/recuperar.

LIMITE_REQUISICAO = 1000

PAUSA_ENTRE_AUDITORIAS = 30
PAUSA_ENTRE_DIAS = 60

LOTE_SUPABASE = 200

# Erros aparecem no console; o controle persistente fica em logs_auditoria_cargas.


class ErroAutenticacao(Exception):
    pass


class ErroServidor(Exception):
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


def coletar_grupo(
    auditoria,
    data_referencia,
    usuarios,
    nivel=0,
):
    indentacao = (
        "    "
        * nivel
    )

    descricao = (
        f"grupo com "
        f"{len(usuarios)} usuário(s)"
    )

    total, resultados = (
        auditoria.consultar(
            data_referencia=(
                data_referencia
            ),
            usuarios=usuarios,
            limite=LIMITE_REQUISICAO,
            descricao=descricao,
        )
    )

    print(
        f"{indentacao}"
        f"{nome_grupo(usuarios)} "
        f"| total={total}"
    )

    if total == 0:
        if resultados:
            raise RuntimeError(
                "Grupo com total zero "
                "retornou registros."
            )

        return (
            [],
            0,
        )

    if total <= LIMITE_REQUISICAO:
        if len(resultados) != total:
            raise RuntimeError(
                "Grupo retornou quantidade "
                "incompleta. "
                f"Total={total}, "
                f"recebidos={len(resultados)}"
            )

        return (
            resultados,
            total,
        )

    if len(usuarios) == 1:
        raise RuntimeError(
            f"Usuário '{usuarios[0]['nome']}' "
            f"possui {total} registros no dia, "
            f"acima do limite de "
            f"{LIMITE_REQUISICAO}. "
            "O dia ficará para tratamento específico."
        )

    print(
        f"{indentacao}"
        f"Grupo acima de "
        f"{LIMITE_REQUISICAO}. "
        "Dividindo automaticamente."
    )

    meio = (
        len(usuarios)
        // 2
    )

    esquerda = (
        usuarios[:meio]
    )

    direita = (
        usuarios[meio:]
    )

    (
        registros_esquerda,
        total_esquerda,
    ) = coletar_grupo(
        auditoria=auditoria,
        data_referencia=(
            data_referencia
        ),
        usuarios=esquerda,
        nivel=nivel + 1,
    )

    (
        registros_direita,
        total_direita,
    ) = coletar_grupo(
        auditoria=auditoria,
        data_referencia=(
            data_referencia
        ),
        usuarios=direita,
        nivel=nivel + 1,
    )

    total_filhos = (
        total_esquerda
        + total_direita
    )

    if total_filhos != total:
        raise RuntimeError(
            "Divisão do grupo não fechou. "
            f"Grupo pai={total}, "
            f"filhos={total_filhos}"
        )

    return (
        registros_esquerda
        + registros_direita,
        total_filhos,
    )


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


def processar_dia(sb, auditoria, usuarios, dia, inicio_ocorrencia, gravar):
    print(f"\n===== {dia} | {'GRAVAÇÃO' if gravar else 'SIMULAÇÃO'} =====", flush=True)
    # Coletar e validar o lote inteiro ANTES de inserir qualquer linha.
    registros, total = coletar_grupo(auditoria, dia, usuarios)
    if len(registros) != total:
        raise RuntimeError(f"Coleta incompleta: total IILEX={total}, recebidos={len(registros)}")
    novos = linhas_do_iilex(registros, dia, inicio_ocorrencia)
    antigos = ler_registros_dia(sb, dia)
    hashes_antigos = {item["hash_log"] for item in antigos}
    hashes_novos = {item["hash_log"] for item in novos}
    faltando = hashes_novos - hashes_antigos
    extras = hashes_antigos - hashes_novos
    print(f"IILEX={total} | banco={len(antigos)} | inserir={len(faltando)} | extras={len(extras)}")
    if extras:
        # Um registro antigo não veio no novo resultado: pode ter sido removido/alterado.
        # Nunca concluir um dia sem investigar isso, e nunca apagar automaticamente.
        raise RuntimeError(
            f"Há {len(extras)} registros no banco ausentes da coleta atual. "
            "Nenhuma alteração feita neste dia; investigar a diferença antes de prosseguir."
        )
    if not gravar:
        return
    iniciado = agora_utc()
    marcar_carga(sb, dia, "processando", total, "Atualização iniciada.", iniciado)
    try:
        inserir = [l for l in novos if l["hash_log"] in faltando]
        for i in range(0, len(inserir), LOTE_SUPABASE):
            lote = inserir[i:i+LOTE_SUPABASE]
            # DO NOTHING em conflito de hash_log, sem alterar logs antigos.
            sb.table(TABELA_LOGS).upsert(
                lote, on_conflict="hash_log", ignore_duplicates=True
            ).execute()
        depois = ler_registros_dia(sb, dia)
        if len(depois) != total or {r["hash_log"] for r in depois} != hashes_novos:
            raise RuntimeError(
                f"Conferência pós-gravação falhou: esperado {total}, encontrado {len(depois)}."
            )
        marcar_carga(sb, dia, "concluido", total,
                     "Carga integral conferida contra o total IILEX.", iniciado, agora_utc())
        print(f"Dia {dia} CONCLUÍDO, {total} logs conferidos.", flush=True)
    except Exception as exc:
        marcar_carga(sb, dia, "erro", total, str(exc), iniciado)
        raise


def executar():
    parser = argparse.ArgumentParser(description="Coleta diária e recuperação da Auditoria IILEX")
    parser.add_argument("--modo", choices=("diario", "recuperar"), required=True)
    parser.add_argument("--inicio", default="2026-10-06", help="Início inclusivo do histórico (AAAA-MM-DD)")
    parser.add_argument("--fim", default=None, help="Fim inclusivo; padrão: ontem no fuso de Recife")
    parser.add_argument("--gravar", action="store_true", help="Grava no Supabase; sem esta opção, apenas compara")
    args = parser.parse_args()
    ontem = datetime.now(FUSO).date() - timedelta(days=1)
    if args.modo == "diario":
        datas = [ontem]
    else:
        inicio = date.fromisoformat(args.inicio)
        fim = date.fromisoformat(args.fim) if args.fim else ontem
        if fim > ontem:
            raise ValueError("Não é permitido coletar o dia atual ou o futuro.")
        if inicio > fim:
            raise ValueError("Início maior que o fim.")
        datas = [inicio + timedelta(days=i) for i in range((fim - inicio).days + 1)]
    if not all((user, senha, url_supabase, token_supabase)):
        raise RuntimeError("Credenciais ausentes: confira os Secrets IILEX_USERNAME, IILEX_PASSWORD, SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY.")
    sb = create_client(url_supabase, token_supabase)
    print("Validando compatibilidade de hash com 07/10/2026...", flush=True)
    inicio_ocorrencia = detectar_inicio_ocorrencia(sb)
    print(f"Compatibilidade OK. Ocorrências começam em {inicio_ocorrencia}.", flush=True)
    usuarios = carregar_usuarios()
    print(f"{len(usuarios)} usuários IILEX. Datas: {datas[0]} até {datas[-1]}.")
    auditoria = AuditoriaIILEX()
    erros = []
    for i, dia in enumerate(datas):
        try:
            processar_dia(sb, auditoria, usuarios, dia, inicio_ocorrencia, args.gravar)
        except (ErroAutenticacao, ErroServidor) as exc:
            # 401, 403, 429, 5xx: abortar a sequência, não gerar mais tráfego.
            print(f"FALHA CRÍTICA em {dia}: {exc}", flush=True)
            raise
        except Exception as exc:
            print(f"ERRO em {dia}: {exc}", flush=True)
            erros.append((dia.isoformat(), str(exc)))
        if i < len(datas) - 1:
            print(f"Pausa entre dias: {PAUSA_ENTRE_DIAS}s")
            time.sleep(PAUSA_ENTRE_DIAS)
    if erros:
        print("Datas pendentes:", erros)
        raise SystemExit(1)
    print("Processamento finalizado sem divergências.")


if __name__ == "__main__":
    executar()
