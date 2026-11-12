"""Testes OFFLINE para auditoria_iilex_diaria.py.

Execute: python -m unittest -v test_auditoria_iilex_diaria.py
Nenhum teste acessa IILEX, Supabase ou a internet.
"""
import importlib.util
import os
import runpy
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch, Mock

# O projeto instala supabase via requirements.txt. O stub permite testar
# exclusivamente a logica Python mesmo antes de instalar dependencias.
try:
    import supabase  # noqa: F401
except ImportError:
    sys.modules["supabase"] = types.SimpleNamespace(create_client=lambda *args: None)

ARQUIVO = Path(__file__).resolve().with_name("auditoria_iilex_diaria.py")
spec = importlib.util.spec_from_file_location("auditoria_iilex_diaria", ARQUIVO)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class Resultado:
    def __init__(self, data):
        self.data = data


class TabelaFalsa:
    def __init__(self, banco, nome):
        self.banco = banco
        self.nome = nome
        self.criterios = []
        self.ordenacao = None
        self.paginacao = None
        self.insercao = None

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, coluna, valor):
        self.criterios.append(("eq", coluna, valor))
        return self

    def gte(self, coluna, valor):
        self.criterios.append(("gte", coluna, valor))
        return self

    def lte(self, coluna, valor):
        self.criterios.append(("lte", coluna, valor))
        return self

    def order(self, coluna):
        self.ordenacao = coluna
        return self

    def range(self, inicio, fim):
        self.paginacao = (inicio, fim)
        return self

    def upsert(self, linhas, on_conflict, ignore_duplicates=False):
        self.insercao = (linhas, on_conflict, ignore_duplicates)
        return self

    def execute(self):
        if self.insercao is not None:
            linhas, chave, ignore = self.insercao
            if isinstance(linhas, dict):
                linhas = [linhas]
            atual = self.banco.dados[self.nome]
            for linha in linhas:
                correspondentes = [i for i, antigo in enumerate(atual)
                                  if antigo.get(chave) == linha.get(chave)]
                if correspondentes:
                    if not ignore:
                        atual[correspondentes[0]] = dict(linha)
                else:
                    atual.append(dict(linha))
            return Resultado(linhas)

        resultado = list(self.banco.dados[self.nome])
        for op, coluna, valor in self.criterios:
            if op == "eq":
                resultado = [r for r in resultado if r.get(coluna) == valor]
            elif op == "gte":
                resultado = [r for r in resultado if r.get(coluna, "") >= valor]
            else:
                resultado = [r for r in resultado if r.get(coluna, "") <= valor]
        if self.ordenacao:
            resultado.sort(key=lambda r: r.get(self.ordenacao, ""))
        if self.paginacao:
            inicio, fim = self.paginacao
            resultado = resultado[inicio:fim + 1]
        return Resultado(resultado)


class BancoFalso:
    def __init__(self):
        self.dados = {mod.TABELA_LOGS: [], mod.TABELA_CARGAS: []}

    def table(self, nome):
        return TabelaFalsa(self, nome)


class AuditoriaFalsa:
    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.calls = []

    def consultar(self, **kwargs):
        self.calls.append(kwargs)
        if not self.respostas:
            raise AssertionError("O teste fez mais consultas do que o esperado")
        return self.respostas.pop(0)


class TestesCredenciais(unittest.TestCase):
    def test_secrets_atuais_github_actions_sao_aceitos(self):
        variaveis = {
            "IILEX_USERNAME": "usuario_github",
            "IILEX_PASSWORD": "senha_github",
            "SUPABASE_URL": "https://exemplo.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "token_teste",
        }
        with patch.dict(os.environ, variaveis, clear=True):
            carregado = runpy.run_path(str(ARQUIVO), run_name="teste_sem_execucao")
        self.assertEqual(carregado["user"], "usuario_github")
        self.assertEqual(carregado["senha"], "senha_github")
        self.assertEqual(carregado["url_supabase"], variaveis["SUPABASE_URL"])
        self.assertEqual(carregado["token_supabase"], variaveis["SUPABASE_SERVICE_ROLE_KEY"])
        self.assertEqual(carregado["VERSAO_SCRIPT"], "2026-11-12-v7-pendencias-desde-20260601")

    def test_credenciais_legadas_continuam_aceitas(self):
        variaveis = {
            "IILEX_USER": "usuario_antigo",
            "IILEX_SENHA": "senha_antiga",
            "SUPABASE_URL": "https://exemplo.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "token_teste",
        }
        with patch.dict(os.environ, variaveis, clear=True):
            carregado = runpy.run_path(str(ARQUIVO), run_name="teste_sem_execucao")
        self.assertEqual(carregado["user"], "usuario_antigo")
        self.assertEqual(carregado["senha"], "senha_antiga")


