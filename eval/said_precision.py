"""How much of a `--said` search's top results is what was asked for: a precision check on his own prompts.

Built 2026-10-25 after a search for his walkthrough requests came back mostly noise (half the top hits
matched only on common words like *make* and *walk*). The ground truth is a pattern over the whole
chunk, so it needs no labelling: a chunk is relevant to a walkthrough question when it mentions a
walkthrough. The questions are the ones a session asks when it predicts what he would say, worded as
a session words them (some as questions, one as the bag of words that produced the noise).

    py/.venv/bin/python eval/said_precision.py            # precision@20 and duplicate count per question
    py/.venv/bin/python eval/said_precision.py --show 2   # also print the top hits of question 2

What it cannot show: whether a relevant chunk ranks above another relevant one, or recall over his
whole record (the pattern defines relevance by one word, so a request worded without it counts as noise).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "py"))
import mannaminne as m  # noqa: E402

WALK = re.compile(r"walk[- ]?through|for my walk|on my walk|audio (walkthrough|piece)", re.I)
QUESTIONS = [
    "when did Fredrik ask for an audio walkthrough to listen to on a walk",
    "make me an audio walkthrough",
    "walkthrough audio listening walk make a walkthrough",
    "what does he want audio walkthroughs to sound like",
    "his feedback after listening to a walkthrough",
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", type=int, default=20)
    ap.add_argument("--show", type=int, default=0, help="print the top hits of question N (1-based)")
    args = ap.parse_args()
    conn = m.load_conn()
    cur = conn.cursor()
    total_p = 0.0
    for i, q in enumerate(QUESTIONS, 1):
        ranked = m.search_results(q, scope=["said"], limit=args.k, conn=conn)
        ids = [r["r"][0] for r in ranked]
        cur.execute("SELECT id, text, content_hash FROM chunks WHERE id = ANY(%s)", [ids])
        rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        good = sum(1 for x in ids if WALK.search(rows.get(x, ("", ""))[0]))
        dupes = len(ids) - len({rows.get(x, ("", x))[1] for x in ids})
        p = good / len(ids) if ids else 0.0
        total_p += p
        print(f"{i}. p@{args.k}={p:.2f} ({good}/{len(ids)})  duplicates={dupes}  {q}")
        if args.show == i:
            for x in ids:
                t = rows.get(x, ("", ""))[0].replace("\n", " ")
                print(f"     {'+' if WALK.search(t) else '-'} {t[:110]}")
    print(f"mean p@{args.k}={total_p / len(QUESTIONS):.2f}")


if __name__ == "__main__":
    main()
