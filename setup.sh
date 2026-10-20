#!/bin/bash
# One-time setup: creates a private Python environment, installs the two
# libraries the cleaner needs, and signs you in to your Outlook.com account.
set -euo pipefail
cd "$(dirname "$0")"

PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
      PY="$(command -v "$cand")"; break
    fi
  fi
done
if [ -z "$PY" ]; then
  echo "No Python 3.9+ found. Install one (e.g. 'brew install python') and re-run." >&2
  exit 1
fi
echo "Using $PY ($("$PY" --version))"

if [ ! -x .venv/bin/python ]; then
  "$PY" -m venv .venv
fi
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt
chmod +x move-junk-to-deleted.sh schedule.sh menubar/*.sh
mkdir -p logs
[ -f rules.toml ] || cp rules.example.toml rules.toml

if grep -q 'PASTE-YOUR-CLIENT-ID-HERE' rules.toml; then
  echo
  echo "Environment ready. Next: paste your client ID into rules.toml, then run ./setup.sh again to sign in."
  exit 0
fi

./move-junk-to-deleted.sh --login
echo
echo "Setup done. Try:  ./move-junk-to-deleted.sh --survey"
