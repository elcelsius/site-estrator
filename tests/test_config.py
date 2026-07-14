"""Testes das configurações carregadas do ambiente."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from config import load_settings


class ConfigTests(unittest.TestCase):
    """Confere conversão de tipos e substituição dos valores padrão."""

    def test_load_settings_uses_environment_overrides(self) -> None:
        """Variáveis de ambiente devem prevalecer sobre os padrões."""

        environment = {
            "DEFAULT_BASE_URL": "https://example.org",
            "OUTPUT_DIR": "saida-teste",
            "MAX_PAGES": "12",
            "MAX_DEPTH": "4",
            "RESPECT_ROBOTS_TXT": "false",
        }
        with patch.dict(os.environ, environment, clear=False):
            settings = load_settings()

        self.assertEqual(settings.base_url, "https://example.org")
        self.assertEqual(settings.output_dir, Path("saida-teste"))
        self.assertEqual(settings.max_pages, 12)
        self.assertEqual(settings.max_depth, 4)
        self.assertFalse(settings.respect_robots_txt)

    def test_invalid_numbers_fall_back_to_defaults(self) -> None:
        """Valores numéricos inválidos não devem impedir a inicialização."""

        with patch.dict(
            os.environ,
            {"MAX_PAGES": "muitos", "POLITENESS_DELAY": "rápido"},
            clear=False,
        ):
            settings = load_settings()

        self.assertEqual(settings.max_pages, 500)
        self.assertEqual(settings.politeness_delay, 0.6)


if __name__ == "__main__":
    unittest.main()