class TestesPlanejamento(unittest.TestCase):
    def test_sem_dias_antes_da_data_inicial(self):
        self.assertEqual(mod.INICIO_HISTORICO, date(2026, 6, 1))
        datas, adiados, motivos = mod.planejar_execucao_diaria(date(2026, 5, 31), {})
        self.assertEqual((datas, adiados, motivos), ([], [], {}))

    def test_data_inicial_01_06_e_inclusiva(self):
        datas, adiados, motivos = mod.planejar_execucao_diaria(date(2026, 6, 1), {})
        self.assertEqual(datas, [date(2026, 6, 1)])
        self.assertEqual(adiados, [])
        self.assertEqual(motivos[date(2026, 6, 1)], "coleta de ontem")

    def test_nenhuma_data_antes_de_junho_entra_no_planejamento(self):
        hoje = date(2026, 6, 4)
        statuses = {"2026-05-29": "erro", "2026-05-31": "erro", "2026-06-02": "concluido"}
        datas, adiados, _ = mod.planejar_execucao_diaria(hoje, statuses, 7, 7)
        self.assertEqual(datas, [date(2026, 6, 4), date(2026, 6, 3), date(2026, 6, 1)])
        self.assertEqual(adiados, [])
        self.assertTrue(all(d >= date(2026, 6, 1) for d in datas))

    def test_ontem_pendente_entra_primeiro(self):
        ontem = date(2026, 11, 11)
        statuses = {(ontem-timedelta(days=i)).isoformat(): "concluido"
                    for i in range(1, (ontem - mod.INICIO_HISTORICO).days + 1)}
        d, _, m = mod.planejar_execucao_diaria(ontem, statuses, 1, 7)
        self.assertEqual(d, [ontem])
        self.assertEqual(m[ontem], "coleta de ontem")

    def test_ontem_concluido_nao_entra_nem_se_rodar_novamente(self):
        ontem = date(2026, 11, 11)
        statuses = {(mod.INICIO_HISTORICO + timedelta(days=i)).isoformat(): "concluido"
                    for i in range((ontem-mod.INICIO_HISTORICO).days+1)}
        d, adiados, motivos = mod.planejar_execucao_diaria(ontem, statuses)
        self.assertEqual((d, adiados, motivos), ([], [], {}))

    def test_falhas_e_dias_sem_marcador_sao_recuperados(self):
        ontem = date(2026, 10, 12)
        status = {"2026-10-10": "erro", "2026-10-11": "processando",
                  "2026-10-08": "concluido"}
        datas, adiados, motivos = mod.planejar_execucao_diaria(ontem, status, 3, 2)
        self.assertEqual(datas[0], ontem)
        self.assertEqual(datas[1], date(2026, 10, 11))
        self.assertEqual(datas[-1], date(2026, 10, 10))
        self.assertIn(date(2026, 6, 1), datas)
        self.assertIn(date(2026, 6, 2), datas)
        self.assertIn(date(2026, 6, 3), adiados)
        self.assertIn(date(2026, 10, 9), adiados)
        self.assertNotIn(date(2026, 10, 8), datas)
        self.assertEqual(motivos[date(2026, 10, 11)], "recuperacao recente")

    def test_concluidos_ultimos_7_dias_nao_sao_reprocessados(self):
        ontem = date(2026, 10, 12)
        status = {(mod.INICIO_HISTORICO + timedelta(days=i)).isoformat(): "concluido"
                  for i in range((ontem - mod.INICIO_HISTORICO).days + 1)}
        datas, adiados, motivos = mod.planejar_execucao_diaria(ontem, status, 7, 0)
        self.assertEqual(datas, [])
        self.assertEqual(adiados, [])
        self.assertEqual(motivos, {})

    def test_ontem_concluido_e_dias_anteriores_falhos(self):
        ontem = date(2026, 10, 12)
        status = {(mod.INICIO_HISTORICO + timedelta(days=i)).isoformat(): "concluido"
                  for i in range((ontem - mod.INICIO_HISTORICO).days + 1)}
        status["2026-10-10"] = "erro"
        status["2026-10-09"] = "processando"
        datas, adiados, motivos = mod.planejar_execucao_diaria(ontem, status, 7, 7)
        self.assertEqual(datas, [date(2026, 10, 9), date(2026, 10, 10)])
        self.assertEqual(adiados, [])
        self.assertIn("executado ao final", motivos[date(2026, 10, 10)])

    def test_planejamento_so_ontem_quando_passado_concluido(self):
        ontem = date(2026, 11, 11)
        status = {(mod.INICIO_HISTORICO + timedelta(days=i)).isoformat(): "concluido"
                  for i in range((ontem - mod.INICIO_HISTORICO).days)}
        datas, adiados, motivos = mod.planejar_execucao_diaria(ontem, status)
        self.assertEqual(datas, [ontem])
        self.assertEqual(adiados, [])
        self.assertEqual(motivos, {ontem: "coleta de ontem"})

    def test_nao_cria_pendencias_artificiais_de_concluidos(self):
        ontem = date(2026, 10, 12)
        status = {(mod.INICIO_HISTORICO + timedelta(days=i)).isoformat(): "concluido"
                  for i in range((ontem - mod.INICIO_HISTORICO).days + 1)}
        status["2026-10-08"] = "erro"
        status["2026-10-11"] = "concluido"
        datas, _, _ = mod.planejar_execucao_diaria(ontem, status, 7, 7)
        self.assertEqual(datas, [date(2026, 10, 8)])

    def test_ler_cargas_com_mais_de_500_linhas(self):
        banco = BancoFalso()
        for i in range(600):
            d = date(2026, 10, 6) + timedelta(days=i)
            banco.dados[mod.TABELA_CARGAS].append({"data_referencia": d.isoformat(), "status": "concluido"})
        fim = date(2026, 10, 6) + timedelta(days=599)
        statuses = mod.ler_status_cargas(banco, fim)
        self.assertEqual(len(statuses), 600)
        self.assertEqual(statuses[fim.isoformat()], "concluido")


