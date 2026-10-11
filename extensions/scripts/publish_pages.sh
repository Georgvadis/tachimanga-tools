#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PUBLISH_DIR="${PUBLISH_DIR:-$ROOT_DIR/../.cache/extension-pages}"
PAGES_URL="${PAGES_REPOSITORY_URL:-https://github.com/Georgvadis/nsfw-ext.git}"
BRANCH_NAME="${PAGES_BRANCH:-gh-pages}"
COMMIT_MESSAGE="${PAGES_COMMIT_MESSAGE:-Publish extension repo}"

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "Missing required command: $1" >&2
        exit 1
    fi
}

require_repo_file() {
    if [[ ! -f "$ROOT_DIR/repo/$1" ]]; then
        echo "Missing repo artifact: $ROOT_DIR/repo/$1" >&2
        echo "Run ./scripts/build_repo.sh first." >&2
        exit 1
    fi
}

main() {
    require_command git
    require_command rsync

    require_repo_file "index.min.json"
    require_repo_file "repo.json"

    mkdir -p "$(dirname "$PUBLISH_DIR")"
    if [[ ! -e "$PUBLISH_DIR" ]]; then
        git clone --single-branch --branch "$BRANCH_NAME" "$PAGES_URL" "$PUBLISH_DIR"
    fi

    # Use an independent clone so source history never enters the release branch.
    if [[ ! -d "$PUBLISH_DIR/.git" ]] ||
       [[ "$(git -C "$PUBLISH_DIR" remote get-url origin)" != "$PAGES_URL" ]] ||
       [[ "$(git -C "$PUBLISH_DIR" branch --show-current)" != "$BRANCH_NAME" ]]; then
        echo "Publish directory must be an independent clone of $PAGES_URL ($BRANCH_NAME)." >&2
        exit 1
    fi
    if [[ -n "$(git -C "$PUBLISH_DIR" status --porcelain)" ]]; then
        echo "Publish directory has uncommitted changes: $PUBLISH_DIR" >&2
        exit 1
    fi
    git -C "$PUBLISH_DIR" pull --ff-only origin "$BRANCH_NAME"

    rsync -av --delete --exclude '.git' "$ROOT_DIR/repo/" "$PUBLISH_DIR/"
    touch "$PUBLISH_DIR/.nojekyll"

    git -C "$PUBLISH_DIR" add .
    if git -C "$PUBLISH_DIR" diff --cached --quiet; then
        echo "No changes to publish."
        exit 0
    fi

    git -C "$PUBLISH_DIR" commit -m "$COMMIT_MESSAGE"
    git -C "$PUBLISH_DIR" push -u origin "$BRANCH_NAME"

    echo "Published repo branch '$BRANCH_NAME' from $PUBLISH_DIR"
}

main "$@"
