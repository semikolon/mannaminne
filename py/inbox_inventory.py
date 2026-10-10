#!/usr/bin/env python3
"""Per-sender-domain fact table for the inbox cleanup + indexing decision.

ONE judgment serves both decisions. Deciding "delete this sender" answers the
indexing question too; deciding "index this sender" answers only one. So the
review sheet is written in the deletion frame, and the index config falls out of
the same verdicts (a second tool, not this one).

READ-ONLY. This script deletes nothing, writes nothing to Gmail, and touches no
mannaminne table. It reads and it reports.

**The output is bulk third-party data and never goes to a git remote.** A list of
3,381 sender domains with volumes is exactly the counterparty aggregate the
push rules exclude, and the mannaminne remote is public. Code here, output to
~/Documents/inbox-inventory/ which is outside every repo.

Two phases, split by what things cost:

  Phase 1 (free, every domain)   — Postgres only. Counts, date span, chunk
                                   weight, current class, whether Fredrik ever
                                   wrote TO the domain, cross-source presence.
  Phase 2 (--gmail, top N only)  — Gmail API. Bytes, unread ratio, category,
                                   starred. Uses search operators so a domain
                                   costs a handful of calls, not one per message.

Phase 2 is opt-in because the free half already answers most of it, and 78 % of
all mail sits in the top 50 domains.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mannaminne import load_conn  # noqa: E402

OUT_DIR = Path.home() / "Documents" / "inbox-inventory"

#: The domain regex is COPIED from mannaminne._ensure_email_class, which is the
#: proven one. A fresh attempt at the same job returned "fredrik@a" — the domain
#: truncated to one character — and looked plausible enough to nearly ship.
DOMAIN_FROM = r"From:[^@\n]*@([A-Za-z0-9._-]+)"
DOMAIN_TO = r"To:[^@\n]*@([A-Za-z0-9._-]+)"

#: Hur många omnämnanden i en personlig källa som krävs för att räknas som
#: närvaro. MÄTT, inte gissat. At >=1 the signal matched 18 413 hostnames and
#: three quarters of all mail, which is noise wearing a signal's clothes. At >=20
#: it matches 602, and the cut lands where it should: newsletter domains mentioned
#: exactly once fall out, while employers, personal domains and tools discussed
#: dozens to hundreds of times stay. Re-measure before changing it.
MENTION_MIN = 20

#: Fredrik's own ADDRESSES, not his domains. Measured from the From: lines that
#: carry his name. The domain version was tried first and overcounted "he wrote
#: to this domain" by more than tenfold — 137 455 against the 11 748 messages he
#: has actually sent, because a webmail or workplace domain is shared with
#: thousands of other people, so every stranger's mail counted as his.
#: Read from a local file, never hard-coded: `semikolon/mannaminne` is a PUBLIC
#: remote and a person's own email addresses have no business in it. One address
#: per line. Absent file = the reply signal is simply unavailable, and the run
#: says so rather than silently reporting "you never wrote to anyone".
OWN_ADDRESSES_FILE = os.path.expanduser("~/.config/mannaminne/own_addresses.txt")


def own_addresses() -> set[str]:
    try:
        with open(OWN_ADDRESSES_FILE, encoding="utf-8") as fh:
            return {l.strip().lower() for l in fh
                    if l.strip() and not l.startswith("#")}
    except FileNotFoundError:
        return set()

#: Money and authority. Losing one of these has real consequence, so they are
#: PROTECTED and never proposed for deletion.
#:
#: Which authorities, collectors and banks write to a person is that person's
#: own business, and this is a PUBLIC repository, so the list is read from a
#: local file: one domain label per line. It should be DERIVED FROM THE ACTUAL
#: DOMAIN LIST, not imagined. The first version here was written from memory and
#: 19 of its terms matched zero domains: a trigger list is a fixture, and one
#: written from imagination catches imagined cases. `main()` reports every label
#: that matches no domain.
#:
#: Matching is by DOMAIN LABEL, never raw substring. A short term matched as a
#: substring also hits every unrelated domain that happens to contain those
#: letters ("eon" inside "moveon"). Label matching removes that whole class
#: structurally instead of curating around each case.
#:
#: The critical entries are TINY by volume, a handful of messages each, which is
#: exactly why a volume ranking would never surface them and a protect list has
#: to exist.
PROTECT_LABELS_FILE = os.path.expanduser("~/.config/mannaminne/inbox_protect_labels.txt")
_protect_labels_cache: dict = {}


def money_authority_labels(path=None) -> frozenset:
    """The protect list. A missing or empty file is refused, never read as
    "nothing to protect": a protect list that silently holds nothing looks, in
    every table this produces, exactly like one that works."""
    path = path or PROTECT_LABELS_FILE
    if path not in _protect_labels_cache:
        try:
            with open(path, encoding="utf-8") as fh:
                labels = frozenset(l.strip().lower() for l in fh
                                   if l.strip() and not l.lstrip().startswith("#"))
        except FileNotFoundError:
            labels = frozenset()
        if not labels:
            raise SystemExit(
                f"no protect list at {path}: refusing to rank mail without it.\n"
                "Write one domain label per line (the authorities, collectors and "
                "banks whose mail must never be proposed for deletion).")
        _protect_labels_cache[path] = labels
    return _protect_labels_cache[path]


def money_hits(domain, labels=None):
    """Label-aware match. `eon.se` hits an `eon` term; `moveon.org` does not."""
    own = set(domain.lower().split("."))
    return sorted(own & (money_authority_labels() if labels is None else set(labels)))


def norm_date(s):
    """The created column is text and sometimes empty. Return None, not a crash."""
    if not s or not isinstance(s, str) or len(s) < 4:
        return None
    return s[:10]


def phase1(conn):
    """Every domain, from Postgres. No network, no Gmail."""
    cur = conn.cursor()

    print("  domänaggregat...", flush=True)
    cur.execute(rf"""
        SELECT lower(substring(c.text from '{DOMAIN_FROM}')) AS domain,
               count(*)                                       AS messages,
               min(nullif(c.created,''))                      AS first_seen,
               max(nullif(c.created,''))                      AS last_seen
        FROM chunks c
        WHERE c.source_kind='email' AND c.chunk_idx=0
        GROUP BY 1
    """)
    dom = {}
    for d, n, first, last in cur.fetchall():
        if not d:
            continue
        dom[d] = {"domain": d, "messages": n,
                  "first_seen": norm_date(first), "last_seen": norm_date(last)}

    print("  chunkvikt per domän (indexkostnaden)...", flush=True)
    cur.execute(r"""
        SELECT e.domain,
               count(*)                                        AS chunks,
               count(*) FILTER (WHERE c.embedding IS NULL)      AS chunks_pending
        FROM chunks c JOIN email_class e ON e.mid = split_part(c.id,'#',1)
        WHERE c.source_kind='email' AND e.domain IS NOT NULL
        GROUP BY 1
    """)
    for d, ch, pend in cur.fetchall():
        dom.setdefault(d, {"domain": d, "messages": 0,
                           "first_seen": None, "last_seen": None})
        dom[d]["chunks"] = ch
        dom[d]["chunks_pending"] = pend

    print("  nuvarande klass...", flush=True)
    cur.execute("SELECT domain, class, count(*) FROM email_class "
                "WHERE domain IS NOT NULL GROUP BY 1,2")
    cls = defaultdict(dict)
    for d, c_, n in cur.fetchall():
        cls[d][c_] = n
    for d, m in cls.items():
        if d in dom:
            dom[d]["classes"] = m

    # Har han någonsin SKRIVIT till domänen? Den starkaste relationssignalen.
    print("  har han skrivit till domänen...", flush=True)
    # Adressmatchning på From:-RADEN (fram till radbrytningen), inte på hela
    # rubriken — ett jokertecken som spänner över To: gör mottagna mejl till skickade.
    addrs = own_addresses()
    if not addrs:
        print(f"    INGEN adresslista ({OWN_ADDRESSES_FILE}) — "
              f"relationssignalen 'du skrev dit' är AVSTÄNGD, inte noll", flush=True)
    own = " OR ".join([r"c.text ~* 'From:[^\n]*" + a.replace(".", "[.]") + "'"
                       for a in addrs])
    rows = []
    if addrs:
        cur.execute(rf"""
            SELECT lower(substring(c.text from '{DOMAIN_TO}')) AS to_domain, count(*)
            FROM chunks c
            WHERE c.source_kind='email' AND c.chunk_idx=0 AND ({own})
            GROUP BY 1
        """)
        rows = cur.fetchall()
    for d, n in rows:
        if d and d in dom:
            dom[d]["written_to"] = n

    # Förekommer domänen i en PERSONLIG källa? Första försöket svepte alla
    # icke-e-post-källor och träffade 631 domäner med 77 % av posten — varje känt
    # värdnamn ligger i någon skärmdump eller något kodblock, så signalen var brus.
    # Messenger, anteckningar, Things3 och AI-chattar är källor där en domän dyker
    # upp för att den betyder något för honom.
    # ETT svep över de icke-e-post-chunksen som drar ut värdnamn, sedan en join.
    # Den uppenbara formen — ett korrelerat EXISTS med ILIKE per domän — hade
    # blivit 3 381 fulla svep över samma 419 000 rader. Ett svep, inte tusentals.
    print("  korsbelägg mot andra källor (ett svep)...", flush=True)
    cur.execute(f"""
        CREATE TEMP TABLE _hosts AS
        SELECT lower(h[1]) AS host, count(*) AS n
        FROM chunks c,
             LATERAL regexp_matches(c.text, '([A-Za-z0-9][A-Za-z0-9.-]*\\.[A-Za-z]{2,})', 'g') h
        WHERE c.source_kind IN ('messenger','note','things3','aichat')
        GROUP BY 1 HAVING count(*) >= {MENTION_MIN}
    """)
    cur.execute("CREATE INDEX ON _hosts (host)")
    cur.execute("SELECT host FROM _hosts")
    hosts = {r[0] for r in cur.fetchall()}
    for d in dom:
        if d in hosts:
            dom[d]["elsewhere"] = True

    for d in dom.values():
        d.setdefault("chunks", 0)
        d.setdefault("chunks_pending", 0)
        d.setdefault("written_to", 0)
        d.setdefault("elsewhere", False)
        d.setdefault("classes", {})
    return dom


def protect_reasons(rec):
    """Positive guard. Anything here is never proposed for deletion.

    Deliberately positive rather than a negative junk-heuristic: the last time a
    negative one was used to judge worthlessness ("has an unsubscribe link") it
    condemned a webmail provider, an employer and a community forum.
    """
    r = []
    if rec["written_to"]:
        r.append(f"du har skrivit dit ({rec['written_to']} ggr)")
    if money_hits(rec["domain"]):
        r.append("pengar/myndighet: " + ", ".join(money_hits(rec["domain"])))
    if rec["elsewhere"]:
        r.append("förekommer i annan källa")
    if any(rec["domain"] == a.split("@")[-1] for a in own_addresses()):
        r.append("din egen domän")
    if rec.get("starred"):
        r.append(f"stjärnmärkt ({rec['starred']} st)")
    if rec.get("labelled"):
        r.append("under egen etikett")
    return r


def gmail_count(q, cap=2000):
    """Real paginated count. `resultSizeEstimate` is NOT usable: it returned
    exactly 201 for `from:<domain>`, `to:<domain>` AND the bare name, while the
    true paginated count was 95. An estimate that returns the same number for
    three different questions is a cap, not an answer.

    Stops at `cap` and says so, since the ranking only needs to know "big".
    """
    tok, n = None, 0
    while n < cap:
        params = {"userId": "me", "q": q, "maxResults": 500}
        if tok:
            params["pageToken"] = tok
        try:
            out = subprocess.run(
                ["gws", "gmail", "users", "messages", "list", "--params",
                 json.dumps(params)], capture_output=True, text=True, timeout=90)
            d = json.loads("\n".join(l for l in out.stdout.splitlines()
                                     if not l.startswith("Using keyring")))
        except Exception:
            return None
        n += len(d.get("messages", []))
        tok = d.get("nextPageToken")
        if not tok:
            return n
    return cap  # "minst så här många"


def phase2(dom, top_n):
    """Gmail-side counts for the domains that could matter to the QUOTA.

    Selection is deliberately not "biggest in the corpus": the corpus carries a
    4.7 GB offline mbox archive whose largest sender has 140 572 messages there
    and 95 in Gmail. Ranking the deletion decision by corpus volume would put a
    domain that frees nothing at the top of the page. So: the biggest by corpus
    (they still need an INDEX verdict) plus everything still active recently
    (they are what actually occupies the account).
    """
    by_size = sorted(dom.values(), key=lambda r: -r["messages"])[:top_n]
    recent = [r for r in dom.values()
              if (r.get("last_seen") or "") >= "2024-01-01" and r["messages"] >= 30]
    picked, seen = [], set()
    for r in by_size + sorted(recent, key=lambda r: -r["messages"]):
        if r["domain"] not in seen:
            seen.add(r["domain"])
            picked.append(r)

    print(f"  Gmail-antal för {len(picked)} domäner (sidräknat, inte uppskattat)...",
          flush=True)
    for i, rec in enumerate(picked, 1):
        d = rec["domain"]
        rec["in_gmail"] = gmail_count(f"from:{d}", cap=40000)
        rec["gmail_big"] = gmail_count(f"from:{d} larger:1M", cap=5000)
        rec["unread"] = gmail_count(f"from:{d} is:unread", cap=40000)
        rec["starred"] = gmail_count(f"from:{d} is:starred", cap=2000)
        print(f"    [{i}/{len(picked)}] {d:<34} Gmail={rec['in_gmail']} "
              f"(korpus {rec['messages']:,})", flush=True)
    return dom


def bucket(rec):
    """Which cluster does this domain fall in? Protection wins over everything."""
    if protect_reasons(rec):
        return "skyddad"
    cls = rec.get("classes", {})
    if cls.get("telemetry"):
        return "redan telemetri"
    if cls.get("reading"):
        return "redan läsande"
    last = rec.get("last_seen") or ""
    dead = last < "2023-01-01" if last else False
    unread = rec.get("unread")
    total = rec.get("gmail_total") or rec["messages"]
    unread_ratio = (unread / total) if (unread is not None and total) else None
    if dead and rec["messages"] >= 50:
        return "död sedan länge"
    if unread_ratio is not None and unread_ratio > 0.9 and rec["messages"] >= 50:
        return "aldrig öppnad"
    if rec.get("promotions") and rec["messages"] >= 50:
        return "reklam enligt Gmail"
    if rec["messages"] >= 200:
        return "stor, oklassad"
    return "svans"


ORDER = ["död sedan länge", "aldrig öppnad", "reklam enligt Gmail",
         "stor, oklassad", "redan läsande", "redan telemetri", "svans", "skyddad"]


def verdicts(rec):
    """Which verdicts make sense here? Protection narrows; it never hides a row.

    An early version put protected domains in their own section and left them out
    of the clusters, which hid the single largest sender behind the very guard
    meant to make the sheet trustworthy. The biggest decision is exactly the one
    most likely to be protected.
    """
    p = protect_reasons(rec)
    base = ["arkivera+radera", "sluta indexera", "behåll"]
    return base if p else ["radera"] + base


def sig(rec):
    bits = []
    if rec["written_to"]:
        bits.append(f"du skrev dit ×{rec['written_to']:,}")
    if money_hits(rec["domain"]):
        bits.append("pengar/myndighet")
    if rec["elsewhere"]:
        bits.append("nämns personligen")
    # Andelen olästa räknas BARA när båda talen är riktiga. Med taken 2 000 och
    # 500 gav sju olika domäner exakt "25 % olästa" — kvoten mellan två tak, som
    # såg ut som en mätning och var en artefakt av mina egna gränser.
    g, u = rec.get("in_gmail"), rec.get("unread")
    if g and u is not None and g < 40000 and u < 40000:
        bits.append(f"{100*min(u,g)/g:.0f} % olästa")
    elif u is not None:
        bits.append(f"{u:,}+ olästa")
    if rec.get("gmail_big"):
        bits.append(f"{rec['gmail_big']} st >1 MB")
    return "; ".join(bits) or "inga relationssignaler"


def gm(rec):
    v = rec.get("in_gmail")
    if v is None:
        return "?"
    return f"{v:,}+" if v >= 2000 else f"{v:,}"


def write_sheet(dom, path, quota_used_gb=13.5, quota_limit_gb=16.1):
    vals = list(dom.values())
    total_msgs = sum(r["messages"] for r in vals)
    total_pend = sum(r["chunks_pending"] for r in vals)
    known = [r for r in vals if r.get("in_gmail") is not None]

    L = []; A = L.append
    A("# Inkorgsgenomgång — beslutsunderlag")
    A("")
    A(f"Framtaget {date.today().isoformat()}. Gmail: **{quota_used_gb} av "
      f"{quota_limit_gb} GB**. Korpusen: {total_msgs:,} meddelanden, "
      f"{len(dom):,} domäner, {total_pend:,} chunks kvar att bädda in.")
    A("")
    A("## Läs det här först, annars blir listan missvisande")
    A("")
    A("**Korpusen och Gmail är inte samma sak.** mannaminne har ätit ett 4,7 GB "
      "mbox-arkiv från FERMI vid sidan av det levande kontot. Den största "
      "avsändaren i korpusen har **140 572 meddelanden i arkivet och 95 i Gmail** "
      "— att radera den ur Gmail skulle frigöra ingenting alls.")
    A("")
    A("Därför står besluten i två separata listor, och de rangordnas olika:")
    A("")
    A("| lista | rangordnas efter | svarar på |")
    A("|---|---|---|")
    A("| **Radering** | antal i Gmail | vad som frigör lagring |")
    A("| **Indexering** | chunks kvar att bädda | vad som kostar GPU-tid |")
    A("")
    A("Vokabulär: `radera` · `arkivera+radera` (ladda ner till FERMI först, "
      "förlorar ingenting) · `sluta indexera` · `behåll`.")
    A("")
    A("Ingenting här raderar något. Ett andra verktyg läser dina beslut och verkställer.")
    A("")

    A("---")
    A("")
    A("## Radering — det som faktiskt ligger i Gmail")
    A("")
    if not known:
        A("*Kör med `--gmail` för att fylla den här listan.*")
        A("")
    else:
        ranked = sorted(known, key=lambda r: -(r.get("in_gmail") or 0))
        A("| # | domän | i Gmail | i korpus | senast | signaler | tillåtna beslut |")
        A("|---:|---|---:|---:|---|---|---|")
        for i, r in enumerate(ranked[:30], 1):
            A(f"| {i} | `{r['domain']}` | {gm(r)} | {r['messages']:,} "
              f"| {r.get('last_seen') or '–'} | {sig(r)} | {' · '.join(verdicts(r))} |")
        A("")
        for i, r in enumerate(ranked[:12], 1):
            A(f"**{i}. `{r['domain']}`** — {gm(r)} i Gmail, {r['messages']:,} i korpus. "
              f"{sig(r)}.")
            A("")
            A("  **Beslut:** ")
            A("")

    A("---")
    A("")
    A("## Indexering — det som kostar GPU-tid")
    A("")
    A("Oberoende av raderingen ovan: även post som inte finns kvar i Gmail "
      "ligger i korpusen och väntar på att bäddas in.")
    A("")
    by_pend = sorted(vals, key=lambda r: -r["chunks_pending"])[:25]
    A("| domän | kvar att bädda | GPU-tid | i Gmail | senast | signaler |")
    A("|---|---:|---:|---:|---|---|")
    for r in by_pend:
        A(f"| `{r['domain']}` | {r['chunks_pending']:,} "
          f"| {r['chunks_pending']/10/3600:.1f} h | {gm(r)} "
          f"| {r.get('last_seen') or '–'} | {sig(r)} |")
    A("")
    A("**Beslut om indexering (en rad räcker, t.ex. \"sluta indexera 1-8\"):** ")
    A("")

    groups = defaultdict(list)
    for rec in vals:
        groups[bucket(rec)].append(rec)
    for name in ORDER:
        rows = sorted(groups.get(name, []), key=lambda r: -r["messages"])
        if not rows or name == "skyddad":
            continue
        msgs = sum(r["messages"] for r in rows)
        pend = sum(r["chunks_pending"] for r in rows)
        A("---")
        A("")
        A(f"## {name} — {len(rows):,} domäner, {msgs:,} i korpus "
          f"({100*msgs/total_msgs:.0f} %)")
        A("")
        A(f"Frigör i indexkön: **{pend:,} chunks**, ~{pend/10/3600:.1f} GPU-timmar.")
        A("")
        A("| domän | i korpus | kvar att bädda | i Gmail | senast |")
        A("|---|---:|---:|---:|---|")
        for r in rows[:15]:
            A(f"| `{r['domain']}` | {r['messages']:,} | {r['chunks_pending']:,} "
              f"| {gm(r)} | {r.get('last_seen') or '–'} |")
        if len(rows) > 15:
            A(f"| *(+{len(rows)-15:,} till)* | | | | |")
        A("")
        A("**Beslut för hela klustret:** ")
        A("")

    path.write_text("\n".join(L), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gmail", action="store_true",
                    help="hämta även Gmail-signaler för de tyngsta domänerna")
    ap.add_argument("--top", type=int, default=50)
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("Fas 1 — Postgres, alla domäner")
    conn = load_conn()
    dom = phase1(conn)
    conn.rollback()
    print(f"  {len(dom):,} domäner")

    # En trigger-lista är ett fixture: den måste prövas mot verkligheten, annars
    # fångar den det inbillade och missar det verkliga. Rapportera nollträffarna.
    unmatched = sorted(money_authority_labels() -
                       {l for d in dom for l in d.split(".")})
    if unmatched:
        print(f"  pengar/myndighet-termer utan träff ({len(unmatched)}): "
              f"{', '.join(unmatched)}")

    if args.gmail:
        print("Fas 2 — Gmail")
        dom = phase2(dom, args.top)

    raw = OUT_DIR / "domains.json"
    raw.write_text(json.dumps(list(dom.values()), ensure_ascii=False, indent=1),
                   encoding="utf-8")
    sheet = OUT_DIR / "genomgang.md"
    write_sheet(dom, sheet)
    print(f"\nRådata  : {raw}")
    print(f"Genomgång: {sheet}")


if __name__ == "__main__":
    main()
