#!/usr/bin/env python3
"""
Categorize manga by artist/author in a Tachimanga backup.

Reads a .tmb backup, creates categories for each unique artist,
assigns manga to their artist's category, and produces a new .tmb
file ready to restore in Tachimanga.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import io
import json
import re
import sqlite3
import sys
import tempfile
import time
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_IMPORT_DIRNAME = "import"
DEFAULT_OUTPUT_DIRNAME = "output"

# Reading-status categories — manga in these is excluded from artist
# categorization regardless of order or id.
SYSTEM_CATEGORIES = {"Запланировано", "Прочитано", "Брошенно", "Читаю", "Отложено"}

# Holding-bin categories — manga is removed from these after being
# assigned to a proper artist category.
HOLDING_CATEGORIES = {"ZDefault", "ZSort", "ZArtists"}

# The Category.name column is VARCHAR(64) in the Tachimanga schema.
MAX_CATEGORY_NAME_LENGTH = 64


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Categorize manga by artist in a TachiManga .tmb backup. "
            "Creates per-artist categories and produces a new .tmb file."
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
        help="Path to the output .tmb file (default: output/<name>_by_artist.tmb)",
    )
    parser.add_argument(
        "--field",
        choices=["artist", "author", "both"],
        default="artist",
        help="Which field to use for categorization (default: artist)",
    )
    parser.add_argument(
        "--min-count",
        type=int,
        default=1,
        help=(
            "Minimum number of manga for an artist to get their own category "
            "(default: 1)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without modifying anything.",
    )
    parser.add_argument(
        "--clean-placeholder",
        action="store_true",
        help="Remove empty Artist1..N placeholder categories.",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Backup I/O
# ---------------------------------------------------------------------------

def find_single_backup() -> Path | None:
    import_dir = PROJECT_ROOT / DEFAULT_IMPORT_DIRNAME
    if not import_dir.is_dir():
        return None
    tmb_files = sorted(import_dir.glob("*.tmb"))
    if len(tmb_files) == 1:
        return tmb_files[0]
    return None


def pick_output_path(backup_path: Path, explicit_output: Path | None) -> Path:
    default_name = f"{backup_path.stem}_by_artist.tmb"
    if explicit_output is not None:
        if explicit_output.exists() and explicit_output.is_dir():
            return explicit_output / default_name
        return explicit_output
    return PROJECT_ROOT / DEFAULT_OUTPUT_DIRNAME / default_name


def load_backup_parts(backup_path: Path) -> tuple[dict, bytes]:
    """Return (meta_dict, contents_zip_bytes) from a .tmb backup."""
    with zipfile.ZipFile(backup_path) as outer:
        meta = json.loads(outer.read("meta.json"))
        contents_bytes = outer.read("contents.zip")
    return meta, contents_bytes


def extract_db_bytes(contents_bytes: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(contents_bytes)) as inner:
        return inner.read("tachimanga.db")


def open_temp_db(db_bytes: bytes) -> tuple[tempfile.NamedTemporaryFile, sqlite3.Connection]:
    tf = tempfile.NamedTemporaryFile(suffix=".db")
    tf.write(db_bytes)
    tf.flush()
    conn = sqlite3.connect(tf.name)
    conn.row_factory = sqlite3.Row
    return tf, conn


def read_modified_db(tf: tempfile.NamedTemporaryFile) -> bytes:
    """Read the database file bytes after modifications."""
    tf.seek(0)
    return tf.read()


def repack_backup(
    meta: dict,
    original_contents_bytes: bytes,
    modified_db_bytes: bytes,
    output_path: Path,
) -> None:
    """Build a new .tmb with the modified database replacing tachimanga.db."""
    # Rebuild contents.zip in memory.
    contents_buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original_contents_bytes)) as src, \
         zipfile.ZipFile(contents_buf, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            if item.filename == "tachimanga.db":
                dst.writestr(item, modified_db_bytes)
            else:
                dst.writestr(item, src.read(item.filename))
    new_contents = contents_buf.getvalue()

    # Update meta.
    new_meta = dict(meta)
    new_meta["checksum"] = hashlib.sha1(new_contents).hexdigest()
    new_meta["size"] = len(new_contents)
    new_meta["name"] = output_path.name

    # Write outer .tmb zip.
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as outer:
        outer.writestr("meta.json", json.dumps(new_meta, indent=2, ensure_ascii=False))
        outer.writestr("contents.zip", new_contents)

    print(f"Wrote {output_path}  ({output_path.stat().st_size:,} bytes)")


# ---------------------------------------------------------------------------
# Artist helpers
# ---------------------------------------------------------------------------

def split_aliases(raw: str) -> list[str]:
    """Split an artist value like 'foo | bar' into individual aliases."""
    parts = [p.strip() for p in raw.split("|")]
    return [p for p in parts if p]


def normalize(name: str) -> str:
    return name.strip().lower()


def normalize_compact(name: str) -> str:
    """Normalize with spaces removed — for fuzzy matching (e.g. RocketMonkey ↔ rocket monkey)."""
    return normalize(name).replace(" ", "").replace("-", "").replace("_", "")


def title_case_artist(name: str) -> str:
    """Convert a lowercase artist alias to Title Case for category name."""
    return " ".join(word.capitalize() for word in name.split())


# Regex patterns for artist extraction from titles:
#   [Artist] ...
#   [Circle (Artist)] ...
#   (Event) [Artist] ...
#   (Event) [Circle (Artist)] ...
_BRACKET_RE = re.compile(r"^\[([^\]]+)\]")
_EVENT_BRACKET_RE = re.compile(r"^\([^)]+\)\s*\[([^\]]+)\]")
_PAREN_ARTIST_RE = re.compile(r".+\(([^)]+)\)$")


def extract_artist_from_title(title: str) -> str | None:
    """
    Try to extract artist name from a title like:
      [Artist] Some Title
      [Circle (Artist)] Some Title
      (Event) [Circle (Artist)] Some Title
    Returns the artist string or None.
    """
    m = _BRACKET_RE.match(title) or _EVENT_BRACKET_RE.match(title)
    if not m:
        return None
    bracket = m.group(1).strip()
    # [Circle (Artist)] → Artist
    m2 = _PAREN_ARTIST_RE.match(bracket)
    if m2:
        return m2.group(1).strip()
    # [Artist] → Artist
    return bracket


# ---------------------------------------------------------------------------
# Database queries
# ---------------------------------------------------------------------------

def get_existing_categories(conn: sqlite3.Connection) -> dict[int, dict]:
    """Return {id: {name, order, ...}} for all non-deleted categories."""
    rows = conn.execute(
        'SELECT id, name, "order", is_default FROM Category WHERE is_delete = 0'
    )
    return {
        int(r["id"]): {
            "name": r["name"],
            "order": int(r["order"]),
            "is_default": bool(r["is_default"]),
        }
        for r in rows
    }


def get_system_manga_ids(conn: sqlite3.Connection) -> set[int]:
    """Return manga IDs that belong to system categories (by name)."""
    placeholders = ", ".join("?" for _ in SYSTEM_CATEGORIES)
    rows = conn.execute(
        f"""
        SELECT DISTINCT cm.manga
        FROM CategoryManga cm
        JOIN Category c ON c.id = cm.category
        WHERE c.name IN ({placeholders}) AND c.is_delete = 0
        """,
        tuple(SYSTEM_CATEGORIES),
    )
    return {int(r["manga"]) for r in rows}


def get_artist_manga_mapping(
    conn: sqlite3.Connection,
    field: str,
    exclude_ids: set[int] | None = None,
) -> dict[str, list[int]]:
    """
    Return {canonical_artist: [manga_id, ...]} for manga in library.

    Values that differ only by case are merged under one canonical key
    (the first-seen casing is kept).  *field* is 'artist', 'author', or 'both'.
    Manga IDs in *exclude_ids* are skipped.
    """
    if exclude_ids is None:
        exclude_ids = set()

    # norm_key → (canonical_raw, [manga_ids])
    seen: dict[str, tuple[str, list[int]]] = {}

    def _add(raw: str, manga_id: int) -> None:
        if manga_id in exclude_ids:
            return
        key = normalize(raw)
        if key in seen:
            ids = seen[key][1]
            if manga_id not in ids:
                ids.append(manga_id)
        else:
            seen[key] = (raw, [manga_id])

    # Track manga IDs that already got an artist from a DB field,
    # so the title fallback doesn't override them.
    has_artist: set[int] = set()

    if field in ("artist", "both"):
        for row in conn.execute(
            "SELECT id, artist FROM Manga "
            "WHERE in_library = 1 AND TRIM(COALESCE(artist, '')) != ''"
        ):
            mid = int(row["id"])
            _add(row["artist"].strip(), mid)
            has_artist.add(mid)

    if field in ("author", "both"):
        for row in conn.execute(
            "SELECT id, author FROM Manga "
            "WHERE in_library = 1 AND TRIM(COALESCE(author, '')) != ''"
        ):
            val = row["author"].strip()
            # Skip publisher-style entries ("Издатель: ...")
            if val.startswith("Издатель:"):
                continue
            mid = int(row["id"])
            _add(val, mid)
            has_artist.add(mid)

    # Fallback: extract artist from title "[Artist] ..." or "[Circle (Artist)] ..."
    for row in conn.execute(
        "SELECT id, title FROM Manga "
        "WHERE in_library = 1 AND TRIM(COALESCE(artist, '')) = '' "
        "  AND TRIM(COALESCE(author, '')) = '' "
        "  AND title LIKE '[%'"
    ):
        mid = int(row["id"])
        if mid in has_artist:
            continue
        parsed = extract_artist_from_title(row["title"])
        if parsed:
            _add(parsed, mid)

    return {canonical: ids for canonical, ids in seen.values()}


def get_existing_assignments(conn: sqlite3.Connection) -> set[tuple[int, int]]:
    """Return set of (category_id, manga_id) for all existing assignments."""
    rows = conn.execute("SELECT category, manga FROM CategoryManga")
    return {(int(r["category"]), int(r["manga"])) for r in rows}


def build_category_lookup(
    categories: dict[int, dict],
) -> tuple[dict[str, int], dict[str, int]]:
    """Build (normalized → cat_id, compact → cat_id) lookups."""
    exact: dict[str, int] = {}
    compact: dict[str, int] = {}
    for cid, info in categories.items():
        exact[normalize(info["name"])] = cid
        compact[normalize_compact(info["name"])] = cid
    return exact, compact


def match_artist_to_category(
    artist_raw: str,
    cat_exact: dict[str, int],
    cat_compact: dict[str, int],
) -> int | None:
    """Try to match an artist value to an existing category. Return category id or None."""
    aliases = split_aliases(artist_raw)
    # First pass: exact normalized match.
    for alias in aliases:
        norm = normalize(alias)
        if norm in cat_exact:
            return cat_exact[norm]
    # Second pass: compact (space/hyphen-insensitive) match.
    for alias in aliases:
        comp = normalize_compact(alias)
        if comp in cat_compact:
            return cat_compact[comp]
    return None


def pick_category_name(artist_raw: str) -> str:
    """Choose a display name for a new artist category."""
    aliases = split_aliases(artist_raw)
    first = aliases[0]
    # If already has mixed case (not all-lower), keep it.
    if first != first.lower():
        return first
    return title_case_artist(first)


# ---------------------------------------------------------------------------
# Database mutations
# ---------------------------------------------------------------------------

def create_category(
    conn: sqlite3.Connection,
    name: str,
    order: int,
) -> int:
    now_ms = int(time.time() * 1000)
    cur = conn.execute(
        """
        INSERT INTO Category (name, "order", is_default, uuid, create_at, update_at,
                              is_delete, dirty, commit_id)
        VALUES (?, ?, 0, ?, ?, ?, 0, 0, 0)
        """,
        (name, order, name, now_ms, now_ms),
    )
    return cur.lastrowid  # type: ignore[return-value]


def insert_category_manga(
    conn: sqlite3.Connection,
    category_id: int,
    manga_id: int,
) -> None:
    conn.execute(
        "INSERT INTO CategoryManga (category, manga) VALUES (?, ?)",
        (category_id, manga_id),
    )


def delete_empty_placeholder_categories(conn: sqlite3.Connection) -> list[str]:
    """Delete Artist1..N categories that have zero manga assigned."""
    removed: list[str] = []
    rows = conn.execute(
        """
        SELECT c.id, c.name FROM Category c
        WHERE c.name GLOB 'Artist[0-9]*'
          AND c.is_delete = 0
          AND c.id NOT IN (SELECT category FROM CategoryManga)
        """
    ).fetchall()
    for row in rows:
        conn.execute("DELETE FROM Category WHERE id = ?", (row["id"],))
        removed.append(row["name"])
    return removed


def get_holding_category_ids(conn: sqlite3.Connection) -> dict[int, str]:
    """Return {category_id: name} for holding-bin categories."""
    placeholders = ", ".join("?" for _ in HOLDING_CATEGORIES)
    rows = conn.execute(
        f"SELECT id, name FROM Category WHERE name IN ({placeholders}) AND is_delete = 0",
        tuple(HOLDING_CATEGORIES),
    )
    return {int(r["id"]): r["name"] for r in rows}


def clean_holding_categories(
    conn: sqlite3.Connection,
    categorized_manga_ids: set[int],
) -> tuple[int, list[str]]:
    """
    Remove categorized manga from holding-bin categories (ZDefault, ZSort, ZArtists).
    Delete the holding category itself if it becomes empty.
    Return (removed_assignments, deleted_category_names).
    """
    holding = get_holding_category_ids(conn)
    if not holding:
        return 0, []

    removed = 0
    for hid in holding:
        cur = conn.execute(
            "DELETE FROM CategoryManga WHERE category = ? AND manga IN ({})".format(
                ", ".join("?" for _ in categorized_manga_ids)
            ),
            (hid, *categorized_manga_ids),
        )
        removed += cur.rowcount

    # Also delete Artist1..N placeholder assignments for categorized manga.
    placeholder_ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM Category WHERE name GLOB 'Artist[0-9]*' AND is_delete = 0"
        )
    ]
    for pid in placeholder_ids:
        cur = conn.execute(
            "DELETE FROM CategoryManga WHERE category = ? AND manga IN ({})".format(
                ", ".join("?" for _ in categorized_manga_ids)
            ),
            (pid, *categorized_manga_ids),
        )
        removed += cur.rowcount

    # Delete now-empty holding/placeholder categories.
    deleted_names: list[str] = []
    for cid in list(holding) + placeholder_ids:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM CategoryManga WHERE category = ?", (cid,)
        ).fetchone()
        if row["cnt"] == 0:
            name_row = conn.execute("SELECT name FROM Category WHERE id = ?", (cid,)).fetchone()
            if name_row:
                conn.execute("DELETE FROM Category WHERE id = ?", (cid,))
                deleted_names.append(name_row["name"])

    return removed, deleted_names


def reorder_categories_alphabetically(conn: sqlite3.Connection) -> None:
    """
    Reorder categories: system categories first (in their original order),
    then all other categories sorted alphabetically.
    """
    system_rows = conn.execute(
        """
        SELECT id, name FROM Category
        WHERE name IN ({}) AND is_delete = 0
        ORDER BY "order"
        """.format(", ".join("?" for _ in SYSTEM_CATEGORIES)),
        tuple(SYSTEM_CATEGORIES),
    ).fetchall()

    other_rows = conn.execute(
        """
        SELECT id, name FROM Category
        WHERE name NOT IN ({}) AND is_delete = 0
        ORDER BY name COLLATE NOCASE
        """.format(", ".join("?" for _ in SYSTEM_CATEGORIES)),
        tuple(SYSTEM_CATEGORIES),
    ).fetchall()

    order = 1
    for row in system_rows:
        conn.execute('UPDATE Category SET "order" = ? WHERE id = ?', (order, row["id"]))
        order += 1
    for row in other_rows:
        conn.execute('UPDATE Category SET "order" = ? WHERE id = ?', (order, row["id"]))
        order += 1


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args = parse_args()

    # Resolve backup path.
    backup_path: Path | None = args.backup
    if backup_path is None:
        backup_path = find_single_backup()
        if backup_path is None:
            import_dir = PROJECT_ROOT / DEFAULT_IMPORT_DIRNAME
            tmb_files = sorted(import_dir.glob("*.tmb")) if import_dir.is_dir() else []
            if not tmb_files:
                print(
                    f"No .tmb files found in {import_dir}. Provide a path as an argument.",
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
    output_path = pick_output_path(
        backup_path, args.output.resolve() if args.output else None
    )

    if not backup_path.is_file():
        print(f"Backup file not found: {backup_path}", file=sys.stderr)
        return 1

    # Load backup.
    meta, contents_bytes = load_backup_parts(backup_path)
    db_bytes = extract_db_bytes(contents_bytes)
    tf, conn = open_temp_db(db_bytes)

    try:
        # ----------------------------------------------------------
        # 1. Gather data
        # ----------------------------------------------------------
        categories = get_existing_categories(conn)
        cat_exact, cat_compact = build_category_lookup(categories)
        system_ids = get_system_manga_ids(conn)
        artist_mapping = get_artist_manga_mapping(conn, args.field, exclude_ids=system_ids)
        existing_assignments = get_existing_assignments(conn)

        max_order = max((info["order"] for info in categories.values()), default=0)

        # Filter by min count.
        if args.min_count > 1:
            artist_mapping = {
                k: v for k, v in artist_mapping.items() if len(v) >= args.min_count
            }

        # ----------------------------------------------------------
        # 2. Match artists to existing categories / plan new ones
        # ----------------------------------------------------------
        matched: dict[str, list[int]] = {}   # artist_raw → [category_ids]
        to_create: dict[str, str] = {}       # artist_raw → new category name
        skipped_long: list[tuple[str, int]] = []  # (artist_raw, manga_count)
        # Track names we're about to create to avoid duplicates.
        pending_names: dict[str, str] = {}   # normalize(cat_name) → artist_raw

        def _match_existing(name: str) -> int | None:
            """Match a single artist name against existing DB categories."""
            return match_artist_to_category(name, cat_exact, cat_compact)

        def _match_pending(name: str) -> str | None:
            """Match against categories we're about to create. Returns artist_raw key."""
            for alias in split_aliases(name):
                norm = normalize(alias)
                if norm in pending_names:
                    return pending_names[norm]
            return None

        for artist_raw in sorted(artist_mapping, key=lambda a: (-len(artist_mapping[a]), a)):
            # 1) Try to match the whole string (handles "|" aliases).
            cid = _match_existing(artist_raw)
            if cid is not None:
                matched[artist_raw] = [cid]
                continue

            # 2) For comma-separated multi-artist entries, try each part.
            parts = [p.strip() for p in artist_raw.split(",") if p.strip()]
            if len(parts) > 1:
                found_existing: list[int] = []
                found_pending: list[str] = []
                for part in parts:
                    c = _match_existing(part)
                    if c is not None and c not in found_existing:
                        found_existing.append(c)
                        continue
                    p_raw = _match_pending(part)
                    if p_raw is not None and p_raw not in found_pending:
                        found_pending.append(p_raw)
                if found_existing or found_pending:
                    if found_existing:
                        matched[artist_raw] = found_existing
                    # Merge manga into pending artist categories.
                    for p_raw in found_pending:
                        artist_mapping[p_raw].extend(artist_mapping[artist_raw])
                    continue
                # None matched — use first individual artist for new category.
                artist_for_name = parts[0]
            else:
                artist_for_name = artist_raw

            # 3) Check if this merges into an already-pending category.
            p_raw = _match_pending(artist_for_name)
            if p_raw is not None:
                artist_mapping[p_raw].extend(artist_mapping[artist_raw])
                continue

            # 4) Create a new category.
            cat_name = pick_category_name(artist_for_name)
            if len(cat_name) > MAX_CATEGORY_NAME_LENGTH:
                skipped_long.append((artist_raw, len(artist_mapping[artist_raw])))
                continue
            to_create[artist_raw] = cat_name
            pending_names[normalize(cat_name)] = artist_raw

        # ----------------------------------------------------------
        # 3. Plan assignments
        # ----------------------------------------------------------
        new_assignments: list[tuple[str, int, int]] = []  # (artist, cat_id, manga_id) — for reporting
        # We'll fill cat_id after creation for new categories.

        # ----------------------------------------------------------
        # 4. Report / dry-run
        # ----------------------------------------------------------
        print(f"\nBackup: {backup_path.name}")
        print(f"Field:  {args.field}")
        print(f"Skipped manga in system categories ({', '.join(sorted(SYSTEM_CATEGORIES))}): {len(system_ids)}")
        print(f"Artists with >= {args.min_count} manga: {len(artist_mapping)}")
        print(f"Matched to existing categories: {len(matched)}")
        print(f"New categories to create: {len(to_create)}")
        if skipped_long:
            print(f"Skipped (name > {MAX_CATEGORY_NAME_LENGTH} chars): {len(skipped_long)}")
            for raw, cnt in skipped_long:
                print(f"  ({cnt} manga)  {raw[:80]}...")

        if to_create:
            print("\n--- New categories ---")
            for artist_raw, cat_name in sorted(to_create.items(), key=lambda x: x[1]):
                count = len(artist_mapping[artist_raw])
                print(f"  {cat_name:<30} ({count} manga)  ← {artist_raw!r}")

        # Count new assignments needed.
        pending_count = 0
        for artist_raw, manga_ids in artist_mapping.items():
            cids = matched.get(artist_raw, [])
            for mid in manga_ids:
                if cids:
                    for c in cids:
                        if (c, mid) not in existing_assignments:
                            pending_count += 1
                elif artist_raw in to_create:
                    pending_count += 1

        print(f"\nNew category-manga assignments to add: {pending_count}")

        if args.dry_run:
            print("\n(dry run — no changes made)")
            return 0

        # ----------------------------------------------------------
        # 5. Apply changes
        # ----------------------------------------------------------

        # Clean placeholders if requested.
        if args.clean_placeholder:
            removed = delete_empty_placeholder_categories(conn)
            if removed:
                print(f"\nRemoved {len(removed)} empty placeholder categories: {', '.join(removed)}")

        # Create new categories.
        next_order = max_order + 1
        created_ids: dict[str, int] = {}
        for artist_raw, cat_name in sorted(to_create.items(), key=lambda x: x[1]):
            cid = create_category(conn, cat_name, next_order)
            created_ids[artist_raw] = cid
            cat_exact[normalize(cat_name)] = cid
            cat_compact[normalize_compact(cat_name)] = cid
            next_order += 1

        # Merge all artist → [category_ids].
        all_artist_cats: dict[str, list[int]] = {}
        all_artist_cats.update(matched)
        for ar, cid in created_ids.items():
            all_artist_cats[ar] = [cid]

        # Insert CategoryManga entries.
        added = 0
        for artist_raw, manga_ids in artist_mapping.items():
            cids = all_artist_cats.get(artist_raw, [])
            for cid in cids:
                for mid in manga_ids:
                    if (cid, mid) in existing_assignments:
                        continue
                    insert_category_manga(conn, cid, mid)
                    existing_assignments.add((cid, mid))
                    added += 1

        # Collect all manga IDs that got an artist category.
        categorized_ids: set[int] = set()
        for artist_raw in all_artist_cats:
            categorized_ids.update(artist_mapping.get(artist_raw, []))

        # Clean holding-bin categories (ZDefault, ZSort, ZArtists, Artist*).
        cleaned, deleted_cats = clean_holding_categories(conn, categorized_ids)

        # Sort all categories alphabetically (system first, then A-Z).
        reorder_categories_alphabetically(conn)

        conn.commit()

        print(f"\nCreated {len(created_ids)} categories, added {added} assignments.")
        if cleaned:
            print(f"Removed {cleaned} old assignments from holding categories.")
        if deleted_cats:
            print(f"Deleted empty categories: {', '.join(deleted_cats)}")

        # ----------------------------------------------------------
        # 6. Repack .tmb
        # ----------------------------------------------------------
        modified_db = read_modified_db(tf)
        repack_backup(meta, contents_bytes, modified_db, output_path)

    finally:
        conn.close()
        tf.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
