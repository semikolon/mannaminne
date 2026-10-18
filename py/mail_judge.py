"""A second reader for the mail no sender rule places.

`email_classes.txt` sorts mail by sender domain, which is free and exact, and
covers the large senders. What it leaves is a long tail: thousands of domains
nobody will ever write a rule for, most of them shops and newsletters. Measured
2026-10-10, that tail held 86 % of the text still waiting to be embedded.

For that tail only, a decision model reads the sender and the subject and picks
one of three: a person, a system reporting on the owner's own account, or bulk.
Bulk leaves the embedding queue (keyword search still covers every word of it).
The other two stay in.

Rules that keep this honest:

- The sender rules run first and always win. This step only ever touches a mail
  whose class is still `unknown`, and a rule written later overrules what was
  set here, because the rule pass runs on every embed.
- Below MIN_PROB the mail stays `unknown`. The guess and its probability are
  kept beside it (`jev_class`, `jev_prob`), so an unchecked guess never reads
  as a fact, and nothing is asked twice.
- `set_by` says who set the class: `rule` or `jev`.
- The model lives outside this package (`~/.claude/hooks/decision_client.py`, or
  the file MANNAMINNE_DECISION_CLIENT names). Without it, without a key, or with
  an empty account, the step does nothing and says so. MANNAMINNE_MAIL_JUDGE=0
  switches it off.
- Only headers leave the machine: subject, From, To, Date.
- Mail from a protected sender (the authorities, collectors and banks named in the
  owner's own `inbox_protect_labels.txt`) is never asked about and never classed
  here. What must never be proposed for deletion must not drop out of search by
  meaning on a model's word either.

Measured on 59 hand-labelled mails drawn from that tail (2026-10-10): all 41 it
called bulk at 0.8 or above were bulk, both personal mails were called personal,
and its two confident errors called network notices `machine`, which only keeps
them in the queue. One call cost $0.00003.
"""
import importlib.util
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

MIN_PROB = 0.8
#: The model's three answers, and the class each becomes in email_class.
CLASS_OF = {"person": "person", "machine": "account", "bulk": "bulk"}
ENV_CLIENT = "MANNAMINNE_DECISION_CLIENT"
ENV_SWITCH = "MANNAMINNE_MAIL_JUDGE"
BATCH = 240

#: The question exactly as it was measured. The examples are invented; none is
#: taken from the mail it was tested on.
QUESTION = {
    "type": "choice",
    "instructions": {
        "question": "What kind of email is this, judged from `email.subject` and `email.from`?",
        "focus": ("Who wrote it and for whom: one person writing to the recipient, a system reporting on "
                  "the recipient's own account, or a sender addressing many subscribers. Subjects may be "
                  "in Swedish."),
    },
    "criteria": {
        "person": {
            "what": "A named individual wrote this message to the recipient or a small group, often a reply.",
            "not_for": "Newsletters signed with a first name; notifications a system sends on someone's behalf.",
            "examples": ["Re: lunch on Thursday, from Anna Berg <anna.berg@firma.se>",
                         "SV: Fråga om leverans, from a named member of staff"],
        },
        "machine": {
            "what": ("A system generated it about the recipient's own account, order, invoice, ticket, login "
                     "or monitored system."),
            "not_for": "Offers and news sent to all customers.",
            "examples": ["Your order has shipped", "Din faktura för mars", "Password changed",
                         "Build failed on main", "Ticket assigned to you"],
        },
        "bulk": {
            "what": "A newsletter, digest, campaign, offer or event announcement sent to many subscribers.",
            "not_for": "Messages about this recipient's own order, invoice or account.",
            "examples": ["Spring sale starts today", "This week in tech", "Join our open evening",
                         "Nyhetsbrev april"],
        },
    },
}


def headers_of(text, title=""):
    """The four header lines of a stored mail's first piece. The subject is the
    stored title when the text carries no Subject: line, which is the usual case."""
    out = {}
    for key in ("From", "To", "Subject", "Date"):
        m = re.search(rf"^{key}:\s*(.*)$", text or "", flags=re.M)
        out[key.lower()] = (m.group(1).strip() if m else "")[:200]
    if not out["subject"]:
        out["subject"] = (title or "")[:200]
    return out


def state_of(headers):
    return json.dumps({"email": {"subject": headers.get("subject", ""), "from": headers.get("from", ""),
                                 "to": headers.get("to", ""), "date": headers.get("date", "")}},
                      ensure_ascii=False)


def verdict(answer):
    """(model's choice, its probability) from one answer, or None when there is none."""
    if not isinstance(answer, dict) or answer.get("choice") not in CLASS_OF:
        return None
    prob = (answer.get("probabilities") or {}).get(answer["choice"], answer.get("confidence"))
    if not isinstance(prob, (int, float)):
        return None
    return answer["choice"], float(prob)


