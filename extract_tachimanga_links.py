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
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_IMPORT_DIRNAME = "import"
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
    parser.add_argument(
        "backup",
        nargs="?",
        type=Path,
        help=(
            "Path to a TachiManga .tmb backup. "
            "If omitted, auto-picks the single .tmb file from import/."
        ),
    )
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
            "By default all sources in the database are used."
        ),
    )
    parser.add_argument(
        "--exclude-source",
        action="append",
        dest="exclude_sources",
        help=(
            "Source name to exclude. Repeat the flag for multiple names. "
            "Applied after --source-name filtering."
        ),
    )
    parser.add_argument(
        "--allowed-domain",
        action="append",
        dest="allowed_domains",
        help=(
            "Allowed domain for exported URLs. Repeat for multiple values. "
            "By default no domain filtering is applied."
        ),
    )
    parser.add_argument(
        "--list-sources",
        action="store_true",
        help="List available sources in the backup and exit.",
    )
    return parser.parse_args()


def find_single_backup() -> Path | None:
    import_dir = PROJECT_ROOT / DEFAULT_IMPORT_DIRNAME
    if not import_dir.is_dir():
        return None
    tmb_files = sorted(import_dir.glob("*.tmb"))
    if len(tmb_files) == 1:
        return tmb_files[0]
    return None


def pick_output_path(backup_path: Path, explicit_output: Path | None) -> Path:
    default_name = f"{backup_path.stem}_all_sources_by_category.txt"
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


def list_sources(connection: sqlite3.Connection) -> list[tuple[str, str, int]]:
    rows = connection.execute(
        """
        SELECT s.name, s.lang, COUNT(*) AS cnt
        FROM Manga AS m
        JOIN Source AS s ON s.id = m.source
        WHERE m.in_library = 1
        GROUP BY s.id, s.name, s.lang
        ORDER BY cnt DESC, s.name
        """
    )
    return [(row["name"], row["lang"], int(row["cnt"])) for row in rows]


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


def load_rows(
    connection: sqlite3.Connection,
    source_names: tuple[str, ...] | None = None,
    exclude_sources: tuple[str, ...] | None = None,
) -> list[MangaRow]:
    query = """
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
    """
    params: list[str] = []

    if source_names:
        placeholders = ", ".join("?" for _ in source_names)
        query += f"  AND s.name IN ({placeholders})\n"
        params.extend(source_names)

    if exclude_sources:
        placeholders = ", ".join("?" for _ in exclude_sources)
        query += f"  AND s.name NOT IN ({placeholders})\n"
        params.extend(exclude_sources)

    query += '        ORDER BY c."order", c.id, m.title COLLATE NOCASE, m.id'

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
        for row in connection.execute(query, params)
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
) -> tuple[dict[str, list[tuple[str, str]]], list[str], collections.Counter[str]]:
    grouped: dict[str, list[tuple[str, str]]] = collections.OrderedDict()
    seen_per_category: dict[str, set[str]] = collections.defaultdict(set)
    skipped: list[str] = []
    source_counts: collections.Counter[str] = collections.Counter()

    for row in rows:
        resolved = resolve_manga_url(row, inferred_prefixes)
        if not resolved:
            skipped.append(
                f"{row.category_name}\t{row.source_name}\t{row.title}\tmissing-url\t{row.url}"
            )
            continue

        if allowed_domains:
            host = normalized_host(resolved)
            if host not in allowed_domains:
                skipped.append(
                    f"{row.category_name}\t{row.source_name}\t{row.title}\tfiltered-domain\t{resolved}"
                )
                continue

        if resolved in seen_per_category[row.category_name]:
            continue

        seen_per_category[row.category_name].add(resolved)
        grouped.setdefault(row.category_name, []).append((resolved, row.title))
        source_counts[row.source_name] += 1

    return grouped, skipped, source_counts


