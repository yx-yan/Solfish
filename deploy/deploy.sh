#!/usr/bin/env bash
# Run on the Mac. The repo is private, so the code is pushed with rsync rather than pulled.
#
#   SOLFISH_DEPLOY_HOST=user@1.2.3.4 deploy/deploy.sh          # push code, install, restart
#   SOLFISH_DEPLOY_HOST=user@1.2.3.4 deploy/deploy.sh backup   # copy the newest snapshot here
#   SOLFISH_DEPLOY_HOST=user@1.2.3.4 deploy/deploy.sh logs     # follow the service log
set -euo pipefail
host="${SOLFISH_DEPLOY_HOST:?set SOLFISH_DEPLOY_HOST=user@host}"
cd "$(dirname "$0")/.."

case "${1:-deploy}" in
deploy)
    rsync -az --delete --exclude .git --exclude .venv --exclude .env --exclude __pycache__ \
        --exclude .pytest_cache --exclude .ruff_cache --exclude '*.egg-info' --exclude evals/results \
        ./ "$host:solfish/"
    ssh "$host" 'set -e
        cd ~/solfish
        [ -d .venv ] || python3 -m venv .venv
        .venv/bin/pip install -q -e ".[server]"
        for f in solfish.service solfish-backup.service solfish-backup.timer; do
            sed "s/USER/$(whoami)/g" deploy/$f | sudo tee /etc/systemd/system/$f >/dev/null
        done
        sudo systemctl daemon-reload
        sudo systemctl enable -q --now solfish-backup.timer
        sudo systemctl enable -q solfish
        sudo systemctl restart solfish
        sleep 3
        systemctl is-active solfish'
    ;;
backup)
    mkdir -p ~/.solfish-backups
    latest=$(ssh "$host" 'ls -1t ~/.solfish/backups/solfish-*.db | head -1')
    rsync -a "$host:$latest" ~/.solfish-backups/
    echo "saved ~/.solfish-backups/$(basename "$latest")"
    ;;
logs)
    ssh "$host" 'journalctl -u solfish -f -n 50'
    ;;
*)
    echo "usage: deploy.sh [deploy|backup|logs]" >&2
    exit 2
    ;;
esac
