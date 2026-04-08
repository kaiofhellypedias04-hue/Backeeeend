import unittest

from modules.storage import build_process_storage_key


class StoragePathTests(unittest.TestCase):
    def test_monta_path_remoto_para_xml(self):
        self.assertEqual(
            build_process_storage_key("xml", "8a3b560c", "nota_001.xml"),
            "processos/8a3b560c/xml/nota_001.xml",
        )

    def test_mapeia_relatorio_para_pasta_planilhas(self):
        self.assertEqual(
            build_process_storage_key("relatorio", "8a3b560c", "auditoria_nfse.xlsx"),
            "processos/8a3b560c/planilhas/auditoria_nfse.xlsx",
        )

    def test_descarta_diretorios_do_nome_do_arquivo(self):
        self.assertEqual(
            build_process_storage_key("pdf", "8a3b560c", r"tmp\danfe\nota_001.pdf"),
            "processos/8a3b560c/pdf/nota_001.pdf",
        )

    def test_rejeita_tipo_invalido(self):
        with self.assertRaises(ValueError):
            build_process_storage_key("zip", "8a3b560c", "arquivo.zip")


if __name__ == "__main__":
    unittest.main()