class TestesFalhasIsoladas(unittest.TestCase):
    def test_pendencia_erro_ao_final_sem_perder_pendencia(self):
        ontem = date(2026, 11, 11)
        statuses = {
            "2026-11-10": "concluido",
            "2026-11-09": "erro",
            "2026-11-08": "processando",
            "2026-11-07": "concluido",
        }
        datas, adiados, motivos = mod.planejar_execucao_diaria(
            ontem, statuses, janela_revisao=7, max_pendentes=3,
        )
        self.assertEqual(datas[0], ontem)
        self.assertLess(datas.index(date(2026, 11, 8)), datas.index(date(2026, 11, 9)))
        self.assertNotIn(date(2026, 11, 10), datas)  # concluido: nao revisitar
        self.assertIn("executado ao final", motivos[date(2026, 11, 9)])
        self.assertIn(date(2026, 11, 9), datas)

    def test_500_persistente_e_classificado_sem_repeticao_infinita(self):
        s1, s2 = Mock(), Mock()
        s1.post.return_value = Mock(status_code=500)
        s2.post.return_value = Mock(status_code=500)
        a = mod.AuditoriaIILEX()
        with (patch.object(mod, "criar_sessao_http", side_effect=[s1, s2]),
              patch.object(mod, "fazer_login"),
              patch.object(mod.time, "sleep") as dormir):
            with self.assertRaises(mod.ErroServidorPersistente):
                a.consultar(date(2026, 11, 9))
        self.assertEqual(a.numero_consultas, 2)
        self.assertEqual(s1.post.call_count, 1)
        self.assertEqual(s2.post.call_count, 1)
        self.assertTrue(any(c.args == (120,) for c in dormir.call_args_list))

    def test_429_nao_e_erro_servidor_persistente(self):
        with self.assertRaises(mod.ErroServidor) as cm:
            mod.validar_http(Mock(status_code=429))
        self.assertNotIsInstance(cm.exception, mod.ErroServidorPersistente)

    def test_500_marca_dia_sem_alterar_logs(self):
        banco = BancoFalso()
        a = AuditoriaFalsa([])
        def raise_persistente(**kwargs):
            raise mod.ErroServidorPersistente("HTTP 500 persistente")
        a.consultar = raise_persistente
        with self.assertRaises(mod.ErroServidorPersistente):
            mod.processar_dia(
                banco, a, [{"id": "1", "nome": "Ana"}],
                date(2026, 11, 9), 1, True,
            )
        self.assertEqual(banco.dados[mod.TABELA_LOGS], [])
        self.assertEqual(banco.dados[mod.TABELA_CARGAS][0]["status"], "erro")

    def test_erro_temporario_nao_e_erro_de_auth(self):
        self.assertFalse(issubclass(mod.ErroServidorPersistente, mod.ErroAutenticacao))


class TestesIntegridadeColeta(unittest.TestCase):
    def setUp(self):
        self.usuarios = [{"id": "1", "nome": "Ana"}, {"id": "2", "nome": "Bia"}]
        self.dia = date(2026, 11, 10)
        self.a = [{"id": i, "data_evento": "2026-11-10 09:00:00"} for i in range(600)]
        self.b = [{"id": i + 600, "data_evento": "2026-11-10 09:00:00"} for i in range(601)]

    def test_folha_coleta_exata(self):
        a = AuditoriaFalsa([(2, [{"id": 1}, {"id": 2}])])
        itens, total = mod.coletar_grupo(a, self.dia, self.usuarios)
        self.assertEqual(total, 2)
        self.assertEqual(len(itens), 2)

    def test_folha_incompleta_bloqueia(self):
        a = AuditoriaFalsa([(3, [{"id": 1}])])
        with self.assertRaises(mod.DivergenciaAuditoria):
            mod.coletar_grupo(a, self.dia, self.usuarios)

    def test_reconsulta_total_pai_atualizado_aceita(self):
        respostas = [(1198, []), (600, self.a), (601, self.b), (1201, [])]
        a = AuditoriaFalsa(respostas)
        itens, total = mod.coletar_grupo(a, self.dia, self.usuarios)
        self.assertEqual((len(itens), total), (1201, 1201))
        self.assertEqual(a.calls[3]["limite"], 1)

    def test_divergencia_persistente_nao_aceita(self):
        respostas = [(1198, []), (600, self.a), (601, self.b), (1198, []), (1198, [])]
        a = AuditoriaFalsa(respostas)
        with self.assertRaisesRegex(mod.DivergenciaAuditoria, "Divisao do grupo"):
            mod.coletar_grupo(a, self.dia, self.usuarios)
        self.assertEqual(len(a.calls), 5)

    def test_coleta_inteira_repetida_apos_divergencia(self):
        respostas = [(1198, []), (600, self.a), (601, self.b), (1198, []), (1198, []),
                     (1201, []), (600, self.a), (601, self.b)]
        a = AuditoriaFalsa(respostas)
        itens, total = mod.coletar_dia_com_retencao_de_erros(a, self.usuarios, self.dia)
        self.assertEqual(len(itens), 1201)
        self.assertEqual(total, 1201)
        self.assertEqual(len(a.calls), 8)

    def test_um_usuario_acima_do_limite_protege_gravacao(self):
        a = AuditoriaFalsa([(1001, [])])
        with self.assertRaisesRegex(RuntimeError, "paginacao/recorte"):
            mod.coletar_grupo(a, self.dia, [self.usuarios[0]])

    def test_hashs_distinguem_duplicatas_legitimas(self):
        registro = {"data_evento": "2026-11-10 10:00:00", "usuario": "Ana"}
        a = mod.linhas_do_iilex([registro, dict(registro)], self.dia, 1)
        self.assertEqual(len({r["hash_log"] for r in a}), 2)
        self.assertEqual(
            [r["hash_log"] for r in a],
            [r["hash_log"] for r in mod.linhas_do_iilex([registro, dict(registro)], self.dia, 1)]
        )
        expected = mod.gerar_hash(mod.canonicalizar(registro), 1)
        self.assertEqual(a[0]["hash_log"], expected)

    def test_hashs_compativeis_com_formulas_antigas(self):
        banco = BancoFalso()
        dia = date(2026, 10, 7)
        registro = {"id": "A", "data_evento": "2026-10-07 10:00:00"}
        chave = mod.gerar_hash(mod.canonicalizar(registro), 1)
        banco.dados[mod.TABELA_LOGS].append({"hash_log": chave, "payload_raw": registro,
                                             "data_referencia": dia.isoformat()})
        self.assertEqual(mod.detectar_inicio_ocorrencia(banco), 1)


