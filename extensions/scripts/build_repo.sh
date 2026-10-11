#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_DIR="$ROOT_DIR/repo"
APK_DIR="$REPO_DIR/apk"
ICON_DIR="$REPO_DIR/icon"
CACHE_DIR="$ROOT_DIR/.cache"
TMP_DIR="$ROOT_DIR/tmp"
OUTPUT_JSON="$ROOT_DIR/output.json"
INSPECTOR_JAR="$CACHE_DIR/Inspector.jar"

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "Missing required command: $1" >&2
        exit 1
    fi
}

require_env() {
    if [[ -z "${!1:-}" ]]; then
        echo "Missing required environment variable: $1" >&2
        exit 1
    fi
}

detect_github_pages_url() {
    local remote_url owner repo

    if ! command -v git >/dev/null 2>&1; then
        return
    fi

    remote_url="$(git -C "$ROOT_DIR" remote get-url origin 2>/dev/null || true)"
    if [[ -z "$remote_url" ]]; then
        return
    fi

    if [[ "$remote_url" =~ ^https://github\.com/([^/]+)/([^/.]+)(\.git)?$ ]]; then
        owner="${BASH_REMATCH[1]}"
        repo="${BASH_REMATCH[2]}"
        printf 'https://%s.github.io/%s\n' "$owner" "$repo"
        return
    fi

    if [[ "$remote_url" =~ ^git@github\.com:([^/]+)/([^/.]+)(\.git)?$ ]]; then
        owner="${BASH_REMATCH[1]}"
        repo="${BASH_REMATCH[2]}"
        printf 'https://%s.github.io/%s\n' "$owner" "$repo"
    fi
}

discover_modules() {
    local module_dir
    while IFS= read -r module_dir; do
        module_dir="${module_dir#"$ROOT_DIR/src/"}"
        printf ':src:%s:%s:assembleRelease\n' "${module_dir%%/*}" "${module_dir##*/}"
    done < <(find "$ROOT_DIR/src" -mindepth 2 -maxdepth 2 -type d | sort)
}

download_inspector() {
    mkdir -p "$CACHE_DIR"
    if [[ -f "$INSPECTOR_JAR" ]]; then
        return
    fi

    local inspector_url
    inspector_url="$(
        python3 - <<'PY'
import json
import urllib.request

with urllib.request.urlopen(
    "https://api.github.com/repos/keiyoushi/extensions-inspector/releases/latest"
) as response:
    data = json.load(response)

for asset in data.get("assets", []):
    url = asset.get("browser_download_url", "")
    if url.endswith(".jar"):
        print(url)
        break
else:
    raise SystemExit("Could not find Inspector jar in latest release")
PY
    )"

    echo "Downloading Inspector: $inspector_url"
    curl -fsSL "$inspector_url" -o "$INSPECTOR_JAR"
}

main() {
    require_command java
    require_command python3
    require_command curl
    require_command find

    require_env ANDROID_HOME
    require_env ALIAS
    require_env KEY_STORE_PASSWORD
    require_env KEY_PASSWORD

    if [[ ! -f "$ROOT_DIR/signingkey.jks" ]]; then
        echo "Missing signing key: $ROOT_DIR/signingkey.jks" >&2
        exit 1
    fi

    mapfile -t modules < <(discover_modules)
    if [[ $# -gt 0 ]]; then
        modules=("$@")
    fi
    if [[ ${#modules[@]} -eq 0 ]]; then
        echo "No extension modules found under $ROOT_DIR/src" >&2
        exit 1
    fi

    mkdir -p "$APK_DIR" "$ICON_DIR" "$TMP_DIR"
    rm -f "$APK_DIR"/*.apk "$ICON_DIR"/*.png "$OUTPUT_JSON"

    echo "Building modules:"
    printf '  %s\n' "${modules[@]}"
    (
        cd "$ROOT_DIR"
        ./gradlew "${modules[@]}"
    )

    while IFS= read -r apk; do
        cp "$apk" "$APK_DIR/"
    done < <(find "$ROOT_DIR/src" -path '*/build/outputs/apk/release/*.apk' -type f | sort)

    if ! find "$APK_DIR" -maxdepth 1 -name '*.apk' -print -quit | grep -q .; then
        echo "No release APKs were produced" >&2
        exit 1
    fi

    local repo_website
    repo_website="${REPO_WEBSITE:-$(detect_github_pages_url)}"
    repo_website="${repo_website:-http://127.0.0.1:8000}"

    download_inspector

    echo "Inspecting APKs"
    java -jar "$INSPECTOR_JAR" "$APK_DIR" "$OUTPUT_JSON" "$TMP_DIR"

    echo "Generating repo metadata"
    (
        cd "$ROOT_DIR"
        python3 scripts/generate_repo.py \
            --name "${REPO_NAME:-NSFW Extensions}" \
            --website "$repo_website"
    )

    echo "Repository ready:"
    echo "  $REPO_DIR/index.min.json"
    echo "Website URL:"
    echo "  $repo_website"
}

main "$@"
