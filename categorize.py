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
import urllib.request
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_IMPORT_DIRNAME = "import"
DEFAULT_OUTPUT_DIRNAME = "output"
DEFAULT_COOKIES_FILE = "cookies.txt"

# Reading-status categories — manga in these is excluded from artist
# categorization regardless of order or id.
SYSTEM_CATEGORIES = {"Запланировано", "Прочитано", "Брошенно", "Читаю", "Отложено"}

# Category for manga where artist cannot be determined.
UNKNOWN_CATEGORY = "Неизвестно"

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
        default="both",
        help="Which DB field to use for categorization (default: both)",
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
        "--cookies",
        type=Path,
        help=(
            "Path to cookies file for ExHentai API access "
            f"(default: {DEFAULT_COOKIES_FILE} in project root). "
            "Format: key=value per line (igneous, ipb_member_id, ipb_pass_hash)."
        ),
    )
    parser.add_argument(
        "--no-api",
        action="store_true",
        help="Skip E-Hentai API lookup even if cookies file exists.",
    )
    return parser.parse_args()


def load_cookies(path: Path | None) -> dict[str, str] | None:
    """
    Load ExHentai cookies from a key=value file.

    Returns a dict with igneous, ipb_member_id, ipb_pass_hash or None
    if the file doesn't exist or required keys are missing/empty.
    """
    if path is None:
        path = PROJECT_ROOT / DEFAULT_COOKIES_FILE
    if not path.is_file():
        return None
    cookies: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        cookies[key.strip()] = value.strip()
    required = ("igneous", "ipb_member_id", "ipb_pass_hash")
    if all(cookies.get(k) for k in required):
        return {k: cookies[k] for k in required}
    return None


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


OTHER_BUCKET = "#"


def letter_bucket(artist_raw: str) -> str:
    """
    Return the letter category for an artist string.

    Picks the primary artist (first comma-separated part, first '|' alias),
    finds the first alphabetic character, and returns it uppercase.
    Non-Latin characters (digits, Japanese, etc.) go to OTHER_BUCKET ('#').
    """
    parts = [p.strip() for p in artist_raw.split(",") if p.strip()]
    primary = parts[0] if parts else artist_raw
    aliases = split_aliases(primary)
    name = aliases[0] if aliases else primary
    for ch in name.lstrip():
        if ch.isalpha():
            up = ch.upper()
            if "A" <= up <= "Z":
                return up
            return OTHER_BUCKET
    return OTHER_BUCKET


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
# E-Hentai API
# ---------------------------------------------------------------------------

_EHENTAI_URL_RE = re.compile(
    r"https?://(?:e[x-]hentai|exhentai)\.org/g/(\d+)/([a-f0-9]+)"
)

EHENTAI_API_URL = "https://api.e-hentai.org/api.php"
EXHENTAI_API_URL = "https://exhentai.org/api.php"
EHENTAI_BATCH_SIZE = 10
EHENTAI_REQUEST_DELAY = 1.5  # seconds between API requests
EHENTAI_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def parse_ehentai_gid_token(url: str) -> tuple[int, str] | None:
    """Extract (gid, token) from an E-Hentai / ExHentai gallery URL."""
    m = _EHENTAI_URL_RE.search(url)
    if m:
        return int(m.group(1)), m.group(2)
    return None


