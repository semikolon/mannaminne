#!/usr/bin/env python3
"""Open consulting assignments, mined from the daily broker mails nobody opens.

Three brokers send a list of new assignments every day. Unread, each mail is
worth nothing, and an ad closes within two to three weeks. This reads those
mails from the corpus, pulls out every assignment, keeps the ones whose title
matches a profile, and can check which public ads are still open.

READ-ONLY on the corpus. `--check` fetches public ad pages.

The profile is personal and lives outside the repo: one regular expression per
line in ~/.config/mannaminne/assignment_profile.txt, matched case-insensitively
against the assignment title.
"""

from __future__ import annotations

import argparse
import html
import os
import re
import sys
import urllib.request
from collections import OrderedDict
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import corpus_mail  # noqa: E402
from mannaminne import CHUNK_OVERLAP, load_conn  # noqa: E402

PROFILE_FILE = os.path.expanduser("~/.config/mannaminne/assignment_profile.txt")

DEPLOJA = "deplojaab.teamtailor-mail.com"
EWORK = "eworkgroup.com"
BRAINVILLE = "brainville.com"

DEPLOJA_AD = re.compile(
    r"([A-ZÅÄÖ0-9.][^()<>]{2,90}?) \((https://career\.deploja\.se/jobs/[^)\s]+)\)")


def profile():
    try:
        with open(PROFILE_FILE, encoding="utf-8") as fh:
            pats = [l.strip() for l in fh if l.strip() and not l.startswith("#")]
    except FileNotFoundError:
        sys.exit(f"no profile: write one pattern per line to {PROFILE_FILE}")
    return re.compile("|".join(f"(?:{p})" for p in pats), re.I)


def join_chunks(parts):
    """A mail's text on one line. corpus_mail.join_chunks drops the overlap
    between chunks; the lists these mails carry are then split on spaces."""
    return re.sub(r"\s+", " ", corpus_mail.join_chunks(parts))


def parse_deploja(text):
    """[(title, url)] from one Deploja match mail."""
    out = []
    for title, url in DEPLOJA_AD.findall(text):
        out.append((title.split("profil ")[-1].strip(), url))
    return out


#: A tag that describes the card BEFORE it: another country the job may be done
#: from, "Remote", an hourly rate. The mail prints them after the card and ends
#: on a tag with nothing below it. Read the other way round they attach to the
#: wrong job: a rate of 850 SEK/h and "or Helsinki" were reported on the two ads
#: that followed the ones they belonged to (2026-10-10).
_EWORK_TAG = re.compile(
    r"^(?:(or [^()•]+? \([A-Z]{3}\)(?: \+ more)?)|(Remote)\b"
    r"|(\d+(?:\.\d+)? (?:SEK|EUR)/h))\s*")


def parse_ework(text):
    """Assignments from one Ework Verama mail. Each is a dict: `ad` is
    'Role Client • City', and `rate`, `remote` and `also` are its own tags."""
    body = text.split("full details.")[-1]
    ads = []
    for chunk in re.split(r"\(SWE\)\s*", body):
        rest = chunk.strip()
        while True:
            m = _EWORK_TAG.match(rest)
            if not m:
                break
            if ads:
                if m.group(1):
                    ads[-1]["also"] = m.group(1)[3:]
                elif m.group(2):
                    ads[-1]["remote"] = True
                else:
                    ads[-1]["rate"] = m.group(3)
            rest = rest[m.end():]
        if "•" in rest and 5 < len(rest) < 140:
            ads.append({"ad": rest, "rate": "", "remote": False, "also": ""})
    return ads


def mails(cur, domain, since):
    cur.execute("""SELECT c.source_id, c.created FROM chunks c
                   JOIN email_class e ON e.mid = c.source_id
                   WHERE c.source_kind='email' AND c.chunk_idx=0
                     AND e.domain=%s AND c.created>=%s ORDER BY c.created""",
                (domain, since))
    for mid, created in cur.fetchall():
        cur.execute("SELECT text FROM chunks WHERE source_id=%s ORDER BY chunk_idx", (mid,))
        yield created, join_chunks([r[0] for r in cur.fetchall()])


def check_deploja(url):
    """(open?, deadline, place) for a public ad page. None when unreachable."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        page = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "replace")
    except Exception:
        return None
    body = re.sub(r"<script.*?</script>|<style.*?</style>", "", page, flags=re.S)
    body = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", body)))
    if "inte längre aktiv" in body or "no longer active" in body:
        return (False, "", "")
    dl = re.search(r"(?:Application deadline|Sista ansökningdatum|Sista ansökningsdatum):\s*"
                   r"(\d{2}-\d{2}-\d{4})", body)
    place = re.search(r"Anställning · ([^·]+) · (\S+)", body)
    return (True, dl.group(1) if dl else "",
            f"{place.group(1).strip()}, {place.group(2)}" if place else "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=str(date.today() - timedelta(days=28)))
    ap.add_argument("--check", action="store_true", help="fetch public ads: still open?")
    args = ap.parse_args()
    want = profile()
    cur = load_conn().cursor()

    dep = OrderedDict()
    for created, text in mails(cur, DEPLOJA, args.since):
        for title, url in parse_deploja(text):
            dep.setdefault(url, (created, title))
    ew = OrderedDict()
    for created, text in mails(cur, EWORK, args.since):
        for ad in parse_ework(text):
            ew.setdefault(ad["ad"], (created, ad))
    bv_mails = list(mails(cur, BRAINVILLE, args.since))

    print(f"# Assignments posted since {args.since}\n")
    print(f"## Deploja: {len(dep)} ads, public pages\n")
    for url, (created, title) in dep.items():
        if not want.search(title):
            continue
        state = ""
        if args.check:
            r = check_deploja(url)
            if r is None:
                state = " | not reachable"
            elif not r[0]:
                state = " | CLOSED"
            else:
                state = f" | open, deadline {r[1] or '?'}, {r[2]}"
        print(f"- {created} | {title}{state} | {url}")
    print(f"\n## Ework Verama: {len(ew)} ads, details behind a login\n")
    for item, (created, ad) in ew.items():
        if want.search(item):
            tags = [t for t in (ad["rate"], "remote" if ad["remote"] else "",
                                f"or {ad['also']}" if ad["also"] else "") if t]
            print(f"- {created} | {item}" + (f" | {', '.join(tags)}" if tags else ""))
    print(f"\n## Brainville: {len(bv_mails)} daily lists, details behind a login\n")
    seen = set()
    for created, text in bv_mails:
        for m in want.finditer(text):
            window = text[max(0, m.start() - 60):m.end() + 110].strip()
            key = window[40:110]
            if key not in seen:
                seen.add(key)
                print(f"- {created} | …{window}…")


if __name__ == "__main__":
    main()
