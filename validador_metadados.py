"""Audita metadados de atos normativos armazenados em Markdown."""

from __future__ import annotations

import argparse
import csv
import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from metadados_documentos import (
    detect_act_type,
    extract_legal_metadata,
    first_markdown_heading,
    read_markdown_document,
)


@dataclass(slots=True)
class ValidationResult:
    """Resultado da validação de um documento Markdown."""

    path: Path
    act_type: str = ""
    act_number: str = ""
    act_year: str = ""
    act_date: str = ""
    publication_page: str = ""
    heading_type: str = ""
    issues: list[str] = field(default_factory=list)
    content_hash: str = ""

    @property
    def logical_key(self) -> tuple[str, str, str]:
        """Retorna a chave usada para detectar atos repetidos."""

        return (
            (self.act_type or self.heading_type).lower(),
            self.act_number,
            self.act_year,
        )


def content_sha1(text: str) -> str:
    """Calcula o SHA-1 do corpo para identificar cópias exatas."""

    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()


def validate_file(path: Path) -> ValidationResult:
    """Valida metadados, título e coerência de um único arquivo."""

    metadata, body = read_markdown_document(path)
    inferred = extract_legal_metadata(body)
    heading = first_markdown_heading(body)
    heading_type = detect_act_type(heading)

    act_type = str(metadata.get("act_type") or "").strip()
    act_number = str(metadata.get("act_number") or inferred.get("act_number") or "")
    act_year = str(metadata.get("act_year") or inferred.get("act_year") or "")
    act_date = str(metadata.get("act_date") or inferred.get("act_date") or "")
    publication = metadata.get("doe") or {}
    if not isinstance(publication, dict):
        publication = {}

    result = ValidationResult(
        path=path,
        act_type=act_type,
        act_number=act_number,
        act_year=act_year,
        act_date=act_date,
        publication_page=str(publication.get("pagina") or ""),
        heading_type=heading_type,
        content_hash=content_sha1(body),
    )

    if not metadata:
        result.issues.append("MISSING_FRONTMATTER")
    if not act_type:
        result.issues.append("MISSING_ACT_TYPE")
    if not act_number:
        result.issues.append("MISSING_ACT_NUMBER")
    if not act_year:
        result.issues.append("MISSING_ACT_YEAR")
    if not act_date:
        result.issues.append("MISSING_ACT_DATE")
    if act_type and heading_type and act_type != heading_type:
        result.issues.append(f"TYPE_MISMATCH({act_type}!={heading_type})")

    path_lower = str(path).lower()
    if "portarias" in path_lower and act_type and act_type != "Portaria":
        result.issues.append("SUBDIR_MISMATCH(portarias!=act_type)")
    if "resolucoes" in path_lower and act_type and act_type != "Resolução":
        result.issues.append("SUBDIR_MISMATCH(resolucoes!=act_type)")

    inferred_publication = inferred.get("doe")
    if inferred_publication and not publication:
        result.issues.append("PUBLICATION_TEXT_BUT_NO_METADATA")
    if publication and not inferred_publication:
        result.issues.append("PUBLICATION_METADATA_BUT_NO_TEXT")

    aliases = metadata.get("aliases")
    if isinstance(aliases, list) and act_type and act_number and not aliases:
        result.issues.append("ALIASES_EMPTY")
    return result


def validate_directory(root_directory: Path) -> list[ValidationResult]:
    """Valida os documentos e marca duplicatas lógicas ou de conteúdo."""

    results: list[ValidationResult] = []
    for markdown_path in sorted(root_directory.rglob("*.md")):
        try:
            results.append(validate_file(markdown_path))
        except (OSError, UnicodeError) as error:
            results.append(
                ValidationResult(
                    path=markdown_path,
                    issues=[f"READ_ERROR({error})"],
                )
            )

    results_by_key: dict[tuple[str, str, str], list[ValidationResult]] = defaultdict(list)
    results_by_hash: dict[str, list[ValidationResult]] = defaultdict(list)
    for result in results:
        results_by_key[result.logical_key].append(result)
        if result.content_hash:
            results_by_hash[result.content_hash].append(result)

    for logical_key, duplicates in results_by_key.items():
        if logical_key != ("", "", "") and len(duplicates) > 1:
            issue = f"DUPLICATE_KEY({logical_key[0]}-{logical_key[1]}/{logical_key[2]})"
            for result in duplicates:
                result.issues.append(issue)

    for duplicates in results_by_hash.values():
        if len(duplicates) > 1:
            for result in duplicates:
                result.issues.append("DUPLICATE_CONTENT")
    return results


