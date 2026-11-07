"""Put the screenshots that are safe to delete from Photos into one album, for Fredrik to delete.

Safe means all three of his checks pass (his precondition 2026-11-07): the original is a file on
VOLTA (and so in Darwin's and B2's restic copies), and every mannaminne chunk of it has a keyword
index and an embedding. Favourites, screenshots in an album of his own, edited and hidden ones are
left out: likely keepers on the phone. The list is computed beforehand and passed as a file of
UUIDs (dotfiles docs/photos_archive_offload_2026-07-28.md, 2026-11-07 sections).

Membership only, through PhotoKit, never Photos.sqlite; nothing is deleted or modified.
PhotoKit refuses Claude Desktop's shell; run it from Ghostty or Terminal.

    py/.venv/bin/python py/build_delete_backed_up_album.py LIST.txt          # dry run
    py/.venv/bin/python py/build_delete_backed_up_album.py LIST.txt --apply
"""
import sys

from Foundation import NSPredicate
from Photos import (PHPhotoLibrary, PHAsset, PHAssetCollection,
                    PHAssetCollectionChangeRequest, PHFetchOptions,
                    PHAssetCollectionTypeAlbum, PHAssetCollectionSubtypeAny,
                    PHAccessLevelReadWrite, PHAuthorizationStatusAuthorized)

ALBUM = "Radera — backup + sökbar (2026-11-07)"
BATCH = 2000


def album(title):
    opts = PHFetchOptions.alloc().init()
    opts.setPredicate_(NSPredicate.predicateWithFormat_("title == %@", title))
    cols = PHAssetCollection.fetchAssetCollectionsWithType_subtype_options_(
        PHAssetCollectionTypeAlbum, PHAssetCollectionSubtypeAny, opts)
    return cols.firstObject() if cols.count() else None


def main(path, apply):
    uuids = [l.strip() for l in open(path) if l.strip()]
    print(f"{len(uuids)} UUIDs in {path}")
    if not apply:
        print("dry run; --apply to build the album")
        return
    if PHPhotoLibrary.authorizationStatusForAccessLevel_(PHAccessLevelReadWrite) != PHAuthorizationStatusAuthorized:
        sys.exit("NOT AUTHORIZED: run in Ghostty or Terminal.app and allow access to all photos")
    lib = PHPhotoLibrary.sharedPhotoLibrary()
    if album(ALBUM) is None:
        def create():  # a PhotoKit change block must return None
            PHAssetCollectionChangeRequest.creationRequestForAssetCollectionWithTitle_(ALBUM)
        ok, err = lib.performChangesAndWait_error_(create, None)
        print(f"created album ok={bool(ok)} {err or ''}")
    dst = album(ALBUM)
    added = 0
    for i in range(0, len(uuids), BATCH):
        part = uuids[i:i + BATCH]
        assets = PHAsset.fetchAssetsWithLocalIdentifiers_options_([f"{u}/L0/001" for u in part], None)
        def add():
            PHAssetCollectionChangeRequest.changeRequestForAssetCollection_(dst).addAssets_(assets)
        ok, err = lib.performChangesAndWait_error_(add, None)
        added += assets.count() if ok else 0
        print(f"batch {i // BATCH + 1}: resolved {assets.count()}/{len(part)} ok={bool(ok)} {err or ''}", flush=True)
    print(f"DONE: {added} of {len(uuids)} in '{ALBUM}'")


if __name__ == "__main__":
    main(sys.argv[1], "--apply" in sys.argv)
