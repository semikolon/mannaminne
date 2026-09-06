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

#: Money and authority. Losing one of these has real consequence given the
#: skuldsanering history, so they are PROTECTED, never proposed for deletion.
#: Substring match against the domain. Verified against the live domain list at
#: the bottom of this file's run — if a term matches nothing, it is reported, so
#: a list that quietly matches zero domains cannot hide.
MONEY_AUTHORITY_TERMS = [
    "kronofogden", "skatteverket", "forsakringskassan", "fk.se",
    "arbetsformedlingen", "katrineholm", "kommun", "bank", "swedbank",
    "nordea", "seb.", "handelsbanken", "lunar", "klarna", "inkasso",
    "lowell", "intrum", "sergel", "svea", "payex", "fortnox", "visma",
    "collectia", "alektum", "kronan", "csn.se", "pensionsmyndigheten",
    "forsakring", "if.se", "folksam", "lansforsakringar", "trygghansa",
    "minpension", "bankid", "swish", "avanza", "nordnet", "bahnhof",
    "telia", "tele2", "vattenfall", "fortum", "e-on.", "eon.se", "hyresgast",
    "advokat", "juridik", "domstol", "polisen", "migrationsverket",
    "1177", "regionsormland", "vardguiden",
]


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


def money_hits(domain):
    return [t for t in MONEY_AUTHORITY_TERMS if t in domain]


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


def gmail(params_json):
    """One gws call. Returns parsed JSON, or None on any failure."""
    try:
        out = subprocess.run(
            ["gws", "gmail", "users", "messages", "list", "--params", params_json],
            capture_output=True, text=True, timeout=60)
        body = "\n".join(l for l in out.stdout.splitlines()
                         if not l.startswith("Using keyring"))
        return json.loads(body)
    except Exception:
        return None


def phase2(dom, top_n):
    """Gmail-side signals for the heaviest domains only.

    Search operators mean a domain costs ~5 calls instead of one per message.
    `resultSizeEstimate` is Gmail's own count for the query.
    """
    ranked = sorted(dom.values(), key=lambda r: -r["messages"])[:top_n]
    print(f"  Gmail-signaler för topp {len(ranked)} domäner...", flush=True)
    for i, rec in enumerate(ranked, 1):
        d = rec["domain"]
        print(f"    [{i}/{len(ranked)}] {d}", flush=True)
        for key, q in (
            ("gmail_total",  f"from:{d}"),
            ("unread",       f"from:{d} is:unread"),
            ("starred",      f"from:{d} is:starred"),
            ("big",          f"from:{d} larger:1M"),
            ("promotions",   f"from:{d} category:promotions"),
        ):
            r = gmail(json.dumps({"userId": "me", "q": q, "maxResults": 1}))
            rec[key] = r.get("resultSizeEstimate", 0) if r else None
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
    """Which verdicts make sense for this domain? Protection narrows, never hides.

    The first version of this sheet put protected domains in their own section at
    the top and left them out of the clusters. That hid the single largest
    sender — nearly half the entire inbox — behind the very guard meant to make
    the sheet trustworthy. Protection is a property of a row, never a reason to
    omit it: the biggest decision is exactly the one most likely to be protected.
    """
    p = protect_reasons(rec)
    if not p:
        return ["radera", "arkivera+radera", "sluta indexera", "behåll"]
    # Skyddad: ingen ren radering föreslås, men arkivera-och-töm förlorar ingenting
    # och frigör precis lika mycket utrymme.
    return ["arkivera+radera", "sluta indexera", "behåll"]


def row(rec):
    bits = []
    if rec["written_to"]:
        bits.append(f"du skrev dit ×{rec['written_to']:,}")
    if money_hits(rec["domain"]):
        bits.append("pengar/myndighet")
    if rec["elsewhere"]:
        bits.append("nämns personligen")
    u = rec.get("unread")
    if u is not None and rec.get("gmail_total"):
        bits.append(f"{100*u/max(1,rec['gmail_total']):.0f} % olästa")
    return "; ".join(bits) or "inga relationssignaler"


