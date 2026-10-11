#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PUBLISH_DIR="${PUBLISH_DIR:-$ROOT_DIR/../nsfw-ext-pages}"
REMOTE_NAME="${PAGES_REMOTE:-origin}"
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

    if ! git -C "$ROOT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        echo "Not a git repository: $ROOT_DIR" >&2
        exit 1
    fi

    if ! git -C "$ROOT_DIR" remote get-url "$REMOTE_NAME" >/dev/null 2>&1; then
        echo "Git remote '$REMOTE_NAME' is not configured in $ROOT_DIR" >&2
        exit 1
    fi

    require_repo_file "index.min.json"
    require_repo_file "repo.json"

    mkdir -p "$(dirname "$PUBLISH_DIR")"

    if [[ ! -d "$PUBLISH_DIR/.git" ]]; then
        git -C "$ROOT_DIR" worktree add -B "$BRANCH_NAME" "$PUBLISH_DIR"
    fi

    rsync -av --delete --exclude '.git' "$ROOT_DIR/repo/" "$PUBLISH_DIR/"
    touch "$PUBLISH_DIR/.nojekyll"

    git -C "$PUBLISH_DIR" add .
    if git -C "$PUBLISH_DIR" diff --cached --quiet; then
        echo "No changes to publish."
        exit 0
    fi

    git -C "$PUBLISH_DIR" commit -m "$COMMIT_MESSAGE"
    git -C "$PUBLISH_DIR" push -u "$REMOTE_NAME" "$BRANCH_NAME"

    echo "Published repo branch '$BRANCH_NAME' from $PUBLISH_DIR"
}

main "$@"
