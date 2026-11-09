#!/usr/bin/env bash
# Runs on the server: a consistent snapshot of the database (safe while Solfish is writing),
# keeping the last 14. `deploy.sh backup` copies the newest one to the Mac.
set -euo pipefail
dir="$HOME/.solfish/backups"
mkdir -p "$dir" && chmod 700 "$dir"
sqlite3 "$HOME/.solfish/solfish.db" ".backup '$dir/solfish-$(date +%F).db'"
chmod 600 "$dir"/*.db
ls -1t "$dir"/solfish-*.db | tail -n +15 | xargs -r rm --
