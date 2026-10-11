#!/usr/bin/env python3

import argparse
import html
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from zipfile import ZipFile

PACKAGE_NAME_REGEX = re.compile(r"package: name='([^']+)'")
VERSION_CODE_REGEX = re.compile(r"versionCode='([^']+)'")
VERSION_NAME_REGEX = re.compile(r"versionName='([^']+)'")
IS_NSFW_REGEX = re.compile(r"'tachiyomi.extension.nsfw' value='([^']+)'")
APPLICATION_LABEL_REGEX = re.compile(r"^application-label:'([^']+)'", re.MULTILINE)
APPLICATION_ICON_REGEX = re.compile(r"^application-icon-(\d+):'([^']+)'$", re.MULTILINE)
LANGUAGE_REGEX = re.compile(r"tachiyomi-([^.]+)")
SIGNER_SHA256_REGEX = re.compile(r"Signer #1 certificate SHA-256 digest: ([0-9A-Fa-f:]+)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-dir", default="repo")
    parser.add_argument("--inspector-output", default="output.json")
    parser.add_argument("--name", required=True)
    parser.add_argument("--website", required=True)
    parser.add_argument("--fingerprint", default=os.environ.get("REPO_SIGNING_FINGERPRINT"))
    return parser.parse_args()


def build_tools_key(path: Path) -> tuple[int, ...]:
    parts: list[int] = []
    for value in path.name.split("."):
        try:
            parts.append(int(value))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def find_latest_build_tools(android_home: Path) -> Path:
    build_tools_dir = android_home / "build-tools"
    candidates = sorted(
        (path for path in build_tools_dir.iterdir() if path.is_dir()),
        key=build_tools_key,
    )
    if not candidates:
        raise SystemExit(f"No build-tools found under {build_tools_dir}")
    return candidates[-1]


def parse_badging(aapt: Path, apk: Path) -> str:
    return subprocess.check_output(
        [aapt, "dump", "--include-meta-data", "badging", str(apk)],
        text=True,
    )


def best_icon_path(badging: str) -> str:
    matches = APPLICATION_ICON_REGEX.findall(badging)
    if not matches:
        raise SystemExit("Could not find application icon path in aapt badging output")
    _, icon_path = max(((int(density), path) for density, path in matches), key=lambda item: item[0])
    return icon_path


def signing_fingerprint(apksigner: Path, apk: Path) -> str:
    output = subprocess.check_output(
        [apksigner, "verify", "--print-certs", str(apk)],
        text=True,
    )
    match = SIGNER_SHA256_REGEX.search(output)
    if not match:
        raise SystemExit("Could not determine signing fingerprint from APK")
    return match.group(1).replace(":", "").lower()


def strip_version_ids(index_data: list[dict]) -> list[dict]:
    stripped: list[dict] = []
    for item in index_data:
        new_item = dict(item)
        new_item["sources"] = []
        for source in item["sources"]:
            clean_source = dict(source)
            clean_source.pop("versionId", None)
            new_item["sources"].append(clean_source)
        stripped.append(new_item)
    return stripped


def main() -> None:
    args = parse_args()

    android_home = Path(os.environ["ANDROID_HOME"])
    build_tools = find_latest_build_tools(android_home)
    aapt = build_tools / "aapt"
    apksigner = build_tools / "apksigner"

    if not aapt.exists():
        raise SystemExit(f"aapt not found: {aapt}")
    if not apksigner.exists() and not args.fingerprint:
        raise SystemExit(
            f"apksigner not found: {apksigner}. Set --fingerprint or REPO_SIGNING_FINGERPRINT."
        )

    repo_dir = Path(args.repo_dir)
    apk_dir = repo_dir / "apk"
    icon_dir = repo_dir / "icon"
    icon_dir.mkdir(parents=True, exist_ok=True)
    for icon in icon_dir.glob("*.png"):
        icon.unlink()

    with Path(args.inspector_output).open(encoding="utf-8") as file:
        inspector_data = json.load(file)

    apks = sorted(apk_dir.glob("*.apk"))
    if not apks:
        raise SystemExit(f"No APK files found in {apk_dir}")

    index_data: list[dict] = []

    for apk in apks:
        badging = parse_badging(aapt, apk)
        package_info = next(line for line in badging.splitlines() if line.startswith("package: "))

        package_name = PACKAGE_NAME_REGEX.search(package_info).group(1)
        version_code = int(VERSION_CODE_REGEX.search(package_info).group(1))
        version_name = VERSION_NAME_REGEX.search(package_info).group(1)
        nsfw_match = IS_NSFW_REGEX.search(badging)
        nsfw = int(nsfw_match.group(1)) if nsfw_match else 0
        label_match = APPLICATION_LABEL_REGEX.search(badging)
        if not label_match:
            raise SystemExit(f"Could not read application label for {apk.name}")
        icon_path = best_icon_path(badging)

        with ZipFile(apk) as archive:
            with archive.open(icon_path) as source, (icon_dir / f"{package_name}.png").open("wb") as target:
                shutil.copyfileobj(source, target)

        language_match = LANGUAGE_REGEX.search(apk.name)
        language = language_match.group(1) if language_match else "all"
        sources = inspector_data[package_name]

        if len(sources) == 1:
            source_language = sources[0]["lang"]
            if (
                source_language != language
                and source_language not in {"all", "other"}
                and language not in {"all", "other"}
            ):
                language = source_language

        entry = {
            "name": label_match.group(1),
            "pkg": package_name,
            "apk": apk.name,
            "lang": language,
            "code": version_code,
            "version": version_name,
            "nsfw": nsfw,
            "sources": [],
        }

        for source in sources:
            entry["sources"].append(
                {
                    "name": source["name"],
                    "lang": source["lang"],
                    "id": source["id"],
                    "baseUrl": source["baseUrl"],
                    "versionId": source["versionId"],
                }
            )

        index_data.append(entry)

    index_data.sort(key=lambda item: item["pkg"])
    index_min_data = strip_version_ids(index_data)

    fingerprint = args.fingerprint or signing_fingerprint(apksigner, apks[0])

    with (repo_dir / "index.json").open("w", encoding="utf-8") as file:
        json.dump(index_data, file, ensure_ascii=False, indent=2)
        file.write("\n")

    with (repo_dir / "index.min.json").open("w", encoding="utf-8") as file:
        json.dump(index_min_data, file, ensure_ascii=False, separators=(",", ":"))
        file.write("\n")

    with (repo_dir / "repo.json").open("w", encoding="utf-8") as file:
        json.dump(
            {
                "meta": {
                    "name": args.name,
                    "website": args.website,
                    "signingKeyFingerprint": fingerprint,
                }
            },
            file,
            ensure_ascii=False,
            indent=2,
        )
        file.write("\n")

    with (repo_dir / "index.html").open("w", encoding="utf-8") as file:
        file.write("<!DOCTYPE html>\n<html>\n<head>\n<meta charset=\"UTF-8\">\n<title>apks</title>\n</head>\n<body>\n<pre>\n")
        for entry in index_data:
            apk_escaped = "apk/" + html.escape(entry["apk"])
            name_escaped = html.escape(entry["name"])
            file.write(f"<a href=\"{apk_escaped}\">{name_escaped}</a>\n")
        file.write("</pre>\n</body>\n</html>\n")

    (repo_dir / ".nojekyll").write_text("", encoding="utf-8")


if __name__ == "__main__":
    main()
