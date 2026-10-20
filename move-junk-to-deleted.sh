#!/bin/bash
# Moves Outlook.com Junk mail that matches rules.toml to Deleted Items.
#   ./move-junk-to-deleted.sh            do it now (this is also what the schedule runs)
#   ./move-junk-to-deleted.sh --survey   preview what would be moved; changes nothing
#   ./move-junk-to-deleted.sh --login    sign in again
cd "$(dirname "$0")" || exit 1

# The scheduler's raw output file is kept under 1 MB (trimmed to its last 200 KB).
# The main log, logs/cleaner.log, caps itself at 4 MB in total.
f=logs/launchd.log
if [ -f "$f" ] && [ "$(stat -f%z "$f")" -gt 1000000 ]; then
  tail -c 200000 "$f" > "$f.tmp" && cat "$f.tmp" > "$f" && rm -f "$f.tmp"
fi

exec .venv/bin/python -u cleaner.py "$@"
