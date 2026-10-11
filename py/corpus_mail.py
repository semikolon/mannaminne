#!/usr/bin/env python3
"""Read the mail in the corpus as MESSAGES, for mining it.

`mannaminne` search returns chunks, ranked for one question, without an id.
Mining needs the opposite shape: every message matching a net, with an id to
read it whole, filtered by sender, sender domain and date.

  senders  who sends, how much, since when
  titles   one sender's subjects, newest first
  search   messages where a query matches, with the matching line
  read     one message's full text
  export   one sender's messages as one text, oldest first, links stripped

READ-ONLY. Keyword search only, so every query rides the full-text index.

A query is plain words (all must occur close together, within one ~750
character chunk), "a quoted phrase", OR between alternatives, -word to exclude.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mannaminne import CHUNK_OVERLAP, load_conn  # noqa: E402

SENDER = r"regexp_replace(substring(h.text from 'From: ([^\n]*)'), ' *<.*$', '')"


def join_chunks(parts):
    """A message's text from its chunks. Each chunk after the first repeats the
    tail of the one before it; joined whole, words double at every boundary."""
    if not parts:
        return ""
    return parts[0] + "".join(p[CHUNK_OVERLAP:] for p in parts[1:])


_LINK = re.compile(r"[ \t]*\(\s*https?://[^\s)]+\s*\)|[ \t]*https?://\S+")


def strip_links(text):
    """A mail's words without its link addresses. Newsletters store every link as
    a tracking address in parentheses, often longer than the sentence around it;
    for reading a sender whole they are most of the bytes and none of the content."""
    return _LINK.sub("", text)


def _filters(args, alias="h"):
    """SQL conditions and parameters shared by the listing commands. `alias` is
    the header chunk (chunk 0), which carries the From line and the date."""
    cond, par = [f"{alias}.source_kind='email'", f"{alias}.chunk_idx=0"], []
    if args.since:
        cond.append(f"{alias}.created >= %s")
        par.append(args.since)
    if args.until:
        cond.append(f"{alias}.created < %s")
        par.append(args.until)
    if args.domain:
        doms = [d.strip().lower() for d in args.domain.split(",") if d.strip()]
        cond.append("(e.domain = ANY(%s) OR e.domain LIKE ANY(%s))")
        par += [doms, ["%." + d for d in doms]]
    if args.sender:
        cond.append(f"{alias}.text ILIKE %s")
        par.append(f"%From: %{args.sender}%")
    return " AND ".join(cond), par


def cmd_senders(cur, args):
    cond, par = _filters(args)
    cur.execute(f"""SELECT {SENDER} sender, e.domain, count(*) n, min(h.created), max(h.created)
                    FROM chunks h LEFT JOIN email_class e ON e.mid = h.source_id
                    WHERE {cond} GROUP BY 1, 2 ORDER BY 3 DESC LIMIT %s""", par + [args.limit])
    for sender, dom, n, first, last in cur.fetchall():
        print(f"{n:>6}  {first[:7]}..{last[:7]}  {dom or '?':<28} {sender or ''}")


def cmd_titles(cur, args):
    cond, par = _filters(args)
    cur.execute(f"""SELECT h.created, h.source_id, {SENDER}, h.title
                    FROM chunks h LEFT JOIN email_class e ON e.mid = h.source_id
                    WHERE {cond} ORDER BY h.created DESC LIMIT %s""", par + [args.limit])
    for created, mid, sender, title in cur.fetchall():
        print(f"{created}  {mid}  {(sender or '')[:30]:<30}  {title}")


def cmd_search(cur, args):
    cond, par = _filters(args)
    q = " ".join(args.query)
    # Match chunks through the index first, bounded, and only then rank and join.
    # Ranking every match of a common word is what once took a minute.
    cur.execute(f"""
        WITH hit AS (
          SELECT c.source_id, c.id, c.text
          FROM chunks c
          WHERE c.source_kind='email'
            AND c.tsv @@ websearch_to_tsquery('simple', %s)
          LIMIT 20000),
        per AS (
          SELECT source_id, count(*) hits, min(id) first_id FROM hit GROUP BY 1)
        SELECT h.created, p.source_id, {SENDER}, h.title, p.hits,
               (SELECT ts_headline('simple', x.text, websearch_to_tsquery('simple', %s),
                                   'MaxWords=34, MinWords=18, MaxFragments=1')
                FROM hit x WHERE x.id = p.first_id)
        FROM per p
        JOIN chunks h ON h.id = p.source_id || '#0'
        LEFT JOIN email_class e ON e.mid = p.source_id
        WHERE {cond}
        ORDER BY {'p.hits DESC, h.created DESC' if args.by == 'hits' else 'h.created DESC'}
        LIMIT %s""", [q, q] + par + [args.limit])
    rows = cur.fetchall()
    for created, mid, sender, title, hits, hl in rows:
        print(f"{created}  {mid}  hits={hits}  {(sender or '')[:28]} | {title}")
        if not args.brief:
            print("      " + re.sub(r"\s+", " ", hl or "")[:260])
    print(f"-- {len(rows)} message(s)", file=sys.stderr)


def cmd_read(cur, args):
    for mid in args.mid:
        cur.execute("SELECT text FROM chunks WHERE source_id=%s ORDER BY chunk_idx", (mid,))
        text = join_chunks([r[0] for r in cur.fetchall()])
        if not text:
            print(f"== {mid}: not found")
            continue
        total = len(text)
        text = text[args.offset:args.offset + args.max_chars]
        print(f"== {mid}  ({total} characters, showing {args.offset}..{args.offset + len(text)})")
        print(text)
        print()


def cmd_export(cur, args):
    """Every message the filters select, oldest first, as one readable text:
    a header line with id, date and subject, then the body without link addresses."""
    if not (args.domain or args.sender):
        sys.exit("export needs --domain or --sender: a whole mailbox is not one reading")
    where, par = _filters(args)
    cur.execute(f"""SELECT h.source_id, left(h.created, 10), h.title
                    FROM chunks h LEFT JOIN email_class e ON e.mid = h.source_id
                    WHERE {where} ORDER BY h.created ASC LIMIT %s""", par + [args.limit])
    heads = cur.fetchall()
    out = open(args.out, "w", encoding="utf-8") if args.out else sys.stdout
    chars = 0
    for mid, created, title in heads:
        cur.execute("SELECT text FROM chunks WHERE source_id=%s ORDER BY chunk_idx", (mid,))
        body = strip_links(join_chunks([r[0] for r in cur.fetchall()]))
        body = re.sub(r"\n{3,}", "\n\n", body).strip()
        if args.max_chars_each:
            body = body[:args.max_chars_each]
        chars += len(body)
        out.write(f"\n===== {mid} | {created} | {title}\n{body}\n")
    if args.out:
        out.close()
    print(f"-- {len(heads)} message(s), {chars} characters"
          + (f", written to {args.out}" if args.out else ""), file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--since", help="YYYY-MM-DD, inclusive")
        p.add_argument("--until", help="YYYY-MM-DD, exclusive")
        p.add_argument("--domain", help="sender domain(s), comma separated; subdomains count")
        p.add_argument("--sender", help="part of the From line, e.g. a newsletter's name")
        p.add_argument("--limit", type=int, default=60)

    p = sub.add_parser("senders"); common(p)
    p = sub.add_parser("titles"); common(p)
    p = sub.add_parser("search"); common(p)
    p.add_argument("query", nargs="+")
    p.add_argument("--by", choices=["date", "hits"], default="hits",
                   help="hits: messages where the query matches in most places first")
    p.add_argument("--brief", action="store_true", help="no matching line")
    p = sub.add_parser("export", help="one sender's messages as one text, links stripped")
    common(p)
    p.set_defaults(limit=5000)
    p.add_argument("--out", help="write here; put it outside any repository")
    p.add_argument("--max-chars-each", type=int, default=0, help="cut each message (0: whole)")
    p = sub.add_parser("read")
    p.add_argument("mid", nargs="+")
    p.add_argument("--max-chars", type=int, default=14000)
    p.add_argument("--offset", type=int, default=0)

    args = ap.parse_args()
    conn = load_conn()
    cur = conn.cursor()
    cur.execute("SET statement_timeout='90s'")
    {"senders": cmd_senders, "titles": cmd_titles, "search": cmd_search,
     "read": cmd_read, "export": cmd_export}[args.cmd](cur, args)


if __name__ == "__main__":
    main()
