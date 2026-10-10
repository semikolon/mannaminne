#!/usr/bin/env python3
"""Which bulk senders does the owner show signs of valuing?

`mail_judge.py` marks mail from unruled senders as bulk, and bulk leaves the
embedding queue. Some of that bulk is a newsletter the owner reads. This tool
finds those from his own behaviour in Gmail, never from a guess about his taste,
and prints a `keep` line for each, ready for `email_classes.txt`.

A publisher is proposed when Gmail shows one of:

  - at least STAR_MIN starred mails from it,
  - at least OPEN_MIN opened mails since SINCE, and at least OPEN_SHARE of what
    it sent since then (a date floor, because an old habit of marking everything
    read makes every old sender look read),
  - at least WROTE_MIN mails from the owner to it.

One signal alone ranks badly (an open rate rewards senders from the years he
read everything; a star count rewards shops whose receipts he starred), so the
rule takes any of three and prints which one fired.

A rule is written for the publisher's registrable domain, so `news.example.org`
and `example.org` are one publisher. A shared sending platform is the exception:
its domain is kept whole, because widening it would take in every newsletter
that happens to send through the same platform.

Read-only. It prints; the owner or his assistant decides what goes into the
file. Counts are cached in ~/.cache/mannaminne/inbox/sender_attention.json.

    inbox_valued_senders.py                 # propose from cached counts, fetch what is missing
    inbox_valued_senders.py --refresh       # count again in Gmail
    inbox_valued_senders.py --dropped 40    # also list the largest senders with no sign
"""
import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

STAR_MIN = 3
OPEN_MIN = 5
OPEN_SHARE = 0.10
WROTE_MIN = 8
SINCE = "2024/06/01"
CACHE = Path.home() / ".cache" / "mannaminne" / "inbox" / "sender_attention.json"

#: Sending platforms and catch-all hosts: many unrelated senders under one
#: registrable domain. A rule for one of these names the exact sending domain.
SHARED = {"beehiiv.com", "kajabimail.net", "mailgun.org", "anpdm.com", "google.com", "europa.eu",
          "googlegroups.com", "intercom-mail.com", "substack.com", "mailchi.mp", "sendgrid.net",
          "hubspotemail.net", "ghost.io", "convertkit-mail.com"}
TWO_PART_SUFFIXES = {"co.uk", "org.uk", "ac.uk", "com.au", "co.nz", "co.jp", "com.br"}


def registrable(domain):
    """`news.example.org` → `example.org`; `list.example.org.uk` → `example.org.uk`."""
    parts = domain.lower().split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in TWO_PART_SUFFIXES:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else domain.lower()


def rule_domain(domain):
    """The domain a keep rule should name for this sender."""
    reg = registrable(domain)
    return domain.lower() if reg in SHARED else reg


def signs(att):
    """The signs of interest in one sender's counts, in the words the config file uses."""
    out = []
    starred, wrote = att.get("starred") or 0, att.get("wrote_to") or 0
    recent, opened = att.get("recent") or 0, att.get("recent_read") or 0
    if starred >= STAR_MIN:
        out.append(f"{starred} stjärnmärkta")
    if opened >= OPEN_MIN and recent and opened / recent >= OPEN_SHARE:
        out.append(f"öppnat {opened} av {recent} sedan mitten av 2024")
    if wrote >= WROTE_MIN:
        out.append(f"{wrote} egna mejl dit")
    return out


def gmail_count(query):
    """Messages matching a Gmail query, counted on one page (500 is plenty to rank by)."""
    for _ in range(2):
        try:
            out = subprocess.run(["gws", "gmail", "users", "messages", "list", "--params",
                                  json.dumps({"userId": "me", "q": query, "maxResults": 500})],
                                 capture_output=True, text=True, timeout=60).stdout
            return len(json.loads(out[out.index("{"):]).get("messages", []))
        except Exception:
            time.sleep(1)
    return None


