import unittest
from unittest.mock import patch

import api
from modules.notas_repo import _build_where


class QueueMetadataTests(unittest.TestCase):
    def test_build_where_aplica_filtros_operacionais_da_fila(self):
        where, params = _build_where(
            {
                "status_fila": " Divergente ",
                "status_fila_manual": " Pendente ",
                "prioridade_manual": " Alta ",
                "responsavel": " Kaio ",
            }
        )

        self.assertIn("status_fila_manual", where)
        self.assertIn("prioridade_manual", where)
        self.assertIn("responsavel", where)
        self.assertEqual(params, ["divergente", "pendente", "alta", "kaio"])

    @patch("api.listar_empresas_e_contadores_fila")
    @patch("api.listar_notas_agrupadas")
    def test_get_nfse_expande_resposta_sem_quebrar_paginacao(self, listar_notas_mock, metadata_mock):
        listar_notas_mock.return_value = ([{"id": 1}], 1944)
        metadata_mock.return_value = {
            "empresas": ["Empresa A", "Empresa B", "Empresa C"],
            "total_empresas": 3,
            "contadores": {
                "notas_na_fila": 1944,
                "alta_prioridade": 281,
                "sla_critico": 12,
            },
        }

        response = api.get_nfse(page=2, page_size=200)

        self.assertEqual(response["items"], [{"id": 1}])
        self.assertEqual(response["total"], 1944)
        self.assertEqual(response["page"], 2)
        self.assertEqual(response["page_size"], 200)
        self.assertEqual(response["empresas_disponiveis"], ["Empresa A", "Empresa B", "Empresa C"])
        self.assertEqual(response["total_empresas"], 3)
        self.assertEqual(
            response["contadores"],
            {
                "notas_na_fila": 1944,
                "alta_prioridade": 281,
                "sla_critico": 12,
            },
        )

    @patch("api.listar_empresas_e_contadores_fila")
    @patch("api.listar_notas_agrupadas")
    def test_get_nfse_respeita_page_size_camel_case_sem_teto_de_500(self, listar_notas_mock, metadata_mock):
        listar_notas_mock.return_value = ([{"id": 501}], 787)
        metadata_mock.return_value = {
            "empresas": ["Empresa A"],
            "total_empresas": 1,
            "contadores": {
                "notas_na_fila": 787,
                "alta_prioridade": 0,
                "sla_critico": 0,
            },
        }

        response = api.get_nfse(page=6, pageSize=1000)

        listar_notas_mock.assert_called_once()
        _, kwargs = listar_notas_mock.call_args
        self.assertEqual(kwargs["page"], 6)
        self.assertEqual(kwargs["page_size"], 1000)
        self.assertEqual(response["page_size"], 1000)
        self.assertEqual(response["total"], 787)


if __name__ == "__main__":
    unittest.main()