class TestesGravacao(unittest.TestCase):
    def setUp(self):
        self.banco = BancoFalso()
        self.dia = date(2026, 11, 10)
        self.usuarios = [{"id": "1", "nome": "Ana"}]
        self.registro = {"id": "123", "data_evento": "2026-11-10 10:00:00",
                         "usuario": "Ana", "acao": "criou"}

    def executar(self, respostas, gravar=True):
        a = AuditoriaFalsa(respostas)
        mod.processar_dia(self.banco, a, self.usuarios, self.dia, 1, gravar)

    def test_erro_antes_da_gravacao_fica_marcado(self):
        with self.assertRaises(mod.DivergenciaAuditoria):
            self.executar([(2, [self.registro]), (2, [self.registro])])
        linhas = self.banco.dados[mod.TABELA_CARGAS]
        self.assertEqual(len(linhas), 1)
        self.assertEqual(linhas[0]["status"], "erro")
        self.assertEqual(self.banco.dados[mod.TABELA_LOGS], [])

    def test_gravacao_concluida_e_idempotente(self):
        self.executar([(1, [self.registro])])
        self.executar([(1, [self.registro])])
        self.assertEqual(len(self.banco.dados[mod.TABELA_LOGS]), 1)
        self.assertEqual(self.banco.dados[mod.TABELA_CARGAS][0]["status"], "concluido")
        self.assertEqual(self.banco.dados[mod.TABELA_CARGAS][0]["total_coletado"], 1)

    def test_log_retroativo_e_adicionado_sem_repetir_antigo(self):
        novo = {"id": "124", "data_evento": "2026-11-10 11:00:00",
                "usuario": "Ana", "acao": "alterou"}
        self.executar([(1, [self.registro])])
        self.executar([(2, [self.registro, novo])])
        linhas = self.banco.dados[mod.TABELA_LOGS]
        self.assertEqual(len(linhas), 2)
        self.assertEqual({r["payload_raw"]["id"] for r in linhas}, {"123", "124"})
        self.assertEqual(self.banco.dados[mod.TABELA_CARGAS][0]["status"], "concluido")

    def test_duplicata_legitima_em_atualizacao_posterior(self):
        self.executar([(1, [self.registro])])
        self.executar([(2, [self.registro, dict(self.registro)])])
        self.assertEqual(len(self.banco.dados[mod.TABELA_LOGS]), 2)
        self.assertEqual(len({x["hash_log"] for x in self.banco.dados[mod.TABELA_LOGS]}), 2)

    def test_dia_sem_logs_pode_ser_concluido(self):
        self.executar([(0, [])])
        self.assertEqual(len(self.banco.dados[mod.TABELA_LOGS]), 0)
        self.assertEqual(self.banco.dados[mod.TABELA_CARGAS][0]["status"], "concluido")
        self.assertEqual(self.banco.dados[mod.TABELA_CARGAS][0]["total_coletado"], 0)

    def test_http500_na_coleta_marca_dia_para_recuperacao(self):
        auditoria = AuditoriaFalsa([])
        def falhar(**_kwargs):
            raise mod.ErroServidor("IILEX indisponivel apos retry: HTTP 500")
        auditoria.consultar = falhar
        with self.assertRaises(mod.ErroServidor):
            mod.processar_dia(self.banco, auditoria, self.usuarios, self.dia, 1, True)
        cargas = self.banco.dados[mod.TABELA_CARGAS]
        self.assertEqual(cargas[0]["status"], "erro")
        self.assertEqual(self.banco.dados[mod.TABELA_LOGS], [])
        statuses = mod.ler_status_cargas(self.banco, self.dia)
        dias, _, _ = mod.planejar_execucao_diaria(self.dia, statuses)
        self.assertIn(self.dia, dias)

    def test_nao_apaga_log_anterior_desaparecido(self):
        self.executar([(1, [self.registro])])
        with self.assertRaisesRegex(RuntimeError, "ausentes"):
            self.executar([(0, [])])
        self.assertEqual(len(self.banco.dados[mod.TABELA_LOGS]), 1)
        self.assertEqual(self.banco.dados[mod.TABELA_CARGAS][0]["status"], "erro")

    def test_modo_simulacao_nao_muda_banco(self):
        self.executar([(1, [self.registro])], gravar=False)
        self.assertEqual(len(self.banco.dados[mod.TABELA_CARGAS]), 0)
        self.assertEqual(len(self.banco.dados[mod.TABELA_LOGS]), 0)