def class_for(choice, prob):
    """The class a verdict earns, or None when it is too unsure to be one."""
    return CLASS_OF[choice] if prob >= MIN_PROB else None


OWN_KEY = "MANNAMINNE_OPENROUTER_API_KEY"
SHARED_KEY = "OPENROUTER_API_KEY"
_TIER_DIR = Path.home() / ".secrets"


def _tier_value(name):
    """One value from the tier files a login shell sources but a launchd job may
    not have. Read into this process only, never printed."""
    for tier in ("tier-mac.env", "tier-all.env"):
        try:
            for line in (_TIER_DIR / tier).read_text(encoding="utf-8").splitlines():
                m = re.match(r"\s*(?:export\s+)?" + re.escape(name) + r"=(.*)", line)
                if m and m.group(1).strip().strip("'\""):
                    return m.group(1).strip().strip("'\"")
        except OSError:
            continue
    return None


def _load_key():
    """mannaminne's own key first (MANNAMINNE_OPENROUTER_API_KEY, env or tier file),
    else the shared OPENROUTER_API_KEY. Fredrik 2026-10-18: one OpenRouter key per
    project, so the dashboard says who spent what. The chosen value is placed in
    OPENROUTER_API_KEY, which decision_client.py reads."""
    own = os.environ.get(OWN_KEY) or _tier_value(OWN_KEY)
    if own:
        os.environ[SHARED_KEY] = own
        return
    if os.environ.get(SHARED_KEY):
        return
    shared = _tier_value(SHARED_KEY)
    if shared:
        os.environ[SHARED_KEY] = shared


