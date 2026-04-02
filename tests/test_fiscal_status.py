import unittest
from datetime import datetime

from modules.export_utils import serialize_export_value
from modules.fiscal_status import (
    compute_base_calculation_status,
    compute_final_note_status,
    compute_final_queue_status,
    is_alertas_fiscais_final_segment_correto,
    is_observacao_fiscal_final_segment_correto,
)


class FiscalStatusTests(unittest.TestCase):
    def test_base_calculo_nao_diverge_quando_base_e_menor_que_total(self):
        self.assertEqual(compute_base_calculation_status(80.0, 100.0), "ok")

    def test_mei_com_base_zerada_permanece_ok(self):
        self.assertEqual(
            compute_base_calculation_status(
                0.0,
                100.0,
                simples_xml="MEI",
                consulta_simples_api="Optante S.N",
            ),
            "ok",
        )

    def test_optante_nao_mei_com_base_zerada_fica_divergente(self):
        self.assertEqual(
            compute_base_calculation_status(
                0.0,
                100.0,
                simples_xml="Optante S.N",
                consulta_simples_api="Optante S.N",
            ),
            "divergente",
        )

    def test_nao_optante_com_base_zerada_fica_divergente(self):
        self.assertEqual(
            compute_base_calculation_status(
                0.0,
                100.0,
                simples_xml="Não optante",
                consulta_simples_api="Não optante",
            ),
            "divergente",
        )

    def test_nao_optante_item_9901_com_base_zerada_continua_divergente(self):
        self.assertEqual(
            compute_base_calculation_status(
                0.0,
                100.0,
                simples_xml="Não optante",
                consulta_simples_api="Não optante",
                codigo_servico="99.01",
            ),
            "divergente",
        )

    def test_base_negativa_permanece_divergente(self):
        self.assertEqual(compute_base_calculation_status(-1.0, 100.0), "divergente")

    def test_base_maior_que_total_permanece_divergente(self):
        self.assertEqual(compute_base_calculation_status(101.0, 100.0), "divergente")

    def test_status_final_considera_base_calculo_divergente(self):
        payload = {
            "status_base_calculo": "divergente",
            "status_simples_nacional": "ok",
            "status_csrf": "ok",
            "status_irrf": "ok",
            "status_inss": "ok",
            "status_valor_liquido": "ok",
        }
        self.assertEqual(compute_final_note_status(payload), "divergente")

    def test_status_final_mantem_divergencia_real(self):
        payload = {
            "status_simples_nacional": "ok",
            "status_csrf": "ok",
            "status_irrf": "divergente",
            "status_inss": "ok",
            "status_valor_liquido": "ok",
        }
        self.assertEqual(compute_final_note_status(payload), "divergente")

    def test_export_preserva_zero_e_datas(self):
        self.assertEqual(serialize_export_value(0), "0")
        self.assertEqual(serialize_export_value(0.0), "0.0")
        self.assertEqual(serialize_export_value(datetime(2026, 1, 2, 3, 4, 5)), "2026-01-02T03:04:05")
        self.assertEqual(serialize_export_value(None), "—")


    def test_notas_repo_status_expr_integration(self):
        from modules.notas_repo import STATUS_EXPR
        from modules.fiscal_status import build_sql_status_expr
        expected = build_sql_status_expr("n").strip()
        self.assertEqual(STATUS_EXPR.strip(), expected, "STATUS_EXPR deve usar regra centralizada")

    def test_alerta_optante_simples_correto_classifica_fila_como_correta(self):
        payload = {
            "status_fila_manual": None,
            "status_csrf": "ok",
            "status_irrf": "ok",
            "status_inss": "ok",
            "status_base_calculo": "ok",
            "status_valor_liquido": "ok",
            "alertas_fiscais": "Optante Simples Correto",
            "observacao_interna": None,
        }
        self.assertTrue(is_alertas_fiscais_final_segment_correto(payload["alertas_fiscais"]))
        self.assertEqual(compute_final_queue_status(payload), "correta")

    def test_alerta_mei_correto_classifica_fila_como_correta(self):
        payload = {
            "status_fila_manual": None,
            "status_csrf": "ok",
            "status_irrf": "ok",
            "status_inss": "ok",
            "status_base_calculo": "ok",
            "status_valor_liquido": "ok",
            "alertas_fiscais": "MEI Correto",
            "observacao_interna": None,
        }
        self.assertTrue(is_alertas_fiscais_final_segment_correto(payload["alertas_fiscais"]))
        self.assertEqual(compute_final_queue_status(payload), "correta")

    def test_alerta_composto_usa_ultimo_trecho_relevante(self):
        payload = {
            "status_fila_manual": None,
            "status_csrf": "ok",
            "status_irrf": "ok",
            "status_inss": "ok",
            "status_base_calculo": "ok",
            "status_valor_liquido": "ok",
            "alertas_fiscais": "BASE ZERADA: INSS retido (10,00) mas base de cálculo é zero. Verificar! | Optante Simples Correto",
            "observacao_interna": None,
        }
        self.assertTrue(is_alertas_fiscais_final_segment_correto(payload["alertas_fiscais"]))
        self.assertEqual(compute_final_queue_status(payload), "correta")

    def test_observacao_positiva_classifica_fila_como_correta(self):
        payload = {
            "status_fila_manual": None,
            "status_csrf": "ok",
            "status_irrf": "ok",
            "status_inss": "ok",
            "status_base_calculo": "ok",
            "status_valor_liquido": "ok",
            "alertas_fiscais": "",
            "observacao_interna": "Observação adicional | Optante Simples Correto",
        }
        self.assertTrue(is_observacao_fiscal_final_segment_correto(payload["observacao_interna"]))
        self.assertEqual(compute_final_queue_status(payload), "correta")

    def test_alerta_divergente_irrf_permanece_divergente(self):
        payload = {
            "status_fila_manual": None,
            "status_csrf": "ok",
            "status_irrf": "divergente",
            "status_inss": "ok",
            "status_base_calculo": "ok",
            "status_valor_liquido": "ok",
            "alertas_fiscais": "IRRF devido e não retido para código de serviço",
            "observacao_interna": None,
        }
        self.assertEqual(compute_final_queue_status(payload), "divergente")

    def test_alerta_divergente_csrf_permanece_divergente(self):
        payload = {
            "status_fila_manual": None,
            "status_csrf": "divergente",
            "status_irrf": "ok",
            "status_inss": "ok",
            "status_base_calculo": "ok",
            "status_valor_liquido": "ok",
            "alertas_fiscais": "CSRF retido divergente no comparativo",
            "observacao_interna": None,
        }
        self.assertEqual(compute_final_queue_status(payload), "divergente")

    def test_status_fila_manual_tem_precedencia_total(self):
        payload = {
            "status_fila_manual": "divergente",
            "status_csrf": "ok",
            "status_irrf": "ok",
            "status_inss": "ok",
            "status_base_calculo": "ok",
            "status_valor_liquido": "ok",
            "alertas_fiscais": "IRRF devido e não retido para código de serviço",
            "observacao_interna": "Optante Simples Correto",
        }
        self.assertEqual(compute_final_queue_status(payload), "divergente")

    def test_status_fila_manual_correta_tem_precedencia_total(self):
        payload = {
            "status_fila_manual": "correta",
            "status_csrf": "ok",
            "status_irrf": "divergente",
            "status_inss": "ok",
            "status_base_calculo": "ok",
            "status_valor_liquido": "ok",
            "alertas_fiscais": "IRRF devido e não retido para código de serviço",
            "observacao_interna": None,
        }
        self.assertEqual(compute_final_queue_status(payload), "correta")

if __name__ == "__main__":
    unittest.main()
