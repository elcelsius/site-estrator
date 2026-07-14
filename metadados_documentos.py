"""Leitura, escrita e enriquecimento de metadados de atos normativos."""

from __future__ import annotations

import argparse
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

MONTHS_PT = {
    "janeiro": 1,
    "fevereiro": 2,
    "março": 3,
    "marco": 3,
    "abril": 4,
    "maio": 5,
    "junho": 6,
    "julho": 7,
    "agosto": 8,
    "setembro": 9,
    "outubro": 10,
    "novembro": 11,
    "dezembro": 12,
}


def parse_portuguese_date(text: str) -> str:
    """Converte datas comuns em português para o padrão ISO ``AAAA-MM-DD``."""

    long_date = re.search(
        r"(\d{1,2})\s+de\s+([A-Za-zçãáéíóúôê]+)\s+de\s+(\d{4})",
        text,
        flags=re.IGNORECASE,
    )
    if long_date:
        day = int(long_date.group(1))
        month = MONTHS_PT.get(long_date.group(2).lower(), 0)
        year = int(long_date.group(3))
    else:
        short_date = re.search(
            r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})",
            text,
        )
        if not short_date:
            return ""
        day = int(short_date.group(1))
        month = int(short_date.group(2))
        year = int(short_date.group(3))

    try:
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def first_markdown_heading(text: str) -> str:
    """Retorna o primeiro título H1 de um documento Markdown."""

    match = re.search(r"(?m)^\s*#\s+(.+?)\s*$", text)
    return match.group(1).strip() if match else ""


def detect_act_type(heading: str) -> str:
    """Identifica o tipo de ato normativo presente em um título."""

    patterns = {
        "Resolução": r"\bresolu[cç][aã]o\b",
        "Portaria": r"\bportaria\b",
        "Ato": r"\bato\b",
        "Deliberação": r"\bdelibera[cç][aã]o\b",
        "Instrução": r"\binstru[cç][aã]o\b",
        "Comunicado": r"\bcomunicado\b",
    }
    for act_type, pattern in patterns.items():
        if re.search(pattern, heading, re.IGNORECASE):
            return act_type
    return ""


def extract_legal_metadata(text: str) -> dict[str, Any]:
    """Infere metadados jurídicos simples sem depender de uma instituição."""

    heading = first_markdown_heading(text)
    if not heading:
        heading = next((line.strip() for line in text.splitlines() if line.strip()), "")

    metadata: dict[str, Any] = {}
    act_type = detect_act_type(heading)
    number_match = re.search(
        r"(?:n[ºo]\s*|no\.\s*|n\.\s*)(\d{1,5})(?:[/\-](\d{4}))?",
        heading,
        flags=re.IGNORECASE,
    )
    act_date = parse_portuguese_date(heading)

    if act_type:
        metadata["act_type"] = act_type
    if number_match:
        metadata["act_number"] = str(int(number_match.group(1)))
        if number_match.group(2):
            metadata["act_year"] = number_match.group(2)
    if act_date:
        metadata["act_date"] = act_date

    publication_match = re.search(
        r"Pub\.\s*DOE\s*n[ºo]?\s*([A-Za-z0-9\-]+)\s*,\s*"
        r"de\s*(\d{1,2}/\d{1,2}/\d{4})\s*,\s*p\.\s*(\d+)",
        text,
        flags=re.IGNORECASE,
    )
    if publication_match:
        metadata["doe"] = {
            "numero": publication_match.group(1),
            "data": publication_match.group(2),
            "pagina": publication_match.group(3),
        }

    if re.search(r"\brevogad[ao]s?\b", text, flags=re.IGNORECASE):
        metadata["status"] = "Revogada"
    elif re.search(r"\bvigent[ea]\b", text, flags=re.IGNORECASE):
        metadata["status"] = "Vigente"

    if act_type and metadata.get("act_number"):
        identifier = f"{act_type} {metadata['act_number']}"
        if metadata.get("act_year"):
            identifier = f"{identifier}/{metadata['act_year']}"
        metadata["aliases"] = [identifier]
    return metadata


def read_markdown_document(path: Path) -> tuple[dict[str, Any], str]:
    """Lê um documento e separa seu front matter YAML do corpo."""

    raw_text = path.read_text(encoding="utf-8", errors="ignore")
    if raw_text.startswith("---"):
        parts = raw_text.split("---", 2)
        if len(parts) == 3:
            try:
                metadata = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                metadata = {}
            if isinstance(metadata, dict):
                return metadata, parts[2].lstrip("\n")
    return {}, raw_text


def write_markdown_document(
    path: Path,
    metadata: dict[str, Any],
    body: str,
) -> None:
    """Grava um documento com front matter YAML em UTF-8."""

    frontmatter = yaml.safe_dump(
        metadata,
        sort_keys=False,
        allow_unicode=True,
    ).strip()
    path.write_text(
        f"---\n{frontmatter}\n---\n{body.strip()}\n",
        encoding="utf-8",
    )


def enrich_directory(
    input_directory: Path,
    *,
    source: str = "",
    institution: str = "",
    owner: str = "",
) -> int:
    """Enriquece os arquivos Markdown e retorna quantos foram alterados."""

    changed_files = 0
    for markdown_path in sorted(input_directory.rglob("*.md")):
        metadata, body = read_markdown_document(markdown_path)
        enriched = dict(metadata)
        inferred = extract_legal_metadata(body)
        for key, value in inferred.items():
            enriched.setdefault(key, value)
        if source:
            enriched.setdefault("source", source)
        if institution:
            enriched.setdefault("institution", institution)
        if owner:
            enriched.setdefault("owner", owner)

        if enriched != metadata:
            write_markdown_document(markdown_path, enriched, body)
            changed_files += 1
            print(f"Metadados enriquecidos: {markdown_path}")
    return changed_files


def parse_arguments() -> argparse.Namespace:
    """Lê os parâmetros do enriquecedor de metadados."""

    parser = argparse.ArgumentParser(
        description="Infere metadados de atos normativos em arquivos Markdown.",
    )
    parser.add_argument("--input-dir", type=Path, default=Path("data/0_extracao_bruta"))
    parser.add_argument("--source", default="", help="Nome opcional da fonte.")
    parser.add_argument("--institution", default="", help="Instituição opcional.")
    parser.add_argument("--owner", default="", help="Responsável opcional.")
    return parser.parse_args()


def main() -> int:
    """Executa o enriquecimento de metadados pela linha de comando."""

    arguments = parse_arguments()
    if not arguments.input_dir.exists():
        print(f"Diretório não encontrado: {arguments.input_dir}")
        return 2
    changed_files = enrich_directory(
        arguments.input_dir,
        source=arguments.source,
        institution=arguments.institution,
        owner=arguments.owner,
    )
    print(f"Enriquecimento concluído. Arquivos alterados: {changed_files}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
