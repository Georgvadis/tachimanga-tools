#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

(
    cd "$ROOT_DIR"
    ./scripts/build_repo.sh "$@"
    ./scripts/publish_pages.sh
)