def write_sheet(dom, path, quota_used_gb=13.5, quota_limit_gb=16.1):
    ranked = sorted(dom.values(), key=lambda r: -r["messages"])
    total_msgs = sum(r["messages"] for r in dom.values())
    total_pend = sum(r["chunks_pending"] for r in dom.values())

    L = []; A = L.append
    A("# Inkorgsgenomgång — beslutsunderlag")
    A("")
    A(f"Framtaget {date.today().isoformat()}. Lagring **{quota_used_gb} av "
      f"{quota_limit_gb} GB**. {total_msgs:,} meddelanden, {len(dom):,} domäner, "
      f"{total_pend:,} chunks kvar att bädda in.")
    A("")
    A("**Läs inte allt.** De tio första domänerna nedan är tre fjärdedelar av all "
      "post. Du kan sluta när du frigjort tillräckligt.")
    A("")
    A("Skriv ditt ord på **Beslut:**-raden. Vokabulären:")
    A("")
    A("| ord | betyder |")
    A("|---|---|")
    A("| `radera` | bort ur Gmail. Korpusen behåller innehållet — e-post prunas aldrig. |")
    A("| `arkivera+radera` | ladda ner till FERMI först, sedan bort ur Gmail. Förlorar ingenting. |")
    A("| `sluta indexera` | stannar kvar i Gmail, men slutar kosta GPU-tid. |")
    A("| `behåll` | orörd. |")
    A("")
    A("Ingenting här raderar något. Ett andra verktyg läser dina beslut och verkställer.")
    A("")

    A("---")
    A("")
    A("## De tyngsta domänerna — en rad, ett beslut")
    A("")
    run = 0
    A("| # | domän | meddelanden | löpande % | senast | signaler | tillåtna beslut |")
    A("|---:|---|---:|---:|---|---|---|")
    for i, r in enumerate(ranked[:25], 1):
        run += r["messages"]
        A(f"| {i} | `{r['domain']}` | {r['messages']:,} | {100*run/total_msgs:.0f} % "
          f"| {r.get('last_seen') or '–'} | {row(r)} | {' · '.join(verdicts(r))} |")
    A("")
    for i, r in enumerate(ranked[:10], 1):
        A(f"**{i}. `{r['domain']}`** — {r['messages']:,} meddelanden, "
          f"{r['chunks_pending']:,} chunks kvar att bädda "
          f"(~{r['chunks_pending']/10/60:.0f} GPU-minuter), "
          f"{r.get('first_seen') or '?'} till {r.get('last_seen') or '?'}. "
          f"{row(r)}.")
        A("")
        A("  **Beslut:** ")
        A("")

    groups = defaultdict(list)
    for rec in ranked[25:]:
        groups[bucket(rec)].append(rec)
    for name in ORDER:
        rows = groups.get(name, [])
        if not rows:
            continue
        msgs = sum(r["messages"] for r in rows)
        pend = sum(r["chunks_pending"] for r in rows)
        A("---")
        A("")
        A(f"## {name} — {len(rows):,} domäner, {msgs:,} meddelanden "
          f"({100*msgs/total_msgs:.0f} % av posten)")
        A("")
        A(f"Frigör i indexkön: **{pend:,} chunks**, ~{pend/10/3600:.1f} GPU-timmar.")
        A("")
        A("| domän | medd. | kvar att bädda | senast | signaler |")
        A("|---|---:|---:|---|---|")
        for r in rows[:20]:
            A(f"| `{r['domain']}` | {r['messages']:,} | {r['chunks_pending']:,} "
              f"| {r.get('last_seen') or '–'} | {row(r)} |")
        if len(rows) > 20:
            A(f"| *(+{len(rows)-20:,} till)* | | | | |")
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
    unmatched = [t for t in MONEY_AUTHORITY_TERMS
                 if not any(t in d for d in dom)]
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
