"""
Plans a region build: reads the Geofabrik index, picks one extract per country and splits the large
countries into their Geofabrik sub-regions, then shares the work between parallel jobs.

  python pipeline/plan.py --select all --out plan.json --matrix matrix.json
  python pipeline/plan.py --select switzerland,austria ...
  python pipeline/plan.py --select europe ...            (every region of a continent)
"""
import argparse
import json
import math
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

INDEX_URL = "https://download.geofabrik.de/index-v1-nogeom.json"
CONTINENTS = {"africa", "antarctica", "asia", "australia-oceania", "central-america", "europe",
              "north-america", "south-america"}
# Geofabrik groups that overlap real countries: never built
SKIP = {"alps", "britain-and-ireland", "dach", "antarctica", "great-britain", "sea", "south-africa-and-lesotho"}
UA = {"User-Agent": "HEMS-Dispatch-region-builder (GitHub Actions)"}


def fetch_json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        return json.loads(r.read())


def head_size(url):
    try:
        req = urllib.request.Request(url, method="HEAD", headers=UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            return int(r.headers.get("Content-Length") or 0)
    except Exception:
        return 0


def file_id(region_id):
    return region_id.replace("/", "-")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--select", default="all", help="all, or region / country / continent ids separated by commas")
    ap.add_argument("--split-mb", type=float, default=900, help="countries larger than this are split in sub-regions")
    ap.add_argument("--chunk-mb", type=float, default=2500, help="OSM data handled by one job")
    ap.add_argument("--max-jobs", type=int, default=60)
    ap.add_argument("--out", default="plan.json")
    ap.add_argument("--matrix", default="matrix.json")
    args = ap.parse_args()

    index = fetch_json(INDEX_URL)
    nodes = {}
    for f in index["features"]:
        p = f["properties"]
        nodes[p["id"]] = {"id": p["id"], "name": p.get("name", p["id"]), "parent": p.get("parent"),
                          "pbf": (p.get("urls") or {}).get("pbf"), "children": []}
    # US states ("us/texas") are listed under north-america: attach them to "us"
    for n in nodes.values():
        prefix = n["id"].rsplit("/", 1)[0] if "/" in n["id"] else None
        if prefix in nodes and n["parent"] != prefix:
            n["parent"] = prefix
    for n in nodes.values():
        if n["parent"] in nodes:
            nodes[n["parent"]]["children"].append(n["id"])

    def chain(nid):
        out = []
        while nid:
            out.append(nid)
            nid = nodes[nid]["parent"] if nid in nodes else None
        return out

    # countries: children of a continent, plus top-level entries that are not continents (e.g. russia)
    countries = []
    for n in nodes.values():
        if n["id"] in SKIP or not n["pbf"]:
            continue
        if n["parent"] in CONTINENTS:
            if n["parent"] == "north-america" and n["id"].startswith("us-"):
                continue  # US census regions overlap the states
            countries.append(n["id"])
        elif n["parent"] is None and n["id"] not in CONTINENTS:
            countries.append(n["id"])

    wanted = [s.strip().lower() for s in args.select.split(",") if s.strip()]
    if "all" not in wanted:
        unknown = [w for w in wanted if w not in nodes]
        if unknown:
            sys.exit(f"Unknown region id(s): {', '.join(unknown)} (see {INDEX_URL})")

    def selected(nid):
        return "all" in wanted or any(c in wanted for c in chain(nid))

    sizes = {}

    def size_of(nid):
        if nid not in sizes:
            sizes[nid] = head_size(nodes[nid]["pbf"])
        return sizes[nid]

    # sizes of the countries (in parallel), then split the big ones
    todo = [c for c in countries if selected(c) or any(selected(d) for d in nodes[c]["children"])]
    if "all" not in wanted:  # a sub-region asked for directly
        todo += [w for w in wanted if w in nodes and w not in todo and nodes[w]["parent"] not in CONTINENTS
                 and nodes[w]["parent"] is not None and w not in CONTINENTS]
    with ThreadPoolExecutor(16) as ex:
        list(ex.map(size_of, todo))

    regions = []

    def visit(nid, country):
        n = nodes[nid]
        kids = [k for k in n["children"] if nodes[k]["pbf"]]
        if kids and size_of(nid) > args.split_mb * 1e6:
            with ThreadPoolExecutor(16) as ex:
                list(ex.map(size_of, kids))
            for k in kids:
                visit(k, country)
            return
        if not selected(nid):
            return
        ch = chain(nid)
        continent = next((c for c in reversed(ch) if c in CONTINENTS), "russia" if "russia" in ch else ch[-1])
        regions.append({
            "id": nid, "file": file_id(nid), "name": n["name"],
            "country": nodes[country]["name"], "continent": nodes[continent]["name"] if continent in nodes else continent,
            "url": n["pbf"], "size": size_of(nid),
        })

    seen = set()
    for c in todo:
        if c in seen:
            continue
        seen.add(c)
        country = c
        if nodes[c]["parent"] not in CONTINENTS and nodes[c]["parent"] is not None:
            country = next((x for x in chain(c) if nodes.get(x, {}).get("parent") in CONTINENTS), c)
        visit(c, country)

    # unique ids, largest first, shared between jobs (greedy)
    uniq = {r["id"]: r for r in regions}
    regions = sorted(uniq.values(), key=lambda r: -r["size"])
    total = sum(r["size"] for r in regions)
    jobs = max(1, min(args.max_jobs, len(regions), math.ceil(total / (args.chunk_mb * 1e6))))
    loads = [0.0] * jobs
    for r in regions:
        j = loads.index(min(loads))
        r["chunk"] = j
        loads[j] += r["size"] or 50e6

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"regions": regions}, fh, indent=1, ensure_ascii=False)
    with open(args.matrix, "w") as fh:
        json.dump({"chunk": list(range(jobs))}, fh)
    print(f"{len(regions)} regions, {total / 1e9:.1f} GB of OSM data, {jobs} jobs")
    for r in regions[:15]:
        print(f"  {r['id']:40s} {r['size'] / 1e6:8.0f} MB  job {r['chunk']}")
    if len(regions) > 15:
        print(f"  ... {len(regions) - 15} more")
    if not regions:
        sys.exit("Nothing selected.")


if __name__ == "__main__":
    main()
