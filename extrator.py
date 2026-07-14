"""Crawler de páginas HTML com saída em texto ou Markdown e download de PDFs."""

from __future__ import annotations

import argparse
import hashlib
import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright
from readability import Document

from config import CrawlerSettings, ensure_directories, load_settings

LogCallback = Callable[[str], None]
ProgressCallback = Callable[[int, int, int], None]

HTTP_TIMEOUT_SECONDS = 90
RETRIES = 3
BACKOFF_SECONDS = 1.5
WAIT_UNTIL = "domcontentloaded"
EXTRA_SPA_WAIT_MS = 400
MIN_CONTENT_CHARS = 500

ALLOWED_QUERY_PARAMS = {"id", "page", "lang"}
DROP_QUERY_PREFIXES = ("utm_", "gclid", "fbclid", "yclid")
BINARY_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".bmp",
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".gz",
    ".mp4",
    ".mp3",
    ".ogg",
    ".wav",
    ".webm",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
}
SKIP_SCHEMES = ("mailto:", "tel:", "javascript:", "data:")
TRACKER_HOST_FRAGMENTS = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "facebook.net",
    "hotjar.com",
    "segment.io",
    "newrelic",
    "nr-data.net",
    "matomo",
    "cloudflareinsights.com",
)

INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1F]')
WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class CrawlOptions:
    """Parâmetros de uma execução do crawler."""

    base_url: str
    output_dir: Path
    pdf_dir: Path
    max_depth: int = 2
    max_pages: int = 500
    same_domain_only: bool = True
    headless: bool = True
    output_format: str = "txt"
    navigation_timeout_ms: int = 120_000
    request_timeout_ms: int = 60_000
    politeness_delay: float = 0.6
    respect_robots_txt: bool = True
    user_agent: str = "SiteExtractor/1.0"

    @classmethod
    def from_settings(cls, settings: CrawlerSettings) -> "CrawlOptions":
        """Cria opções de execução a partir das configurações do ambiente."""

        return cls(
            base_url=settings.base_url,
            output_dir=settings.output_dir,
            pdf_dir=settings.pdf_dir,
            max_depth=settings.max_depth,
            max_pages=settings.max_pages,
            same_domain_only=settings.same_domain_only,
            headless=settings.headless,
            output_format=settings.output_format,
            navigation_timeout_ms=settings.navigation_timeout_ms,
            request_timeout_ms=settings.request_timeout_ms,
            politeness_delay=settings.politeness_delay,
            respect_robots_txt=settings.respect_robots_txt,
            user_agent=settings.user_agent,
        )

    def validate(self) -> None:
        """Valida os campos que podem tornar a execução ambígua ou insegura."""

        parsed_url = urlparse(self.base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("A URL inicial deve usar http:// ou https://.")
        if self.max_depth < 0:
            raise ValueError("A profundidade não pode ser negativa.")
        if self.max_pages < 1:
            raise ValueError("O limite de páginas deve ser maior que zero.")
        if self.output_format not in {"txt", "md"}:
            raise ValueError("O formato de saída deve ser 'txt' ou 'md'.")


@dataclass(slots=True)
class CrawlSummary:
    """Contadores finais de uma execução do crawler."""

    visited_urls: int = 0
    saved_documents: int = 0
    downloaded_pdfs: int = 0
    failed_urls: int = 0
    cancelled: bool = False


def sanitize_filename(name: str, max_length: int = 200) -> str:
    """Converte texto livre em um nome de arquivo portátil."""

    if not name:
        return "documento"
    sanitized = INVALID_FILENAME_CHARS.sub("_", name)
    sanitized = WHITESPACE.sub("_", sanitized)
    sanitized = re.sub(r"_+", "_", sanitized).strip("._")
    return sanitized[:max_length].rstrip("._") or "documento"


def normalize_url(raw_url: str, keep_hash_routing: bool = True) -> str:
    """Normaliza uma URL e descarta parâmetros comuns de rastreamento."""

    if not raw_url or raw_url.lower().startswith(SKIP_SCHEMES):
        return ""

    try:
        parsed = urlparse(raw_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ""

        path = re.sub(r"/{2,}", "/", parsed.path or "/")
        if path != "/":
            path = path.rstrip("/")

        kept_params = []
        for name, value in parse_qsl(parsed.query, keep_blank_values=False):
            lower_name = name.lower()
            if any(lower_name.startswith(prefix) for prefix in DROP_QUERY_PREFIXES):
                continue
            if not ALLOWED_QUERY_PARAMS or name in ALLOWED_QUERY_PARAMS:
                kept_params.append((name, value))

        fragment = ""
        # Fragmentos comuns são descartados; rotas hash de SPAs identificam páginas reais.
        if keep_hash_routing and parsed.fragment.startswith(("!/", "/")):
            fragment = re.sub(r"/{2,}", "/", parsed.fragment).rstrip("/")

        return urlunparse(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                path,
                "",
                urlencode(kept_params, doseq=True),
                fragment,
            )
        )
    except ValueError:
        return ""


def is_same_domain(url: str, base_hostname: str) -> bool:
    """Informa se a URL pertence ao domínio inicial ou a um subdomínio."""

    hostname = (urlparse(url).hostname or "").lower()
    return hostname == base_hostname or hostname.endswith(f".{base_hostname}")


def url_extension(url: str) -> str:
    """Retorna a extensão em minúsculas do caminho de uma URL."""

    return Path(urlparse(url).path).suffix.lower()


def is_pdf(url: str) -> bool:
    """Verifica se a URL aponta para um arquivo PDF."""

    return url_extension(url) == ".pdf"


def is_other_binary(url: str) -> bool:
    """Verifica se a URL aponta para um binário que o crawler não processa."""

    return url_extension(url) in BINARY_EXTENSIONS


def is_url_in_scope(url: str, base_hostname: str, same_domain_only: bool) -> bool:
    """Aplica a política de domínio aos links encontrados."""

    if not url or is_other_binary(url):
        return False
    if same_domain_only:
        return is_same_domain(url, base_hostname)
    return True


def build_output_filename(url: str, extension: str) -> str:
    """Produz um nome estável a partir da URL canônica."""

    parsed = urlparse(url)
    host = sanitize_filename(parsed.netloc.replace(":", "_"), max_length=80)
    path_part = (parsed.path.strip("/") or "index").replace("/", "_")
    fragment_part = ""
    if parsed.fragment:
        fragment = parsed.fragment.replace("!/", "").lstrip("/")
        if fragment:
            fragment_part = f"__FRAG_{fragment.replace('/', '_')}"

    base_name = sanitize_filename(
        f"{host}_{path_part}{fragment_part}",
        max_length=200,
    )
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    suffix = f"__{digest}.{extension}"
    available_length = 240 - len(suffix)
    return f"{base_name[:available_length].rstrip('._')}{suffix}"


def yaml_frontmatter(metadata: dict[str, Any]) -> str:
    """Serializa metadados simples sem exigir uma biblioteca YAML."""

    def escape(value: Any) -> str:
        """Escapa um valor destinado a uma string YAML entre aspas."""

        return str(value).replace("\n", " ").replace('"', '\\"')

    lines = ["---"]
    for key, value in metadata.items():
        if isinstance(value, (list, tuple)):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f'  - "{escape(item)}"')
        else:
            lines.append(f'{key}: "{escape(value)}"')
    lines.append("---\n")
    return "\n".join(lines)


