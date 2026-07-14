"""Extrator configurável para páginas com árvores de navegação dinâmicas."""

from __future__ import annotations

import argparse
import hashlib
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import yaml
from markdownify import markdownify
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from config import ensure_directories, load_settings

DEFAULT_ITEM_SELECTOR = ".ui-treenode-label"
DEFAULT_TOGGLER_SELECTOR = (
    ".ui-tree-toggler.ui-icon-triangle-1-e, [aria-expanded='false'].ui-tree-toggler"
)
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1F]')
WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class DynamicMenuOptions:
    """Configura a extração de uma árvore de navegação dinâmica."""

    url: str
    content_selector: str
    output_dir: Path
    pdf_dir: Path
    item_selector: str = DEFAULT_ITEM_SELECTOR
    toggler_selector: str = DEFAULT_TOGGLER_SELECTOR
    title_selector: str = "h1"
    preclick_selector: str = ""
    select_selector: str = ""
    select_label: str = ""
    select_value: str = ""
    max_items: int = 50_000
    headless: bool = True
    navigation_timeout_ms: int = 120_000
    request_timeout_ms: int = 60_000
    click_delay: float = 0.2

    def validate(self) -> None:
        """Valida os seletores e limites necessários para iniciar o navegador."""

        parsed_url = urlparse(self.url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("A URL deve usar http:// ou https://.")
        if not self.content_selector.strip():
            raise ValueError("Informe o seletor do conteúdo principal.")
        if not self.item_selector.strip():
            raise ValueError("Informe o seletor dos itens da árvore.")
        if self.max_items < 1:
            raise ValueError("O limite de itens deve ser maior que zero.")


@dataclass(slots=True)
class DynamicMenuSummary:
    """Contadores produzidos pela extração de um menu dinâmico."""

    found_items: int = 0
    saved_documents: int = 0
    downloaded_pdfs: int = 0
    skipped_items: int = 0
    failed_items: int = 0


def sanitize_filename(name: str, max_length: int = 150) -> str:
    """Converte um rótulo do menu em nome de arquivo portátil."""

    if not name:
        return "documento"
    sanitized = INVALID_FILENAME_CHARS.sub("_", name)
    sanitized = WHITESPACE.sub("_", sanitized).strip("._")
    return sanitized[:max_length].rstrip("._") or "documento"


def short_hash(content: str) -> str:
    """Gera um identificador curto e estável para o conteúdo."""

    return hashlib.sha1(content.encode("utf-8", "ignore")).hexdigest()[:10]


def html_to_markdown(html: str) -> str:
    """Converte HTML em Markdown e reduz quebras de linha excessivas."""

    if not html:
        return ""
    text = markdownify(
        html,
        heading_style="ATX",
        strip=["script", "style", "noscript"],
    )
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def write_document(path: Path, metadata: dict[str, Any], body: str) -> None:
    """Grava metadados YAML e corpo Markdown em um único arquivo."""

    frontmatter = yaml.safe_dump(
        metadata,
        sort_keys=False,
        allow_unicode=True,
    ).strip()
    path.write_text(
        f"---\n{frontmatter}\n---\n{body.strip()}\n",
        encoding="utf-8",
    )


def wait_for_ajax_idle(page: Any, timeout_ms: int = 12_000) -> None:
    """Espera componentes PrimeFaces deixarem o estado visual de carregamento."""

    try:
        page.wait_for_function(
            """() => {
                const busy = document.querySelectorAll(
                    '.ui-state-disabled, .ui-ajax-loading, .ui-blockui'
                ).length > 0;
                return !busy;
            }""",
            timeout=timeout_ms,
        )
    except PlaywrightTimeout:
        pass


def select_dataset(page: Any, options: DynamicMenuOptions) -> None:
    """Seleciona uma opção de dropdown antes de percorrer a árvore."""

    if not options.select_selector:
        return
    if not options.select_label and not options.select_value:
        return

    page.wait_for_selector(
        options.select_selector,
        state="visible",
        timeout=options.request_timeout_ms,
    )
    if options.select_value:
        page.select_option(options.select_selector, value=options.select_value)
    else:
        page.select_option(options.select_selector, label=options.select_label)
    wait_for_ajax_idle(page)


def perform_preclick(page: Any, selector: str) -> None:
    """Executa um clique preparatório opcional antes de expandir a árvore."""

    if not selector:
        return
    locator = page.locator(selector)
    if not locator.count():
        raise RuntimeError(f"O seletor de pré-clique não foi encontrado: {selector}")
    locator.first.scroll_into_view_if_needed(timeout=2_000)
    locator.first.click(timeout=3_000, force=True)
    wait_for_ajax_idle(page)


def expand_tree(page: Any, selector: str, click_delay: float) -> None:
    """Expande os nós recolhidos até a árvore estabilizar."""

    for _ in range(100):
        togglers = page.locator(selector)
        count = togglers.count()
        if not count:
            return

        successful_clicks = 0
        for index in range(count):
            try:
                toggler = togglers.nth(index)
                toggler.scroll_into_view_if_needed(timeout=2_000)
                toggler.click(timeout=3_000, force=True)
                successful_clicks += 1
                time.sleep(click_delay)
            except Exception:
                continue
        wait_for_ajax_idle(page, timeout_ms=15_000)
        if not successful_clicks:
            return


def collect_menu_items(page: Any, selector: str) -> list[tuple[int, str]]:
    """Captura o índice no DOM e o rótulo dos itens utilizáveis da árvore."""

    menu_items: list[tuple[int, str]] = []
    items = page.locator(selector)
    for index in range(items.count()):
        try:
            label = (items.nth(index).inner_text(timeout=3_000) or "").strip()
            if label:
                menu_items.append((index, label))
        except Exception:
            continue
    return menu_items


def current_content(page: Any, selector: str) -> tuple[str, str]:
    """Retorna o HTML e o texto do container configurado."""

    locator = page.locator(selector).first
    if not locator.count():
        return "", ""
    try:
        return (
            locator.inner_html(timeout=4_000) or "",
            locator.inner_text(timeout=4_000) or "",
        )
    except Exception:
        return "", ""


def wait_for_content_change(
    page: Any,
    selector: str,
    previous_html: str,
    timeout_seconds: float = 10,
) -> bool:
    """Aguarda o AJAX substituir o conteúdo exibido anteriormente."""

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        html, _ = current_content(page, selector)
        if html and html != previous_html:
            return True
        time.sleep(0.2)
    return False


def read_title(page: Any, selector: str, fallback: str) -> str:
    """Obtém o título associado ao item atual."""

    if selector:
        try:
            title = page.locator(selector).first.inner_text(timeout=2_000).strip()
            if title:
                return title
        except Exception:
            pass
    return fallback.strip()


def download_linked_pdfs(
    page: Any,
    content_selector: str,
    base_url: str,
    destination: Path,
) -> int:
    """Baixa os links PDF presentes no conteúdo usando a sessão do navegador."""

    downloaded = 0
    anchors = page.locator(content_selector).first.locator("a[href]")
    for index in range(anchors.count()):
        try:
            href = anchors.nth(index).get_attribute("href") or ""
            absolute_url = urljoin(base_url, href)
            if Path(urlparse(absolute_url).path).suffix.lower() != ".pdf":
                continue
            response = page.context.request.get(absolute_url)
            if not response.ok:
                continue
            source_name = Path(urlparse(absolute_url).path).name or "documento.pdf"
            filename = (
                f"{sanitize_filename(Path(source_name).stem)}__{short_hash(absolute_url)}.pdf"
            )
            (destination / filename).write_bytes(response.body())
            downloaded += 1
        except Exception:
            continue
    return downloaded


def run_dynamic_menu(options: DynamicMenuOptions) -> DynamicMenuSummary:
    """Percorre os itens da árvore e salva cada conteúdo carregado."""

    options.validate()
    ensure_directories(options.output_dir, options.pdf_dir)
    summary = DynamicMenuSummary()

    print(f"Abrindo {options.url}")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=options.headless)
        context = browser.new_context(accept_downloads=True)
        context.set_default_navigation_timeout(options.navigation_timeout_ms)
        context.set_default_timeout(options.request_timeout_ms)
        page = context.new_page()
        try:
            page.goto(
                options.url,
                wait_until="domcontentloaded",
                timeout=options.navigation_timeout_ms,
            )
            select_dataset(page, options)
            perform_preclick(page, options.preclick_selector)
            expand_tree(page, options.toggler_selector, options.click_delay)

            menu_items = collect_menu_items(page, options.item_selector)
            summary.found_items = len(menu_items)
            print(f"Itens encontrados: {summary.found_items}")

            selected_items = menu_items[: options.max_items]
            item_count = len(selected_items)
            for progress_index, (dom_index, label) in enumerate(selected_items, start=1):
                previous_html, _ = current_content(
                    page,
                    options.content_selector,
                )
                item = page.locator(options.item_selector).nth(dom_index)
                try:
                    item.scroll_into_view_if_needed(timeout=2_000)
                    # O item pode iniciar um download ou atualizar o painel por AJAX.
                    try:
                        with page.expect_download(timeout=2_000) as download_info:
                            item.click(timeout=3_000)
                    except PlaywrightTimeout:
                        pass
                    else:
                        download = download_info.value
                        filename = sanitize_filename(download.suggested_filename)
                        download.save_as(options.pdf_dir / filename)
                        summary.downloaded_pdfs += 1
                        print(f"[{progress_index}/{item_count}] PDF: {filename}")
                        continue

                    if not wait_for_content_change(
                        page,
                        options.content_selector,
                        previous_html,
                    ):
                        summary.skipped_items += 1
                        print(f"[{progress_index}/{item_count}] Sem alteração: {label}")
                        continue
                    wait_for_ajax_idle(page)

                    content_html, content_text = current_content(
                        page,
                        options.content_selector,
                    )
                    if not content_text.strip():
                        summary.skipped_items += 1
                        continue

                    title = read_title(page, options.title_selector, label)
                    body = html_to_markdown(content_html)
                    if title and not body.lstrip().startswith("# "):
                        body = f"# {title}\n\n{body}"

                    filename = f"{sanitize_filename(label)}__{short_hash(body)}.md"
                    metadata = {
                        "url": options.url,
                        "clicked_item": label,
                        "title": title,
                        "content_selector": options.content_selector,
                        "extracted_at": datetime.now(timezone.utc).isoformat(),
                    }
                    write_document(options.output_dir / filename, metadata, body)
                    summary.saved_documents += 1
                    pdf_count = download_linked_pdfs(
                        page,
                        options.content_selector,
                        options.url,
                        options.pdf_dir,
                    )
                    summary.downloaded_pdfs += pdf_count
                    print(f"[{progress_index}/{item_count}] Salvo: {filename}")
                    time.sleep(options.click_delay)
                except Exception as error:
                    summary.failed_items += 1
                    print(f"[{progress_index}/{item_count}] Falha em {label}: {error}")
        finally:
            context.close()
            browser.close()

    return summary


