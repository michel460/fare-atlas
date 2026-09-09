#!/usr/bin/env bash
# Cron entry point for the fare tracker. Usage: run.sh daily|digest|test-slack [--full-window]
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Machine-specific paths, if this checkout has them. Keeps the tracker itself
# free of any one box's layout.
[ -f "$DIR/local.env" ] && . "$DIR/local.env"
# Where the API keys live, and where the viewer's JSON is published. Both are
# overridable so this script is not tied to one machine's layout.
ENV="${FARE_ENV_FILE:-$HOME/.config/fare-atlas/env}"
VIEWER_DIR="${FARE_VIEWER_DIR:-$HOME/edge/site/today/flights}"
# Extract only what we need: the env file holds values with shell
# metacharacters, so never `source` it.
export SERPAPI_API_KEY="$(grep -m1 '^SERPAPI_API_KEY=' "$ENV" | cut -d= -f2-)"
export ARIA_SLACK_BOT_TOKEN="$(grep -m1 '^ARIA_SLACK_BOT_TOKEN=' "$ENV" | cut -d= -f2-)"
export SLACK_BOT_TOKEN="$(grep -m1 '^SLACK_BOT_TOKEN=' "$ENV" | cut -d= -f2-)"
# Where alerts go. Any executable taking --header and reading stdin; leave it
# unset and the tracker prints to the log instead of notifying.
export FARE_NOTIFY_CMD="${FARE_NOTIFY_CMD:-}"
export PATH="$HOME/.local/bin:$PATH"
mkdir -p "$DIR/logs"
echo "=== $(date -Is) run.sh $*" >> "$DIR/logs/tracker.log"
"$DIR/.venv/bin/python" "$DIR/tracker.py" "$@" >> "$DIR/logs/tracker.log" 2>&1
rc=$?
# Watchlist runs after the tracker and is never allowed to affect it:
# its exit code is swallowed so a reader failure cannot turn the cron
# receipt red or lose the fixed-date alerts. Keyless, no SerpAPI quota.
if [ "${1:-daily}" = "daily" ]; then
  "$DIR/.venv/bin/python" "$DIR/watchlist.py" daily >> "$DIR/logs/watchlist.log" 2>&1 || true
  # Refresh the viewer's data from the same database, so the globe shows this
  # morning's prices instead of a snapshot that ages.
  if [ -d "$VIEWER_DIR" ]; then
    "$DIR/.venv/bin/python" "$DIR/export_globe.py" "$VIEWER_DIR" >> "$DIR/logs/watchlist.log" 2>&1 || true
  fi
fi
exit $rc