class TestesDataEHTTP(unittest.TestCase):
    def test_payload_data_ate_mesmo_dia_nao_vaza_dia_seguinte(self):
        dia = date(2026, 11, 10)
        payload = mod.montar_payload(dia, [{"id": "1", "nome": "Ana"}], 1000)
        campos = dict(payload)
        self.assertEqual(campos["dataDe"], "10/11/2026")
        self.assertEqual(campos["dataAte"], "10/11/2026")
        self.assertEqual(campos["limite"], "1000")
        self.assertEqual(payload[-1], ("usuarios[]", "1"))

    def test_evento_de_dia_seguinte_ainda_e_bloqueado(self):
        with self.assertRaisesRegex(ValueError, "evento de 2026-11-11"):
            mod.construir_linha(
                {"id": "1", "data_evento": "2026-11-11 00:00:01"},
                date(2026, 11, 10), "hash-falso",
            )

    def test_preflight_confirma_janela_de_um_dia(self):
        evento = {"id": "123", "data_evento": "2026-10-07 10:11:12"}
        auditoria = AuditoriaFalsa([(345, [evento])])
        mod.validar_filtro_dia_unico(auditoria, [{"id": "1", "nome": "Ana"}])
        self.assertEqual(auditoria.calls[0]["data_referencia"], date(2026, 10, 7))
        self.assertEqual(auditoria.calls[0]["limite"], 1)

    def test_preflight_bloqueia_filtro_exclusivo_que_nao_devolve_eventos(self):
        auditoria = AuditoriaFalsa([(0, [])])
        with self.assertRaisesRegex(RuntimeError, "bloqueada qualquer gravacao"):
            mod.validar_filtro_dia_unico(auditoria, [{"id": "1", "nome": "Ana"}])

    def test_preflight_bloqueia_data_extravasada(self):
        auditoria = AuditoriaFalsa([
            (2, [{"id": "123", "data_evento": "2026-10-08 09:00:00"}])
        ])
        with self.assertRaisesRegex(RuntimeError, "fora da data"):
            mod.validar_filtro_dia_unico(auditoria, [{"id": "1", "nome": "Ana"}])

    def test_http_500_e_temporario(self):
        with self.assertRaises(mod.ErroServidorTemporario):
            mod.validar_http(Mock(status_code=500))

    def test_http_403_e_429_nao_sao_repetidos(self):
        for status, classe in [(403, mod.ErroAutenticacao), (429, mod.ErroServidor)]:
            with self.subTest(status=status):
                with self.assertRaises(classe) as cm:
                    mod.validar_http(Mock(status_code=status))
                self.assertNotIsInstance(cm.exception, mod.ErroServidorTemporario)

    def test_500_repete_uma_vez_depois_de_cooldown(self):
        # A cada tentativa uma sessao, simulando a primeira com 500.
        s1 = Mock()
        s1.post.return_value = Mock(status_code=500)
        s2 = Mock()
        s2.post.return_value = Mock(status_code=200)
        s2.post.return_value.json.return_value = {"total": 1, "resultados": [{"id": 1}]}
        auditoria = mod.AuditoriaIILEX()
        with (patch.object(mod, "criar_sessao_http", side_effect=[s1, s2]),
              patch.object(mod, "fazer_login"),
              patch.object(mod.time, "sleep") as dormir):
            total, registros = auditoria.consultar(date(2026, 11, 10), [], 1000)
        self.assertEqual((total, len(registros)), (1, 1))
        self.assertEqual(auditoria.numero_consultas, 2)
        self.assertTrue(any(c.args == (120,) for c in dormir.call_args_list))
        self.assertEqual(s1.post.call_count, 1)
        self.assertEqual(s2.post.call_count, 1)

    def test_500_persistente_aborta_apos_duas_tentativas(self):
        s1, s2 = Mock(), Mock()
        s1.post.return_value = Mock(status_code=500)
        s2.post.return_value = Mock(status_code=503)
        auditoria = mod.AuditoriaIILEX()
        with (patch.object(mod, "criar_sessao_http", side_effect=[s1, s2]),
              patch.object(mod, "fazer_login"),
              patch.object(mod.time, "sleep") as dormir):
            with self.assertRaisesRegex(mod.ErroServidor, "apos 2 tentativa"):
                auditoria.consultar(date(2026, 11, 10))
        self.assertEqual(auditoria.numero_consultas, 2)
        self.assertTrue(any(c.args == (120,) for c in dormir.call_args_list))

    def test_429_nao_repete_consulta(self):
        s = Mock()
        s.post.return_value = Mock(status_code=429)
        auditoria = mod.AuditoriaIILEX()
        with (patch.object(mod, "criar_sessao_http", return_value=s),
              patch.object(mod, "fazer_login"),
              patch.object(mod.time, "sleep") as dormir):
            with self.assertRaisesRegex(mod.ErroServidor, "HTTP 429"):
                auditoria.consultar(date(2026, 11, 10))
        self.assertEqual(auditoria.numero_consultas, 1)
        dormir.assert_not_called()

    def test_timeout_repete_uma_vez(self):
        import requests
        s1, s2 = Mock(), Mock()
        s1.post.side_effect = requests.Timeout("tempo excedido")
        s2.post.return_value = Mock(status_code=200)
        s2.post.return_value.json.return_value = {"total": 0, "resultados": []}
        auditoria = mod.AuditoriaIILEX()
        with (patch.object(mod, "criar_sessao_http", side_effect=[s1, s2]),
              patch.object(mod, "fazer_login"),
              patch.object(mod.time, "sleep") as dormir):
            total, registros = auditoria.consultar(date(2026, 11, 10))
        self.assertEqual((total, registros), (0, []))
        self.assertTrue(any(c.args == (120,) for c in dormir.call_args_list))


if __name__ == "__main__":
    unittest.main(verbosity=2)