def collect_links(page: Any) -> set[str]:
    """Coleta e normaliza os links disponíveis na página atual."""

    links: set[str] = set()
    try:
        anchors = page.locator("a[href]")
        for index in range(anchors.count()):
            try:
                href = anchors.nth(index).get_attribute("href") or ""
                absolute_url = page.evaluate(
                    "(value) => new URL(value, document.baseURI).toString()",
                    href,
                )
                normalized_url = normalize_url(absolute_url)
                if normalized_url:
                    links.add(normalized_url)
            except Exception:
                continue
    except Exception:
        return links
    return links


def should_block_request(request: Any) -> bool:
    """Bloqueia recursos pesados e rastreadores que não ajudam na extração."""

    if request.resource_type in {"image", "media", "font"}:
        return True
    request_url = request.url.lower()
    return any(fragment in request_url for fragment in TRACKER_HOST_FRAGMENTS)


def navigate_with_retries(
    page: Any,
    url: str,
    options: CrawlOptions,
    log: LogCallback,
    stop_event: Event,
) -> bool:
    """Abre uma página com tentativas adicionais para falhas transitórias."""

    last_error: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        if stop_event.is_set():
            return False
        try:
            page.goto(
                url,
                timeout=options.navigation_timeout_ms,
                wait_until=WAIT_UNTIL,
            )
            page.wait_for_timeout(EXTRA_SPA_WAIT_MS)
            return True
        except PlaywrightTimeout as error:
            last_error = error
            log(f"Timeout ao navegar ({attempt}/{RETRIES}): {url}")
        except Exception as error:
            last_error = error
            log(f"Erro ao navegar ({attempt}/{RETRIES}) em {url}: {error}")
        if stop_event.wait(BACKOFF_SECONDS**attempt):
            return False

    log(f"Falha definitiva em {url}: {last_error}")
    return False


