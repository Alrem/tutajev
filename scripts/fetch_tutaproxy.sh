#!/usr/bin/env bash
# Fetch tutaproxy (AGPL-3.0) at a pinned commit into vendor/tutaproxy.
# Its `tuta` package is used as a library; the proxy servers are never started.
set -euo pipefail

REPO_URL="https://github.com/peix2/tutaproxy-public.git"
PINNED_SHA="85c7b4c6c55122e5b30712090129ac2d0effca39"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${TUTAPROXY_PATH:-$ROOT/vendor/tutaproxy}"

if [[ ! -d "$DEST/.git" ]]; then
    mkdir -p "$(dirname "$DEST")"
    git clone --quiet "$REPO_URL" "$DEST"
fi

git -C "$DEST" fetch --quiet origin "$PINNED_SHA"
git -C "$DEST" -c advice.detachedHead=false checkout --quiet "$PINNED_SHA"
echo "tutaproxy checked out at $PINNED_SHA in $DEST"