def fetch_ehentai_artists(
    gid_token_list: list[tuple[int, str]],
    cookies: dict[str, str] | None = None,
) -> dict[int, list[str]]:
    """
    Fetch artist tags from E-Hentai API for a batch of galleries.

    Returns {gid: [artist_name, ...]}.  Uses ExHentai endpoint when
    cookies are provided, plain E-Hentai otherwise.
    """
    result: dict[int, list[str]] = {}
    use_ex = bool(cookies)
    api_url = EXHENTAI_API_URL if use_ex else EHENTAI_API_URL

    for i in range(0, len(gid_token_list), EHENTAI_BATCH_SIZE):
        batch = gid_token_list[i : i + EHENTAI_BATCH_SIZE]
        payload = json.dumps({
            "method": "gdata",
            "gidlist": [[gid, token] for gid, token in batch],
            "namespace": 1,
        }).encode()

        headers = {
            "Content-Type": "application/json",
            "User-Agent": EHENTAI_USER_AGENT,
        }
        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        req = urllib.request.Request(api_url, data=payload, headers=headers)

        batch_num = i // EHENTAI_BATCH_SIZE + 1
        data = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read())
                break
            except Exception as exc:
                if attempt < 2:
                    print(f"  Batch {batch_num}: retry {attempt + 1} after error: {exc}",
                          file=sys.stderr)
                    time.sleep(3)
                else:
                    print(f"  Batch {batch_num}: failed after 3 attempts: {exc}",
                          file=sys.stderr)
        if data is None:
            continue

        for entry in data.get("gmetadata", []):
            gid = int(entry["gid"])
            artists = [
                tag.split(":", 1)[1]
                for tag in entry.get("tags", [])
                if tag.startswith("artist:") or tag.startswith("group:")
            ]
            # Deduplicate (artist and group can have the same name).
            seen_names: set[str] = set()
            unique: list[str] = []
            for a in artists:
                if a not in seen_names:
                    seen_names.add(a)
                    unique.append(a)
            if unique:
                result[gid] = unique

        # Rate-limit between batches.
        if i + EHENTAI_BATCH_SIZE < len(gid_token_list):
            time.sleep(EHENTAI_REQUEST_DELAY)

    return result


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
    ehentai_cookies: dict[str, str] | None = None,
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

    # Fallback: extract artist from title "[Artist] ..." or "(Event) [Artist] ..."
    for row in conn.execute(
        "SELECT id, title FROM Manga "
        "WHERE in_library = 1 AND TRIM(COALESCE(artist, '')) = '' "
        "  AND TRIM(COALESCE(author, '')) = '' "
        "  AND (title LIKE '[%' OR title LIKE '(%')"
    ):
        mid = int(row["id"])
        if mid in has_artist:
            continue
        parsed = extract_artist_from_title(row["title"])
        if parsed:
            _add(parsed, mid)
            has_artist.add(mid)

    # Fallback: E-Hentai / ExHentai API lookup for remaining unknowns.
    if ehentai_cookies is not None:
        covered_ids = has_artist | {
            mid
            for _, ids in seen.values()
            for mid in ids
        }
        api_candidates: list[tuple[int, int, str]] = []  # (manga_id, gid, token)
        for row in conn.execute(
            "SELECT id, url, real_url FROM Manga "
            "WHERE in_library = 1 AND TRIM(COALESCE(artist, '')) = '' "
            "  AND TRIM(COALESCE(author, '')) = ''"
        ):
            mid = int(row["id"])
            if mid in exclude_ids or mid in covered_ids:
                continue
            resolved = (row["real_url"] or row["url"] or "").strip()
            parsed_url = parse_ehentai_gid_token(resolved)
            if parsed_url:
                api_candidates.append((mid, parsed_url[0], parsed_url[1]))

        if api_candidates:
            print(f"Fetching artist tags from E-Hentai API for {len(api_candidates)} galleries...")
            gid_token_list = [(gid, token) for _, gid, token in api_candidates]
            gid_to_manga: dict[int, int] = {gid: mid for mid, gid, _ in api_candidates}
            api_results = fetch_ehentai_artists(gid_token_list, ehentai_cookies)
            api_found = 0
            for gid, artists in api_results.items():
                mid = gid_to_manga.get(gid)
                if mid is None:
                    continue
                # Use first artist as primary; join multiple with " | " for alias handling.
                artist_str = " | ".join(artists) if len(artists) > 1 else artists[0]
                _add(artist_str, mid)
                api_found += 1
            print(f"  API resolved artists for {api_found}/{len(api_candidates)} galleries.")

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
        VALUES (?, ?, 0, ?, ?, ?, 0, 1, 0)
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