def attention(domain):
    return {"domain": domain,
            "starred": gmail_count(f"from:{domain} is:starred"),
            "wrote_to": gmail_count(f"to:{domain}"),
            "recent": gmail_count(f"from:{domain} after:{SINCE}"),
            "recent_read": gmail_count(f"from:{domain} -is:unread after:{SINCE}")}


def bulk_senders(cur):
    """{sender domain: (waiting pieces, mails, usual display name)} for mail the judge called bulk
    and that is bulk now, or that a rule has since taken back. Mail from a protected sender is
    neither: the judge's class was never applied to it, so it needs no keep line."""
    cur.execute(r"""SELECT e.domain, count(*), count(DISTINCT c.source_id),
                           mode() WITHIN GROUP (ORDER BY substring(h.text from 'From:[ ]*"?([^"<\n]*)'))
                    FROM chunks c JOIN email_class e ON e.mid = c.source_id
                    JOIN chunks h ON h.id = c.source_id || '#0'
                    WHERE c.source_kind='email' AND c.embedding IS NULL
                      AND e.jev_class='bulk' AND (e.class='bulk' OR e.set_by='rule')
                      AND e.domain IS NOT NULL AND position('.' in e.domain) > 0
                    GROUP BY 1""")
    return {d: (pieces, mails, (name or "").strip()) for d, pieces, mails, name in cur.fetchall()}


def load_cache():
    try:
        return {r["domain"]: r for r in json.loads(CACHE.read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        return {}


def propose(senders, counts):
    """(kept, dropped): publishers with a sign and without, largest first.
    Each entry: rule domain, name, pieces, mails, signs."""
    groups = {}
    for domain, (pieces, mails, name) in senders.items():
        g = groups.setdefault(rule_domain(domain), {"pieces": 0, "mails": 0, "signs": set(), "name": "", "top": -1})
        g["pieces"] += pieces
        g["mails"] += mails
        if pieces > g["top"]:
            g["top"], g["name"] = pieces, name
        g["signs"].update(signs(counts.get(domain, {})))
    rows = [{"rule": k, "name": g["name"], "pieces": g["pieces"], "mails": g["mails"], "signs": sorted(g["signs"])}
            for k, g in groups.items()]
    rows.sort(key=lambda r: -r["pieces"])
    return [r for r in rows if r["signs"]], [r for r in rows if not r["signs"]]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--refresh", action="store_true", help="count again in Gmail for every sender")
    ap.add_argument("--dropped", type=int, default=0, help="also list this many of the largest senders with no sign")
    args = ap.parse_args()
    from mannaminne import load_conn
    conn = load_conn()
    cur = conn.cursor()
    cur.execute("SET statement_timeout='120s'")
    senders = bulk_senders(cur)
    conn.rollback()
    counts = {} if args.refresh else load_cache()
    missing = [d for d in senders if d not in counts or counts[d].get("recent_read") is None]
    if missing:
        print(f"# counting {len(missing)} sender(s) in Gmail...", file=sys.stderr)
        with ThreadPoolExecutor(10) as pool:
            for row in pool.map(attention, missing):
                counts[row["domain"]] = {**counts.get(row["domain"], {}), **row}
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(list(counts.values()), ensure_ascii=False, indent=1), encoding="utf-8")
    kept, dropped = propose(senders, counts)
    print(f"# {len(kept)} utgivare med tecken på intresse ({sum(r['pieces'] for r in kept)} textbitar), "
          f"{len(dropped)} utan ({sum(r['pieces'] for r in dropped)} textbitar)")
    for r in kept:
        print(f"# {r['name'] or r['rule']}: {'; '.join(r['signs'])}")
        print(f"keep {r['rule']}")
    if args.dropped:
        print(f"\n# De {args.dropped} största utan tecken:")
        for r in dropped[:args.dropped]:
            print(f"#   {r['pieces']:6d}  {r['rule']:<34} {r['name']}")


if __name__ == "__main__":
    main()