def canonical_url(page: Any, fallback_url: str) -> str:
    """Obtém a URL canônica declarada pela página, quando válida."""

    try:
        canonical_link = page.locator("link[rel='canonical']")
        if not canonical_link.count():
            return fallback_url
        href = canonical_link.get_attribute("href")
        if href:
            absolute_url = page.evaluate(
                "(value) => new URL(value, document.baseURI).toString()",
                href,
            )
            return normalize_url(absolute_url) or fallback_url
    except Exception:
        pass
    return fallback_url


def page_language(page: Any) -> str:
    """Lê o idioma declarado no documento."""

    try:
        language = page.get_attribute("html", "lang")
        return (language or "").strip()
    except Exception:
        return ""


def last_modified(page: Any) -> str:
    """Procura uma data de modificação em metadados conhecidos."""

    selectors = (
        "meta[property='article:modified_time']",
        "meta[name='last-modified']",
        "meta[name='modified']",
        "meta[itemprop='dateModified']",
    )
    for selector in selectors:
        try:
            metadata = page.locator(selector)
            if not metadata.count():
                continue
            value = metadata.get_attribute("content")
            if value:
                return value
        except Exception:
            continue
    return ""


def html_to_markdown(html: str) -> str:
    """Converte HTML para Markdown e normaliza espaços verticais."""

    if not html:
        return ""
    content = markdownify(
        html,
        heading_style="ATX",
        strip=["script", "style", "noscript"],
    )
    content = re.sub(r"[ \t]+\n", "\n", content)
    return re.sub(r"\n{3,}", "\n\n", content).strip()


def html_to_plain_text(html: str) -> str:
    """Extrai texto legível do HTML preservando separações entre blocos."""

    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    for unwanted in soup(["script", "style", "noscript"]):
        unwanted.decompose()
    lines = [line.strip() for line in soup.get_text("\n").splitlines()]
    return "\n".join(line for line in lines if line)


def extract_main_content(page: Any) -> tuple[str, str]:
    """Retorna um título provável e o HTML da região principal."""

    document_html = page.content()
    try:
        document = Document(document_html)
        title = (document.short_title() or "").strip()
        article_html = document.summary(html_partial=True)
        article_text = BeautifulSoup(article_html, "lxml").get_text(" ", strip=True)
        if len(article_text) >= MIN_CONTENT_CHARS:
            return title, article_html
    except Exception:
        pass

    for selector in ("main", "article", "[role='main']", "#main", ".content", "body"):
        try:
            locator = page.locator(selector)
            if locator.count() and len(locator.inner_text(timeout=2_000)) >= MIN_CONTENT_CHARS:
                return (page.title() or "").strip(), locator.inner_html(timeout=2_000)
        except Exception:
            continue
    return (page.title() or "").strip(), ""


def reveal_navigation_menus(page: Any) -> None:
    """Tenta revelar links escondidos em menus acionados por hover ou clique."""

    selectors = (
        "nav li",
        ".menu li",
        ".dropdown",
        "[role='menu'] li",
        "[aria-haspopup='true']",
        ".navbar li",
        ".nav-item",
        ".dropdown-toggle",
    )
    for selector in selectors:
        try:
            items = page.locator(selector)
            for index in range(min(items.count(), 100)):
                item = items.nth(index)
                try:
                    item.hover(timeout=500)
                    if not item.locator("a[href]").count():
                        item.click(timeout=300, force=True)
                except Exception:
                    continue
        except Exception:
            continue


