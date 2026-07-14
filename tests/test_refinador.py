"""Testes da integração opcional com serviços de refino."""

from __future__ import annotations

import io
import unittest
from types import SimpleNamespace

from refinador import call_gemini, gemini_preflight


class FakeGeminiModels:
    """Substitui o cliente remoto por respostas determinísticas."""

    def __init__(self) -> None:
        """Inicializa o registro das chamadas recebidas."""

        self.calls: list[dict[str, object]] = []

    def generate_content(self, **arguments: object) -> SimpleNamespace:
        """Registra os argumentos e devolve uma resposta curta."""

        self.calls.append(arguments)
        return SimpleNamespace(text="Conteúdo refinado")


class GeminiIntegrationTests(unittest.TestCase):
    """Confere o contrato usado com o SDK atual do Google."""

    def setUp(self) -> None:
        """Cria um cliente falso e um log em memória para cada teste."""

        self.models = FakeGeminiModels()
        self.client = SimpleNamespace(models=self.models)
        self.log_file = io.StringIO()

    def test_preflight_uses_models_api(self) -> None:
        """O preflight deve consultar o modelo pela interface nova do SDK."""

        result = gemini_preflight(
            self.client,
            "gemini-test",
            self.log_file,
        )

        self.assertTrue(result)
        self.assertEqual(self.models.calls[0]["model"], "gemini-test")
        self.assertEqual(self.models.calls[0]["contents"], "ping")

    def test_call_gemini_returns_text_and_timestamp(self) -> None:
        """Uma resposta válida deve retornar texto e instante de controle."""

        text, called_at = call_gemini(
            self.client,
            "gemini-test",
            "Trecho de entrada",
            self.log_file,
            None,
        )

        self.assertEqual(text, "Conteúdo refinado")
        self.assertGreater(called_at, 0)


if __name__ == "__main__":
    unittest.main()
