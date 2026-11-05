#!/usr/bin/env python3
"""
Helpers for deploy.sh (run with the project's .venv Python).

  prepare.py rules OUT   write rules.toml (and its [inbox] section) as JSON for the Worker
  prepare.py token OUT   write the Mac's sign-in (refresh token) to OUT, to start the Worker's own
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cleaner  # noqa: E402


def write_private(path: str, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def as_json(r: cleaner.Rules) -> dict:
    return {
        "senders": sorted(r.senders),
        "domains": r.domains,
        "sender_name_contains": r.sender_name_contains,
        "sender_name_regex": [rx.pattern for rx in r.sender_name_regex],
        "subject_contains": r.subject_contains,
        "from_regex": [rx.pattern for rx in r.from_regex],
        "subject_regex": [rx.pattern for rx in r.subject_regex],
        "permanent_domains": r.permanent_domains,
    }


def rules(out: str) -> None:
    cfg = cleaner.load_config()
    data = as_json(cleaner.Rules(cfg))
    if cfg.get("inbox"):
        data["inbox"] = as_json(cleaner.Rules(cfg["inbox"]))
    write_private(out, json.dumps(data))


def token(out: str) -> None:
    # The Worker starts from the Mac's sign-in; after its first refresh it keeps its own token.
    if not cleaner.TOKEN_CACHE_PATH.exists():
        raise SystemExit("Sign in on the Mac first: ./move-junk-to-deleted.sh --login")
    cache = json.loads(cleaner.TOKEN_CACHE_PATH.read_text())
    tokens = [t["secret"] for t in cache.get("RefreshToken", {}).values()]
    if not tokens:
        raise SystemExit("No sign-in found. Run: ./move-junk-to-deleted.sh --login")
    write_private(out, tokens[0])


if __name__ == "__main__":
    {"rules": rules, "token": token}[sys.argv[1]](sys.argv[2])