def load_robots_policy(
    base_url: str,
    session: requests.Session,
    options: CrawlOptions,
    log: LogCallback,
) -> RobotFileParser | None:
    """Baixa e interpreta o robots.txt do domínio inicial."""

    if not options.respect_robots_txt:
        return None

    parsed = urlparse(base_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    parser = RobotFileParser()
    parser.set_url(robots_url)
    try:
        response = session.get(
            robots_url,
            timeout=options.request_timeout_ms / 1_000,
        )
        if response.status_code == 200:
            parser.parse(response.text.splitlines())
            log(f"Política robots.txt carregada de {robots_url}")
            return parser
        if response.status_code in {401, 403}:
            parser.disallow_all = True
            log(f"O acesso ao robots.txt foi negado ({response.status_code}).")
            return parser
    except requests.RequestException as error:
        log(f"Não foi possível consultar robots.txt: {error}")
    return None


def download_pdf(
    url: str,
    destination: Path,
    session: requests.Session,
    log: LogCallback,
) -> Path | None:
    """Baixa um PDF e devolve seu caminho local quando bem-sucedido."""

    try:
        response = session.get(url, timeout=HTTP_TIMEOUT_SECONDS)
        response.raise_for_status()
        if not response.content:
            return None

        source_name = Path(urlparse(url).path).name or "documento.pdf"
        stem = sanitize_filename(Path(source_name).stem, max_length=180)
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
        destination.mkdir(parents=True, exist_ok=True)
        output_path = destination / f"{stem}__{digest}.pdf"
        output_path.write_bytes(response.content)
        log(f"PDF baixado: {output_path}")
        return output_path
    except (OSError, requests.RequestException) as error:
        log(f"Falha ao baixar PDF {url}: {error}")
        return None


def save_document(
    *,
    url: str,
    title: str,
    language: str,
    modified_at: str,
    content_html: str,
    options: CrawlOptions,
) -> Path:
    """Grava o conteúdo extraído no formato escolhido pela execução."""

    output_path = options.output_dir / build_output_filename(
        url,
        options.output_format,
    )
    if options.output_format == "md":
        content = html_to_markdown(content_html)
        metadata = {
            "url": url,
            "title": title,
            "lang": language,
            "last_modified": modified_at,
            "extracted_at": datetime.now(timezone.utc).isoformat(),
        }
        if title and not content.lstrip().startswith("# "):
            content = f"# {title}\n\n{content}"
        output_path.write_text(
            f"{yaml_frontmatter(metadata)}{content}\n",
            encoding="utf-8",
        )
    else:
        content = html_to_plain_text(content_html)
        heading = f"{title}\n{'=' * len(title)}\n\n" if title else ""
        output_path.write_text(
            f"{heading}Fonte: {url}\n\n{content}\n",
            encoding="utf-8",
        )
    return output_path


def enqueue_discovered_links(
    links: set[str],
    *,
    current_depth: int,
    options: CrawlOptions,
    base_hostname: str,
    pending_urls: deque[tuple[str, int]],
    queued_urls: set[str],
    visited_urls: set[str],
) -> int:
    """Adiciona links inéditos à próxima camada do rastreamento."""

    if current_depth >= options.max_depth:
        return 0

    added_links = 0
    for link in links:
        if link in queued_urls or link in visited_urls:
            continue
        if not is_url_in_scope(link, base_hostname, options.same_domain_only):
            continue
        queued_urls.add(link)
        pending_urls.append((link, current_depth + 1))
        added_links += 1
    return added_links


def _effective_delay(
    robots_policy: RobotFileParser | None,
    options: CrawlOptions,
) -> float:
    """Combina o intervalo configurado com o solicitado em robots.txt."""

    if robots_policy is None:
        return options.politeness_delay
    robots_delay = robots_policy.crawl_delay(options.user_agent)
    return max(options.politeness_delay, float(robots_delay or 0))


def crawl_site(
    options: CrawlOptions,
    *,
    stop_event: Event | None = None,
    log: LogCallback = print,
    progress: ProgressCallback | None = None,
) -> CrawlSummary:
    """Rastreia um site por largura até a profundidade e o limite definidos."""

    options.validate()
    stop_event = stop_event or Event()
    if stop_event.is_set():
        log("Rastreamento cancelado antes da inicialização.")
        return CrawlSummary(cancelled=True)

    base_url = normalize_url(options.base_url)
    base_hostname = (urlparse(base_url).hostname or "").lower()
    ensure_directories(options.output_dir, options.pdf_dir)

    summary = CrawlSummary()
    # A profundidade acompanha a URL na fila para não depender da ordem dos links.
    pending_urls: deque[tuple[str, int]] = deque([(base_url, 0)])
    queued_urls = {base_url}
    visited_urls: set[str] = set()
    session = requests.Session()
    session.headers.update({"User-Agent": options.user_agent})
    robots_policy = load_robots_policy(base_url, session, options, log)
    request_delay = _effective_delay(robots_policy, options)

    log(f"Iniciando rastreamento de {base_hostname}.")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=options.headless)
        context = browser.new_context(user_agent=options.user_agent)
        context.set_default_navigation_timeout(options.navigation_timeout_ms)
        context.set_default_timeout(options.request_timeout_ms)
        context.route(
            "**/*",
            lambda route: (
                route.abort() if should_block_request(route.request) else route.continue_()
            ),
        )
        page = context.new_page()

        try:
            while pending_urls and summary.visited_urls < options.max_pages:
                if stop_event.is_set():
                    summary.cancelled = True
                    break

                url, depth = pending_urls.popleft()
                if url in visited_urls:
                    continue
                visited_urls.add(url)

                if robots_policy and not robots_policy.can_fetch(options.user_agent, url):
                    log(f"Ignorado por robots.txt: {url}")
                    continue

                if is_pdf(url):
                    if download_pdf(url, options.pdf_dir, session, log):
                        summary.downloaded_pdfs += 1
                    continue

                if not navigate_with_retries(page, url, options, log, stop_event):
                    if stop_event.is_set():
                        summary.cancelled = True
                        break
                    summary.failed_urls += 1
                    continue

                summary.visited_urls += 1
                if progress:
                    progress(
                        summary.visited_urls,
                        options.max_pages,
                        len(pending_urls),
                    )

                try:
                    reveal_navigation_menus(page)
                    links = collect_links(page)
                    title_guess, content_html = extract_main_content(page)
                    plain_content = html_to_plain_text(content_html)

                    if len(plain_content) >= MIN_CONTENT_CHARS:
                        resolved_url = canonical_url(page, url)
                        output_path = save_document(
                            url=resolved_url,
                            title=(page.title() or title_guess).strip(),
                            language=page_language(page),
                            modified_at=last_modified(page),
                            content_html=content_html,
                            options=options,
                        )
                        summary.saved_documents += 1
                        log(f"[{summary.visited_urls}] Arquivo salvo: {output_path}")
                    else:
                        log(f"Conteúdo insuficiente, arquivo não salvo: {url}")

                    enqueue_discovered_links(
                        links,
                        current_depth=depth,
                        options=options,
                        base_hostname=base_hostname,
                        pending_urls=pending_urls,
                        queued_urls=queued_urls,
                        visited_urls=visited_urls,
                    )
                except Exception as error:
                    summary.failed_urls += 1
                    log(f"Erro ao processar {url}: {error}")

                if request_delay and stop_event.wait(request_delay):
                    summary.cancelled = True
                    break
        finally:
            context.close()
            browser.close()
            session.close()

    if stop_event.is_set():
        summary.cancelled = True
        log("Rastreamento interrompido pelo usuário.")
    else:
        log(
            "Rastreamento concluído: "
            f"{summary.visited_urls} páginas, "
            f"{summary.saved_documents} arquivos e "
            f"{summary.downloaded_pdfs} PDFs."
        )
    return summary


