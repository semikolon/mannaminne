#!/usr/bin/env python3
"""Build two review/offload albums in Photos from the screenshot triage, using
photoscript (Rhet Turnbull's AppleScript wrapper — the SAFE path; never touches
Photos.sqlite, drives Photos.app which serializes + syncs to iCloud). Additive
only (album membership); no move/modify/delete/re-upload. Research + safety
checklist: ~/dotfiles/docs/research/photos-album-safety-2026-07-28/.

Albums:
  "Review — mixed screenshots"   (has both photo + text — Fredrik's judgment)
  "Offload — text screenshots"   (pure text — safe to delete from iCloud later)
Smaller album first (live test), chunked adds, per-chunk progress, final verify.
"""
import sys, re, time
sys.path.insert(0, "py")
import mannaminne as m
import photoscript

SCENE = {'People','Person','Face','Selfie','Beach','Sky','Sunset','Sunrise','Outdoor','Water','Sea',
         'Ocean','Plant','Tree','Flower','Food','Meal','Animal','Dog','Cat','Bird','Mountain','Landscape',
         'Snow','Nature','Sports','Vehicle','Car','Room','Interior Room','Living Room','Furniture','Art'}
CHUNK = 400

def classify():
    c = m.load_conn().cursor()
    c.execute("""SELECT source_id, string_agg(text,' ' ORDER BY chunk_idx)
                 FROM chunks WHERE source_kind='screenshot' AND project='iphone-screenshot'
                 GROUP BY source_id""")
    text, mixed = [], []
    for sid, t in c.fetchall():
        uuid = sid.split('photo:')[-1]
        labs = set()
        ml = re.search(r'\[([^\]]+)\]', t or '')
        if ml:
            labs = {x.strip() for x in ml.group(1).split(',')}
        ocr = len(re.sub(r'^\S+\s*\[[^\]]*\]', '', t or '').strip())
        has_scene = bool(labs & SCENE)
        if ocr >= 200 and not has_scene:
            text.append(uuid)
        elif has_scene and ocr >= 200:
            mixed.append(uuid)
    return text, mixed

def add_all(lib, name, uuids):
    # fresh album (delete any prior same-name build so re-runs are idempotent)
    for a in lib.albums():
        if a.title == name:
            print(f"  removing existing '{name}' to rebuild", flush=True)
            lib.delete_album(a)
    album = lib.create_album(name)
    print(f"[{name}] created; adding {len(uuids)} photos in chunks of {CHUNK}", flush=True)
    added = missing = 0
    for i in range(0, len(uuids), CHUNK):
        chunk = uuids[i:i+CHUNK]
        photos = []
        for u in chunk:
            try:
                photos.append(photoscript.Photo(u))
            except Exception:
                missing += 1
        if photos:
            album.add(photos)
            added += len(photos)
        print(f"  [{name}] {added}/{len(uuids)} added ({missing} unresolved) "
              f"@ {time.strftime('%H:%M:%S')}", flush=True)
    final = len(album.photos())
    print(f"[{name}] DONE — album has {final} photos (added {added}, {missing} unresolved)", flush=True)
    return final

def main():
    text, mixed = classify()
    print(f"triage: text-primary={len(text)}  mixed={len(mixed)}", flush=True)
    lib = photoscript.PhotosLibrary()
    # clean up the smoke-test album
    for a in lib.albums():
        if a.title == "zz-photoscript-SMOKETEST":
            lib.delete_album(a); print("removed smoke-test album", flush=True)
    # smaller album first (live test), then the big one
    add_all(lib, "Review — mixed screenshots", mixed)
    add_all(lib, "Offload — text screenshots", text)
    print("ALL DONE", flush=True)

if __name__ == "__main__":
    main()