# Testes de orquestracao: nenhum IILEX/Supabase real eh acionado.
class TestesExecucaoResiliente(unittest.TestCase):
    def _rodar(self, statuses, falhas, janela=5):
        from datetime import datetime
        executados = []
        def executar_dia(sb, auditoria, usuarios, dia, inicio_ocorrencia, gravar):
            executados.append(dia)
            if dia in falhas:
                raise mod.ErroServidorPersistente("HTTP 500 apos duas tentativas")
        with (
            patch.object(mod, "datetime") as dt,
            patch.object(mod, "create_client", return_value=BancoFalso()),
            patch.multiple(mod, user="teste", senha="teste", url_supabase="https://example.supabase.co", token_supabase="fake"),
            patch.object(mod, "detectar_inicio_ocorrencia", return_value=1),
            patch.object(mod, "ler_status_cargas", return_value=statuses),
            patch.object(mod, "carregar_usuarios", return_value=[{"id":"1", "nome":"Ana"}]),
            patch.object(mod, "validar_filtro_dia_unico"),
            patch.object(mod, "processar_dia", side_effect=executar_dia),
            patch.object(mod.time, "sleep"),
            patch.object(sys, "argv", ["script", "--modo", "diario", "--gravar",
                                      "--janela-revisao", str(janela), "--max-pendentes", "0"]),
        ):
            dt.now.return_value = datetime(2026, 11, 12, 8, 0, tzinfo=mod.FUSO)
            with self.assertRaises(SystemExit) as cm:
                mod.executar()
            self.assertEqual(cm.exception.code, 1)
        return executados

    def test_um_dia_500_nao_impede_conclusao_dos_outros(self):
        statuses = {"2026-11-10":"concluido", "2026-11-09":"erro",
                    "2026-11-08":"processando", "2026-11-07":"concluido"}
        executados = self._rodar(statuses, {date(2026,11,9)}, janela=5)
        self.assertEqual(executados[0], date(2026,11,11))
        self.assertIn(date(2026,11,8), executados)
        self.assertNotIn(date(2026,11,7), executados)  # concluido
        self.assertNotIn(date(2026,11,10), executados) # concluido
        self.assertEqual(executados[-1], date(2026,11,9))

    def test_dois_dias_500_abrem_circuit_breaker_e_poupam_servidor(self):
        statuses = {"2026-11-10":"concluido", "2026-11-09":"erro",
                    "2026-11-08":"erro", "2026-11-07":"erro"}
        executados = self._rodar(statuses, {date(2026,11,9),date(2026,11,8)}, janela=5)
        self.assertIn(date(2026,11,9), executados)
        self.assertIn(date(2026,11,8), executados)
        self.assertNotIn(date(2026,11,7), executados)


class TestesFallbackHTTP500(unittest.TestCase):
    def setUp(self):
        self.usuarios = [{"id": str(i), "nome": f"Usuario {i}"} for i in range(1, 17)]
        self.dia = date(2026, 11, 9)

    def _evento(self, id_, marcador=None):
        return {"id": f"log-{id_}-{marcador or 1}", "usuario": str(id_),
                "data_evento": "2026-11-09 11:00:00"}

    def _auditoria(self, eventos_por_usuario, alterar_segunda=False, overlap=False,
                   falhar_subgrupo=False, erro_global=None):
        usuarios = self.usuarios
        class Falsa:
            def __init__(self):
                self.calls = []
                self.rodadas = 0
            def consultar(self, **kw):
                ids = [str(u["id"]) for u in kw.get("usuarios", [])]
                self.calls.append(ids)
                if set(ids) == {str(u["id"]) for u in usuarios}:
                    raise erro_global or mod.ErroHTTP500Persistente("500 global")
                if falhar_subgrupo and len(ids)==8:
                    raise mod.ErroHTTP500Persistente("500 em subgrupo")
                # 1/2 e 3/4 consultas sao particoes distintas.
                if len(ids)==8:
                    self.rodadas += 1
                registros = [r for id_ in ids for r in eventos_por_usuario.get(id_, [])]
                if alterar_segunda and self.rodadas >= 3 and ids and len(ids)==8:
                    registros = registros[:-1]  # altera a segunda particao
                if overlap and '9' in ids:
                    registros.append(self_evento('99', 'duplicado'))
                if overlap and '1' in ids:
                    registros.append(self_evento('99', 'duplicado'))
                return len(registros), registros
        self_evento = self._evento
        return Falsa()

    def test_http500_global_recupera_com_duas_particoes(self):
        eventos = {str(i): [self._evento(i)] for i in range(1,17)}
        # Duplicata legitima deve manter multiplicidade 2.
        eventos['1'].append(dict(eventos['1'][0]))
        auditoria = self._auditoria(eventos)
        regs, total = mod.coletar_dia_com_retencao_de_erros(
            auditoria, self.usuarios, self.dia)
        self.assertEqual((len(regs), total), (17,17))
        self.assertTrue(auditoria.coleta_por_particoes)
        self.assertEqual(len(auditoria.calls), 5)  # pai + 2 + 2
        self.assertEqual(mod.canonicalizar(regs[0]), mod.canonicalizar(regs[1]))

    def test_fallback_usa_divisoes_independentes(self):
        eventos = {str(i): [self._evento(i)] for i in range(1,17)}
        aud = self._auditoria(eventos)
        mod.coletar_dia_com_retencao_de_erros(aud, self.usuarios, self.dia)
        self.assertEqual(aud.calls[1], [str(i) for i in range(1,9)])
        self.assertEqual(aud.calls[3], [str(i) for i in range(1,17,2)])

    def test_fallback_recusa_dados_mutaveis(self):
        eventos = {str(i): [self._evento(i)] for i in range(1,17)}
        aud = self._auditoria(eventos, alterar_segunda=True)
        with self.assertRaisesRegex(mod.DivergenciaAuditoria, 'independentes discordam'):
            mod.coletar_dia_com_retencao_de_erros(aud, self.usuarios, self.dia)

    def test_fallback_recusa_sobreposicao(self):
        eventos = {str(i): [self._evento(i)] for i in range(1,17)}
        aud = self._auditoria(eventos, overlap=True)
        with self.assertRaisesRegex(mod.DivergenciaAuditoria, 'apareceram em ambos'):
            mod.coletar_dia_com_retencao_de_erros(aud, self.usuarios, self.dia)

    def test_fallback_subgrupo_http500_recuperado_por_divisao(self):
        eventos = {str(i): [self._evento(i)] for i in range(1,17)}
        aud = self._auditoria(eventos, falhar_subgrupo=True)
        registros, total = mod.coletar_dia_com_retencao_de_erros(
            aud, self.usuarios, self.dia)
        self.assertEqual((len(registros), total), (16,16))
        self.assertTrue(aud.coleta_por_particoes)
        self.assertTrue(any(len(ids)==4 for ids in aud.calls))

    def test_timeout_nao_ativa_particoes(self):
        aud = self._auditoria({}, erro_global=mod.ErroServidorPersistente('timeout'))
        with self.assertRaisesRegex(mod.ErroServidorPersistente,'timeout'):
            mod.coletar_dia_com_retencao_de_erros(aud, self.usuarios, self.dia)
        self.assertEqual(len(aud.calls),1)

    def test_limitador_impede_trafego_excessivo(self):
        aud = self._auditoria({})
        limited = mod.ConsultasFallbackLimitadas(aud, limite=2)
        for i in range(2):
            limited.consultar(usuarios=[self.usuarios[i]])
        with self.assertRaisesRegex(mod.ErroServidorPersistente, 'limite de 2'):
            limited.consultar(usuarios=[self.usuarios[2]])
        self.assertEqual(len(aud.calls),2)

    def test_sem_particoes_independentes_nao_grava(self):
        with self.assertRaisesRegex(mod.ErroServidorPersistente, 'menos de 4'):
            mod.coletar_por_particoes_apos_http500(object(),self.usuarios[:2],self.dia)

    def test_http500_classificado_para_fallback_e_503_nao(self):
        with self.assertRaises(mod.ErroHTTP500Temporario):
            mod.validar_http(Mock(status_code=500))
        with self.assertRaises(mod.ErroServidorTemporario) as cm:
            mod.validar_http(Mock(status_code=503))
        self.assertNotIsInstance(cm.exception, mod.ErroHTTP500Temporario)

    def test_processamento_salva_validacao_alternativa(self):
        eventos = {str(i): [self._evento(i)] for i in range(1,17)}
        auditoria = self._auditoria(eventos)
        banco = BancoFalso()
        mod.processar_dia(banco, auditoria, self.usuarios, self.dia, 1, True)
        logs = banco.dados[mod.TABELA_LOGS]
        cargas = banco.dados[mod.TABELA_CARGAS]
        self.assertEqual(len(logs),16)
        self.assertEqual(cargas[0]['status'],'concluido')
        self.assertIn('duas particoes independentes', cargas[0]['mensagem'])


