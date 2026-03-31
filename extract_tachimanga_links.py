#!/usr/bin/env python3

from __future__ import annotations

import argparse
import collections
import io
import sqlite3
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse


DEFAULT_SOURCE_NAMES = ("E-Hentai", "Hitomi")
DEFAULT_ALLOWED_DOMAINS = ("e-hentai.org", "exhentai.org", "hitomi.la")
PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIRNAME = "output"


@dataclass(frozen=True)
class MangaRow:
    category_name: str
    category_order: int
    title: str
    source_id: int
    source_name: str
    url: str
    real_url: str
    base_url: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract category-grouped links for selected sources from a "
            "TachiManga .tmb backup."
        )
    )
    parser.add_argument("backup", type=Path, help="Path to a TachiManga .tmb backup")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Path to the output .txt file",
    )
    parser.add_argument(
        "--source-name",
        action="append",
        dest="source_names",
        help=(
            "Source name to keep. Repeat the flag for multiple names. "
            "Defaults to E-Hentai and Hitomi."
        ),
    )
    parser.add_argument(
        "--allowed-domain",
        action="append",
        dest="allowed_domains",
        help=(
            "Allowed domain for exported URLs. Repeat for multiple values. "
            "Defaults to e-hentai.org, exhentai.org and hitomi.la."
        ),
    )
    return parser.parse_args()


def pick_output_path(backup_path: Path, explicit_output: Path | None) -> Path:
    default_name = f"{backup_path.stem}_ehentai_hitomi_by_category.txt"
    if explicit_output is not None:
        if explicit_output.exists() and explicit_output.is_dir():
            return explicit_output / default_name
        return explicit_output
    return PROJECT_ROOT / DEFAULT_OUTPUT_DIRNAME / default_name


def pick_database_output_path(backup_path: Path, output_path: Path) -> Path:
    return output_path.parent / f"{backup_path.stem}_tachimanga.db"


def load_nested_database_bytes(backup_path: Path) -> bytes:
    with zipfile.ZipFile(backup_path) as outer_zip:
        try:
            contents_bytes = outer_zip.read("contents.zip")
        except KeyError as exc:
            raise RuntimeError("Backup does not contain contents.zip") from exc

    with zipfile.ZipFile(io.BytesIO(contents_bytes)) as inner_zip:
        try:
            return inner_zip.read("tachimanga.db")
        except KeyError as exc:
            raise RuntimeError("contents.zip does not contain tachimanga.db") from exc


def connect_to_temp_db(db_bytes: bytes) -> tuple[tempfile.NamedTemporaryFile, sqlite3.Connection]:
    temp_file = tempfile.NamedTemporaryFile(suffix=".db")
    temp_file.write(db_bytes)
    temp_file.flush()
    connection = sqlite3.connect(temp_file.name)
    connection.row_factory = sqlite3.Row
    return temp_file, connection


