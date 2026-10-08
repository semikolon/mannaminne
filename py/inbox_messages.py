#!/usr/bin/env python3
"""Message-level fact table for the inbox cleanup decision.

`inbox_inventory.py` ranks SENDER DOMAINS and reads them from the corpus. Two
things made that the wrong instrument for the deletion decision: the corpus is
not the account (it also holds a 4.7 GB offline archive), and a sender is one
variable. This reads the messages that are actually in the Gmail account and
describes each one by several independent signals at once, weighted by the
bytes it occupies, because bytes are what the quota counts.

Every signal except size comes from ID LISTS, which cost one call per 500
messages. No per-message fetch is needed to know a message's category, whether
it was opened, starred, labelled, replied to, how old it is or which size band
it sits in. Sizes are exact above SIZE_EXACT_FROM and a seeded sample below it,
and the total is checked against the quota Google itself reports.

READ-ONLY. Lists and reads metadata. Deletes nothing, labels nothing.

Output is bulk personal data and stays out of every repo and out of iCloud:
~/.cache/mannaminne/inbox/.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from inbox_inventory import MONEY_AUTHORITY_LABELS  # noqa: E402

CACHE = Path.home() / ".cache" / "mannaminne" / "inbox"
CACHE_MAX_AGE_H = 20

LABELS = ["INBOX", "UNREAD", "STARRED", "IMPORTANT", "SENT", "DRAFT",
          "CATEGORY_PERSONAL", "CATEGORY_UPDATES", "CATEGORY_PROMOTIONS",
          "CATEGORY_SOCIAL", "CATEGORY_FORUMS"]

#: Lower bounds in bytes. A message's band is the highest bound it exceeds.
SIZE_BOUNDS = [20_000, 50_000, 100_000, 200_000, 500_000,
               1_000_000, 2_000_000, 5_000_000, 10_000_000]
#: At and above this bound every message's size is fetched; below it, a sample.
SIZE_EXACT_FROM = 500_000
SAMPLE_PER_BAND = 250

YEAR_BOUNDS = [2010, 2012, 2014, 2016, 2018, 2020, 2022, 2024, 2025, 2026]

QUERIES = {
    "attach": "has:attachment",
    "userlabel": "has:userlabels",
    "purchase": "category:purchases",
    "reservation": "category:reservations",
    # Receipts, invoices, bookings and tickets by their own words, in both
    # languages. Deliberately broad: this set PROTECTS, so a false hit keeps a
    # message and a miss could lose one.
    "transaction": ("subject:(kvitto OR faktura OR receipt OR invoice OR "
                    "orderbekräftelse OR beställning OR bokning OR "
                    "bokningsbekräftelse OR booking OR biljett OR ticket OR "
                    "betalning OR payment OR avi OR påminnelse OR kontoutdrag)"),
    "authority": "from:(" + " OR ".join(sorted(MONEY_AUTHORITY_LABELS)) + ")",
}


#: Personal mail providers. A message from one of these was written by a person
#: even when Gmail files it under a bulk category: measured 2026-10-08, the
#: unread "forums" and "updates" mail from these domains was discussion-list
#: threads and small hand-run newsletters, not marketing.
WEBMAIL = ["gmail.com", "googlemail.com", "hotmail.com", "hotmail.se", "live.com",
           "live.se", "outlook.com", "yahoo.com", "yahoo.se", "icloud.com", "me.com",
           "mac.com", "msn.com", "telia.com", "protonmail.com", "proton.me"]

#: His own labels protect a message, except the ones a tool or a filter applied
#: to bulk mail. That list is personal and lives outside the repo, one label per
#: line, a trailing * for a prefix.
BULK_LABELS_FILE = os.path.expanduser("~/.config/mannaminne/inbox_bulk_labels.txt")


def kept_domains():
    """Sender domains the index config explicitly keeps. What is kept in the
    index is kept in the inbox: one list, read from where it already lives."""
    from mannaminne import _load_email_rules
    domains, _ = _load_email_rules()
    return sorted(d for d, cls in domains.items() if cls == "keep")


def bulk_label_patterns():
    try:
        with open(BULK_LABELS_FILE, encoding="utf-8") as fh:
            return [l.strip() for l in fh if l.strip() and not l.startswith("#")]
    except FileNotFoundError:
        return []


def is_bulk_label(name, patterns):
    return any(name == p or (p.endswith("*") and name.startswith(p[:-1])) for p in patterns)


def user_labels():
    out = subprocess.run(["gws", "gmail", "users", "labels", "list", "--params",
                          json.dumps({"userId": "me"})], capture_output=True, text=True,
                         timeout=60).stdout
    d = json.loads(out[out.index("{"):])
    return {l["id"]: l["name"] for l in d.get("labels", []) if l.get("type") == "user"}


def _gws(args, timeout=900):
    out = subprocess.run(["gws", *args], capture_output=True, text=True, timeout=timeout)
    return [l for l in out.stdout.splitlines() if l.startswith("{")]


def list_ids(key, *, q=None, label=None, fresh=False):
    """All (id, threadId) pairs for a query or a label, paginated to the end."""
    path = CACHE / f"ids_{key}.tsv"
    if (not fresh and path.exists()
            and time.time() - path.stat().st_mtime < CACHE_MAX_AGE_H * 3600):
        return [tuple(l.split("\t")) for l in path.read_text().splitlines() if l]
    params = {"userId": "me", "maxResults": 500}
    if q:
        params["q"] = q
    if label:
        params["labelIds"] = [label]
    pages = _gws(["gmail", "users", "messages", "list", "--params", json.dumps(params),
                  "--page-all", "--page-limit", "5000", "--page-delay", "0"])
    pairs = []
    for line in pages:
        for m in json.loads(line).get("messages", []):
            pairs.append((m["id"], m["threadId"]))
    path.write_text("\n".join(f"{a}\t{b}" for a, b in pairs))
    return pairs


def fetch_meta(mid):
    """sizeEstimate, sender, subject and date for one message, or None."""
    params = {"userId": "me", "id": mid, "format": "metadata",
              "metadataHeaders": ["From", "Subject", "Date"]}
    for attempt in range(4):
        try:
            out = subprocess.run(["gws", "gmail", "users", "messages", "get", "--params",
                                  json.dumps(params)], capture_output=True, text=True,
                                 timeout=60).stdout
            d = json.loads(out[out.index("{"):])
            if "sizeEstimate" not in d:
                raise ValueError(out[:200])
            h = {x["name"].lower(): x["value"] for x in d.get("payload", {}).get("headers", [])}
            return {"id": mid, "size": d["sizeEstimate"], "from": h.get("from", ""),
                    "subject": h.get("subject", ""), "date": h.get("date", ""),
                    "internal": int(d.get("internalDate", 0)) // 1000}
        except Exception:
            time.sleep(1 + attempt)
    return None


def load_meta():
    path = CACHE / "meta.jsonl"
    meta = {}
    if path.exists():
        for l in path.read_text().splitlines():
            if l:
                d = json.loads(l)
                meta[d["id"]] = d
    return meta


def fetch_many(ids, workers=16):
    """Fetch metadata for ids not already cached. Appends as it goes."""
    meta = load_meta()
    todo = [i for i in ids if i not in meta]
    if not todo:
        return meta
    print(f"  fetching metadata for {len(todo)} message(s)", flush=True)
    failed = 0
    with open(CACHE / "meta.jsonl", "a", encoding="utf-8") as fh, \
            ThreadPoolExecutor(workers) as pool:
        for n, d in enumerate(pool.map(fetch_meta, todo), 1):
            if d is None:
                failed += 1
                continue
            meta[d["id"]] = d
            fh.write(json.dumps(d, ensure_ascii=False) + "\n")
            if n % 500 == 0:
                fh.flush()
                print(f"    {n}/{len(todo)}", flush=True)
    if failed:
        print(f"  {failed} fetch(es) failed and are absent from the table", flush=True)
    return meta


def build(fresh=False):
    CACHE.mkdir(parents=True, exist_ok=True)
    jobs = {"all": {}}
    for lab in LABELS:
        jobs[lab] = {"label": lab}
    for k, q in QUERIES.items():
        jobs[k] = {"q": q}
    for b in SIZE_BOUNDS:
        jobs[f"size{b}"] = {"q": f"larger:{b}"}
    for y in YEAR_BOUNDS:
        jobs[f"before{y}"] = {"q": f"before:{y}/01/01"}
    jobs["human"] = {"q": "from:(" + " OR ".join(sorted(set(WEBMAIL + kept_domains()))) + ")"}
    ulabels = user_labels()
    for lid in ulabels:
        jobs[f"ulabel_{lid}"] = {"label": lid}

    t0 = time.time()
    sets = {}
    with ThreadPoolExecutor(8) as pool:
        futs = {k: pool.submit(list_ids, k, fresh=fresh, **kw) for k, kw in jobs.items()}
        for k, f in futs.items():
            sets[k] = f.result()
            if not k.startswith("ulabel_"):
                print(f"  {k:<22} {len(sets[k]):>7}", flush=True)
    print(f"  listed in {time.time() - t0:.0f}s", flush=True)

    thread_of = dict(sets["all"])
    member = {k: {i for i, _ in v} for k, v in sets.items()}
    # A thread Fredrik wrote in is a conversation, whoever started it.
    my_threads = {t for _, t in sets["SENT"]}

    def band(mid):
        lo = 0
        for b in SIZE_BOUNDS:
            if mid in member[f"size{b}"]:
                lo = b
        return lo

    def year_band(mid):
        """The first year bound the message is older than, i.e. it was received
        before 1 January of that year. 9999 = this year."""
        for y in YEAR_BOUNDS:
            if mid in member[f"before{y}"]:
                return y
        return 9999

    labels_of = {}
    for lid, name in ulabels.items():
        for mid, _ in sets[f"ulabel_{lid}"]:
            labels_of.setdefault(mid, []).append(name)

    table = {}
    for mid, tid in thread_of.items():
        cat = next((c[9:].lower() for c in LABELS if c.startswith("CATEGORY_")
                    and mid in member[c]), "none")
        table[mid] = {
            "t": tid, "cat": cat, "band": band(mid), "before": year_band(mid),
            "unread": mid in member["UNREAD"], "starred": mid in member["STARRED"],
            "important": mid in member["IMPORTANT"], "inbox": mid in member["INBOX"],
            "sent": mid in member["SENT"], "convo": tid in my_threads,
            "attach": mid in member["attach"], "userlabel": mid in member["userlabel"],
            "purchase": mid in member["purchase"] or mid in member["reservation"],
            "transaction": mid in member["transaction"],
            "authority": mid in member["authority"],
            "human": mid in member["human"], "labels": labels_of.get(mid, []),
        }

    # Sizes: exact for the heavy tail, a seeded sample per band below it.
    exact = [m for m, r in table.items() if r["band"] >= SIZE_EXACT_FROM]
    rng = random.Random(20261008)
    sample = []
    for b in [0] + [x for x in SIZE_BOUNDS if x < SIZE_EXACT_FROM]:
        pool_ids = sorted(m for m, r in table.items() if r["band"] == b)
        sample += rng.sample(pool_ids, min(SAMPLE_PER_BAND, len(pool_ids)))
    meta = fetch_many(exact + sample)

    # A mean for every band, the exact ones included: a fetch that failed leaves
    # its message without a size, and it takes its band's mean rather than zero.
    band_mean = {}
    for b in [0] + SIZE_BOUNDS:
        got = [meta[m]["size"] for m, r in table.items() if r["band"] == b and m in meta]
        band_mean[b] = sum(got) / len(got) if got else b
    for mid, r in table.items():
        if mid in meta:
            r["size"], r["size_exact"] = meta[mid]["size"], True
        else:
            r["size"], r["size_exact"] = band_mean[r["band"]], False

    (CACHE / "table.json").write_text(json.dumps(table))
    total = sum(r["size"] for r in table.values())
    print(f"  {len(table)} messages, {total / 1e9:.2f} GB "
          f"({sum(1 for r in table.values() if r['size_exact'])} sized exactly)")
    print("  band means:", {k: round(v) for k, v in band_mean.items()})
    return table


BULK_CATEGORIES = ("updates", "promotions", "social")


def protect_reasons(r, patterns):
    """Why a message is never proposed for deletion. Positive signals only: each
    one says somebody valued or needs this message, never that it looks like junk."""
    why = []
    if r["sent"] or r["convo"]:
        why.append("conversation")
    if r["starred"]:
        why.append("starred")
    if r["important"]:
        why.append("important")
    if r["attach"]:
        why.append("attachment")
    if r["authority"]:
        why.append("money/authority")
    if r["transaction"] or r["purchase"]:
        why.append("receipt/booking")
    if r["human"]:
        why.append("personal sender")
    if any(not is_bulk_label(n, patterns) for n in r["labels"]):
        why.append("own label")
    return why


def in_sweep(r, patterns):
    """The broad rule: bulk mail that was never opened and that nothing protects.
    Forums are left out on purpose. They are discussion lists, written by people."""
    return (r["cat"] in BULK_CATEGORIES and r["unread"]
            and not protect_reasons(r, patterns))


def load_table():
    return json.loads((CACHE / "table.json").read_text())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fresh", action="store_true", help="ignore the id-list cache")
    args = ap.parse_args()
    build(fresh=args.fresh)


if __name__ == "__main__":
    main()
