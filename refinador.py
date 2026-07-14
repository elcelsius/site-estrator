"""Limpa, deduplica e opcionalmente refina documentos com uma LLM."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, TextIO

import requests
import yaml
from dotenv import load_dotenv

try:
    from google import genai
    from google.genai import types as genai_types
except ImportError:
    genai = None
    genai_types = None

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None


RAW_DIR = Path("data/0_extracao_bruta")
PROCESSED_DIR = RAW_DIR / "_processados"
ERROR_DIR = RAW_DIR / "_com_erro"
LOG_DIR = RAW_DIR / "log"
OUTPUT_DIR = Path("data/1_markdowns_limpos")
MANIFEST_PATH = OUTPUT_DIR / "manifest.jsonl"

MIN_CONTENT_CHARS = 500
SIMHASH_MAX_DISTANCE = 3
SIMHASH_TEXT_LIMIT = 200_000
SIMHASH_MAX_FEATURES = 20_000
TOKEN_PATTERN = re.compile(r"[0-9A-Za-zÀ-ÖØ-öø-ÿ_]+", re.UNICODE)
LOW_QUALITY_MARKERS = (
    "resultados da busca",
    "sem resultados",
    "pesquisar",
    "buscar",
    "posts recentes",
    "arquivos do mês",
)

GEMINI_DEFAULT_MODEL = "gemini-2.5-flash"
LLM_MAX_CHARS = 20_000
LLM_CHUNK_CHARS = 5_000
LLM_TEMPERATURE = 0.0
LLM_TIMEOUT_SECONDS = 120
LLM_MAX_RETRIES = 4
LLM_REQUESTS_PER_MINUTE = 20

REFINEMENT_PROMPT = """Você é um refinador de texto para RAG. Reescreva levemente para clareza, padronize títulos, quebras de linha e listas e corrija erros de OCR, sem inventar informações. Preserve o idioma original, nomes, números, datas e citações. Retorne somente o corpo do documento em Markdown, sem prólogos, epílogos, comentários ou JSON. Se não houver conteúdo, retorne vazio."""
OLLAMA_SYSTEM_PROMPT = """Você refina documentos para indexação em RAG. Devolva apenas o conteúdo final em Markdown válido. Não inclua preâmbulos, JSON, explicações ou texto externo ao documento. Não invente dados e preserve o idioma, os números e as datas do original."""


@dataclass(frozen=True, slots=True)
class RefinementOptions:
    """Opções efetivas de uma execução do refinador."""

    provider: str
    model: str
    only_path: str | None
    google_heuristic: bool
    ollama_heuristic: bool
    llm_optional: bool
    show_progress: bool
    ollama_base_url: str


@dataclass(slots=True)
class RefinementStats:
    """Contadores acumulados durante o refino."""

    input_files: int = 0
    refined_files: int = 0
    duplicates: int = 0
    failed_files: int = 0


def utc_now_iso() -> str:
    """Retorna o instante atual em UTC no formato ISO 8601."""

    return datetime.now(timezone.utc).isoformat()


def build_log_path() -> Path:
    """Cria um nome de log que identifica a execução pelo horário local."""

    timestamp = datetime.now().strftime("%Y-%m-%d-%H_%M_%S")
    return LOG_DIR / f"{timestamp}.log"


def normalize_text(text: str) -> str:
    """Normaliza espaços e limita o trecho usado na deduplicação."""

    normalized = re.sub(r"\s+", " ", text, flags=re.MULTILINE).strip().lower()
    return normalized[:SIMHASH_TEXT_LIMIT]


def extract_features(
    text: str,
    max_features: int = SIMHASH_MAX_FEATURES,
) -> list[tuple[str, int]]:
    """Extrai termos e frequências limitadas para o cálculo de simhash."""

    frequencies = Counter(TOKEN_PATTERN.findall(text))
    return [
        (token, min(frequency, 255)) for token, frequency in frequencies.most_common(max_features)
    ]


def calculate_simhash64(features: Iterable[tuple[str, int]]) -> int:
    """Calcula um simhash de 64 bits a partir de termos ponderados."""

    vector = [0] * 64
    for feature, weight in features:
        feature_hash = int(
            hashlib.sha1(feature.encode("utf-8")).hexdigest()[:16],
            16,
        )
        for bit in range(64):
            vector[bit] += weight if (feature_hash >> bit) & 1 else -weight

    result = 0
    for bit, value in enumerate(vector):
        if value >= 0:
            result |= 1 << bit
    return result


def hamming_distance(first: int, second: int) -> int:
    """Conta quantos bits diferem entre dois hashes."""

    return (first ^ second).bit_count()


def read_text(path: Path) -> str:
    """Lê um arquivo UTF-8 e retorna vazio em caso de falha."""

    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def split_frontmatter(markdown_text: str) -> tuple[dict[str, Any], str]:
    """Separa o front matter YAML do corpo do documento."""

    if markdown_text.startswith("---"):
        parts = markdown_text.split("---", 2)
        if len(parts) == 3:
            try:
                metadata = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                metadata = {}
            if isinstance(metadata, dict):
                return metadata, parts[2].strip()
    return {}, markdown_text.strip()


def make_frontmatter(metadata: dict[str, Any]) -> str:
    """Serializa metadados como front matter YAML."""

    serialized = yaml.safe_dump(
        metadata,
        sort_keys=False,
        allow_unicode=True,
    ).strip()
    return f"---\n{serialized}\n---\n"


def is_low_quality(text: str) -> bool:
    """Detecta páginas curtas ou compostas principalmente por navegação."""

    if len(text) < MIN_CONTENT_CHARS:
        return True
    lower_text = text.lower()
    marker_count = sum(marker in lower_text for marker in LOW_QUALITY_MARKERS)
    return marker_count >= 2


def llm_is_useful(body: str) -> bool:
    """Estima se o texto se beneficia do refino por modelo de linguagem."""

    if len(body) < 1_200:
        return False
    digit_ratio = sum(character.isdigit() for character in body) / len(body)
    long_lines = sum(len(line) > 180 for line in body.splitlines())
    return digit_ratio > 0.12 or long_lines >= 4


def should_apply_llm(options: RefinementOptions, body: str) -> bool:
    """Aplica a política de custo escolhida para cada provedor."""

    if options.provider == "google":
        return llm_is_useful(body) if options.google_heuristic else True
    if options.provider == "ollama":
        return llm_is_useful(body) if options.ollama_heuristic else True
    return False


def wait_for_rate_limit(
    previous_call_at: float | None,
    requests_per_minute: int = LLM_REQUESTS_PER_MINUTE,
) -> float:
    """Mantém um intervalo mínimo entre chamadas ao serviço remoto."""

    if not requests_per_minute:
        return time.time()
    minimum_interval = 60.0 / max(1, requests_per_minute)
    now = time.time()
    if previous_call_at is not None:
        elapsed = now - previous_call_at
        if elapsed < minimum_interval:
            time.sleep(minimum_interval - elapsed)
            now = time.time()
    return now


def chunk_text(text: str, chunk_chars: int = LLM_CHUNK_CHARS) -> list[str]:
    """Divide texto longo procurando respeitar limites entre parágrafos."""

    text = text.strip()
    if len(text) <= chunk_chars:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        limit = min(len(text), start + chunk_chars)
        paragraph_end = text.rfind("\n\n", start, limit)
        minimum_break = start + int(chunk_chars * 0.6)
        end = limit if paragraph_end < minimum_break else paragraph_end + 2
        chunks.append(text[start:end])
        start = end
    return chunks


def write_log(log_file: TextIO, event: str, **fields: Any) -> None:
    """Registra um evento estruturado no log JSON Lines."""

    record = {"ts": utc_now_iso(), "event": event, **fields}
    log_file.write(json.dumps(record, ensure_ascii=False) + "\n")
    log_file.flush()


def gemini_preflight(client: Any, model_name: str, log_file: TextIO) -> bool:
    """Faz uma chamada curta para validar credenciais e modelo do Gemini."""

    if genai_types is None:
        return False
    try:
        client.models.generate_content(
            model=model_name,
            contents="ping",
            config=genai_types.GenerateContentConfig(temperature=0),
        )
        write_log(
            log_file,
            "llm_preflight_ok",
            provider="google",
            model=model_name,
        )
        return True
    except Exception as error:
        write_log(
            log_file,
            "llm_preflight_error",
            provider="google",
            model=model_name,
            error=str(error),
        )
        return False


def call_gemini(
    client: Any,
    model_name: str,
    chunk: str,
    log_file: TextIO,
    previous_call_at: float | None,
) -> tuple[str, float]:
    """Envia um trecho ao Gemini com repetição e limite de chamadas."""

    if genai_types is None:
        raise RuntimeError("A biblioteca google-genai não está instalada.")
    last_call_at = previous_call_at
    for attempt in range(1, LLM_MAX_RETRIES + 1):
        try:
            last_call_at = wait_for_rate_limit(last_call_at)
            response = client.models.generate_content(
                model=model_name,
                contents=[REFINEMENT_PROMPT, chunk],
                config=genai_types.GenerateContentConfig(
                    temperature=LLM_TEMPERATURE,
                ),
            )
            response_text = getattr(response, "text", "") or ""
            if not response_text.strip():
                raise RuntimeError("O modelo retornou uma resposta vazia.")
            write_log(
                log_file,
                "llm_chunk_ok",
                provider="google",
                model=model_name,
                attempt=attempt,
                chars_in=len(chunk),
                chars_out=len(response_text),
            )
            return response_text, last_call_at
        except Exception as error:
            write_log(
                log_file,
                "llm_chunk_error",
                provider="google",
                model=model_name,
                attempt=attempt,
                error=str(error),
            )
            if attempt == LLM_MAX_RETRIES:
                raise
            time.sleep(min(60, 5 * attempt) + random.uniform(0, 0.5))
    raise RuntimeError("O limite de tentativas do Gemini foi atingido.")


def ollama_preflight(
    base_url: str,
    model_name: str,
    log_file: TextIO,
) -> bool:
    """Consulta o Ollama local e verifica se o modelo está disponível."""

    try:
        response = requests.get(f"{base_url.rstrip('/')}/api/tags", timeout=10)
        response.raise_for_status()
        models = response.json().get("models", [])
        available_names = {model.get("name", "") for model in models if isinstance(model, dict)}
        is_available = model_name in available_names or any(
            name.startswith(model_name) for name in available_names
        )
        write_log(
            log_file,
            "llm_preflight_ok" if is_available else "llm_preflight_warning",
            provider="ollama",
            model=model_name,
            available_models=sorted(available_names),
        )
        return is_available
    except (requests.RequestException, ValueError) as error:
        write_log(
            log_file,
            "llm_preflight_error",
            provider="ollama",
            model=model_name,
            error=str(error),
        )
        return False


def call_ollama(
    base_url: str,
    model_name: str,
    chunk: str,
    log_file: TextIO,
) -> str:
    """Envia um trecho ao endpoint local de geração do Ollama."""

    payload = {
        "model": model_name,
        "system": OLLAMA_SYSTEM_PROMPT,
        "prompt": f"{REFINEMENT_PROMPT}\n\n---\n\n{chunk}",
        "stream": False,
        "keep_alive": "24h",
    }
    response = requests.post(
        f"{base_url.rstrip('/')}/api/generate",
        json=payload,
        timeout=LLM_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    response_text = response.json().get("response", "") or ""
    if not response_text.strip():
        raise RuntimeError("O Ollama retornou uma resposta vazia.")
    write_log(
        log_file,
        "llm_chunk_ok",
        provider="ollama",
        model=model_name,
        chars_in=len(chunk),
        chars_out=len(response_text),
    )
    return response_text


def strip_model_wrappers(text: str) -> str:
    """Remove cercas e preâmbulos que alguns modelos acrescentam à resposta."""

    if not text:
        return text
    lines = text.splitlines()
    if lines and re.match(
        r"^here\s+is\s+the\s+rewritten\s+text\s+in\s+markdown\s+format:?:?$",
        lines[0].strip(),
        flags=re.IGNORECASE,
    ):
        lines = lines[1:]
    cleaned = "\n".join(lines).strip()
    fenced = re.fullmatch(r"```(?:markdown|md)?\s*(.*?)\s*```", cleaned, re.DOTALL)
    return fenced.group(1).strip() if fenced else cleaned


def refine_with_gemini(
    client: Any,
    model_name: str,
    body: str,
    log_file: TextIO,
    llm_required: bool,
) -> tuple[str, int, int]:
    """Refina o documento em trechos usando o Gemini."""

    chunks = chunk_text(body[:LLM_MAX_CHARS])
    output_parts: list[str] = []
    error_count = 0
    last_call_at: float | None = None
    for chunk in chunks:
        try:
            response, last_call_at = call_gemini(
                client,
                model_name,
                chunk,
                log_file,
                last_call_at,
            )
            output_parts.append(strip_model_wrappers(response))
        except Exception:
            error_count += 1
            if llm_required:
                raise
            output_parts.append(chunk.strip())
    return "\n\n".join(output_parts).strip(), len(chunks), error_count


def refine_with_ollama(
    base_url: str,
    model_name: str,
    body: str,
    log_file: TextIO,
    llm_required: bool,
) -> tuple[str, int, int]:
    """Refina o documento em trechos usando um Ollama local."""

    chunks = chunk_text(body[:LLM_MAX_CHARS])
    output_parts: list[str] = []
    error_count = 0
    for chunk in chunks:
        try:
            response = call_ollama(base_url, model_name, chunk, log_file)
            output_parts.append(strip_model_wrappers(response))
        except Exception as error:
            error_count += 1
            write_log(
                log_file,
                "llm_chunk_error",
                provider="ollama",
                model=model_name,
                error=str(error),
            )
            if llm_required:
                raise
            output_parts.append(chunk.strip())
    return "\n\n".join(output_parts).strip(), len(chunks), error_count


def load_manifest_outputs(manifest_path: Path) -> set[str]:
    """Lê os caminhos de saída já registrados no manifesto."""

    outputs: set[str] = set()
    if not manifest_path.exists():
        return outputs
    try:
        with manifest_path.open("r", encoding="utf-8") as manifest_file:
            for line in manifest_file:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                output_path = record.get("out")
                if output_path:
                    outputs.add(output_path)
    except OSError:
        pass
    return outputs


def iter_input_files(only_path: str | None) -> Iterable[Path]:
    """Lista os Markdown elegíveis, opcionalmente filtrados por caminho."""

    if only_path:
        direct_path = Path(only_path)
        if direct_path.is_file():
            yield direct_path
            return
        relative_path = RAW_DIR / only_path
        if relative_path.is_file():
            yield relative_path
            return
        yield from (path for path in RAW_DIR.rglob(only_path) if path.is_file())
        return

    ignored_directories = {PROCESSED_DIR, ERROR_DIR, LOG_DIR}
    for path in RAW_DIR.rglob("*.md"):
        if any(directory in path.parents for directory in ignored_directories):
            continue
        yield path


def ask_provider_interactive(
    default_provider: str,
    default_model: str,
) -> tuple[str, str, bool]:
    """Solicita provedor, modelo e política de custo no terminal."""

    print("Escolha o modo de refino:")
    print("  [1] API do Google (Gemini)")
    print("  [2] Ollama local")
    print("  [3] Sem LLM")
    provider_map = {"1": "google", "2": "ollama", "3": "none"}
    default_answer = {"google": "1", "ollama": "2", "none": "3"}.get(
        default_provider,
        "3",
    )
    answer = input(f"Opção [1/2/3], padrão={default_answer}: ").strip() or default_answer
    provider = provider_map.get(answer, default_provider)

    use_heuristic = False
    if provider == "google":
        heuristic_answer = (
            input("Usar o modo econômico, que seleciona os textos mais complexos? [S/n]: ")
            .strip()
            .lower()
        )
        use_heuristic = heuristic_answer != "n"

    model = default_model
    if provider != "none":
        typed_model = input(f"Modelo [padrão: {default_model}]: ").strip()
        if typed_model:
            model = typed_model
    return provider, model, use_heuristic


def move_to_directory(source: Path, destination_directory: Path) -> Path:
    """Move um arquivo sem sobrescrever outro de mesmo nome."""

    destination_directory.mkdir(parents=True, exist_ok=True)
    destination = destination_directory / source.name
    if destination.exists():
        digest = hashlib.sha1(str(source).encode("utf-8")).hexdigest()[:8]
        destination = destination_directory / f"{source.stem}__{digest}{source.suffix}"
    shutil.move(str(source), destination)
    return destination


def prepare_llm(
    options: RefinementOptions,
    log_file: TextIO,
) -> tuple[str, bool, Any | None]:
    """Valida o provedor e retorna o modo efetivo e sua disponibilidade."""

    provider = options.provider
    if provider == "google":
        api_key = os.getenv("GEMINI_API_KEY", "")
        if genai is None or genai_types is None or not api_key:
            message = "google-genai ausente ou GEMINI_API_KEY não definida"
            write_log(log_file, "llm_preflight_error", provider="google", error=message)
            if not options.llm_optional:
                raise RuntimeError(message)
            return "none", False, None
        client = genai.Client(
            api_key=api_key,
            http_options=genai_types.HttpOptions(
                timeout=LLM_TIMEOUT_SECONDS * 1_000,
            ),
        )
        if gemini_preflight(client, options.model, log_file):
            return provider, True, client
        client.close()
    elif provider == "ollama":
        if ollama_preflight(options.ollama_base_url, options.model, log_file):
            return provider, True, None
    else:
        return "none", False, None

    if options.llm_optional:
        return "none", False, None
    raise RuntimeError(f"O provedor {provider} não está disponível.")


def refine_body(
    options: RefinementOptions,
    provider: str,
    body: str,
    log_file: TextIO,
    gemini_client: Any | None,
) -> tuple[str, int, int, bool]:
    """Refina um corpo quando a política indicar o uso da LLM."""

    effective_options = RefinementOptions(
        provider=provider,
        model=options.model,
        only_path=options.only_path,
        google_heuristic=options.google_heuristic,
        ollama_heuristic=options.ollama_heuristic,
        llm_optional=options.llm_optional,
        show_progress=options.show_progress,
        ollama_base_url=options.ollama_base_url,
    )
    if not should_apply_llm(effective_options, body):
        return body, 0, 0, False

    llm_required = not options.llm_optional
    if provider == "google":
        if gemini_client is None:
            raise RuntimeError("O cliente do Gemini não foi inicializado.")
        refined, chunks, errors = refine_with_gemini(
            gemini_client,
            options.model,
            body,
            log_file,
            llm_required,
        )
        return refined, chunks, errors, errors < chunks
    if provider == "ollama":
        refined, chunks, errors = refine_with_ollama(
            options.ollama_base_url,
            options.model,
            body,
            log_file,
            llm_required,
        )
        return refined, chunks, errors, errors < chunks
    return body, 0, 0, False


def process_documents(
    files: list[Path],
    options: RefinementOptions,
    provider: str,
    llm_available: bool,
    gemini_client: Any | None,
    manifest_file: TextIO,
    log_file: TextIO,
) -> RefinementStats:
    """Processa os documentos, atualiza o manifesto e organiza as entradas."""

    stats = RefinementStats()
    known_hashes: list[int] = []
    known_outputs = load_manifest_outputs(MANIFEST_PATH)
    progress_bar = (
        tqdm(total=len(files), desc="Refinando", unit="doc")
        if tqdm and options.show_progress
        else None
    )

    try:
        for input_path in files:
            stats.input_files += 1
            try:
                raw_text = read_text(input_path)
                if not raw_text:
                    raise RuntimeError("arquivo vazio ou ilegível")
                metadata, body = split_frontmatter(raw_text)
                body = re.sub(r"\n{3,}", "\n\n", body).strip()
                if is_low_quality(body):
                    raise RuntimeError("conteúdo de baixa qualidade")

                normalized_body = normalize_text(body)
                features = extract_features(normalized_body)
                if not features:
                    raise RuntimeError("conteúdo sem termos indexáveis")
                simhash = calculate_simhash64(features)
                if any(
                    hamming_distance(simhash, previous) <= SIMHASH_MAX_DISTANCE
                    for previous in known_hashes
                ):
                    stats.duplicates += 1
                    write_log(
                        log_file,
                        "duplicate",
                        file=str(input_path),
                        reason=f"hamming<={SIMHASH_MAX_DISTANCE}",
                    )
                    move_to_directory(input_path, PROCESSED_DIR)
                    continue
                known_hashes.append(simhash)

                if llm_available:
                    refined_body, chunk_count, chunk_errors, llm_applied = refine_body(
                        options,
                        provider,
                        body,
                        log_file,
                        gemini_client,
                    )
                else:
                    refined_body, chunk_count, chunk_errors, llm_applied = (
                        body,
                        0,
                        0,
                        False,
                    )

                metadata = dict(metadata)
                metadata["refined_by"] = (
                    "gemini"
                    if provider == "google" and llm_applied
                    else "ollama"
                    if provider == "ollama" and llm_applied
                    else "local"
                )
                if llm_applied:
                    metadata.update(
                        {
                            "refined_model": options.model,
                            "refined_at": utc_now_iso(),
                            "llm_chunks": chunk_count,
                            "llm_chunk_errors": chunk_errors,
                        }
                    )
                metadata["content_hash"] = hashlib.sha1(
                    normalize_text(refined_body).encode("utf-8")
                ).hexdigest()

                try:
                    relative_parent = input_path.resolve().parent.relative_to(RAW_DIR.resolve())
                except ValueError:
                    relative_parent = Path(input_path.parent.name)
                if relative_parent == Path("."):
                    output_stem = input_path.stem
                else:
                    # O hash da subpasta evita colisões sem expor o caminho no nome final.
                    parent_digest = hashlib.sha1(str(relative_parent).encode("utf-8")).hexdigest()[
                        :8
                    ]
                    output_stem = f"{input_path.stem}__{parent_digest}"
                output_path = OUTPUT_DIR / f"{output_stem}__REF.md"
                temporary_path = output_path.with_suffix(".tmp")
                temporary_path.write_text(
                    f"{make_frontmatter(metadata)}{refined_body}\n",
                    encoding="utf-8",
                )
                os.replace(temporary_path, output_path)

                output_key = os.fspath(output_path)
                manifest_record = {
                    "ts": utc_now_iso(),
                    "in": str(input_path),
                    "out": output_key,
                    "simhash64_hex": hex(simhash),
                    "llm_applied": llm_applied,
                    "provider": provider if llm_applied else "",
                    "model": options.model if llm_applied else "",
                }
                if output_key not in known_outputs:
                    manifest_file.write(json.dumps(manifest_record, ensure_ascii=False) + "\n")
                    manifest_file.flush()
                    known_outputs.add(output_key)

                write_log(
                    log_file,
                    "ok",
                    file=str(input_path),
                    output=output_key,
                    simhash64_hex=hex(simhash),
                    llm_applied=llm_applied,
                    provider=manifest_record["provider"],
                    model=manifest_record["model"],
                )
                move_to_directory(input_path, PROCESSED_DIR)
                stats.refined_files += 1
            except Exception as error:
                stats.failed_files += 1
                write_log(
                    log_file,
                    "error",
                    file=str(input_path),
                    error=str(error),
                )
                if input_path.exists():
                    move_to_directory(input_path, ERROR_DIR)
            finally:
                if progress_bar:
                    progress_bar.set_postfix(
                        ok=stats.refined_files,
                        dup=stats.duplicates,
                        err=stats.failed_files,
                    )
                    progress_bar.update(1)
    finally:
        if progress_bar:
            progress_bar.close()
    return stats


def parse_arguments() -> argparse.Namespace:
    """Lê os parâmetros do refinador."""

    parser = argparse.ArgumentParser(
        description="Refina e deduplica documentos com Gemini, Ollama ou localmente.",
    )
    parser.add_argument("--only", help="Caminho ou padrão relativo a data/0_extracao_bruta.")
    parser.add_argument("--provider", choices=("google", "ollama", "none"))
    parser.add_argument("--model")
    parser.add_argument("--google-heuristic", action="store_true")
    parser.add_argument("--ollama-heuristic", action="store_true")
    parser.add_argument(
        "--llm-optional",
        action="store_true",
        help="Continua localmente quando a LLM falhar.",
    )
    parser.add_argument("--no-progress", action="store_true")
    parser.add_argument("--no-prompt", action="store_true")
    return parser.parse_args()


def build_options(arguments: argparse.Namespace) -> RefinementOptions:
    """Combina argumentos, perguntas interativas e variáveis de ambiente."""

    provider = arguments.provider
    model_name = arguments.model
    google_heuristic = arguments.google_heuristic
    ollama_model = os.getenv("OLLAMA_MODEL", "llama3:8b-instruct-q4_K_M")
    gemini_model = os.getenv("GEMINI_MODEL", GEMINI_DEFAULT_MODEL)

    if provider is None and not arguments.no_prompt:
        provider, model_name, google_heuristic = ask_provider_interactive(
            "none",
            gemini_model,
        )
    provider = provider or "none"
    if not model_name:
        if provider == "google":
            model_name = gemini_model
        elif provider == "ollama":
            model_name = ollama_model
        else:
            model_name = ""

    return RefinementOptions(
        provider=provider,
        model=model_name,
        only_path=arguments.only,
        google_heuristic=google_heuristic,
        ollama_heuristic=arguments.ollama_heuristic,
        llm_optional=arguments.llm_optional,
        show_progress=not arguments.no_progress,
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
    )


def main() -> int:
    """Executa o pipeline de refino pela linha de comando."""

    load_dotenv()
    arguments = parse_arguments()
    options = build_options(arguments)
    for directory in (RAW_DIR, PROCESSED_DIR, ERROR_DIR, LOG_DIR, OUTPUT_DIR):
        directory.mkdir(parents=True, exist_ok=True)

    log_path = build_log_path()
    with (
        MANIFEST_PATH.open("a", encoding="utf-8") as manifest_file,
        log_path.open("a", encoding="utf-8") as log_file,
    ):
        write_log(
            log_file,
            "run_start",
            provider=options.provider,
            model=options.model,
            llm_optional=options.llm_optional,
            google_heuristic=options.google_heuristic,
            ollama_heuristic=options.ollama_heuristic,
        )
        print(f"Log: {log_path}")

        try:
            effective_provider, llm_available, gemini_client = prepare_llm(
                options,
                log_file,
            )
        except RuntimeError as error:
            print(f"Falha na configuração da LLM: {error}")
            return 2

        files = list(iter_input_files(options.only_path))
        print(f"Iniciando refino: {len(files)} arquivo(s), modo {effective_provider}.")
        try:
            stats = process_documents(
                files,
                options,
                effective_provider,
                llm_available,
                gemini_client,
                manifest_file,
                log_file,
            )
        except KeyboardInterrupt:
            print("Refino interrompido.")
            return 130
        finally:
            if gemini_client is not None:
                gemini_client.close()

        write_log(
            log_file,
            "run_end",
            input_files=stats.input_files,
            refined_files=stats.refined_files,
            duplicates=stats.duplicates,
            failed_files=stats.failed_files,
            output_directory=str(OUTPUT_DIR),
        )
        print(
            "Refino concluído: "
            f"entrada={stats.input_files}, "
            f"ok={stats.refined_files}, "
            f"duplicados={stats.duplicates}, "
            f"falhas={stats.failed_files}."
        )
        return 1 if stats.failed_files else 0


if __name__ == "__main__":
    raise SystemExit(main())
