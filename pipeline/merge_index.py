"""
Merges the region descriptions written by the build jobs into index.json, the list the app downloads.
Regions not rebuilt this time keep their previous entry.

  python pipeline/merge_index.py --metas metas --previous old_index.json --out index.json
"""
import argparse
import glob
import json
import os
from datetime import datetime, timezone

ATTRIBUTION = ("Map data (c) OpenStreetMap contributors, "
               "available under the Open Database License (ODbL) 1.0 - openstreetmap.org/copyright")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--metas", default="metas")
    ap.add_argument("--previous", default="")
    ap.add_argument("--out", default="index.json")
    args = ap.parse_args()

    regions = {}
    if args.previous and os.path.exists(args.previous):
        try:
            with open(args.previous, encoding="utf-8") as fh:
                for r in json.load(fh).get("regions", []):
                    regions[r["id"]] = r
        except (ValueError, KeyError):
            pass
    new = skipped = 0
    for path in glob.glob(os.path.join(args.metas, "**", "*.json"), recursive=True):
        with open(path, encoding="utf-8") as fh:
            r = json.load(fh)
        if r.get("skipped"):
            regions.pop(r["id"], None)
            skipped += 1
            continue
        regions[r["id"]] = r
        new += 1

    out = {
        "schema": 1,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "attribution": ATTRIBUTION,
        "regions": sorted(regions.values(), key=lambda r: (r["continent"], r["country"], r["name"])),
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"index.json: {len(out['regions'])} regions ({new} built now, {skipped} skipped without usable data)")


if __name__ == "__main__":
    main()