def result_as_row(result: ValidationResult) -> list[str]:
    """Converte um resultado na ordem de colunas do relatório CSV."""

    return [
        str(result.path),
        result.act_type,
        result.act_number,
        result.act_year,
        result.act_date,
        result.publication_page,
        result.heading_type,
        ";".join(result.issues),
        "TRUE" if not result.issues else "FALSE",
    ]


def write_csv_report(path: Path, results: list[ValidationResult]) -> None:
    """Grava o relatório detalhado em CSV."""

    with path.open("w", encoding="utf-8", newline="") as report_file:
        writer = csv.writer(report_file)
        writer.writerow(
            [
                "path",
                "act_type",
                "act_number",
                "act_year",
                "act_date",
                "publication_page",
                "heading_type",
                "issues",
                "ok",
            ]
        )
        writer.writerows(result_as_row(result) for result in results)


def write_markdown_report(
    path: Path,
    root_directory: Path,
    results: list[ValidationResult],
) -> None:
    """Grava um resumo legível da auditoria em Markdown."""

    issue_counts = Counter(issue for result in results for issue in (result.issues or ["OK"]))
    lines = [
        "# Relatório de validação de metadados",
        "",
        f"Raiz analisada: `{root_directory}`",
        "",
        "## Resumo por problema",
        "",
    ]
    for issue, count in sorted(
        issue_counts.items(),
        key=lambda item: (-item[1], item[0]),
    ):
        lines.append(f"- **{issue}**: {count}")

    lines.extend(
        [
            "",
            "## Recomendações",
            "",
            "- Preencher tipo, número, ano e data no front matter.",
            "- Conferir a coerência entre o H1 e os metadados.",
            "- Registrar os dados de publicação quando constarem no texto.",
            "- Remover chaves lógicas ou conteúdos duplicados.",
            "",
            "## Itens problemáticos",
            "",
        ]
    )
    for result in results:
        if result.issues:
            lines.append(f"- `{result.path}` → {', '.join(result.issues)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_arguments() -> argparse.Namespace:
    """Lê os caminhos usados na validação e nos relatórios."""

    parser = argparse.ArgumentParser(
        description="Valida metadados de atos normativos em arquivos Markdown.",
    )
    parser.add_argument("--root", type=Path, default=Path("data/0_extracao_bruta"))
    parser.add_argument(
        "--save-csv",
        type=Path,
        default=Path("relatorio_validacao.csv"),
    )
    parser.add_argument(
        "--save-md",
        type=Path,
        default=Path("relatorio_validacao.md"),
    )
    return parser.parse_args()


def main() -> int:
    """Executa a validação e apresenta um resumo no console."""

    arguments = parse_arguments()
    if not arguments.root.exists():
        print(f"Diretório não encontrado: {arguments.root}")
        return 2

    results = validate_directory(arguments.root)
    if not results:
        print(f"Nenhum arquivo Markdown encontrado em {arguments.root}.")
        return 0

    write_csv_report(arguments.save_csv, results)
    write_markdown_report(arguments.save_md, arguments.root, results)
    valid_count = sum(not result.issues for result in results)
    print(
        f"Arquivos analisados: {len(results)} | "
        f"OK: {valid_count} | Com problemas: {len(results) - valid_count}"
    )
    print(f"CSV: {arguments.save_csv}")
    print(f"Markdown: {arguments.save_md}")
    return 1 if valid_count != len(results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
