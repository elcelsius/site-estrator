"""Testes unitários das regras independentes do navegador."""

from __future__ import annotations

import tempfile
import unittest
from collections import deque
from pathlib import Path
from threading import Event
from unittest.mock import patch

from extrator import (
    CrawlOptions,
    build_output_filename,
    crawl_site,
    enqueue_discovered_links,
    is_url_in_scope,
    normalize_url,
    save_document,
)


class ExtractorHelpersTests(unittest.TestCase):
    """Valida normalização, escopo e formatos de saída."""

    def test_normalize_url_removes_tracking_parameters(self) -> None:
        """Parâmetros de campanha não devem criar páginas duplicadas."""

        normalized = normalize_url("https://Example.com/noticias/?utm_source=mail&id=42#rodape")
        self.assertEqual(normalized, "https://example.com/noticias?id=42")

    def test_scope_accepts_subdomains_but_not_similar_domains(self) -> None:
        """A comparação de domínio não pode aceitar sufixos enganosos."""

        self.assertTrue(is_url_in_scope("https://docs.example.com/a", "example.com", True))
        self.assertFalse(is_url_in_scope("https://notexample.com/a", "example.com", True))

    def test_output_filename_is_stable(self) -> None:
        """A mesma URL deve produzir sempre o mesmo nome de arquivo."""

        url = "https://example.com/uma/pagina"
        self.assertEqual(
            build_output_filename(url, "txt"),
            build_output_filename(url, "txt"),
        )
        self.assertTrue(build_output_filename(url, "txt").endswith(".txt"))

    def test_save_document_writes_plain_text(self) -> None:
        """O modo usado pela GUI deve gerar um arquivo .txt legível."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            options = CrawlOptions(
                base_url="https://example.com",
                output_dir=Path(temporary_directory),
                pdf_dir=Path(temporary_directory) / "pdf",
                output_format="txt",
            )
            output_path = save_document(
                url="https://example.com/pagina",
                title="Título",
                language="pt-BR",
                modified_at="",
                content_html="<main><p>Conteúdo principal.</p></main>",
                options=options,
            )

            self.assertEqual(output_path.suffix, ".txt")
            saved_text = output_path.read_text(encoding="utf-8")
            self.assertIn("Título", saved_text)
            self.assertIn("Conteúdo principal.", saved_text)

    def test_max_depth_prevents_new_queue_entries(self) -> None:
        """Links encontrados no limite de profundidade não devem ser enfileirados."""

        options = CrawlOptions(
            base_url="https://example.com",
            output_dir=Path("saida"),
            pdf_dir=Path("pdf"),
            max_depth=1,
        )
        pending_urls: deque[tuple[str, int]] = deque()
        added = enqueue_discovered_links(
            {"https://example.com/nivel-2"},
            current_depth=1,
            options=options,
            base_hostname="example.com",
            pending_urls=pending_urls,
            queued_urls={"https://example.com"},
            visited_urls={"https://example.com"},
        )

        self.assertEqual(added, 0)
        self.assertFalse(pending_urls)

    def test_pre_cancelled_crawl_does_not_open_browser(self) -> None:
        """Uma parada antecipada deve evitar a inicialização do Playwright."""

        options = CrawlOptions(
            base_url="https://example.com",
            output_dir=Path("saida"),
            pdf_dir=Path("pdf"),
        )
        stop_event = Event()
        stop_event.set()
        with patch("extrator.sync_playwright") as playwright:
            summary = crawl_site(options, stop_event=stop_event, log=lambda _: None)

        self.assertTrue(summary.cancelled)
        playwright.assert_not_called()


if __name__ == "__main__":
    unittest.main()