def load_client():
    """The decision client module, or None when this machine has none."""
    if os.environ.get(ENV_SWITCH, "").strip().lower() in ("0", "false", "no", "off"):
        return None
    path = Path(os.environ.get(ENV_CLIENT) or Path.home() / ".claude" / "hooks" / "decision_client.py")
    if not path.is_file():
        return None
    _load_key()
    if not os.environ.get("OPENROUTER_API_KEY"):
        return None
    try:
        spec = importlib.util.spec_from_file_location("mannaminne_decision_client", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


def ask(client, states, workers=8, timeout=20.0):
    """One (verdict, cost) per state, verdict None where the model gave no answer.
    One mail per request: several mails in one request change each other's answers."""
    def one(state):
        try:
            out = client.decide(state, {"kind": QUESTION}, timeout=timeout)
        except Exception:
            return None, 0.0
        if not out:
            return None, 0.0
        return verdict((out.get("answers") or {}).get("kind")), float(out.get("cost_usd") or 0.0)
    with ThreadPoolExecutor(max(1, min(workers, len(states) or 1))) as pool:
        return list(pool.map(one, states))


def ensure_columns(cur):
    """Adds the three columns once. Looks first, because adding a column takes a
    lock on the whole table and this is called at the start of every embed run."""
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='email_class'")
    have = {r[0] for r in cur.fetchall()}
    for name, kind in (("set_by", "text"), ("jev_class", "text"), ("jev_prob", "real")):
        if name not in have:
            cur.execute("SET LOCAL lock_timeout = '15s'")
            cur.execute(f"ALTER TABLE email_class ADD COLUMN IF NOT EXISTS {name} {kind}")


def protected_labels():
    """Domain labels of senders that are never classed here: the owner's protect
    list. No list on this machine means nothing is protected, which is right for
    a machine that has no such mail and no such file."""
    try:
        from inbox_inventory import money_authority_labels
        return sorted(money_authority_labels())
    except (SystemExit, Exception):
        return []


#: A sender is protected when one part of its domain is a protected label:
#: `mail.examplebank.se` has the part `examplebank`. Whole parts only, never substrings.
PROTECTED_SQL = "string_to_array(lower(coalesce(e.domain, '')), '.') && %s::text[]"

#: Mail that is still unplaced, has never been asked about, is not from a protected
#: sender, and has text waiting to be embedded. Mail whose text is already embedded
#: gains nothing from a class.
WAITING_SQL = """
    FROM chunks c JOIN email_class e ON e.mid = c.source_id
    WHERE c.source_kind='email' AND c.chunk_idx=0
      AND e.class='unknown' AND e.jev_class IS NULL
      AND NOT (""" + PROTECTED_SQL + """)
      AND EXISTS (SELECT 1 FROM chunks p
                  WHERE p.source_id = c.source_id AND p.source_kind='email' AND p.embedding IS NULL)"""


def count_waiting(cur, protected=()):
    cur.execute("SELECT count(*)" + WAITING_SQL, (list(protected),))
    return cur.fetchone()[0]


def fetch_waiting(cur, limit, skip=(), protected=()):
    """`skip`: mail this run already asked about without an answer, so one mail the
    model refuses cannot be fetched again and again in place of the rest."""
    cur.execute("SELECT c.source_id, c.title, left(c.text, 700)" + WAITING_SQL
                + " AND c.source_id <> ALL(%s) LIMIT %s", (list(protected), list(skip), limit))
    return cur.fetchall()


def release_protected(cur, protected):
    """Take back any class this step ever gave a protected sender's mail. Runs at the
    start of every run, so a label added to the protect list later reaches mail
    that was classed before it. The guess stays on record; the class does not."""
    if not protected:
        return 0
    cur.execute("UPDATE email_class e SET class='unknown', set_by=NULL WHERE e.set_by='jev' AND "
                + PROTECTED_SQL, (list(protected),))
    return cur.rowcount


def store(cur, rows):
    """rows: (mid, model's choice, probability). The class changes only where the
    mail is still unplaced and the verdict is sure enough; the guess is kept either way."""
    for mid, choice, prob in rows:
        cls = class_for(choice, prob)
        cur.execute("""UPDATE email_class
                       SET jev_class=%s, jev_prob=%s,
                           class  = CASE WHEN %s::text IS NOT NULL AND class='unknown' THEN %s ELSE class END,
                           set_by = CASE WHEN %s::text IS NOT NULL AND class='unknown' THEN 'jev' ELSE set_by END
                       WHERE mid=%s""", (CLASS_OF[choice], prob, cls, cls, cls, mid))


def run(conn, limit=0, workers=8, max_usd=0.25, dry_run=False, say=print):
    """Ask about waiting mail until there is none, the limit or the budget is
    reached, or the model stops answering. Each batch is committed, so a run that
    is stopped loses nothing and the next one continues."""
    cur = conn.cursor()
    ensure_columns(cur)
    protected = protected_labels()
    released = release_protected(cur, protected)
    conn.commit()
    if released:
        say(f"mail judge: {released} mail(s) from protected senders put back as unplaced")
    total = count_waiting(cur, protected)
    conn.commit()
    summary = {"waiting": total, "asked": 0, "answered": 0, "classed": {}, "unsure": 0, "usd": 0.0, "stopped": ""}
    if not total:
        say("mail judge: nothing waiting")
        return summary
    client = load_client()
    if client is None:
        summary["stopped"] = "no decision client, no key, or switched off"
        say(f"mail judge: {total} mail(s) waiting, not asked: {summary['stopped']}")
        return summary
    goal = min(total, limit) if limit else total
    say(f"mail judge: {total} mail(s) waiting, asking about {goal} (budget ${max_usd:.2f})")
    start = time.monotonic()
    unanswered = set()
    while summary["asked"] < goal:
        rows = fetch_waiting(cur, min(BATCH, goal - summary["asked"]), skip=unanswered, protected=protected)
        conn.commit()
        if not rows:
            break
        answers = ask(client, [state_of(headers_of(text, title)) for _, title, text in rows], workers=workers)
        got = [(mid, v[0], v[1]) for (mid, _, _), (v, _) in zip(rows, answers) if v]
        unanswered.update(mid for (mid, _, _), (v, _) in zip(rows, answers) if not v)
        summary["asked"] += len(rows)
        summary["answered"] += len(got)
        summary["usd"] += sum(cost for _, cost in answers)
        for _, choice, prob in got:
            cls = class_for(choice, prob)
            if cls:
                summary["classed"][cls] = summary["classed"].get(cls, 0) + 1
            else:
                summary["unsure"] += 1
        if not dry_run:
            store(cur, got)
            conn.commit()
        if not got:
            summary["stopped"] = "the model gave no answer for a whole batch (empty account, or no network)"
            break
        if summary["usd"] >= max_usd:
            summary["stopped"] = f"budget of ${max_usd:.2f} reached"
            break
        if dry_run:
            summary["stopped"] = "dry run: one batch asked, nothing stored"
            break
        if summary["asked"] % (BATCH * 10) == 0:
            say(f"  {summary['asked']}/{goal} asked, {summary['classed']}, ${summary['usd']:.2f}, "
                f"{time.monotonic() - start:.0f}s")
    say(f"mail judge: asked {summary['asked']}, answered {summary['answered']}, classed {summary['classed']}, "
        f"left unplaced as unsure {summary['unsure']}, ${summary['usd']:.3f}"
        + (f"; stopped: {summary['stopped']}" if summary["stopped"] else ""))
    return summary
