#!/usr/bin/env python3
"""Build the "Offload — impersonal (safe)" Photos album from the classifier verdicts
(/tmp/ss_class.jsonl). ONLY 'impersonal'-labelled screenshots go in; 'personal' and
'ambiguous' are left out (kept). The un-backed screenshots (no VOLTA copy, can't be
reinstated) are EXCLUDED so a bulk-delete of this album can never lose them.

Silent PhotoKit (PHAssetCollectionChangeRequest / performChangesAndWait) — never
touches Photos.sqlite, additive-only (album membership). Run in Terminal.app if the
Photos permission is ever re-prompted.

    py/.venv/bin/python py/build_impersonal_album.py
"""
import sys, json, os
sys.path.insert(0, "/Users/fredrikbranstrom/Projects/mannaminne/py")
from Foundation import NSPredicate
from Photos import (PHPhotoLibrary, PHAsset, PHAssetCollection,
                    PHAssetCollectionChangeRequest, PHFetchOptions,
                    PHAssetCollectionTypeAlbum, PHAssetCollectionSubtypeAny,
                    PHAccessLevelReadWrite, PHAuthorizationStatusAuthorized)

CACHE = "/tmp/ss_class.jsonl"
ALBUM = "Offload — impersonal (safe)"
# un-backed / unrecoverable screenshots — must NEVER enter a bulk-delete album.
# The 2 Offload members with no VOLTA pixel copy (verified 2026-07-29):
UNBACKED = {"F0934F5C-D9D5-46BB-8D1B-BDCDD388BF20",   # IMG_1402 / 2019
            "A5C34016-B8A4-45B4-B89C-54FED4E836D5"}   # IMG_9819 / 2023


def impersonal_uuids():
    seen = {}
    for l in open(CACHE):
        if not l.strip():
            continue
        d = json.loads(l)
        seen[d["uuid"].upper()] = d["label"]      # last verdict wins
    imp = [u for u, lab in seen.items() if lab == "impersonal" and u not in UNBACKED]
    from collections import Counter
    print("verdict counts:", dict(Counter(seen.values())))
    print(f"impersonal (after excluding {len(UNBACKED)} un-backed):", len(imp))
    return imp


def ensure_auth():
    st = PHPhotoLibrary.authorizationStatusForAccessLevel_(PHAccessLevelReadWrite)
    return st == PHAuthorizationStatusAuthorized


def fetch_assets(uuids):
    ids = [f"{u}/L0/001" for u in uuids]
    return PHAsset.fetchAssetsWithLocalIdentifiers_options_(ids, None)


def delete_existing(lib, name):
    opts = PHFetchOptions.alloc().init()
    opts.setPredicate_(NSPredicate.predicateWithFormat_("title == %@", name))
    cols = PHAssetCollection.fetchAssetCollectionsWithType_subtype_options_(
        PHAssetCollectionTypeAlbum, PHAssetCollectionSubtypeAny, opts)
    if cols.count():
        lib.performChangesAndWait_error_(
            lambda: PHAssetCollectionChangeRequest.deleteAssetCollections_(cols), None)
        print(f"  removed existing '{name}'")


def main():
    if not ensure_auth():
        print("NOT AUTHORIZED — run inside Terminal.app and approve 'Allow Access to All Photos'.")
        sys.exit(1)
    uuids = impersonal_uuids()
    assets = fetch_assets(uuids)
    got = assets.count()
    lib = PHPhotoLibrary.sharedPhotoLibrary()
    delete_existing(lib, ALBUM)

    def changes():
        req = PHAssetCollectionChangeRequest.creationRequestForAssetCollectionWithTitle_(ALBUM)
        req.addAssets_(assets)

    ok, err = lib.performChangesAndWait_error_(changes, None)
    print(f"[{ALBUM}] resolved {got}/{len(uuids)} assets; created+added ok={bool(ok)} "
          f"{('err=' + str(err)) if err else ''}")


if __name__ == "__main__":
    main()
