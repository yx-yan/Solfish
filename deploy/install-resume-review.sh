#!/usr/bin/env bash
# Install (or reinstall) the weekly résumé review as a launchd agent on this Mac.
# Needs SOLFISH_RESUME_DIR and SOLFISH_PROJECTS_DIR in .env; with SOLFISH_TELEGRAM_* set, the
# outcome is sent to Telegram. Remove with:
#   launchctl bootout gui/$(id -u)/com.solfish.resume-review
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
plist="$HOME/Library/LaunchAgents/com.solfish.resume-review.plist"
mkdir -p "$HOME/.solfish/logs" "$(dirname "$plist")"
sed -e "s|REPO|$repo|g" -e "s|HOME|$HOME|g" "$repo/deploy/resume-review.plist" > "$plist"
launchctl bootout "gui/$(id -u)/com.solfish.resume-review" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$plist"
echo "installed: every Sunday 20:00; log at ~/.solfish/logs/resume-review.log"
echo "run now:   launchctl kickstart gui/$(id -u)/com.solfish.resume-review"