def delete_empty_categories(conn: sqlite3.Connection) -> list[str]:
    """Delete all non-system empty categories (zero manga assigned)."""
    placeholders = ", ".join("?" for _ in SYSTEM_CATEGORIES)
    rows = conn.execute(
        f"""
        SELECT c.id, c.name FROM Category c
        WHERE c.is_delete = 0
          AND c.name NOT IN ({placeholders})
          AND c.id NOT IN (SELECT category FROM CategoryManga)
        """,
        tuple(SYSTEM_CATEGORIES),
    ).fetchall()
    removed: list[str] = []
    for row in rows:
        conn.execute("DELETE FROM Category WHERE id = ?", (row["id"],))
        removed.append(row["name"])
    return removed


def clean_old_assignments(
    conn: sqlite3.Connection,
    manga_target_cats: dict[int, set[int]],
) -> tuple[int, list[str]]:
    """
    For each categorized manga, remove ALL category assignments except
    system categories and the target artist category.
    Delete categories that become empty after cleanup.
    Return (removed_assignments, deleted_category_names).
    """
    # Get system category IDs to protect.
    system_cat_ids: set[int] = set()
    placeholders = ", ".join("?" for _ in SYSTEM_CATEGORIES)
    for r in conn.execute(
        f"SELECT id FROM Category WHERE name IN ({placeholders}) AND is_delete = 0",
        tuple(SYSTEM_CATEGORIES),
    ):
        system_cat_ids.add(int(r["id"]))

    keep_pairs: set[tuple[int, int]] = set()  # (category_id, manga_id)
    for mid, target_cids in manga_target_cats.items():
        for cid in target_cids:
            keep_pairs.add((cid, mid))

    # Find all assignments for categorized manga that should be removed.
    all_manga = tuple(manga_target_cats)
    removed = 0
    affected_cats: set[int] = set()

    for mid in all_manga:
        rows = conn.execute(
            "SELECT category FROM CategoryManga WHERE manga = ?", (mid,)
        ).fetchall()
        for r in rows:
            cid = int(r["category"])
            if cid in system_cat_ids:
                continue
            if (cid, mid) in keep_pairs:
                continue
            conn.execute(
                "DELETE FROM CategoryManga WHERE category = ? AND manga = ?",
                (cid, mid),
            )
            affected_cats.add(cid)
            removed += 1

    # Mark all affected categories (and target categories) as dirty for sync.
    all_dirty_cats = affected_cats | {
        cid for cids in manga_target_cats.values() for cid in cids
    }
    for cid in all_dirty_cats:
        if cid not in system_cat_ids:
            conn.execute(
                'UPDATE Category SET dirty = 1 WHERE id = ?', (cid,)
            )

    # Delete categories that became empty.
    deleted_names: list[str] = []
    for cid in affected_cats:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM CategoryManga WHERE category = ?", (cid,)
        ).fetchone()
        if row["cnt"] == 0:
            name_row = conn.execute(
                "SELECT name FROM Category WHERE id = ?", (cid,)
            ).fetchone()
            if name_row:
                conn.execute("DELETE FROM Category WHERE id = ?", (cid,))
                deleted_names.append(name_row["name"])

    return removed, deleted_names


