"""Reconstrói o manifesto JSON Lines a partir dos documentos refinados."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

DEFAULT_OUTPUT_DIR = Path("data/1_markdowns_limpos")
DEFAULT_RAW_DIR = Path("data/0_extracao_bruta")
DEFAULT_MANIFEST = DEFAULT_OUTPUT_DIR / "manifest.jsonl"


def split_frontmatter(markdown_text: str) -> tuple[dict[str, Any], str]:
    """Separa o front matter YAML do corpo de um documento."""

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


def normalize_text(text: str) -> str:
    """Normaliza texto antes do cálculo de similaridade."""

    return re.sub(r"\s+", " ", text, flags=re.MULTILINE).strip().lower()[:200_000]


def calculate_simhash64(text: str) -> int:
    """Calcula um simhash de 64 bits usando frequência de termos."""

    tokens = re.findall(r"[0-9A-Za-zÀ-ÖØ-öø-ÿ_]+", text, re.UNICODE)
    frequencies = Counter(tokens)
    vector = [0] * 64
    for token, frequency in frequencies.most_common(20_000):
        weight = min(frequency, 255)
        token_hash = int(
            hashlib.sha1(token.encode("utf-8")).hexdigest()[:16],
            16,
        )
        for bit in range(64):
            # O peso do termo desloca o vetor conforme cada bit do seu hash.
            vector[bit] += weight if (token_hash >> bit) & 1 else -weight

    result = 0
    for bit, value in enumerate(vector):
        if value >= 0:
            result |= 1 << bit
    return result


def find_original(stem: str, raw_directory: Path) -> str:
    """Localiza o documento bruto correspondente ao arquivo refinado."""

    for candidate in raw_directory.rglob("*.md"):
        if candidate.stem == stem:
            return str(candidate)
    return ""


def rebuild_manifest(
    output_directory: Path,
    raw_directory: Path,
    manifest_path: Path,
) -> int:
    """Recria o manifesto e retorna a quantidade de registros gravados."""

    refined_files = sorted(output_directory.glob("*__REF.md"))
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    written_records = 0

    with manifest_path.open("w", encoding="utf-8") as manifest_file:
        for markdown_path in refined_files:
            try:
                text = markdown_path.read_text(encoding="utf-8")
                metadata, body = split_frontmatter(text)
                source_stem = markdown_path.stem.removesuffix("__REF")
                original = metadata.get("source_in") or find_original(
                    source_stem,
                    raw_directory,
                )
                refined_by = str(metadata.get("refined_by") or "").lower().strip()
                provider = refined_by if refined_by in {"gemini", "ollama"} else ""
                if provider == "gemini":
                    provider = "google"

                record = {
                    "ts": metadata.get("refined_at") or "",
                    "in": original,
                    "out": str(markdown_path),
                    "simhash64_hex": hex(calculate_simhash64(normalize_text(body))),
                    "llm_applied": refined_by in {"gemini", "ollama"},
                    "provider": provider,
                    "model": metadata.get("refined_model") or "",
                }
                manifest_file.write(json.dumps(record, ensure_ascii=False) + "\n")
                written_records += 1
            except (OSError, UnicodeError, yaml.YAMLError) as error:
                print(f"Falha ao ler {markdown_path}: {error}")
    return written_records


def parse_arguments() -> argparse.Namespace:
    """Lê os caminhos usados na reconstrução do manifesto."""

    parser = argparse.ArgumentParser(
        description="Reconstrói o manifesto a partir dos arquivos *__REF.md.",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--out", type=Path, default=DEFAULT_MANIFEST)
    return parser.parse_args()


def main() -> int:
    """Executa a reconstrução do manifesto pela linha de comando."""

    arguments = parse_arguments()
    item_count = rebuild_manifest(
        arguments.output_dir,
        arguments.raw_dir,
        arguments.out,
    )
    print(f"Manifesto reconstruído: {arguments.out} | itens: {item_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
