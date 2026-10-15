"""
Builds the regions of one job: download the OSM extract, run the HEMS builder, compress,
upload to the GitHub release and write a small description of each region for index.json.
Everything downloaded is deleted as soon as the region is done (runner disk is small).

  python pipeline/build_chunk.py --plan plan.json --chunk 3 --out out [--release regions] [--no-upload]
"""
import argparse
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
# tools/osm-builder next to pipeline/ (region repository) or two levels up (HEMS Dispatch project)
BUILDER = next((p for p in (os.path.join(HERE, "..", "tools", "osm-builder", "build_hems_db.py"),
                            os.path.join(HERE, "..", "..", "tools", "osm-builder", "build_hems_db.py"))
                if os.path.exists(p)), os.path.join(HERE, "..", "tools", "osm-builder", "build_hems_db.py"))
UA = {"User-Agent": "HEMS-Dispatch-region-builder (GitHub Actions)"}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def download(url, dest, tries=4):
    for attempt in range(1, tries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as fh:
                shutil.copyfileobj(r, fh, 4 << 20)
            return
        except Exception as ex:
            log(f"  download failed ({ex}), attempt {attempt}/{tries}")
            time.sleep(20 * attempt)
    raise RuntimeError("download failed")


def build(pbf, out_json, work):
    """Fast in-memory node index first; if the job runs out of memory, the slower on-disk index."""
    for idx in ("flex_mem", f"sparse_file_array,{os.path.join(work, 'nodes.idx')}"):
        cmd = [sys.executable, BUILDER, pbf, "--app-json", out_json, "--node-index", idx]
        log("  " + " ".join(os.path.basename(c) if c == BUILDER else c for c in cmd[1:]))
        p = subprocess.run(cmd)
        if os.path.exists(os.path.join(work, "nodes.idx")):
            os.remove(os.path.join(work, "nodes.idx"))
        if p.returncode == 0 and os.path.exists(out_json):
            return
        log(f"  builder exit code {p.returncode}" + (", retrying with the on-disk node index" if idx == "flex_mem" else ""))
    raise RuntimeError("builder failed")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    ap.add_argument("--chunk", type=int, required=True)
    ap.add_argument("--out", default="out")
    ap.add_argument("--release", default="regions")
    ap.add_argument("--no-upload", action="store_true")
    args = ap.parse_args()

    with open(args.plan, encoding="utf-8") as fh:
        regions = [r for r in json.load(fh)["regions"] if r["chunk"] == args.chunk]
    work = os.path.abspath(os.path.join(args.out, "work"))
    meta_dir = os.path.abspath(os.path.join(args.out, "meta"))
    os.makedirs(work, exist_ok=True)
    os.makedirs(meta_dir, exist_ok=True)
    log(f"Job {args.chunk}: {len(regions)} regions")
    failed = []

    for r in regions:
        t0 = time.time()
        log(f"== {r['id']} ({r['size'] / 1e6:.0f} MB)")
        ext = next((e for e in (".osm.pbf", ".osm.gz", ".osm.bz2", ".osm") if r["url"].endswith(e)), ".osm.pbf")
        pbf = os.path.join(work, r["file"] + ext)
        raw = os.path.join(work, r["file"] + ".json")
        gz = os.path.join(work, r["file"] + ".json.gz")
        try:
            download(r["url"], pbf)
            build(pbf, raw, work)
            os.remove(pbf)
            with open(raw, encoding="utf-8") as fh:
                data = json.load(fh)
            info = {
                "id": r["id"], "file": r["file"] + ".json.gz", "name": r["name"], "country": r["country"],
                "continent": r["continent"], "bounds": data.get("bounds"),
                "hospitals": len(data["hospitals"]), "sites": len(data["sites"]), "lz": len(data["lz"]),
                "bases": len(data["bases"]), "osmDate": data.get("source_timestamp", ""), "built": data.get("built_at", ""),
                "sizeJson": os.path.getsize(raw),
            }
            del data
            if info["sites"] == 0 or info["hospitals"] == 0:
                info["skipped"] = "no emergency site or no hospital helipad in the OSM data"
                log(f"  skipped: {info['skipped']}")
            else:
                with open(raw, "rb") as src, gzip.open(gz, "wb", compresslevel=9) as dst:
                    shutil.copyfileobj(src, dst, 4 << 20)
                info["sizeGz"] = os.path.getsize(gz)
                info["sha256"] = sha256(gz)
                if not args.no_upload:
                    subprocess.run(["gh", "release", "upload", args.release, gz, "--clobber"], check=True)
                log(f"  {info['hospitals']} hospital helipads, {info['sites']} sites, {info['lz']} LZ, "
                    f"{info['sizeGz'] / 1e6:.1f} MB compressed, {time.time() - t0:.0f} s")
            with open(os.path.join(meta_dir, r["file"] + ".json"), "w", encoding="utf-8") as fh:
                json.dump(info, fh, ensure_ascii=False)
        except Exception as ex:
            log(f"  FAILED: {ex}")
            failed.append(r["id"])
        finally:
            for f in (pbf, raw):
                if os.path.exists(f):
                    os.remove(f)
            if os.path.exists(gz) and not args.no_upload:
                os.remove(gz)

    if failed:
        log(f"Failed: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
