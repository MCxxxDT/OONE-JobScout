#!/usr/bin/env bash
# Finder double-click entrypoint.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec /bin/bash "$ROOT_DIR/uninstall.sh" "$@"
