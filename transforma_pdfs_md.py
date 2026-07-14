"""Converte arquivos PDF locais em documentos Markdown com metadados."""

from __future__ import annotations

import argparse
import hashlib
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pdfminer.high_level

from metadados_documentos import extract_legal_metadata, write_markdown_document

DEFAULT_INPUT_DIR = Path("pdf/originais")
DEFAULT_OUTPUT_DIR = Path("data/0_extracao_bruta")
DEFAULT_AUDIT_DIR = Path("pdf/convertidos")
DEFAULT_DONE_DIR = Path("pdf/finalizados")
DEFAULT_ERROR_DIR = Path("pdf/erro")


@dataclass(slots=True)
class ConversionSummary:
    """Contadores do processamento de um lote de PDFs."""

    total: int = 0
    converted: int = 0
    failed: int = 0


def short_sha1(text: str) -> str:
    """Gera um hash curto para diferenciar nomes de documentos."""

    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:10]


def build_markdown_body(extracted_text: str) -> str:
    """Promove a primeira linha não vazia a título do documento."""

    lines = extracted_text.splitlines()
    title_index = next(
        (index for index, line in enumerate(lines) if line.strip()),
        None,
    )
    if title_index is None:
        return ""
    title = lines[title_index].strip()
    remaining_text = "\n".join(lines[title_index + 1 :]).strip()
    return f"# {title}\n\n{remaining_text}".strip()


def convert_pdf(
    pdf_path: Path,
    output_directory: Path,
    audit_directory: Path,
) -> Path:
    """Converte um único PDF e devolve o caminho do Markdown gerado."""

    extracted_text = pdfminer.high_level.extract_text(str(pdf_path)) or ""
    body = build_markdown_body(extracted_text)
    if not body:
        raise ValueError("O PDF não contém texto extraível.")

    metadata = {
        "source_pdf": str(pdf_path),
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }
    metadata.update(extract_legal_metadata(body))
    output_path = output_directory / (f"{pdf_path.stem}__{short_sha1(body)}.md")
    write_markdown_document(output_path, metadata, body)
    shutil.copy2(output_path, audit_directory / output_path.name)
    return output_path


def convert_directory(
    input_directory: Path,
    output_directory: Path,
    audit_directory: Path,
    *,
    move_converted: bool = False,
    move_failed: bool = False,
    done_directory: Path = DEFAULT_DONE_DIR,
    error_directory: Path = DEFAULT_ERROR_DIR,
) -> ConversionSummary:
    """Converte todos os PDFs do diretório e organiza os arquivos de origem."""

    output_directory.mkdir(parents=True, exist_ok=True)
    audit_directory.mkdir(parents=True, exist_ok=True)
    summary = ConversionSummary()

    for pdf_path in sorted(input_directory.glob("*.pdf")):
        summary.total += 1
        try:
            output_path = convert_pdf(
                pdf_path,
                output_directory,
                audit_directory,
            )
            summary.converted += 1
            print(f"Convertido: {pdf_path.name} -> {output_path.name}")
            if move_converted:
                done_directory.mkdir(parents=True, exist_ok=True)
                shutil.move(str(pdf_path), done_directory / pdf_path.name)
        except Exception as error:
            summary.failed += 1
            print(f"Falha ao converter {pdf_path}: {error}")
            if move_failed:
                error_directory.mkdir(parents=True, exist_ok=True)
                shutil.move(str(pdf_path), error_directory / pdf_path.name)
    return summary


def parse_arguments() -> argparse.Namespace:
    """Lê as opções da conversão de PDFs."""

    parser = argparse.ArgumentParser(
        description="Converte PDFs locais em documentos Markdown.",
    )
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output-md", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--audit-md", type=Path, default=DEFAULT_AUDIT_DIR)
    parser.add_argument("--move-ok", action="store_true")
    parser.add_argument("--move-err", action="store_true")
    return parser.parse_args()


def main() -> int:
    """Executa a conversão em lote pela linha de comando."""

    arguments = parse_arguments()
    if not arguments.input_dir.exists():
        print(f"Diretório não encontrado: {arguments.input_dir}")
        return 2
    summary = convert_directory(
        arguments.input_dir,
        arguments.output_md,
        arguments.audit_md,
        move_converted=arguments.move_ok,
        move_failed=arguments.move_err,
    )
    print(
        "Conversão concluída: "
        f"{summary.total} PDF(s), {summary.converted} convertido(s), "
        f"{summary.failed} falha(s)."
    )
    return 1 if summary.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
