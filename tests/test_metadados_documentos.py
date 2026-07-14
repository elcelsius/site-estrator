"""Testes das heurísticas genéricas de documentos jurídicos."""

from __future__ import annotations

import unittest

from metadados_documentos import (
    detect_act_type,
    extract_legal_metadata,
    parse_portuguese_date,
)


class DocumentMetadataTests(unittest.TestCase):
    """Confere datas, identificadores e estados inferidos."""

    def test_parse_portuguese_date_validates_calendar(self) -> None:
        """Datas válidas são normalizadas e datas impossíveis são rejeitadas."""

        self.assertEqual(
            parse_portuguese_date("14 de julho de 2026"),
            "2026-07-14",
        )
        self.assertEqual(parse_portuguese_date("31 de fevereiro de 2026"), "")

    def test_extract_legal_metadata_has_no_institution_defaults(self) -> None:
        """A inferência não deve introduzir nomes de organizações."""

        body = "# Portaria nº 27/2026, de 14 de julho de 2026\n\nTexto vigente."
        metadata = extract_legal_metadata(body)

        self.assertEqual(metadata["act_type"], "Portaria")
        self.assertEqual(metadata["act_number"], "27")
        self.assertEqual(metadata["act_year"], "2026")
        self.assertEqual(metadata["act_date"], "2026-07-14")
        self.assertNotIn("institution", metadata)
        self.assertNotIn("owner", metadata)

    def test_detect_act_type_accepts_unaccented_ocr_text(self) -> None:
        """Variações comuns de OCR não devem impedir a detecção do tipo."""

        self.assertEqual(detect_act_type("Resolucao nº 10"), "Resolução")


if __name__ == "__main__":
    unittest.main()
