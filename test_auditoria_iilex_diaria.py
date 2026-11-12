"""Testes OFFLINE para auditoria_iilex_diaria.py.

Execute: python -m unittest -v test_auditoria_iilex_diaria.py
Nenhum teste acessa IILEX, Supabase ou a internet.
"""
import importlib.util
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

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


class TestesPlanejamento(unittest.TestCase):
    def test_sem_dias_antes_da_data_inicial(self):
        datas, adiados, motivos = mod.planejar_execucao_diaria(date(2026, 10, 5), {})
        self.assertEqual((datas, adiados, motivos), ([], [], {}))

    def test_ontem_sempre_primeiro(self):
        ontem = date(2026, 11, 11)
        d, _, m = mod.planejar_execucao_diaria(ontem, {"2026-11-11": "concluido"}, 1, 0)
        self.assertEqual(d, [ontem])
        self.assertEqual(m[ontem], "coleta de ontem")

    def test_falhas_e_dias_sem_marcador_sao_recuperados(self):
        ontem = date(2026, 10, 12)
        status = {"2026-10-10": "erro", "2026-10-11": "processando",
                  "2026-10-08": "concluido"}
        datas, adiados, motivos = mod.planejar_execucao_diaria(ontem, status, 3, 2)
        self.assertEqual(datas[0], ontem)
        self.assertEqual(datas[1:3], [date(2026, 10, 11), date(2026, 10, 10)])
        self.assertIn(date(2026, 10, 6), datas)
        self.assertIn(date(2026, 10, 7), datas)
        self.assertIn(date(2026, 10, 9), adiados)
        self.assertNotIn(date(2026, 10, 8), datas)
        self.assertEqual(motivos[date(2026, 10, 11)], "recuperacao recente")

    def test_revisao_concluidos_ultimos_7_dias(self):
        ontem = date(2026, 10, 12)
        status = {(ontem - timedelta(days=i)).isoformat(): "concluido" for i in range(7)}
        datas, adiados, motivos = mod.planejar_execucao_diaria(ontem, status, 7, 0)
        self.assertEqual(datas, [ontem - timedelta(days=i) for i in range(7)])
        self.assertEqual(adiados, [])
        self.assertIn("revisao", motivos[date(2026, 10, 11)])

    def test_ler_cargas_com_mais_de_500_linhas(self):
        banco = BancoFalso()
        for i in range(600):
            d = date(2026, 10, 6) + timedelta(days=i)
            banco.dados[mod.TABELA_CARGAS].append({"data_referencia": d.isoformat(), "status": "concluido"})
        fim = date(2026, 10, 6) + timedelta(days=599)
        statuses = mod.ler_status_cargas(banco, fim)
        self.assertEqual(len(statuses), 600)
        self.assertEqual(statuses[fim.isoformat()], "concluido")


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
