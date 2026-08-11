#!/bin/bash
# Install the hourly snapshot-refresh job on macOS (launchd).
#
# Usage:  bash scripts/install_mac_refresh.sh
# Remove: launchctl unload ~/Library/LaunchAgents/com.macrodashboard.refresh.plist
#         rm ~/Library/LaunchAgents/com.macrodashboard.refresh.plist

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_SRC="$REPO_DIR/scripts/com.macrodashboard.refresh.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.macrodashboard.refresh.plist"

if [[ "$(uname)" != "Darwin" ]]; then
  echo "This installer is for macOS (launchd). On Linux, add a cron entry like:"
  echo "  0 * * * * cd $REPO_DIR && python3 scripts/refresh_snapshots.py --push"
  exit 1
fi

# Fail early if a plain 'git push' would prompt for credentials —
# launchd jobs have no terminal to type them into.
if ! git -C "$REPO_DIR" push --dry-run >/dev/null 2>&1; then
  echo "ERROR: 'git push' does not work non-interactively in $REPO_DIR."
  echo "Run 'git push' once manually so macOS Keychain stores your credentials, then re-run this installer."
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
sed -e "s|__REPO_DIR__|$REPO_DIR|g" -e "s|__HOME__|$HOME|g" "$PLIST_SRC" > "$PLIST_DST"

launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"

echo "Installed: refreshes Truth Social + FedWatch snapshots hourly and pushes on change."
echo "  Branch pushed : $(git -C "$REPO_DIR" branch --show-current) (whatever is checked out here)"
echo "  Log file      : ~/Library/Logs/macrodashboard-refresh.log"
echo "  Run once now  : launchctl start com.macrodashboard.refresh"
