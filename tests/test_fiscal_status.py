import unittest
from datetime import datetime

from modules.export_utils import serialize_export_value
from modules.fiscal_status import (
    build_base_calculation_alert,
    compute_base_calculation_status,
    compute_final_note_status,
    compute_final_queue_status,
    has_real_alertas_fiscais_divergencia,
    is_alertas_fiscais_final_segment_correto,
    is_observacao_fiscal_final_segment_correto,
    resolve_manual_note_status,
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
                simples_xml="Nao optante",
                consulta_simples_api="Nao optante",
            ),
            "divergente",
        )

    def test_nao_optante_item_9901_com_base_zerada_continua_divergente(self):
        self.assertEqual(
            compute_base_calculation_status(
                0.0,
                100.0,
                simples_xml="Nao optante",
                consulta_simples_api="Nao optante",
                codigo_servico="99.01",
            ),
            "divergente",
        )

    def test_base_negativa_permanece_divergente(self):
        self.assertEqual(compute_base_calculation_status(-1.0, 100.0), "divergente")

    def test_base_maior_que_total_permanece_divergente(self):
        self.assertEqual(compute_base_calculation_status(101.0, 100.0), "divergente")

    def test_alerta_base_zerada_para_optante_nao_mei(self):
        self.assertEqual(
            build_base_calculation_alert(
                0.0,
                100.0,
                simples_xml="Optante S.N",
                consulta_simples_api="Optante S.N",
            ),
            "BASE ZERADA: base de calculo zerada para Optante Simples. Verificar.",
        )

    def test_alerta_base_zerada_para_nao_optante(self):
        self.assertEqual(
            build_base_calculation_alert(
                0.0,
                100.0,
                simples_xml="Nao optante",
                consulta_simples_api="Nao optante",
            ),
            "BASE ZERADA: base de calculo zerada para Nao Optante. Verificar.",
        )

    def test_alerta_base_zerada_nao_e_gerado_para_mei(self):
        self.assertIsNone(
            build_base_calculation_alert(
                0.0,
                100.0,
                simples_xml="MEI",
                consulta_simples_api="Optante S.N",
            )
        )

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

    def test_notas_repo_status_nota_reusa_expr_da_fila(self):
        from modules.notas_repo import STATUS_FILA_EXPR, STATUS_NOTA_EXPR

        self.assertEqual(STATUS_NOTA_EXPR.strip(), STATUS_FILA_EXPR.strip())

    def test_notas_repo_anexa_alerta_sem_duplicar(self):
        from modules.notas_repo import _append_alerta_if_missing

        alerta = "BASE ZERADA: base de calculo zerada para Nao Optante. Verificar."
        self.assertEqual(_append_alerta_if_missing(None, alerta), alerta)
        self.assertEqual(
            _append_alerta_if_missing("IRRF divergente", alerta),
            "IRRF divergente | BASE ZERADA: base de calculo zerada para Nao Optante. Verificar.",
        )
        self.assertEqual(
            _append_alerta_if_missing(alerta, alerta),
            alerta,
        )

    def test_alerta_optante_simples_correto_sozinho_classifica_fila_como_correta(self):
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

    def test_alerta_mei_correto_sozinho_classifica_fila_como_correta(self):
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

    def test_alerta_base_zerada_optante_classifica_fila_como_divergente(self):
        alerta = "BASE ZERADA: base de calculo zerada para Optante Simples. Verificar."
        self.assertTrue(has_real_alertas_fiscais_divergencia(alerta))
        payload = {
            "status_fila_manual": None,
            "status_csrf": "ok",
            "status_irrf": "ok",
            "status_inss": "ok",
            "status_base_calculo": "divergente",
            "status_valor_liquido": "ok",
            "alertas_fiscais": alerta,
            "observacao_interna": None,
        }
        self.assertEqual(compute_final_queue_status(payload), "divergente")

    def test_alerta_inss_esperado_com_correto_permanece_divergente(self):
        alerta = "Optante Simples: INSS esperado (Anexo IV=SIM) para codigo 7.02, mas veio 0.00. | Optante Simples Correto"
        self.assertTrue(has_real_alertas_fiscais_divergencia(alerta))
        payload = {
            "status_fila_manual": None,
            "status_csrf": "ok",
            "status_irrf": "ok",
            "status_inss": "divergente",
            "status_base_calculo": "ok",
            "status_valor_liquido": "ok",
            "alertas_fiscais": alerta,
            "observacao_interna": None,
        }
        self.assertEqual(compute_final_queue_status(payload), "divergente")

    def test_alerta_irrf_devido_com_correto_permanece_divergente(self):
        alerta = "IRRF devido e nao retido para codigo 7.02. Deveria ser: 10.00 (1.5%) | Optante Simples Correto"
        self.assertTrue(has_real_alertas_fiscais_divergencia(alerta))
        payload = {
            "status_fila_manual": None,
            "status_csrf": "ok",
            "status_irrf": "divergente",
            "status_inss": "ok",
            "status_base_calculo": "ok",
            "status_valor_liquido": "ok",
            "alertas_fiscais": alerta,
            "observacao_interna": None,
        }
        self.assertEqual(compute_final_queue_status(payload), "divergente")

    def test_alerta_csrf_devido_com_mei_correto_permanece_divergente(self):
        alerta = "CSRF devido e nao retido para codigo 7.02. Deveria ser: 10.00 (4.65%) | MEI Correto"
        self.assertTrue(has_real_alertas_fiscais_divergencia(alerta))
        payload = {
            "status_fila_manual": None,
            "status_csrf": "divergente",
            "status_irrf": "ok",
            "status_inss": "ok",
            "status_base_calculo": "ok",
            "status_valor_liquido": "ok",
            "alertas_fiscais": alerta,
            "observacao_interna": None,
        }
        self.assertEqual(compute_final_queue_status(payload), "divergente")

    def test_alerta_base_zerada_nao_optante_classifica_fila_como_divergente(self):
        alerta = "BASE ZERADA: base de calculo zerada para Nao Optante. Verificar."
        self.assertTrue(has_real_alertas_fiscais_divergencia(alerta))
        payload = {
            "status_fila_manual": None,
            "status_csrf": "ok",
            "status_irrf": "ok",
            "status_inss": "ok",
            "status_base_calculo": "divergente",
            "status_valor_liquido": "ok",
            "alertas_fiscais": alerta,
            "observacao_interna": None,
        }
        self.assertEqual(compute_final_queue_status(payload), "divergente")

    def test_alerta_misto_correto_e_base_zerada_permanece_divergente(self):
        alerta = "Optante Simples Correto | BASE ZERADA: base de calculo zerada para Optante Simples. Verificar."
        self.assertTrue(has_real_alertas_fiscais_divergencia(alerta))
        payload = {
            "status_fila_manual": None,
            "status_csrf": "ok",
            "status_irrf": "ok",
            "status_inss": "ok",
            "status_base_calculo": "divergente",
            "status_valor_liquido": "ok",
            "alertas_fiscais": alerta,
            "observacao_interna": None,
        }
        self.assertEqual(compute_final_queue_status(payload), "divergente")

    def test_mei_correto_com_base_zerada_sem_outra_divergencia_permanece_correta(self):
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

    def test_observacao_positiva_classifica_fila_como_correta(self):
        payload = {
            "status_fila_manual": None,
            "status_csrf": "ok",
            "status_irrf": "ok",
            "status_inss": "ok",
            "status_base_calculo": "ok",
            "status_valor_liquido": "ok",
            "alertas_fiscais": "",
            "observacao_interna": "Observacao adicional | Optante Simples Correto",
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
            "alertas_fiscais": "IRRF devido e nao retido para codigo de servico",
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
            "alertas_fiscais": "IRRF devido e nao retido para codigo de servico",
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
            "alertas_fiscais": "IRRF devido e nao retido para codigo de servico",
            "observacao_interna": None,
        }
        self.assertEqual(compute_final_queue_status(payload), "correta")

    def test_status_fila_manual_substituida_e_normalizado(self):
        self.assertEqual(resolve_manual_note_status("Substituída"), "substituida")
        self.assertEqual(compute_final_queue_status({"status_fila_manual": "Substituída"}), "substituida")

    def test_status_compare_tolera_um_centavo(self):
        from modules.notas_repo import _status_compare

        self.assertEqual(_status_compare(1768.57, 1768.56), "ok")

    def test_status_compare_mantem_divergencia_real(self):
        from modules.notas_repo import _status_compare

        self.assertEqual(_status_compare(1768.58, 1768.56), "divergente")

    def test_validacao_retencoes_tolera_um_centavo_sem_alerta(self):
        from modules.nfse_xml_converter import NFSeXMLConverter

        conv = NFSeXMLConverter()
        conv.REGRAS_RETENCOES["7.02"] = {
            "irrf_percent": 1.5,
            "irrf_flag": "SIM",
            "csrf_percent": 4.65,
            "csrf_flag": "SIM",
            "inss_bool": False,
            "inss_flag": "NÃO",
        }
        dados = {
            "Valor Total": 1000.0,
            "Valor B/C": 1000.0,
            "IRRF": 15.01,
            "CSRF": 46.49,
            "INSS": 0.0,
        }

        resultado = conv.validar_retencoes(
            dados,
            regime="Lucro Presumido",
            codigo_servico="7.02",
            tipo_retencao_csrf="",
            tem_irrf_retido=True,
            tem_inss_retido=False,
        )

        self.assertEqual(resultado["alertas"], [])

    def test_validacao_retencoes_mantem_alerta_quando_ultrapassa_tolerancia(self):
        from modules.nfse_xml_converter import NFSeXMLConverter

        conv = NFSeXMLConverter()
        conv.REGRAS_RETENCOES["7.02"] = {
            "irrf_percent": 1.5,
            "irrf_flag": "SIM",
            "csrf_percent": 4.65,
            "csrf_flag": "SIM",
            "inss_bool": False,
            "inss_flag": "NÃO",
        }
        dados = {
            "Valor Total": 1000.0,
            "Valor B/C": 1000.0,
            "IRRF": 15.02,
            "CSRF": 46.50,
            "INSS": 0.0,
        }

        resultado = conv.validar_retencoes(
            dados,
            regime="Lucro Presumido",
            codigo_servico="7.02",
            tipo_retencao_csrf="",
            tem_irrf_retido=True,
            tem_inss_retido=False,
        )

        self.assertTrue(any("IRRF retido divergente" in alerta for alerta in resultado["alertas"]))


if __name__ == "__main__":
    unittest.main()