def render_output(
    backup_path: Path,
    grouped: dict[str, list[tuple[str, str]]],
    source_names: tuple[str, ...] | None,
    allowed_domains: tuple[str, ...] | None,
    source_counts: collections.Counter[str],
) -> str:
    total_links = sum(len(entries) for entries in grouped.values())

    if source_counts:
        source_stats = ", ".join(
            f"{name} ({count})" for name, count in source_counts.most_common()
        )
    else:
        source_stats = "none"

    lines = [
        f"# Backup: {backup_path.name}",
        f"# Sources: {', '.join(source_names) if source_names else 'all'}",
        f"# Domains: {', '.join(allowed_domains) if allowed_domains else 'all'}",
        f"# Categories: {len(grouped)}",
        f"# Total links: {total_links}",
        f"# By source: {source_stats}",
        "",
    ]
    for category_name, entries in grouped.items():
        lines.append(f"## {category_name} ({len(entries)})")
        for url, title in entries:
            lines.append(f"{url}  # {title}" if title else url)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    args = parse_args()

    backup_path: Path | None = args.backup
    if backup_path is None:
        backup_path = find_single_backup()
        if backup_path is None:
            import_dir = PROJECT_ROOT / DEFAULT_IMPORT_DIRNAME
            tmb_files = sorted(import_dir.glob("*.tmb")) if import_dir.is_dir() else []
            if not tmb_files:
                print(
                    f"No .tmb files found in {import_dir}. "
                    "Provide a path as an argument.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"Multiple .tmb files in {import_dir}:\n"
                    + "\n".join(f"  {f.name}" for f in tmb_files)
                    + "\nSpecify which one to use.",
                    file=sys.stderr,
                )
            return 1
        print(f"Auto-picked: {backup_path.name}")

    backup_path = backup_path.resolve()
    output_path = pick_output_path(backup_path, args.output.resolve() if args.output else None)
    database_output_path = pick_database_output_path(backup_path, output_path)

    if not backup_path.is_file():
        print(f"Backup file not found: {backup_path}", file=sys.stderr)
        return 1

    source_names = tuple(args.source_names) if args.source_names else None
    exclude_sources = tuple(args.exclude_sources) if args.exclude_sources else None
    allowed_domains = (
        tuple(domain.lower() for domain in args.allowed_domains)
        if args.allowed_domains
        else None
    )

    db_bytes = load_nested_database_bytes(backup_path)
    temp_db, connection = connect_to_temp_db(db_bytes)
    try:
        if args.list_sources:
            sources = list_sources(connection)
            if not sources:
                print("No sources with manga in library.")
            else:
                max_name = max(len(name) for name, _, _ in sources)
                max_lang = max(len(lang) for _, lang, _ in sources)
                for name, lang, count in sources:
                    print(f"  {name:<{max_name}}  ({lang:<{max_lang}})  — {count} in library")
            return 0

        inferred_prefixes = infer_url_prefixes(connection)
        rows = load_rows(connection, source_names, exclude_sources)
        grouped, skipped, source_counts = build_export(
            rows, inferred_prefixes, set(allowed_domains) if allowed_domains else set()
        )
    finally:
        connection.close()
        temp_db.close()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    database_output_path.write_bytes(db_bytes)
    output_path.write_text(
        render_output(backup_path, grouped, source_names, allowed_domains, source_counts),
        encoding="utf-8",
    )

    skipped_path = output_path.with_suffix(output_path.suffix + ".skipped.tsv")
    skipped_path.write_text(
        "category\tsource\ttitle\treason\tvalue\n" + "\n".join(skipped) + ("\n" if skipped else ""),
        encoding="utf-8",
    )

    total_links = sum(len(entries) for entries in grouped.values())
    print(f"Wrote {total_links} links across {len(grouped)} categories to {output_path}")
    print(f"Saved extracted database to {database_output_path}")
    print(f"Skipped {len(skipped)} rows; details saved to {skipped_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