def reorder_categories_alphabetically(conn: sqlite3.Connection) -> None:
    """
    Reorder categories: system categories first (in their original order),
    then "Неизвестно" (if it exists), then all other categories sorted
    alphabetically.
    """
    excluded = tuple(SYSTEM_CATEGORIES) + (UNKNOWN_CATEGORY,)

    system_rows = conn.execute(
        """
        SELECT id, name FROM Category
        WHERE name IN ({}) AND is_delete = 0
        ORDER BY "order"
        """.format(", ".join("?" for _ in SYSTEM_CATEGORIES)),
        tuple(SYSTEM_CATEGORIES),
    ).fetchall()

    unknown_rows = conn.execute(
        "SELECT id, name FROM Category WHERE name = ? AND is_delete = 0",
        (UNKNOWN_CATEGORY,),
    ).fetchall()

    other_rows = conn.execute(
        """
        SELECT id, name FROM Category
        WHERE name NOT IN ({}) AND is_delete = 0
        """.format(", ".join("?" for _ in excluded)),
        excluded,
    ).fetchall()

    # Sort: A-Z first, then '#' (OTHER_BUCKET), then anything else alphabetically.
    def sort_key(row: sqlite3.Row) -> tuple:
        name = row["name"]
        if name == OTHER_BUCKET:
            return (2, name)
        return (1, name.lower())

    other_rows = sorted(other_rows, key=sort_key)

    order = 1
    for row in system_rows:
        conn.execute('UPDATE Category SET "order" = ? WHERE id = ?', (order, row["id"]))
        order += 1
    for row in unknown_rows:
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
        ehentai_cookies = None
        if not args.no_api:
            ehentai_cookies = load_cookies(
                args.cookies.resolve() if args.cookies else None
            )
            if ehentai_cookies:
                print("ExHentai cookies loaded — API lookup enabled.")
            else:
                print("No cookies found — E-Hentai API lookup skipped.")
        artist_mapping = get_artist_manga_mapping(
            conn, args.field, exclude_ids=system_ids,
            ehentai_cookies=ehentai_cookies,
        )
        existing_assignments = get_existing_assignments(conn)

        max_order = max((info["order"] for info in categories.values()), default=0)

        # Filter by min count.
        if args.min_count > 1:
            artist_mapping = {
                k: v for k, v in artist_mapping.items() if len(v) >= args.min_count
            }

        # ----------------------------------------------------------
        # 2. Group manga by artist letter bucket (A-Z, '#' for other)
        # ----------------------------------------------------------
        letter_to_manga: dict[str, set[int]] = collections.defaultdict(set)
        for artist_raw, manga_ids in artist_mapping.items():
            letter = letter_bucket(artist_raw)
            letter_to_manga[letter].update(manga_ids)

        # ----------------------------------------------------------
        # 3. Report / dry-run
        # ----------------------------------------------------------
        print(f"\nBackup: {backup_path.name}")
        print(f"Field:  {args.field}")
        print(f"Skipped manga in system categories ({', '.join(sorted(SYSTEM_CATEGORIES))}): {len(system_ids)}")
        print(f"Artists with >= {args.min_count} manga: {len(artist_mapping)}")
        print(f"Letter buckets: {len(letter_to_manga)}")
        for letter in sorted(letter_to_manga, key=lambda x: (x == OTHER_BUCKET, x)):
            print(f"  {letter}: {len(letter_to_manga[letter])} manga")

        # Count uncategorizable manga (no artist from any source).
        all_library_ids_preview = {
            int(r["id"])
            for r in conn.execute("SELECT id FROM Manga WHERE in_library = 1")
        }
        will_be_categorized: set[int] = set()
        for ids in letter_to_manga.values():
            will_be_categorized.update(ids)
        uncategorized_preview = all_library_ids_preview - system_ids - will_be_categorized
        if uncategorized_preview:
            print(f"Manga without detectable artist → '{UNKNOWN_CATEGORY}': {len(uncategorized_preview)}")

        if args.dry_run:
            print("\n(dry run — no changes made)")
            return 0

        # ----------------------------------------------------------
        # 4. Apply changes — create letter categories, assign manga
        # ----------------------------------------------------------
        next_order = max_order + 1
        letter_cids: dict[str, int] = {}
        created_ids: dict[str, int] = {}  # for compatibility with summary print
        for letter in sorted(letter_to_manga, key=lambda x: (x == OTHER_BUCKET, x)):
            existing_row = conn.execute(
                "SELECT id FROM Category WHERE name = ? AND is_delete = 0",
                (letter,),
            ).fetchone()
            if existing_row:
                cid = int(existing_row["id"])
            else:
                cid = create_category(conn, letter, next_order)
                created_ids[letter] = cid
                next_order += 1
            letter_cids[letter] = cid

        added = 0
        for letter, manga_ids in letter_to_manga.items():
            cid = letter_cids[letter]
            for mid in manga_ids:
                if (cid, mid) in existing_assignments:
                    continue
                insert_category_manga(conn, cid, mid)
                existing_assignments.add((cid, mid))
                added += 1

        categorized_ids: set[int] = set()
        for ids in letter_to_manga.values():
            categorized_ids.update(ids)

        # ----------------------------------------------------------
        # 5b. Handle uncategorizable manga → "Неизвестно"
        # ----------------------------------------------------------
        # Find all in-library manga that did NOT get an artist category.
        all_library_ids = {
            int(r["id"])
            for r in conn.execute("SELECT id FROM Manga WHERE in_library = 1")
        }
        uncategorized_ids = all_library_ids - system_ids - categorized_ids

        unknown_added = 0
        if uncategorized_ids:
            # Find or create the "Неизвестно" category.
            unknown_row = conn.execute(
                "SELECT id FROM Category WHERE name = ? AND is_delete = 0",
                (UNKNOWN_CATEGORY,),
            ).fetchone()
            if unknown_row:
                unknown_cid = int(unknown_row["id"])
            else:
                unknown_cid = create_category(conn, UNKNOWN_CATEGORY, next_order)
                next_order += 1

            for mid in uncategorized_ids:
                if (unknown_cid, mid) not in existing_assignments:
                    insert_category_manga(conn, unknown_cid, mid)
                    existing_assignments.add((unknown_cid, mid))
                    unknown_added += 1

            # Include uncategorized manga in the set for holding cleanup.
            categorized_ids.update(uncategorized_ids)

        # Build manga → target category mapping for cleanup.
        manga_target_cats: dict[int, set[int]] = collections.defaultdict(set)
        for letter, manga_ids in letter_to_manga.items():
            cid = letter_cids[letter]
            for mid in manga_ids:
                manga_target_cats[mid].add(cid)
        if uncategorized_ids:
            for mid in uncategorized_ids:
                manga_target_cats[mid].add(unknown_cid)

        # Remove all non-system, non-target assignments; delete empty categories.
        cleaned, deleted_cats = clean_old_assignments(conn, manga_target_cats)

        # Delete any remaining empty non-system categories.
        leftover_empty = delete_empty_categories(conn)
        deleted_cats.extend(leftover_empty)

        # Sort all categories alphabetically (system first, then A-Z).
        reorder_categories_alphabetically(conn)

        # Reset sync state so restored backup is treated as new data.
        conn.execute("UPDATE SyncState SET enable = 0, max_commit_id = 0, last_sync_time = 0")
        conn.execute("DELETE FROM SyncCommit")
        conn.execute("DELETE FROM ChapterSync")
        for tbl in ("Category", "CategoryManga", "Manga", "Chapter"):
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()}
            updates = []
            if "commit_id" in cols:
                updates.append("commit_id = 0")
            if "dirty" in cols:
                updates.append("dirty = 1")
            if updates:
                conn.execute(f"UPDATE {tbl} SET " + ", ".join(updates))

        conn.commit()

        print(f"\nCreated {len(created_ids)} categories, added {added} assignments.")
        if unknown_added:
            print(f"Assigned {unknown_added} manga without detectable artist to '{UNKNOWN_CATEGORY}'.")
        if cleaned:
            print(f"Removed {cleaned} old assignments from other categories.")
        if deleted_cats:
            print(f"Deleted {len(deleted_cats)} empty categories: {', '.join(deleted_cats)}")

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