class TestesSegurancaExtraV5(unittest.TestCase):
    def test_login_500_nao_ativa_fallback_de_filtros(self):
        sessao = Mock()
        sessao.headers = {"User-Agent": "teste"}
        sessao.post.return_value = Mock(status_code=500)
        with self.assertRaisesRegex(mod.ErroServidorTemporario, 'durante o login') as cm:
            mod.fazer_login(sessao)
        self.assertNotIsInstance(cm.exception, mod.ErroHTTP500Temporario)

    def test_dados_divergentes_nao_sao_gravados(self):
        modelo = TestesFallbackHTTP500()
        modelo.setUp()
        eventos = {str(i): [modelo._evento(i)] for i in range(1,17)}
        aud = modelo._auditoria(eventos, alterar_segunda=True)
        banco = BancoFalso()
        with self.assertRaises(mod.DivergenciaAuditoria):
            mod.processar_dia(banco, aud, modelo.usuarios, modelo.dia, 1, True)
        self.assertEqual(len(banco.dados[mod.TABELA_LOGS]),0)
        self.assertEqual(banco.dados[mod.TABELA_CARGAS][0]['status'], 'erro')


class TestesFallbackRecursivoV6(unittest.TestCase):
    """Reproduz especificamente o novo erro: o grupo de 8 retorna HTTP 500."""
    def setUp(self):
        self.dia = date(2026, 11, 9)
        self.usuarios = [{"id": str(i), "nome": f"Pessoa {i}"} for i in range(1,17)]
        self.eventos = {str(i): [
            {"id": f"registro-{i}", "usuario": str(i),
             "data_evento": "2026-11-09 12:00:00"}
        ] for i in range(1,17)}

    def _fake(self, error_usuario=None, error_len8=False, overlap=False,
              error_other=None):
        eventos = self.eventos
        all_ids = {str(i) for i in range(1,17)}
        class Falsa:
            def __init__(self):
                self.consultas = []
            def consultar(self, **kw):
                ids = [str(u["id"]) for u in kw.get("usuarios",[])]
                self.consultas.append(ids)
                if error_other and (set(ids)==all_ids):
                    raise error_other
                if set(ids)==all_ids or (error_len8 and len(ids)==8) or (
                    error_usuario and str(error_usuario) in ids
                ):
                    raise mod.ErroHTTP500Persistente("IILEX 500")
                regs = [r for id_ in ids for r in eventos.get(id_, [])]
                if overlap:
                    if "1" in ids or "9" in ids:
                        regs = regs + [{"id":"overlap","data_evento":"2026-11-09 12:00:00"}]
                return len(regs), regs
        return Falsa()

    def test_subgrupo_8_http500_com_divisao_recupera_dia(self):
        aud = self._fake(error_len8=True)
        regs, total = mod.coletar_dia_com_retencao_de_erros(
            aud,self.usuarios,self.dia)
        self.assertEqual(len(regs),16)
        self.assertEqual(total,16)
        self.assertTrue(aud.coleta_por_particoes)
        self.assertTrue(any(len(ids)==4 for ids in aud.consultas))
        self.assertLessEqual(len(aud.consultas)-1,mod.MAX_CONSULTAS_FALLBACK)

    def test_um_usuario_http500_e_identificado_sem_gravar_parcial(self):
        aud = self._fake(error_usuario="15")
        banco = BancoFalso()
        with self.assertRaisesRegex(mod.ErroServidorPersistente,
                                    r"Pessoa 15.*id=15.*dia pendente"):
            mod.processar_dia(banco,aud,self.usuarios,self.dia,1,True)
        self.assertEqual(len(banco.dados[mod.TABELA_LOGS]),0)
        self.assertEqual(banco.dados[mod.TABELA_CARGAS][0]['status'],'erro')
        self.assertTrue(any(ids==['15'] for ids in aud.consultas))

    def test_403_429_nao_ativam_divisao(self):
        for exc in (mod.ErroAutenticacao("403"),mod.ErroServidor("429")):
            aud = self._fake(error_other=exc)
            with self.assertRaises(type(exc)):
                mod.coletar_dia_com_retencao_de_erros(
                    aud,self.usuarios,self.dia)
            self.assertEqual(len(aud.consultas),1)

    def test_limite_se_aplica_a_subdivisoes_recursivas(self):
        aud = self._fake(error_len8=True)
        limitado = mod.ConsultasFallbackLimitadas(aud, limite=2)
        with self.assertRaisesRegex(mod.ErroServidorPersistente,'limite de 2'):
            mod.coletar_grupo(limitado,self.dia,self.usuarios[:8],
                              subdividir_http500=True)
        self.assertEqual(len(aud.consultas),2)

    def test_sobreposicao_em_subgrupos_recursivos_bloqueia(self):
        aud = self._fake(error_len8=True, overlap=True)
        with self.assertRaisesRegex(mod.DivergenciaAuditoria,'apareceram em ambos|sobreposto'):
            mod.coletar_dia_com_retencao_de_erros(
                aud,self.usuarios,self.dia)

    def test_somente_http500_persistente_subdivide(self):
        class Falsa:
            calls=0
            def consultar(self, **kw):
                self.calls+=1
                raise mod.ErroServidorPersistente('timeout')
        aud=Falsa()
        with self.assertRaises(mod.ErroServidorPersistente):
            mod.coletar_grupo(aud,self.dia,self.usuarios[:8],
                              subdividir_http500=True)
        self.assertEqual(aud.calls,1)

    def test_verificacao_independente_usa_multiconjuntos(self):
        # Uma duplicata de payload legitima nao pode sumir na contagem.
        self.eventos['1'].append(dict(self.eventos['1'][0]))
        aud=self._fake(error_len8=True)
        regs,total=mod.coletar_dia_com_retencao_de_erros(
            aud,self.usuarios,self.dia)
        self.assertEqual((len(regs),total),(17,17))
        self.assertEqual(Counter(mod.canonicalizar(r) for r in regs)[
            mod.canonicalizar(self.eventos['1'][0])],2)


