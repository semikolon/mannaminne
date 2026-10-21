"""Lift the likely keepers out of "Offload — impersonal (safe)" before it is deleted.

The album is a delete surface: Fredrik selects all of it and deletes it from Photos.
The classifier put a screenshot there from its text alone, so it could not see that
he had marked one as a favourite or filed it in an album of his own. Those are the
likeliest keepers (2026-10-21: 43 favourites, 90 in other albums). This removes
them from the delete album and adds them to "Lyfta ur Offload — impersonal" so he
can see what was lifted. Membership only, through PhotoKit, never Photos.sqlite;
nothing is deleted or modified.

    py/.venv/bin/python py/lift_keepers_from_offload_album.py          # dry run
    py/.venv/bin/python py/lift_keepers_from_offload_album.py --apply
"""
import sys
from collections import Counter

import osxphotos
from Foundation import NSPredicate
from Photos import (PHPhotoLibrary, PHAsset, PHAssetCollection,
                    PHAssetCollectionChangeRequest, PHFetchOptions,
                    PHAssetCollectionTypeAlbum, PHAssetCollectionSubtypeAny,
                    PHAccessLevelReadWrite, PHAuthorizationStatusAuthorized)

LIBRARY = "/Volumes/FERMI/Photos Library.photoslibrary"
DELETE_ALBUM = "Offload — impersonal (safe)"
OWN = {DELETE_ALBUM, "Offload — text screenshots", "Review — mixed screenshots"}
LIFTED_ALBUM = "Lyfta ur Offload — impersonal"


def keepers():
    db = osxphotos.PhotosDB(LIBRARY)
    out, why = [], Counter()
    for p in db.photos(albums=[DELETE_ALBUM]):
        others = sorted(set(p.albums) - OWN - {LIFTED_ALBUM})
        if p.favorite or others:
            out.append(p.uuid)
            if p.favorite:
                why["favourite"] += 1
            for a in others:
                why[f"album: {a}"] += 1
    return out, why


def album(title):
    opts = PHFetchOptions.alloc().init()
    opts.setPredicate_(NSPredicate.predicateWithFormat_("title == %@", title))
    cols = PHAssetCollection.fetchAssetCollectionsWithType_subtype_options_(
        PHAssetCollectionTypeAlbum, PHAssetCollectionSubtypeAny, opts)
    return cols.firstObject() if cols.count() else None


def main(apply):
    uuids, why = keepers()
    print(f"{len(uuids)} keepers in '{DELETE_ALBUM}'")
    for reason, n in why.most_common():
        print(f"  {n:4}  {reason}")
    if not apply:
        print("dry run; --apply to move them")
        return
    if PHPhotoLibrary.authorizationStatusForAccessLevel_(PHAccessLevelReadWrite) != PHAuthorizationStatusAuthorized:
        sys.exit("NOT AUTHORIZED: run in Terminal.app and allow access to all photos")
    assets = PHAsset.fetchAssetsWithLocalIdentifiers_options_([f"{u}/L0/001" for u in uuids], None)
    src = album(DELETE_ALBUM)
    if src is None:
        sys.exit(f"album not found: {DELETE_ALBUM}")
    dst = album(LIFTED_ALBUM)
    lib = PHPhotoLibrary.sharedPhotoLibrary()

    def changes():
        PHAssetCollectionChangeRequest.changeRequestForAssetCollection_(src).removeAssets_(assets)
        if dst is None:
            PHAssetCollectionChangeRequest.creationRequestForAssetCollectionWithTitle_(
                LIFTED_ALBUM).addAssets_(assets)
        else:
            PHAssetCollectionChangeRequest.changeRequestForAssetCollection_(dst).addAssets_(assets)

    ok, err = lib.performChangesAndWait_error_(changes, None)
    print(f"resolved {assets.count()}/{len(uuids)}; moved ok={bool(ok)} {err or ''}")


if __name__ == "__main__":
    main("--apply" in sys.argv)
