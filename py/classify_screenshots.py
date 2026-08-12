#!/usr/bin/env python3
"""Classify the text-screenshot OCR (already in mannaminne) as impersonal-reference
vs personal, so the impersonal pile can be bulk-offloaded from iCloud while keepers
stay. HIGH-PRECISION toward KEEP: anything unclear -> 'ambiguous' (kept, never offloaded).

Resumable: writes {uuid,label} to /tmp/ss_class.jsonl as it goes; re-running skips done.
Reads OCR from mannaminne (no re-OCR). Model: gemini-3.5-flash (fleet default), minimal
thinking, structured JSON. Batched to keep call-count + cost low.

    py/.venv/bin/python py/classify_screenshots.py [uuid_file] [batch]
"""
import sys, os, json, time, re
sys.path.insert(0, "/Users/fredrikbranstrom/Projects/mannaminne/py")
import mannaminne as m
from google import genai
from google.genai import types

UUID_FILE = sys.argv[1] if len(sys.argv) > 1 else "/tmp/offload_now.txt"
BATCH     = int(sys.argv[2]) if len(sys.argv) > 2 else 80
CACHE     = "/tmp/ss_class.jsonl"
MODELS    = ["gemini-3.5-flash", "gemini-3-flash-preview", "gemini-3.1-flash-lite"]
MAXCHARS  = 500

POLICY = """You sort a person's phone SCREENSHOTS into two piles by their EXTRACTED TEXT.
The screenshots are being offloaded from a full iCloud; the "impersonal" pile will be
bulk-deleted (originals are safely archived + searchable elsewhere), the rest is kept
on the phone. So ONLY label "impersonal" when you are confident it is NOT personally
meaningful. When unsure, label "ambiguous" (it will be kept).

impersonal  = reference/information the user screenshotted to remember, with no personal
              or relational meaning: news/articles/blogs, tweets & public social posts,
              receipts / orders / booking & payment confirmations, product/shopping/web
              pages, search results, how-to / documentation / reference, maps & directions,
              app settings / generic UI, memes, code / technical output, schedules/tables.
personal    = private or emotionally meaningful: 1:1 or group PRIVATE conversations
              (iMessage / WhatsApp / Messenger / SMS / Signal / dating apps), the user's
              OWN written notes / thoughts / drafts, invitations, anything intimate,
              relational, health, private finances, or clearly about the user's own life
              and people. A conversation between people = personal even if it also
              contains a link or info.
ambiguous   = genuinely unclear, or too little text to tell -> KEPT.

The text is often Swedish and/or English. Judge on meaning, not language.
Return one verdict per numbered item."""

SCHEMA = types.Schema(
    type=types.Type.ARRAY,
    items=types.Schema(
        type=types.Type.OBJECT,
        required=["i", "label"],
        properties={
            "i": types.Schema(type=types.Type.INTEGER),
            "label": types.Schema(type=types.Type.STRING,
                                  enum=["impersonal", "personal", "ambiguous"]),
        },
    ),
)


def load_targets():
    uuids = [l.strip() for l in open(UUID_FILE) if l.strip()]
    conn = m.load_conn(); cur = conn.cursor()
    cur.execute("""SELECT source_id, string_agg(text,' ' ORDER BY chunk_idx)
                   FROM chunks WHERE source_kind='screenshot' GROUP BY source_id""")
    txt = {}
    for sid, t in cur.fetchall():
        u = sid.split("photo:")[-1]
        # strip the "IMG_x.PNG [scene,labels]" header the OCR rows carry
        body = re.sub(r"^\S+\s*\[[^\]]*\]", "", t or "").strip()
        txt[u.upper()] = body[:MAXCHARS]
    out = []
    for u in uuids:
        body = txt.get(u.upper(), "")
        out.append((u, body))
    return out


def done_set():
    if not os.path.exists(CACHE):
        return set()
    return {json.loads(l)["uuid"].upper() for l in open(CACHE) if l.strip()}


def classify_batch(client, batch):
    lines = []
    for i, (_, body) in enumerate(batch):
        snippet = body if body else "(no text)"
        lines.append(f"[{i}] {snippet}")
    prompt = POLICY + "\n\nITEMS:\n" + "\n\n".join(lines)
    last = None
    for model in MODELS:
        for attempt in range(4):
            try:
                resp = client.models.generate_content(
                    model=model, contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=SCHEMA,
                        thinking_config=types.ThinkingConfig(thinking_level="LOW"),
                    ),
                )
                data = json.loads(resp.text)
                by_i = {int(d["i"]): d["label"] for d in data if "i" in d}
                # any item the model skipped -> ambiguous (kept)
                return [by_i.get(i, "ambiguous") for i in range(len(batch))]
            except Exception as e:
                last = e
                msg = str(e).lower()
                if "429" in msg or "resource_exhausted" in msg or "quota" in msg:
                    time.sleep(min(30, 4 * (attempt + 1)))
                elif "503" in msg or "overload" in msg or "500" in msg:
                    time.sleep(3 * (attempt + 1))
                else:
                    break  # non-retryable for this model -> try next model
    raise RuntimeError(f"all models failed for batch: {last}")


def main():
    key = os.environ["GEMINI_API_KEY"]
    client = genai.Client(api_key=key)
    targets = load_targets()
    done = done_set()
    todo = [t for t in targets if t[0].upper() not in done]
    with_text = sum(1 for _, b in targets if b)
    print(f"targets={len(targets)} with_ocr={with_text} already_done={len(done)} todo={len(todo)}", flush=True)
    out = open(CACHE, "a")
    n = 0
    for s in range(0, len(todo), BATCH):
        batch = todo[s:s + BATCH]
        labels = classify_batch(client, batch)
        for (u, _), lab in zip(batch, labels):
            out.write(json.dumps({"uuid": u, "label": lab}) + "\n")
        out.flush()
        n += len(batch)
        if (s // BATCH) % 10 == 0 or n >= len(todo):
            print(f"  {n}/{len(todo)} classified", flush=True)
    out.close()
    # summary
    from collections import Counter
    c = Counter(json.loads(l)["label"] for l in open(CACHE) if l.strip())
    print("DONE. label counts:", dict(c), flush=True)


if __name__ == "__main__":
    main()