def parse_arguments() -> argparse.Namespace:
    """Lê os parâmetros de linha de comando."""

    settings = load_settings()
    parser = argparse.ArgumentParser(
        description="Extrai páginas de um site para arquivos de texto ou Markdown.",
    )
    parser.add_argument("--base-url", default=settings.base_url)
    parser.add_argument("--max-depth", type=int, default=settings.max_depth)
    parser.add_argument("--max-pages", type=int, default=settings.max_pages)
    parser.add_argument("--output-dir", type=Path, default=settings.output_dir)
    parser.add_argument("--pdf-dir", type=Path, default=settings.pdf_dir)
    parser.add_argument(
        "--output-format",
        choices=("txt", "md"),
        default=settings.output_format,
    )
    parser.add_argument(
        "--same-domain-only",
        action=argparse.BooleanOptionalAction,
        default=settings.same_domain_only,
    )
    parser.add_argument(
        "--respect-robots-txt",
        action=argparse.BooleanOptionalAction,
        default=settings.respect_robots_txt,
    )
    browser_mode = parser.add_mutually_exclusive_group()
    browser_mode.add_argument(
        "--headed",
        dest="headless",
        action="store_false",
        help="Exibe o navegador durante a extração.",
    )
    browser_mode.add_argument(
        "--headless",
        dest="headless",
        action="store_true",
        help="Executa o navegador em segundo plano.",
    )
    parser.set_defaults(headless=settings.headless)
    return parser.parse_args()


def main() -> int:
    """Executa o crawler pela linha de comando."""

    settings = load_settings()
    arguments = parse_arguments()
    options = CrawlOptions(
        base_url=arguments.base_url,
        output_dir=arguments.output_dir,
        pdf_dir=arguments.pdf_dir,
        max_depth=arguments.max_depth,
        max_pages=arguments.max_pages,
        same_domain_only=arguments.same_domain_only,
        headless=arguments.headless,
        output_format=arguments.output_format,
        navigation_timeout_ms=settings.navigation_timeout_ms,
        request_timeout_ms=settings.request_timeout_ms,
        politeness_delay=settings.politeness_delay,
        respect_robots_txt=arguments.respect_robots_txt,
        user_agent=settings.user_agent,
    )
    try:
        crawl_site(options)
    except (OSError, ValueError) as error:
        print(f"Erro de configuração: {error}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
