import unittest
from datetime import datetime

from modules.export_utils import serialize_export_value
from modules.fiscal_status import compute_base_calculation_status, compute_final_note_status


class FiscalStatusTests(unittest.TestCase):
    def test_base_calculo_nao_diverge_quando_base_e_menor_que_total(self):
        self.assertEqual(compute_base_calculation_status(80.0, 100.0), "ok")

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

if __name__ == "__main__":
    unittest.main()

