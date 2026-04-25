#!/usr/bin/env python3
"""
Take a post-sync backup (where in_library got reset to 0 by cloud) and
re-apply the library state from a source backup, keeping the cloud-assigned
commit_ids so the next sync pushes proper updates.

Usage:
  ./restore_library.py <post_sync.tmb> <source.tmb> [-o output.tmb]
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import io
import json
import sqlite3
import sys
import tempfile
import time
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
SYSTEM_CATEGORIES = {"Запланировано", "Прочитано", "Брошенно", "Читаю", "Отложено"}


def open_db(tmb_path: Path) -> tuple[zipfile.ZipFile, dict, bytes, sqlite3.Connection, str]:
    """Return (None, meta, contents_bytes, conn, db_temp_path)."""
    with zipfile.ZipFile(tmb_path) as oz:
        meta = json.loads(oz.read("meta.json"))
        contents = oz.read("contents.zip")
    inner = io.BytesIO(contents)
    tmpdir = tempfile.mkdtemp()
    with zipfile.ZipFile(inner) as iz:
        iz.extractall(tmpdir)
    db_path = next(p for p in Path(tmpdir).iterdir() if p.suffix == ".db")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return None, meta, contents, conn, tmpdir


def repack(meta: dict, src_contents: bytes, tmpdir: str, output: Path) -> None:
    """Rebuild contents.zip from tmpdir, then outer .tmb with new meta."""
    inner_buf = io.BytesIO()
    src_inner = io.BytesIO(src_contents)
    with zipfile.ZipFile(src_inner) as orig:
        with zipfile.ZipFile(inner_buf, "w", zipfile.ZIP_DEFLATED) as new:
            for item in orig.infolist():
                fp = Path(tmpdir) / item.filename
                if fp.is_file():
                    new.writestr(item, fp.read_bytes())
    new_contents = inner_buf.getvalue()
    meta = dict(meta)
    meta["size"] = len(new_contents)
    meta["checksum"] = hashlib.sha1(new_contents).hexdigest()
    meta["name"] = output.name
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as outer:
        outer.writestr("meta.json", json.dumps(meta, indent=2, ensure_ascii=False))
        outer.writestr("contents.zip", new_contents)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("post_sync", type=Path, help="Backup after the cloud trashed in_library/CategoryManga.")
    p.add_argument("source", type=Path, help="Backup with the desired library + categories (pre-sync).")
    p.add_argument("-o", "--output", type=Path, help="Output .tmb path.")
    args = p.parse_args()

    if not args.post_sync.is_file() or not args.source.is_file():
        print("Both .tmb paths must exist.", file=sys.stderr)
        return 1

    output = args.output or PROJECT_ROOT / "output" / f"{args.post_sync.stem}_relib.tmb"

    # 1. Read source: which manga should be in library + which letter they belong to.
    print(f"Reading source: {args.source.name}")
    _, _, _, src_conn, src_tmpdir = open_db(args.source)
    src_lib: dict[tuple[int, str], dict] = {}  # (source, url) -> {title, in_library_at, letter}
    rows = src_conn.execute(
        "SELECT m.source, m.url, m.title, m.in_library_at, c.name AS letter "
        "FROM Manga m "
        "JOIN CategoryManga cm ON cm.manga = m.id "
        "JOIN Category c ON c.id = cm.category "
        "WHERE m.in_library = 1 AND c.is_delete = 0 AND c.name NOT IN ({})".format(
            ",".join("?" for _ in SYSTEM_CATEGORIES)
        ),
        tuple(SYSTEM_CATEGORIES),
    ).fetchall()
    for r in rows:
        key = (r["source"], r["url"])
        src_lib[key] = {
            "title": r["title"],
            "in_library_at": r["in_library_at"],
            "letter": r["letter"],
        }
    src_conn.close()
    print(f"  Source library: {len(src_lib)} manga in letter categories")

    # 2. Read post-sync, locate matching manga, restore in_library + assignments.
    print(f"Patching post-sync: {args.post_sync.name}")
    _, meta, contents, conn, tmpdir = open_db(args.post_sync)
    cur = conn.cursor()

    # Map (source, url) -> manga_id in post-sync DB
    post_id_by_key: dict[tuple[int, str], int] = {}
    for r in cur.execute("SELECT id, source, url FROM Manga"):
        post_id_by_key[(r["source"], r["url"])] = int(r["id"])

    # Letter category id by name
    letter_cid: dict[str, int] = {}
    for r in cur.execute("SELECT id, name FROM Category WHERE is_delete = 0"):
        letter_cid[r["name"]] = int(r["id"])

    # Apply: mark manga in_library=1, dirty=1; recreate CategoryManga; mark category dirty=1.
    now_ms = int(time.time() * 1000)
    matched = 0
    missing = 0
    by_letter: dict[str, list[int]] = collections.defaultdict(list)

    for key, info in src_lib.items():
        mid = post_id_by_key.get(key)
        if mid is None:
            missing += 1
            continue
        cid = letter_cid.get(info["letter"])
        if cid is None:
            # Letter category should exist (we created them via categorize.py).
            continue
        in_lib_at = info["in_library_at"] or now_ms
        cur.execute(
            "UPDATE Manga SET in_library = 1, in_library_at = ?, default_category = 0, dirty = 1, update_at = ? WHERE id = ?",
            (in_lib_at, now_ms, mid),
        )
        # CategoryManga has no soft-delete; insert if missing.
        existing = cur.execute(
            "SELECT 1 FROM CategoryManga WHERE category = ? AND manga = ?",
            (cid, mid),
        ).fetchone()
        if not existing:
            cur.execute(
                "INSERT INTO CategoryManga (category, manga) VALUES (?, ?)",
                (cid, mid),
            )
        by_letter[info["letter"]].append(mid)
        matched += 1

    # Mark all visible (non-system) categories as dirty so they push.
    placeholders = ",".join("?" for _ in SYSTEM_CATEGORIES)
    cur.execute(
        f"UPDATE Category SET dirty = 1 WHERE is_delete = 0 AND name NOT IN ({placeholders})",
        tuple(SYSTEM_CATEGORIES),
    )

    # Undelete the Russian system categories so they reappear after sync.
    # If the cloud honors the undelete, the Russian names come back instead of
    # the English defaults the app would otherwise auto-create.
    sys_undeleted = cur.execute(
        f"UPDATE Category SET is_delete = 0, dirty = 1, update_at = ? "
        f"WHERE name IN ({placeholders}) AND is_delete = 1",
        (now_ms,) + tuple(SYSTEM_CATEGORIES),
    ).rowcount
    if sys_undeleted:
        print(f"  Undeleted {sys_undeleted} system categories (Russian).")

    print(f"  Restored in_library + assignments for {matched} manga (missing in post-sync: {missing})")
    for letter in sorted(by_letter):
        print(f"    {letter}: {len(by_letter[letter])}")

    conn.commit()
    conn.close()

    repack(meta, contents, tmpdir, output)

    import shutil
    shutil.rmtree(tmpdir)
    shutil.rmtree(src_tmpdir)

    print(f"\nWrote {output}  ({output.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