class TestesRotinaSomentePendencias(unittest.TestCase):
    def test_sem_pendencias_nao_acessa_iilex_nem_refaz_hash(self):
        from datetime import datetime as _datetime
        ontem = date(2026, 11, 11)
        status = {(mod.INICIO_HISTORICO + timedelta(days=i)).isoformat(): "concluido"
                  for i in range((ontem - mod.INICIO_HISTORICO).days + 1)}
        with (
            patch.object(mod, "datetime") as dt,
            patch.object(mod, "create_client", return_value=BancoFalso()),
            patch.multiple(mod, user="teste", senha="teste", url_supabase="https://example.supabase.co", token_supabase="fake"),
            patch.object(mod, "ler_status_cargas", return_value=status),
            patch.object(mod, "detectar_inicio_ocorrencia") as hashes,
            patch.object(mod, "carregar_usuarios") as usuarios,
            patch.object(mod, "validar_filtro_dia_unico") as preflight,
            patch.object(mod, "processar_dia") as processar,
            patch.object(sys, "argv", ["script", "--modo", "diario", "--gravar"]),
        ):
            dt.now.return_value = _datetime(2026, 11, 12, 10, 0, tzinfo=mod.FUSO)
            mod.executar()
            hashes.assert_not_called()
            usuarios.assert_not_called()
            preflight.assert_not_called()
            processar.assert_not_called()

    def test_recuperacao_manual_ainda_permite_reconsultar_concluido(self):
        from datetime import datetime as _datetime
        dia = date(2026, 11, 10)
        with (
            patch.object(mod, "datetime") as dt,
            patch.object(mod, "create_client", return_value=BancoFalso()),
            patch.multiple(mod, user="teste", senha="teste", url_supabase="https://example.supabase.co", token_supabase="fake"),
            patch.object(mod, "detectar_inicio_ocorrencia", return_value=1),
            patch.object(mod, "carregar_usuarios", return_value=[{"id": "1", "nome": "Ana"}]),
            patch.object(mod, "validar_filtro_dia_unico"),
            patch.object(mod, "processar_dia") as processar,
            patch.object(sys, "argv", ["script", "--modo", "recuperar", "--inicio", dia.isoformat(), "--fim", dia.isoformat(), "--gravar"]),
        ):
            dt.now.return_value = _datetime(2026, 11, 12, 10, 0, tzinfo=mod.FUSO)
            mod.executar()
            self.assertEqual(processar.call_count, 1)
            self.assertEqual(processar.call_args.args[3], dia)
