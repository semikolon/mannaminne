#!/usr/bin/env python3
"""Build the two screenshot-triage albums via NATIVE PhotoKit — SILENT (no
per-batch confirmation dialog, unlike photoscript/AppleScript), never touches
Photos.sqlite, additive-only (album membership; no move/modify/delete/re-upload).

RUN THIS IN Terminal.app (NOT via an editor/automation) so the Photos permission
attributes to Terminal — approve the one "Allow Access to All Photos" prompt.
    /Users/fredrikbranstrom/Projects/mannaminne/py/.venv/bin/python \
        /Users/fredrikbranstrom/Projects/mannaminne/py/build_albums_photokit.py

Research: ~/dotfiles/docs/research/photos-album-safety-2026-07-28/03_silent_album_add.md
"""
import sys, re
sys.path.insert(0, "/Users/fredrikbranstrom/Projects/mannaminne/py")
import mannaminne as m
from Foundation import NSRunLoop, NSDate, NSPredicate
from Photos import (PHPhotoLibrary, PHAsset, PHAssetCollection,
                    PHAssetCollectionChangeRequest, PHFetchOptions,
                    PHAssetCollectionTypeAlbum, PHAssetCollectionSubtypeAny,
                    PHAccessLevelReadWrite, PHAuthorizationStatusAuthorized)

SCENE = {'People','Person','Face','Selfie','Beach','Sky','Sunset','Sunrise','Outdoor','Water','Sea',
         'Ocean','Plant','Tree','Flower','Food','Meal','Animal','Dog','Cat','Bird','Mountain','Landscape',
         'Snow','Nature','Sports','Vehicle','Car','Room','Interior Room','Living Room','Furniture','Art'}

def classify():
    c = m.load_conn().cursor()
    c.execute("""SELECT source_id, string_agg(text,' ' ORDER BY chunk_idx)
                 FROM chunks WHERE source_kind='screenshot' AND project='iphone-screenshot'
                 GROUP BY source_id""")
    text, mixed = [], []
    for sid, t in c.fetchall():
        uuid = sid.split('photo:')[-1]
        ml = re.search(r'\[([^\]]+)\]', t or '')
        labs = {x.strip() for x in ml.group(1).split(',')} if ml else set()
        ocr = len(re.sub(r'^\S+\s*\[[^\]]*\]', '', t or '').strip())
        if ocr >= 200 and not (labs & SCENE):
            text.append(uuid)
        elif (labs & SCENE) and ocr >= 200:
            mixed.append(uuid)
    return text, mixed

def ensure_auth():
    st = PHPhotoLibrary.authorizationStatusForAccessLevel_(PHAccessLevelReadWrite)
    if st == PHAuthorizationStatusAuthorized:
        return True
    print("Requesting Photos access — approve the prompt (attributed to Terminal)...")
    done = []
    PHPhotoLibrary.requestAuthorizationForAccessLevel_handler_(PHAccessLevelReadWrite, lambda s: done.append(s))
    deadline = NSDate.dateWithTimeIntervalSinceNow_(180)
    while not done and NSDate.date().compare_(deadline) < 0:
        NSRunLoop.currentRunLoop().runMode_beforeDate_("NSDefaultRunLoopMode",
                                                       NSDate.dateWithTimeIntervalSinceNow_(0.25))
    return bool(done) and done[0] == PHAuthorizationStatusAuthorized

def fetch_assets(uuids):
    ids = [f"{u}/L0/001" for u in uuids]
    return PHAsset.fetchAssetsWithLocalIdentifiers_options_(ids, None)

def delete_album_named(lib, name):
    opts = PHFetchOptions.alloc().init()
    opts.setPredicate_(NSPredicate.predicateWithFormat_("title == %@", name))
    cols = PHAssetCollection.fetchAssetCollectionsWithType_subtype_options_(
        PHAssetCollectionTypeAlbum, PHAssetCollectionSubtypeAny, opts)
    if cols.count():
        lib.performChangesAndWait_error_(
            lambda: PHAssetCollectionChangeRequest.deleteAssetCollections_(cols), None)
        print(f"  removed existing album '{name}'")

def build(lib, name, uuids):
    delete_album_named(lib, name)
    assets = fetch_assets(uuids)
    got = assets.count()
    def changes():
        req = PHAssetCollectionChangeRequest.creationRequestForAssetCollectionWithTitle_(name)
        req.addAssets_(assets)
    ok, err = lib.performChangesAndWait_error_(changes, None)
    print(f"[{name}] resolved {got}/{len(uuids)} assets; created+added ok={bool(ok)} "
          f"{('err='+str(err)) if err else ''}", flush=True)
    return got

def main():
    if not ensure_auth():
        print("NOT AUTHORIZED — run this inside Terminal.app and approve 'Allow Access to All Photos'.")
        sys.exit(1)
    text, mixed = classify()
    print(f"triage: text-primary={len(text)}  mixed={len(mixed)}", flush=True)
    lib = PHPhotoLibrary.sharedPhotoLibrary()
    build(lib, "Review — mixed screenshots", mixed)     # smaller first (live test)
    build(lib, "Offload — text screenshots", text)
    print("ALL DONE — both albums built silently.", flush=True)

if __name__ == "__main__":
    main()
