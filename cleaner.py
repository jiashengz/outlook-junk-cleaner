#!/usr/bin/env python3
"""
Junk-folder cleaner for Outlook.com / Hotmail, using the Microsoft Graph API.

It does exactly one thing: messages in the Junk Email folder that match a rule
in rules.toml are marked as read and moved to Deleted Items. Nothing else is
ever touched.

Usage (normally through ./move-junk-to-deleted.sh):
  python cleaner.py            move matching Junk mail to Deleted Items
  python cleaner.py --survey   show what is in Junk and what would be moved (changes nothing)
  python cleaner.py --discover suggest rules for spam the current rules miss (changes nothing)
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
        self.subject_regex = [re.compile(p, re.I) for p in cfg.get("subject_regex", []) or []]

    def match(self, msg: dict) -> str | None:
        """Return a short reason string if the message matches, else None."""
        addr, name = sender_of(msg)
        raw_subject = msg.get("subject") or ""
        subject = raw_subject.lower()

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
        for rx in self.subject_regex:
            if rx.search(raw_subject):
                return f"subject matches /{rx.pattern}/"
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

    def mark_read(self, msg_id: str) -> None:
        self._req("PATCH", f"{GRAPH}/me/messages/{msg_id}", json={"isRead": True})

    def move_to_deleted_items(self, msg_id: str) -> str:
        """Move the message; returns its new id (a move gives the message a new id)."""
        r = self._req("POST", f"{GRAPH}/me/messages/{msg_id}/move",
                      json={"destinationId": "deleteditems"})
        return r.json()["id"]


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
            # Mark read AFTER the move, on the moved copy: marking first can be lost
            # when Outlook moves the message before the read flag is saved.
            graph.mark_read(graph.move_to_deleted_items(m["id"]))
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
        domains[base_domain(dom)] += 1

    print(f"\n{len(msgs)} messages in Junk.\n\nTop sender domains (subdomains grouped):")
    for dom, n in domains.most_common(30):
        print(f"  {n:4d}  {dom}")

    print("\nWhat the rules would do with each message (MOVE = to Deleted Items):")
    for m in msgs:
        reason = rules.match(m)
        tag, extra = ("MOVE", f"  ({reason})") if reason else ("  - ", "")
        print(f"  {tag}  {describe(m)}{extra}")
    print()


FREEMAIL = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
            "msn.com", "yahoo.com", "icloud.com", "me.com", "aol.com", "proton.me",
            "protonmail.com", "gmx.com", "mail.com", "qq.com", "163.com"}


def base_domain(domain: str) -> str:
    """The registered domain: mail.example.com -> example.com, but keep three parts for
    two-level endings: matrix.teraflowbrasil.com.br -> teraflowbrasil.com.br."""
    parts = domain.split(".")
    if (len(parts) >= 3 and len(parts[-1]) == 2
            and parts[-2] in ("co", "com", "net", "org", "gov", "edu", "ac", "or", "ne", "go")):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


SCATTERED_DIGITS = "(?=[a-z0-9]*[0-9][a-z]+[0-9])"  # a digit, letters, another digit


def _random_label(label: str) -> bool:
    """A domain part that looks machine-made: 8+ letters/digits with digits in two separate
    places (4ptuuh8w7oax). Real names keep digits together (1800flowers, microsoft365)."""
    return (len(label) >= 8 and label.isascii() and label.isalnum()
            and re.search(SCATTERED_DIGITS, label.lower()) is not None)


def suggest_rules(leftover: list[dict]) -> list[tuple[str, str, str, list[dict]]]:
    """Candidate rules for Junk mail the current rules leave behind.
    Returns (rules.toml key, value, why, messages it would catch), biggest first."""
    by_name, by_domain, by_subject, by_shape, odd_endings = {}, {}, {}, {}, []
    for m in leftover:
        addr, name = sender_of(m)
        if name and "@" not in name:
            by_name.setdefault(name.lower(), []).append(m)
        subj = (m.get("subject") or "").strip().lower()
        if len(subj) >= 10:
            by_subject.setdefault(subj, []).append(m)
        if "@" not in addr:
            continue
        labels = addr.rsplit("@", 1)[1].split(".")
        base = base_domain(".".join(labels))
        if base not in FREEMAIL:
            by_domain.setdefault(base, []).append(m)
        ending, body = labels[-1], labels[:-1]
        if not (ending.isascii() and ending.isalpha() and len(ending) >= 2) and not ending.startswith("xn--"):
            odd_endings.append(m)
        elif body and all(_random_label(l) for l in body):
            by_shape.setdefault((len(body), ending), []).append(m)

    out = []
    for name, ms in by_name.items():
        if len(ms) >= 2:
            out.append(("sender_name_contains", name, "same sender name", ms))
    for dom, ms in by_domain.items():
        why = "same domain" if len(ms) >= 2 else "this sender's domain (seen once: check it is spam)"
        out.append(("domains", dom, why, ms))
    for subj, ms in by_subject.items():
        if len(ms) >= 2:
            out.append(("subject_contains", subj, "same subject", ms))
    for (n, ending), ms in by_shape.items():
        # Shortest length seen at each position, never below 8, so it generalizes a little.
        # Every part keeps the scattered-digits check, or the rule would hit long real names.
        mins = [min(len(sender_of(m)[0].rsplit("@", 1)[1].split(".")[i]) for m in ms) for i in range(n)]
        rx = ("@" + r"\.".join(f"{SCATTERED_DIGITS}[a-z0-9]{{{max(8, k)},}}" for k in mins)
              + r"\." + re.escape(ending) + "$")
        out.append(("from_regex", rx, f"random-looking domain ending .{ending}", ms))
    if odd_endings:
        out.append(("from_regex", r"@[^@]+\.(?!xn--)(?![a-z]{2,}$)[^.]+$",
                    "domain ending that is not plain letters (digits, hyphen, emoji...)", odd_endings))

    # Keep only suggestions that really catch these messages with the cleaner's own matching.
    checked = []
    for key, value, why, _ in out:
        rules = Rules({key: [value]})
        caught = [m for m in leftover if rules.match(m)]
        if caught:
            checked.append((key, value, why, caught))
    return sorted(checked, key=lambda s: -len(s[3]))


def discover(cfg: dict) -> None:
    """Read-only: suggest rules for Junk mail the current rules leave behind."""
    rules = Rules(cfg)
    graph = Graph(get_token(cfg["client_id"]))
    leftover = [m for m in graph.junk_messages() if not rules.match(m)]

    print(f"\n{len(leftover)} messages in Junk are not matched by your rules.")
    print("(Outlook.com empties Junk after about 10 days, so this covers roughly that period.)\n")
    if not leftover:
        return
    suggestions = suggest_rules(leftover)
    covered = set()
    if suggestions:
        print("Suggested rules - add the value to that list in rules.toml, then check with --survey:\n")
        for key, value, why, caught in suggestions:
            literal = f"'{value}'" if "'" not in value else json.dumps(value)
            print(f"  {key} += {literal}")
            print(f"      {why}; would catch {len(caught)} of these:")
            for m in caught[:3]:
                print(f"        {describe(m)}")
            if len(caught) > 3:
                print(f"        ... and {len(caught) - 3} more")
            print()
            covered.update(id(m) for m in caught)
    rest = [m for m in leftover if id(m) not in covered]
    if rest:
        print("No pattern found for these (one-offs, or possibly real mail):")
        for m in rest:
            print(f"  {describe(m)}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Move Outlook.com Junk mail that matches rules.toml to Deleted Items.")
    ap.add_argument("--login", action="store_true", help="sign in (one time)")
    ap.add_argument("--survey", action="store_true", help="show what would be moved, change nothing")
    ap.add_argument("--discover", action="store_true",
                    help="suggest rules for Junk mail the rules leave behind, change nothing")
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
        if args.discover:
            discover(cfg)
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