def infer_url_prefixes(connection: sqlite3.Connection) -> dict[int, str]:
    prefix_counts: dict[int, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    rows = connection.execute(
        """
        SELECT source, url, real_url
        FROM Manga
        WHERE TRIM(COALESCE(url, '')) <> ''
          AND TRIM(COALESCE(real_url, '')) <> ''
        """
    )
    for row in rows:
        source_id = int(row["source"])
        url = row["url"].strip()
        real_url = row["real_url"].strip()
        if not url or not real_url:
            continue
        if real_url.endswith(url):
            prefix = real_url[: -len(url)]
            if prefix:
                prefix_counts[source_id][prefix] += 1

    inferred: dict[int, str] = {}
    for source_id, counts in prefix_counts.items():
        inferred[source_id] = counts.most_common(1)[0][0]
    return inferred


def load_rows(connection: sqlite3.Connection, source_names: Iterable[str]) -> list[MangaRow]:
    placeholders = ", ".join("?" for _ in source_names)
    query = f"""
        SELECT
            c.name AS category_name,
            c."order" AS category_order,
            m.title AS title,
            s.id AS source_id,
            s.name AS source_name,
            m.url AS url,
            m.real_url AS real_url,
            e.base_url AS base_url
        FROM Category AS c
        JOIN CategoryManga AS cm ON cm.category = c.id
        JOIN Manga AS m ON m.id = cm.manga
        JOIN Source AS s ON s.id = m.source
        JOIN Extension AS e ON e.id = s.extension
        WHERE m.in_library = 1
          AND s.name IN ({placeholders})
        ORDER BY c."order", c.id, m.title COLLATE NOCASE, m.id
    """
    return [
        MangaRow(
            category_name=row["category_name"],
            category_order=int(row["category_order"]),
            title=row["title"] or "",
            source_id=int(row["source_id"]),
            source_name=row["source_name"] or "",
            url=(row["url"] or "").strip(),
            real_url=(row["real_url"] or "").strip(),
            base_url=(row["base_url"] or "").strip(),
        )
        for row in connection.execute(query, tuple(source_names))
    ]


def is_absolute_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def normalized_host(value: str) -> str:
    return urlparse(value).netloc.lower()


def join_prefix_and_path(prefix: str, path: str) -> str:
    if prefix.endswith("/") and path.startswith("/"):
        return prefix[:-1] + path
    if not prefix.endswith("/") and not path.startswith("/"):
        return prefix + "/" + path
    return prefix + path


def resolve_manga_url(row: MangaRow, inferred_prefixes: dict[int, str]) -> str | None:
    if row.real_url:
        return row.real_url
    if is_absolute_http_url(row.url):
        return row.url
    if row.url:
        inferred_prefix = inferred_prefixes.get(row.source_id)
        if inferred_prefix:
            return join_prefix_and_path(inferred_prefix, row.url)
        if row.base_url:
            return join_prefix_and_path(row.base_url, row.url)
    return None


def build_export(
    rows: list[MangaRow],
    inferred_prefixes: dict[int, str],
    allowed_domains: set[str],
) -> tuple[dict[str, list[str]], list[str]]:
    grouped: dict[str, list[str]] = collections.OrderedDict()
    seen_per_category: dict[str, set[str]] = collections.defaultdict(set)
    skipped: list[str] = []

    for row in rows:
        resolved = resolve_manga_url(row, inferred_prefixes)
        if not resolved:
            skipped.append(
                f"{row.category_name}\t{row.source_name}\t{row.title}\tmissing-url\t{row.url}"
            )
            continue

        host = normalized_host(resolved)
        if host not in allowed_domains:
            skipped.append(
                f"{row.category_name}\t{row.source_name}\t{row.title}\tfiltered-domain\t{resolved}"
            )
            continue

        if resolved in seen_per_category[row.category_name]:
            continue

        seen_per_category[row.category_name].add(resolved)
        grouped.setdefault(row.category_name, []).append(resolved)

    return grouped, skipped


def render_output(
    backup_path: Path,
    grouped: dict[str, list[str]],
    source_names: Iterable[str],
    allowed_domains: Iterable[str],
) -> str:
    total_links = sum(len(urls) for urls in grouped.values())
    lines = [
        f"# Backup: {backup_path.name}",
        f"# Sources: {', '.join(source_names)}",
        f"# Domains: {', '.join(allowed_domains)}",
        f"# Categories: {len(grouped)}",
        f"# Total links: {total_links}",
        "",
    ]
    for category_name, urls in grouped.items():
        lines.append(f"## {category_name} ({len(urls)})")
        lines.extend(urls)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    args = parse_args()
    backup_path = args.backup.resolve()
    output_path = pick_output_path(backup_path, args.output.resolve() if args.output else None)
    database_output_path = pick_database_output_path(backup_path, output_path)

    if not backup_path.is_file():
        print(f"Backup file not found: {backup_path}", file=sys.stderr)
        return 1

    source_names = tuple(args.source_names or DEFAULT_SOURCE_NAMES)
    allowed_domains = tuple(domain.lower() for domain in (args.allowed_domains or DEFAULT_ALLOWED_DOMAINS))

    db_bytes = load_nested_database_bytes(backup_path)
    temp_db, connection = connect_to_temp_db(db_bytes)
    try:
        inferred_prefixes = infer_url_prefixes(connection)
        rows = load_rows(connection, source_names)
        grouped, skipped = build_export(rows, inferred_prefixes, set(allowed_domains))
    finally:
        connection.close()
        temp_db.close()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    database_output_path.write_bytes(db_bytes)
    output_path.write_text(
        render_output(backup_path, grouped, source_names, allowed_domains),
        encoding="utf-8",
    )

    skipped_path = output_path.with_suffix(output_path.suffix + ".skipped.tsv")
    skipped_path.write_text(
        "category\tsource\ttitle\treason\tvalue\n" + "\n".join(skipped) + ("\n" if skipped else ""),
        encoding="utf-8",
    )

    total_links = sum(len(urls) for urls in grouped.values())
    print(f"Wrote {total_links} links across {len(grouped)} categories to {output_path}")
    print(f"Saved extracted database to {database_output_path}")
    print(f"Skipped {len(skipped)} rows; details saved to {skipped_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