def parse_arguments() -> argparse.Namespace:
    """Lê os parâmetros da extração de menu dinâmico."""

    settings = load_settings()
    parser = argparse.ArgumentParser(
        description="Extrai conteúdo de uma árvore dinâmica configurada por seletores.",
    )
    parser.add_argument("--url", default=settings.base_url)
    parser.add_argument(
        "--content-selector",
        required=True,
        help="Seletor CSS ou XPath do conteúdo atualizado após cada clique.",
    )
    parser.add_argument("--item-selector", default=DEFAULT_ITEM_SELECTOR)
    parser.add_argument("--toggler-selector", default=DEFAULT_TOGGLER_SELECTOR)
    parser.add_argument("--title-selector", default="h1")
    parser.add_argument("--preclick-selector", default="")
    parser.add_argument("--select-selector", default="")
    parser.add_argument("--select-label", default="")
    parser.add_argument("--select-value", default="")
    parser.add_argument("--output-dir", type=Path, default=settings.output_dir)
    parser.add_argument("--pdf-dir", type=Path, default=settings.pdf_dir)
    parser.add_argument("--max-items", type=int, default=50_000)
    parser.add_argument("--headed", dest="headless", action="store_false")
    parser.add_argument("--headless", dest="headless", action="store_true")
    parser.set_defaults(headless=settings.headless)
    return parser.parse_args()


def main() -> int:
    """Executa o extrator de menu dinâmico pela linha de comando."""

    settings = load_settings()
    arguments = parse_arguments()
    options = DynamicMenuOptions(
        url=arguments.url,
        content_selector=arguments.content_selector,
        output_dir=arguments.output_dir,
        pdf_dir=arguments.pdf_dir,
        item_selector=arguments.item_selector,
        toggler_selector=arguments.toggler_selector,
        title_selector=arguments.title_selector,
        preclick_selector=arguments.preclick_selector,
        select_selector=arguments.select_selector,
        select_label=arguments.select_label,
        select_value=arguments.select_value,
        max_items=arguments.max_items,
        headless=arguments.headless,
        navigation_timeout_ms=settings.navigation_timeout_ms,
        request_timeout_ms=settings.request_timeout_ms,
    )
    try:
        summary = run_dynamic_menu(options)
    except (OSError, ValueError) as error:
        print(f"Erro de configuração: {error}")
        return 2

    print(
        "Extração concluída: "
        f"{summary.saved_documents} documento(s), "
        f"{summary.downloaded_pdfs} PDF(s), "
        f"{summary.failed_items} falha(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
