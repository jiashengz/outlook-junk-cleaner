#!/usr/bin/env python3
"""
Junk-folder cleaner for Outlook.com / Hotmail, using the Microsoft Graph API.

It does exactly one thing: messages in the Junk Email folder that match a rule
in rules.toml are moved to Deleted Items. Nothing else is ever touched.

Usage (normally through ./move-junk-to-deleted.sh):
  python cleaner.py            move matching Junk mail to Deleted Items
  python cleaner.py --survey   show what is in Junk and what would be moved (changes nothing)
  python cleaner.py --login    one-time sign-in (device code in your browser)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from collections import Counter
from logging.handlers import RotatingFileHandler
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "rules.toml"
TOKEN_CACHE_PATH = HERE / ".token_cache.json"
LOG_DIR = HERE / "logs"

GRAPH = "https://graph.microsoft.com/v1.0"
AUTHORITY = "https://login.microsoftonline.com/consumers"  # personal accounts
SCOPES = ["Mail.ReadWrite"]

# Hard limits so a run can never go on forever or fill the disk.
LOG_MAX_BYTES = 1_000_000   # cleaner.log rotates at 1 MB ...
LOG_BACKUPS = 3             # ... keeping 3 old copies: at most 4 MB in total
MAX_PAGES = 50              # read at most 50 x 100 = 5000 Junk messages per run

log = logging.getLogger("junk-cleaner")


# --------------------------------------------------------------------------
# Config and logging
# --------------------------------------------------------------------------

def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(LOG_DIR / "cleaner.log", maxBytes=LOG_MAX_BYTES,
                             backupCount=LOG_BACKUPS)
    fh.setFormatter(fmt)
    log.setLevel(logging.INFO)
    log.addHandler(fh)
    if sys.stdout.isatty():  # interactive: also show on screen (launchd runs have no TTY)
        sh = logging.StreamHandler(sys.stdout)
    else:  # piped (e.g. through tee) or launchd: still surface problems on stderr
        sh = logging.StreamHandler(sys.stderr)
        sh.setLevel(logging.WARNING)
    sh.setFormatter(fmt)
    log.addHandler(sh)


def notify(title: str, message: str) -> None:
    """Best-effort macOS notification (silently does nothing elsewhere)."""
    if sys.platform != "darwin":
        return
    script = f'display notification {json.dumps(message)} with title {json.dumps(title)}'
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=10)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Matching (pure functions, no network)
# --------------------------------------------------------------------------

def _lower_list(values) -> list[str]:
    return [str(v).strip().lower() for v in (values or []) if str(v).strip()]


class Rules:
    """The delete rules. A message matches if ANY rule matches."""

    def __init__(self, cfg: dict):
        self.senders = set(_lower_list(cfg.get("senders")))
        self.domains = [d.lstrip("@.") for d in _lower_list(cfg.get("domains"))]
        self.sender_name_contains = _lower_list(cfg.get("sender_name_contains"))
        self.subject_contains = _lower_list(cfg.get("subject_contains"))
        self.from_regex = [re.compile(p, re.I) for p in cfg.get("from_regex", []) or []]

    def match(self, msg: dict) -> str | None:
        """Return a short reason string if the message matches, else None."""
        addr, name = sender_of(msg)
        subject = (msg.get("subject") or "").lower()

        if addr and addr in self.senders:
            return f"sender {addr}"
        if "@" in addr:
            domain = addr.rsplit("@", 1)[1]
            for d in self.domains:
                if domain == d or domain.endswith("." + d):
                    return f"domain {d}"
        for kw in self.sender_name_contains:
            if kw in name.lower():
                return f"sender name contains '{kw}'"
        for kw in self.subject_contains:
            if kw in subject:
                return f"subject contains '{kw}'"
        for rx in self.from_regex:
            if rx.search(addr):
                return f"address matches /{rx.pattern}/"
        return None


def sender_of(msg: dict) -> tuple[str, str]:
    """(address, display name) from the From field, falling back to Sender."""
    for field in ("from", "sender"):
        ea = (msg.get(field) or {}).get("emailAddress") or {}
        addr = (ea.get("address") or "").strip().lower()
        name = (ea.get("name") or "").strip()
        if addr or name:
            return addr, name
    return "", ""


def describe(msg: dict) -> str:
    addr, name = sender_of(msg)
    when = (msg.get("receivedDateTime") or "")[:16].replace("T", " ")
    subj = (msg.get("subject") or "(no subject)").replace("\n", " ")
    if len(subj) > 70:
        subj = subj[:67] + "..."
    who = f"{name} <{addr}>" if name else f"<{addr}>"
    return f"{when} | {who} | {subj}"


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

def _msal_app(client_id: str):
    import msal

    cache = msal.SerializableTokenCache()
    if TOKEN_CACHE_PATH.exists():
        cache.deserialize(TOKEN_CACHE_PATH.read_text())
    app = msal.PublicClientApplication(client_id, authority=AUTHORITY, token_cache=cache)
    return app, cache


def _save_cache(cache) -> None:
    if cache.has_state_changed:
        TOKEN_CACHE_PATH.write_text(cache.serialize())
        os.chmod(TOKEN_CACHE_PATH, 0o600)


def login(client_id: str) -> None:
    app, cache = _msal_app(client_id)
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise SystemExit(f"Could not start sign-in: {flow.get('error_description', flow)}")
    print()
    print(flow["message"])  # "To sign in, use a web browser to open ... and enter the code ..."
    print()
    result = app.acquire_token_by_device_flow(flow)
    _save_cache(cache)
    if "access_token" not in result:
        raise SystemExit(f"Sign-in failed: {result.get('error_description', result)}")
    who = (result.get("id_token_claims") or {}).get("preferred_username", "your account")
    print(f"Signed in as {who}. The sign-in is saved in {TOKEN_CACHE_PATH.name}.")


def get_token(client_id: str) -> str:
    app, cache = _msal_app(client_id)
    accounts = app.get_accounts()
    result = app.acquire_token_silent(SCOPES, account=accounts[0]) if accounts else None
    _save_cache(cache)
    if not result or "access_token" not in result:
        msg = "Sign-in expired or missing. Run: ./move-junk-to-deleted.sh --login"
        log.error(msg)
        notify("Junk cleaner", msg)
        raise SystemExit(2)
    return result["access_token"]


# --------------------------------------------------------------------------
# Graph calls
# --------------------------------------------------------------------------

class Graph:
    def __init__(self, token: str):
        import requests

        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"

    def _req(self, method: str, url: str, **kw):
        for attempt in range(6):  # at most 6 tries, each waiting at most 60 s
            r = self.s.request(method, url, timeout=60, **kw)
            if r.status_code in (429, 503, 504):
                try:
                    wait = int(r.headers.get("Retry-After", 2 ** attempt))
                except ValueError:
                    wait = 2 ** attempt
                log.warning("Graph throttled (%s), waiting %ss", r.status_code, min(wait, 60))
                time.sleep(min(wait, 60))
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"{method} {url} -> {r.status_code}: {r.text[:300]}")
            return r
        raise RuntimeError(f"{method} {url} kept being throttled; giving up this run")

    def junk_messages(self) -> list[dict]:
        url = f"{GRAPH}/me/mailFolders/junkemail/messages"
        params = {"$select": "id,subject,from,sender,receivedDateTime",
                  "$top": "100", "$orderby": "receivedDateTime desc"}
        out: list[dict] = []
        for _ in range(MAX_PAGES):
            data = self._req("GET", url, params=params).json()
            out.extend(data.get("value", []))
            url = data.get("@odata.nextLink")
            params = None  # nextLink already carries the query
            if not url:
                break
        return out

    def move_to_deleted_items(self, msg_id: str) -> None:
        self._req("POST", f"{GRAPH}/me/messages/{msg_id}/move",
                  json={"destinationId": "deleteditems"})


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def run(cfg: dict) -> int:
    rules = Rules(cfg)
    graph = Graph(get_token(cfg["client_id"]))
    msgs = graph.junk_messages()

    moved = errors = 0
    for m in msgs:
        reason = rules.match(m)
        if not reason:
            continue
        try:
            graph.move_to_deleted_items(m["id"])
            moved += 1
            log.info("Moved to Deleted Items  (%s) %s", reason, describe(m))
        except Exception as e:  # keep going with the rest
            errors += 1
            log.error("Failed on %s: %s", describe(m), e)

    log.info("Run done: %d in Junk, %d moved to Deleted Items, %d left, %d errors",
             len(msgs), moved, len(msgs) - moved - errors, errors)
    return 1 if errors else 0


def survey(cfg: dict) -> None:
    """Read-only summary of the Junk folder, to help write rules."""
    rules = Rules(cfg)
    graph = Graph(get_token(cfg["client_id"]))
    msgs = graph.junk_messages()

    domains = Counter()
    for m in msgs:
        addr, _ = sender_of(m)
        dom = addr.rsplit("@", 1)[1] if "@" in addr else "(none)"
        parts = dom.split(".")
        domains[".".join(parts[-2:]) if len(parts) >= 2 else dom] += 1

    print(f"\n{len(msgs)} messages in Junk.\n\nTop sender domains (subdomains grouped):")
    for dom, n in domains.most_common(30):
        print(f"  {n:4d}  {dom}")

    print("\nWhat the rules would do with each message (MOVE = to Deleted Items):")
    for m in msgs:
        reason = rules.match(m)
        tag, extra = ("MOVE", f"  ({reason})") if reason else ("  - ", "")
        print(f"  {tag}  {describe(m)}{extra}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Move Outlook.com Junk mail that matches rules.toml to Deleted Items.")
    ap.add_argument("--login", action="store_true", help="sign in (one time)")
    ap.add_argument("--survey", action="store_true", help="show what would be moved, change nothing")
    args = ap.parse_args()

    cfg = load_config()
    cid = str(cfg.get("client_id", "")).strip()
    if not cid or cid.startswith("PASTE"):
        raise SystemExit("Put your app's client ID into client_id in rules.toml first.")

    if args.login:
        login(cid)
        return 0
    setup_logging()
    import requests
    try:
        if args.survey:
            survey(cfg)
            return 0
        return run(cfg)
    except requests.exceptions.RequestException as e:
        # Offline, asleep mid-run, or Microsoft unreachable: nothing was changed
        # by the failed call; the next scheduled run simply tries again.
        log.warning("Network unavailable, skipping this run: %s", type(e).__name__)
        return 3
    except SystemExit:
        raise
    except Exception:
        # Anything unexpected goes into the size-capped log as one entry,
        # not into an ever-growing launchd error file.
        log.exception("Run failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
