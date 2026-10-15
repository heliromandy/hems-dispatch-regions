#!/usr/bin/env python3
"""
HEMS Dispatch - OSM database builder.

Reads an OpenStreetMap extract (.osm.pbf, .osm or .osm.gz) and writes an SQLite
database used by the HEMS Dispatch app:

  hospital_helipads  helipads that belong to a hospital (mission destinations)
  lz                 landing zones that passed the terrain and obstacle checks
  sites              places where an emergency can happen (home, road, trail...)
  site_lz            for each site, the nearest LZs within the configured radius
  meta               build parameters, source and the required ODbL attribution

Optional: GeoTIFF elevation tiles (e.g. Copernicus GLO-30) add altitude and
slope, and LZs that are too steep are rejected.

Usage:
  python build_hems_db.py switzerland-latest.osm.pbf -o hems_ch.sqlite
  python build_hems_db.py region.osm.pbf -o region.sqlite --dem-glob "dem/*.tif" --geojson preview.geojson
  python build_hems_db.py switzerland-latest.osm.pbf -o hems_ch.sqlite --app-json hems_region.json
"""

import argparse
import gc
from array import array
import glob as globmod
import json
import math
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

import numpy as np
import osmium
import shapely
from osmium.geom import WKBFactory
from pyproj import Transformer
from shapely import wkb as shp_wkb
from shapely.geometry import MultiPolygon, Point, Polygon
from shapely.ops import polylabel

ATTRIBUTION = ("Map data (c) OpenStreetMap contributors, "
               "available under the Open Database License (ODbL) 1.0 - openstreetmap.org/copyright")

# Building values mapped to an emergency-site kind. Anything else tagged
# building=* becomes the generic kind "building".
BUILDING_SITE_KIND = {
    "house": "home", "detached": "home", "residential": "home", "apartments": "home",
    "semidetached_house": "home", "terrace": "home", "bungalow": "home", "dormitory": "home",
    "farm": "farm", "barn": "farm", "farm_auxiliary": "farm", "stable": "farm", "cowshed": "farm",
    "industrial": "workplace", "warehouse": "workplace", "construction": "workplace",
    "commercial": "workplace", "office": "workplace",
    "school": "public", "retail": "public", "supermarket": "public", "civic": "public",
    "public": "public", "train_station": "public", "sports_hall": "sports",
    "hut": "alpine_hut", "cabin": "home",
}
ROAD_CLASSES = {"motorway", "trunk", "primary", "secondary", "tertiary"}
TRAIL_SAC = {"hiking", "mountain_hiking", "demanding_mountain_hiking", "alpine_hiking",
             "demanding_alpine_hiking", "difficult_alpine_hiking"}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def parse_rules(rules):
    """{'power=line': 40, 'aerialway=*': 50} -> {('power','line'): 40, ('aerialway','*'): 50}"""
    out = {}
    for key, buf in rules.items():
        if key.startswith("_"):
            continue
        k, v = key.split("=", 1)
        out[(k, v)] = float(buf)
    return out


def match_rule(tags, rules):
    """Return the largest buffer of the rules that match these tags, or None."""
    best = None
    for (k, v), buf in rules.items():
        tv = tags.get(k)
        if tv is None or tv == "no":
            continue
        if v == "*" or tv == v:
            best = buf if best is None else max(best, buf)
    return best


def osm_ref(kind, oid):
    return f"{kind}{oid}"


class Collector(osmium.SimpleHandler):
    """One read of the file. Keeps geometries in lon/lat; projection happens afterwards."""

    def __init__(self, cfg):
        super().__init__()
        self.f = WKBFactory()
        lz = cfg["lz"]
        self.open_tags = {k: set(v) for k, v in lz["open_areas"].items()}
        self.pitch_excluded = set(lz.get("pitch_excluded_sports", []))
        ob = cfg["obstacles"]
        self.r_areas = parse_rules(ob["areas"])
        self.r_lines = parse_rules(ob["lines"])
        self.r_points = parse_rules(ob["points"])
        self.use_buildings = bool(ob.get("buildings", True))

        self.open_areas = []      # (ref, kind, geom)
        self.obst_geoms = []      # (geom, buffer)
        self.buildings = array("d")  # flat minx, miny, maxx, maxy (compact: large countries have millions)
        self.hospitals = []       # dict
        self.helipads = []        # dict
        self.aerodromes = []      # dict: airports and airstrips, possible pilot bases
        self.site_points = []     # (kind, lon, lat, ref, detail)
        self.site_lines = []      # (kind, geom, ref, detail)
        self.bounds = [180.0, 90.0, -180.0, -90.0]
        self.latest_edit = None
        self.errors = 0
        # Pre-thinning of site points while reading (half the final cell), so a country
        # with millions of buildings does not keep millions of candidate sites in memory.
        self._pre_cell_deg = float(cfg["sites"]["thin_cell_m"]) / 2 / 111320.0
        self._pre_seen = set()

    def _add_aerodrome(self, t, lon, lat, ref):
        ident = t.get("icao") or t.get("ref") or t.get("local_ref") or t.get("faa") or ""
        self.aerodromes.append({"ref": ref, "lon": lon, "lat": lat, "name": t.get("name", ""),
                                "ident": ident.strip().upper(), "ele": t.get("ele"),
                                "kind": t.get("aeroway")})

    def _add_site(self, kind, lon, lat, ref, detail):
        c = self._pre_cell_deg
        key = (kind, int(lat // c), int(lon * math.cos(math.radians(lat)) // c))
        if key in self._pre_seen:
            return
        self._pre_seen.add(key)
        self.site_points.append((kind, lon, lat, ref, detail))

    # -- helpers -----------------------------------------------------------
    def _grow(self, minx, miny, maxx, maxy):
        b = self.bounds
        b[0] = min(b[0], minx); b[1] = min(b[1], miny)
        b[2] = max(b[2], maxx); b[3] = max(b[3], maxy)

    def _ts(self, o):
        ts = o.timestamp
        if ts is not None and (self.latest_edit is None or ts > self.latest_edit):
            self.latest_edit = ts

    def _open_kind(self, tags):
        for k, values in self.open_tags.items():
            v = tags.get(k)
            if v in values:
                if k == "leisure" and v == "pitch":
                    sport = tags.get("sport", "")
                    if any(s in self.pitch_excluded for s in sport.split(";")):
                        return None
                return v
        return None

    # -- callbacks -----------------------------------------------------------
    def node(self, n):
        t = n.tags
        if len(t) == 0:
            return
        self._ts(n)
        try:
            lon, lat = n.location.lon, n.location.lat
        except osmium.InvalidLocationError:
            return
        ref = osm_ref("n", n.id)
        if t.get("amenity") == "hospital":
            self.hospitals.append({"ref": ref, "name": t.get("name", ""), "emergency": t.get("emergency", ""),
                                   "lon": lon, "lat": lat, "geom": None})
        if t.get("aeroway") in ("helipad", "heliport"):
            self.helipads.append({"ref": ref, "lon": lon, "lat": lat, "name": t.get("name", ""),
                                  "ele": t.get("ele"), "emergency": t.get("emergency", ""),
                                  "ident": (t.get("icao") or t.get("ref") or "").strip().upper(),
                                  "kind": t.get("aeroway")})
        if t.get("aeroway") in ("aerodrome", "airstrip"):
            self._add_aerodrome(t, lon, lat, ref)
        buf = match_rule(t, self.r_points)
        if buf is not None:
            self.obst_geoms.append((Point(lon, lat), buf))
        if t.get("sport") == "climbing":
            self._add_site("climbing", lon, lat, ref, "")
        if t.get("tourism") in ("alpine_hut", "wilderness_hut"):
            self._add_site("alpine_hut", lon, lat, ref, t.get("tourism"))
        self._grow(lon, lat, lon, lat)

    def way(self, w):
        t = w.tags
        if len(t) == 0:
            return
        line_buf = match_rule(t, self.r_lines)
        hw = t.get("highway")
        road = hw in ROAD_CLASSES
        trail = hw in ("path", "footway", "track") and t.get("sac_scale") in TRAIL_SAC
        piste = t.get("piste:type") in ("downhill", "nordic", "skitour")
        if line_buf is None and not (road or trail or piste):
            return
        self._ts(w)
        try:
            g = shp_wkb.loads(self.f.create_linestring(w), hex=True)
        except Exception:
            self.errors += 1
            return
        if line_buf is not None:
            self.obst_geoms.append((g, line_buf))
        ref = osm_ref("w", w.id)
        if road:
            self.site_lines.append(("road", g, ref, hw))
        elif trail:
            self.site_lines.append(("trail", g, ref, t.get("sac_scale")))
        elif piste:
            detail = t.get("piste:type") + (":" + t["piste:difficulty"] if "piste:difficulty" in t else "")
            self.site_lines.append(("ski_piste", g, ref, detail))

    def area(self, a):
        t = a.tags
        if len(t) == 0:
            return
        open_kind = self._open_kind(t)
        obst_buf = match_rule(t, self.r_areas)
        bld = t.get("building")
        is_bld = bld is not None and bld != "no"
        hospital = t.get("amenity") == "hospital"
        helipad = t.get("aeroway") in ("helipad", "heliport")
        aerodrome = t.get("aeroway") in ("aerodrome", "airstrip")
        landuse = t.get("landuse")
        leisure = t.get("leisure")
        site_kind = None
        if landuse == "farmyard":
            site_kind = "farm"
        elif landuse in ("industrial", "construction", "quarry"):
            site_kind = "workplace"
        elif leisure in ("pitch", "track", "sports_centre"):
            site_kind = "sports"
        elif t.get("natural") == "beach":
            site_kind = "beach"
        elif t.get("tourism") in ("alpine_hut", "wilderness_hut"):
            site_kind = "alpine_hut"
        if not (open_kind or obst_buf is not None or is_bld or hospital or helipad or aerodrome or site_kind):
            return
        self._ts(a)
        try:
            g = shp_wkb.loads(self.f.create_multipolygon(a), hex=True)
        except Exception:
            self.errors += 1
            return
        if not g.is_valid:
            g = shapely.make_valid(g)
        if g.is_empty:
            return
        ref = osm_ref("w" if a.from_way() else "r", a.orig_id())
        minx, miny, maxx, maxy = g.bounds
        self._grow(minx, miny, maxx, maxy)

        if is_bld:
            if self.use_buildings:
                self.buildings.extend((minx, miny, maxx, maxy))
            c = g.representative_point()
            self._add_site(BUILDING_SITE_KIND.get(bld, "building"), c.x, c.y, ref, bld)
        if open_kind:
            self.open_areas.append((ref, open_kind, g))
        if obst_buf is not None:
            self.obst_geoms.append((g, obst_buf))
        if hospital:
            c = g.representative_point()
            self.hospitals.append({"ref": ref, "name": t.get("name", ""), "emergency": t.get("emergency", ""),
                                   "lon": c.x, "lat": c.y, "geom": g})
        if helipad:
            c = g.representative_point()
            self.helipads.append({"ref": ref, "lon": c.x, "lat": c.y, "name": t.get("name", ""),
                                  "ele": t.get("ele"), "emergency": t.get("emergency", ""),
                                  "ident": (t.get("icao") or t.get("ref") or "").strip().upper(),
                                  "kind": t.get("aeroway")})
        if aerodrome:
            c = g.representative_point()
            self._add_aerodrome(t, c.x, c.y, ref)
        if site_kind and not is_bld:
            c = g.representative_point()
            self._add_site(site_kind, c.x, c.y, ref, landuse or leisure or "")


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------

class Proj:
    """UTM zone of the extract's centre. Accurate enough for clearances on a regional extract."""

    def __init__(self, bounds):
        lon = (bounds[0] + bounds[2]) / 2
        lat = (bounds[1] + bounds[3]) / 2
        zone = int((lon + 180) // 6) + 1
        self.epsg = (32600 if lat >= 0 else 32700) + zone
        self.fwd = Transformer.from_crs(4326, self.epsg, always_xy=True)
        self.inv = Transformer.from_crs(self.epsg, 4326, always_xy=True)

    def _f(self, c):
        x, y = self.fwd.transform(c[:, 0], c[:, 1])
        return np.column_stack([x, y])

    def geom(self, g):
        return shapely.transform(g, self._f)

    def xy(self, lon, lat):
        return self.fwd.transform(np.asarray(lon, dtype=float), np.asarray(lat, dtype=float))

    def lonlat(self, x, y):
        return self.inv.transform(np.asarray(x, dtype=float), np.asarray(y, dtype=float))


# ---------------------------------------------------------------------------
# Elevation (optional)
# ---------------------------------------------------------------------------

class Dem:
    def __init__(self, pattern):
        import rasterio  # only needed when a DEM is used
        self.rasterio = rasterio
        self.paths = sorted(globmod.glob(pattern)) if pattern else []
        self.tiles = []
        for p in self.paths:
            with rasterio.open(p) as ds:
                self.tiles.append((p, ds.bounds))

    def __bool__(self):
        return bool(self.tiles)

    def sample(self, lon, lat):
        """Vectorised elevation lookup, NaN where no tile covers the point."""
        lon = np.asarray(lon, dtype=float)
        lat = np.asarray(lat, dtype=float)
        out = np.full(lon.shape, np.nan)
        for path, b in self.tiles:
            m = (lon >= b.left) & (lon < b.right) & (lat > b.bottom) & (lat <= b.top) & np.isnan(out)
            if not m.any():
                continue
            with self.rasterio.open(path) as ds:
                arr = ds.read(1, masked=True)
                rows, cols = self.rasterio.transform.rowcol(ds.transform, lon[m], lat[m])
                rows = np.clip(np.asarray(rows), 0, ds.height - 1)
                cols = np.clip(np.asarray(cols), 0, ds.width - 1)
                vals = arr[rows, cols]
                vals = np.ma.filled(vals.astype(float), np.nan)
                out[m] = vals
        return out


def elev_and_slope(dem, proj, x, y, probe):
    """Elevation at (x, y) and slope in degrees from four probes at +/- probe metres."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    xs = np.concatenate([x, x + probe, x - probe, x, x])
    ys = np.concatenate([y, y, y, y + probe, y - probe])
    lon, lat = proj.lonlat(xs, ys)
    e = dem.sample(lon, lat).reshape(5, -1)
    dzdx = (e[1] - e[2]) / (2 * probe)
    dzdy = (e[3] - e[4]) / (2 * probe)
    slope = np.degrees(np.arctan(np.hypot(dzdx, dzdy)))
    return e[0], slope


# ---------------------------------------------------------------------------
# Build steps
# ---------------------------------------------------------------------------

def build_obstacle_index(col, proj, cfg):
    geoms, bufs = [], []
    for g, b in col.obst_geoms:
        geoms.append(proj.geom(g))
        bufs.append(b)
    if len(col.buildings):
        arr = np.frombuffer(col.buildings, dtype=float).reshape(-1, 4)
        x1, y1 = proj.xy(arr[:, 0], arr[:, 1])
        x2, y2 = proj.xy(arr[:, 2], arr[:, 3])
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        rad = np.hypot(x2 - x1, y2 - y1) / 2 + cfg["obstacles"].get("building_buffer_m", 5)
        geoms.extend(shapely.points(cx, cy))
        bufs.extend(rad.tolist())
    geoms = np.array(geoms, dtype=object)
    bufs = np.array(bufs, dtype=float)
    tree = shapely.STRtree(geoms) if len(geoms) else None
    return tree, geoms, bufs


def obstacle_clearance(pt, tree, geoms, bufs, search):
    """Distance from pt to the nearest obstacle edge minus that obstacle's buffer."""
    if tree is None:
        return math.inf
    idx = tree.query(pt, predicate="dwithin", distance=search)
    if len(idx) == 0:
        return math.inf
    d = shapely.distance(pt, geoms[idx]) - bufs[idx]
    return float(d.min())


def build_lz(col, proj, cfg, tree, geoms, bufs):
    lzc = cfg["lz"]
    r_min = float(lzc["min_clear_radius_m"])
    inside = bool(lzc["require_disc_inside_area"])
    min_area = float(lzc["min_area_m2"])
    spacing = float(lzc["grid_spacing_m"])
    search = r_min + (bufs.max() if len(bufs) else 0) + 1
    lz = []  # dicts with x, y
    stats = {"areas": 0, "candidates": 0, "rejected_inside": 0, "rejected_obstacle": 0}

    for ref, kind, g_ll in col.open_areas:
        g = proj.geom(g_ll)
        polys = list(g.geoms) if isinstance(g, MultiPolygon) else ([g] if isinstance(g, Polygon) else
                [p for p in getattr(g, "geoms", []) if isinstance(p, Polygon)])
        for poly in polys:
            if poly.area < min_area:
                continue
            stats["areas"] += 1
            cands = [polylabel(poly, tolerance=2.0)]
            if poly.area > 2 * spacing * spacing:
                minx, miny, maxx, maxy = poly.bounds
                gx = np.arange(minx + spacing / 2, maxx, spacing)
                gy = np.arange(miny + spacing / 2, maxy, spacing)
                if len(gx) and len(gy):
                    X, Y = np.meshgrid(gx, gy)
                    pts = shapely.points(X.ravel(), Y.ravel())
                    pts = pts[shapely.contains(poly, pts)]
                    first = cands[0]
                    cands.extend(p for p in pts if p.distance(first) > spacing / 2)
            boundary = poly.boundary
            for p in cands:
                stats["candidates"] += 1
                inner = p.distance(boundary)
                if inside and inner < r_min:
                    stats["rejected_inside"] += 1
                    continue
                clr = obstacle_clearance(p, tree, geoms, bufs, search)
                if clr < r_min:
                    stats["rejected_obstacle"] += 1
                    continue
                radius = min(clr, inner) if inside else clr
                lz.append({"x": p.x, "y": p.y, "kind": kind, "clear": radius,
                           "area": poly.area, "ref": ref, "ele": None})
    return lz, stats


def link_helipads(col, proj, cfg):
    """Split helipads into hospital helipads (destinations) and other helipads (usable as LZ)."""
    max_d = float(cfg["hospitals"]["helipad_max_dist_m"])
    hosp_geoms = []
    for h in col.hospitals:
        if h["geom"] is not None:
            hosp_geoms.append(proj.geom(h["geom"]))
        else:
            x, y = proj.xy(h["lon"], h["lat"])
            hosp_geoms.append(Point(float(x), float(y)))
    hosp_geoms = np.array(hosp_geoms, dtype=object)
    tree = shapely.STRtree(hosp_geoms) if len(hosp_geoms) else None
    hospital_pads, other_pads = [], []
    for hp in col.helipads:
        x, y = proj.xy(hp["lon"], hp["lat"])
        p = Point(float(x), float(y))
        best = None
        if tree is not None:
            idx = tree.query(p, predicate="dwithin", distance=max_d)
            if len(idx):
                d = shapely.distance(p, hosp_geoms[idx])
                j = int(np.argmin(d))
                best = (int(idx[j]), float(d[j]))
        rec = dict(hp, x=float(x), y=float(y))
        if best is not None:
            h = col.hospitals[best[0]]
            rec.update(hospital_name=h["name"], hospital_ref=h["ref"],
                       hospital_emergency=h["emergency"], dist=best[1])
            hospital_pads.append(rec)
        else:
            other_pads.append(rec)
    return hospital_pads, other_pads


def build_sites(col, proj, cfg):
    sc = cfg["sites"]
    cell = float(sc["thin_cell_m"])
    spacing = float(sc["line_sample_spacing_m"])
    raw = []
    if col.site_points:
        kinds, lons, lats, refs, details = zip(*col.site_points)
        xs, ys = proj.xy(lons, lats)
        raw.extend(zip(kinds, xs, ys, refs, details))
    for kind, g_ll, ref, detail in col.site_lines:
        g = proj.geom(g_ll)
        L = g.length
        if L <= 0:
            continue
        dists = np.arange(spacing / 2, L, spacing) if L > spacing / 2 else np.array([L / 2])
        pts = shapely.line_interpolate_point(g, dists)
        for p in pts:
            raw.append((kind, p.x, p.y, ref, detail))
    # thin: one site per kind per grid cell
    seen, sites = set(), []
    for kind, x, y, ref, detail in raw:
        key = (kind, int(x // cell), int(y // cell))
        if key in seen:
            continue
        seen.add(key)
        sites.append({"kind": kind, "x": float(x), "y": float(y), "ref": ref, "detail": detail or "", "ele": None})
    return sites, len(raw)


def link_sites(sites, lz, cfg):
    sc = cfg["sites"]
    max_d = float(sc["site_to_lz_max_m"])
    n_alt = int(sc["lz_alternatives"])
    if not lz:
        return [], []
    lz_pts = shapely.points([l["x"] for l in lz], [l["y"] for l in lz])
    tree = shapely.STRtree(lz_pts)
    kept, links = [], []
    for s in sites:
        p = Point(s["x"], s["y"])
        idx = tree.query(p, predicate="dwithin", distance=max_d)
        if len(idx) == 0:
            continue
        d = shapely.distance(p, lz_pts[idx])
        order = np.argsort(d)[:n_alt]
        s_id = len(kept) + 1
        kept.append(s)
        for rank, j in enumerate(order):
            links.append((s_id, int(idx[j]) + 1, round(float(d[j]), 1), rank))
    return kept, links


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE hospital_helipads (
  id INTEGER PRIMARY KEY, lat REAL NOT NULL, lon REAL NOT NULL, elev_m REAL,
  helipad_name TEXT, helipad_osm TEXT, hospital_name TEXT, hospital_osm TEXT,
  hospital_emergency TEXT, dist_to_hospital_m REAL);
CREATE TABLE lz (
  id INTEGER PRIMARY KEY, lat REAL NOT NULL, lon REAL NOT NULL, kind TEXT NOT NULL,
  clear_radius_m REAL, area_m2 REAL, elev_m REAL, slope_deg REAL, osm TEXT);
CREATE TABLE sites (
  id INTEGER PRIMARY KEY, lat REAL NOT NULL, lon REAL NOT NULL, kind TEXT NOT NULL,
  detail TEXT, elev_m REAL, osm TEXT);
CREATE TABLE site_lz (
  site_id INTEGER NOT NULL REFERENCES sites(id), lz_id INTEGER NOT NULL REFERENCES lz(id),
  dist_m REAL NOT NULL, rank INTEGER NOT NULL, PRIMARY KEY (site_id, rank));
CREATE INDEX ix_hh_pos ON hospital_helipads(lat, lon);
CREATE INDEX ix_lz_pos ON lz(lat, lon);
CREATE INDEX ix_sites_pos ON sites(lat, lon);
CREATE INDEX ix_sites_kind ON sites(kind);
CREATE TABLE bases (
  id INTEGER PRIMARY KEY, lat REAL NOT NULL, lon REAL NOT NULL, ident TEXT, name TEXT,
  kind TEXT, elev_m REAL, osm TEXT);
"""


def nan_to_none(v):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else round(f, 1)


def parse_ele(v):
    if not v:
        return None
    try:
        return float(str(v).replace("m", "").replace(",", ".").strip())
    except ValueError:
        return None


def write_db(path, proj, hospital_pads, other_pads, lz, sites, links, meta):
    """Writes the SQLite file (skipped when path is None) and returns the LZ and site rows."""
    con = None
    if path:
        if os.path.exists(path):
            os.remove(path)
        con = sqlite3.connect(path)
        con.executescript(SCHEMA)

    def ll(x, y):
        lon, lat = proj.lonlat(x, y)
        return round(float(lat), 6), round(float(lon), 6)

    rows = []
    for hp in hospital_pads:
        rows.append((hp["lat"], hp["lon"], nan_to_none(hp.get("ele_m")), hp["name"], hp["ref"],
                     hp["hospital_name"], hp["hospital_ref"], hp["hospital_emergency"], round(hp["dist"], 1)))
    if con:
        con.executemany("INSERT INTO hospital_helipads (lat, lon, elev_m, helipad_name, helipad_osm, hospital_name, "
                        "hospital_osm, hospital_emergency, dist_to_hospital_m) VALUES (?,?,?,?,?,?,?,?,?)",
                        [(round(r[0], 6), round(r[1], 6)) + r[2:] for r in rows])

    lz_rows = []
    for l in lz:
        lat, lon = ll(l["x"], l["y"])
        lz_rows.append((lat, lon, l["kind"], nan_to_none(l["clear"]), nan_to_none(l["area"]),
                        nan_to_none(l["ele"]), nan_to_none(l.get("slope")), l["ref"]))
    if con:
        con.executemany("INSERT INTO lz (lat, lon, kind, clear_radius_m, area_m2, elev_m, slope_deg, osm) "
                        "VALUES (?,?,?,?,?,?,?,?)", lz_rows)

    s_rows = []
    for s in sites:
        lat, lon = ll(s["x"], s["y"])
        s_rows.append((lat, lon, s["kind"], s["detail"], nan_to_none(s["ele"]), s["ref"]))
    if con:
        con.executemany("INSERT INTO sites (lat, lon, kind, detail, elev_m, osm) VALUES (?,?,?,?,?,?)", s_rows)
        con.executemany("INSERT INTO site_lz (site_id, lz_id, dist_m, rank) VALUES (?,?,?,?)", links)
        con.executemany("INSERT INTO meta (key, value) VALUES (?,?)", list(meta.items()))
        con.commit()
        con.execute("VACUUM")
        con.close()
    return lz_rows, s_rows


def collect_bases(col):
    """Airports, heliports and helipads a pilot can pick as home base (named or with an identifier)."""
    out, seen = [], set()
    for a in col.aerodromes + col.helipads:
        name = (a.get("name") or "").strip()
        ident = (a.get("ident") or "").strip().upper()
        if not name and not ident:
            continue
        key = (round(a["lat"], 3), round(a["lon"], 3), ident or name)
        if key in seen:
            continue
        seen.add(key)
        out.append((round(a["lat"], 6), round(a["lon"], 6), ident, name, a.get("kind") or "",
                    parse_ele(a.get("ele")), a["ref"]))
    return out


def write_bases(path, bases):
    con = sqlite3.connect(path)
    con.executemany("INSERT INTO bases (lat, lon, ident, name, kind, elev_m, osm) VALUES (?,?,?,?,?,?,?)", bases)
    con.commit()
    con.close()


def write_app_json(path, hospital_pads, lz_rows, s_rows, links, bases, meta):
    """Compact file read by the HEMS Dispatch app (no database library needed on the app side)."""
    lz_kinds = sorted({r[2] for r in lz_rows})
    site_kinds = sorted({r[2] for r in s_rows})
    lz_k = {k: i for i, k in enumerate(lz_kinds)}
    site_k = {k: i for i, k in enumerate(site_kinds)}
    per_site = {}
    for site_id, lz_id, dist, rank in links:
        per_site.setdefault(site_id, []).append((rank, lz_id - 1, dist))
    sites = []
    for i, r in enumerate(s_rows, start=1):
        row = [r[0], r[1], site_k[r[2]]]
        for _, lz_index, dist in sorted(per_site.get(i, [])):
            row += [lz_index, round(dist)]
        sites.append(row)
    data = {
        "schema_version": 1,
        "attribution": meta["attribution"],
        "source_file": meta["source_file"],
        "source_timestamp": meta["source_timestamp"],
        "built_at": meta["built_at"],
        "bounds": json.loads(meta["bounds"]),
        "lz_kinds": lz_kinds,
        "site_kinds": site_kinds,
        "bases": [[b[0], b[1], b[2], b[3], b[4], b[5]] for b in bases],
        "hospitals": [[round(h["lat"], 6), round(h["lon"], 6), h["hospital_name"] or "", h["name"] or "",
                       nan_to_none(h.get("ele_m"))] for h in hospital_pads],
        "lz": [[r[0], r[1], lz_k[r[2]], r[5], r[6]] for r in lz_rows],
        "sites": sites,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))


def write_geojson(path, hospital_pads, other_pads, lz_rows, s_rows, max_sites=5000):
    feats = []
    for hp in hospital_pads:
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [hp["lon"], hp["lat"]]},
                      "properties": {"layer": "hospital_helipad", "hospital": hp["hospital_name"],
                                     "marker-color": "#c0261c"}})
    for r in lz_rows:
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [r[1], r[0]]},
                      "properties": {"layer": "lz", "kind": r[2], "clear_radius_m": r[3], "slope_deg": r[6],
                                     "marker-color": "#1d7a3a"}})
    step = max(1, len(s_rows) // max_sites)
    for r in s_rows[::step]:
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [r[1], r[0]]},
                      "properties": {"layer": "site", "kind": r[2], "detail": r[3], "marker-color": "#0a6a8a"}})
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": feats}, fh)


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Build the HEMS Dispatch SQLite database from an OSM extract.")
    ap.add_argument("input", help="OSM extract (.osm.pbf, .osm, .osm.gz)")
    ap.add_argument("-o", "--output", help="SQLite file to write (overwritten). Optional with --app-json")
    ap.add_argument("-c", "--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
    ap.add_argument("--dem-glob", help="GeoTIFF elevation tiles, e.g. \"dem/*.tif\" (overrides config)")
    ap.add_argument("--geojson", help="Also write a GeoJSON preview to check the result on a map")
    ap.add_argument("--app-json", help="Also write the compact data file used by the app (e.g. hems_region.json)")
    ap.add_argument("--node-index", default="flex_mem",
                    help="osmium node location index, e.g. flex_mem (fast) or sparse_file_array,nodes.idx (low RAM)")
    args = ap.parse_args()
    if not args.output and not args.app_json:
        ap.error("give -o and/or --app-json")

    with open(args.config, encoding="utf-8") as fh:
        cfg = json.load(fh)
    dem_glob = args.dem_glob if args.dem_glob is not None else cfg.get("dem", {}).get("glob", "")
    t0 = time.time()

    log(f"Reading {args.input}")
    col = Collector(cfg)
    col.apply_file(args.input, locations=True, idx=args.node_index)
    log(f"  open areas {len(col.open_areas)}, obstacles {len(col.obst_geoms)}, buildings {len(col.buildings) // 4}, "
        f"hospitals {len(col.hospitals)}, helipads {len(col.helipads)}, geometry errors {col.errors}")
    if col.bounds[0] > col.bounds[2]:
        sys.exit("No usable data found in the extract.")

    proj = Proj(col.bounds)
    log(f"Projection EPSG:{proj.epsg}")

    dem = Dem(dem_glob) if dem_glob else None
    if dem_glob and not dem:
        log(f"  WARNING: no DEM tile matches {dem_glob!r}, altitude and slope skipped")
        dem = None

    log("Indexing obstacles")
    tree, geoms, bufs = build_obstacle_index(col, proj, cfg)
    col.obst_geoms = []           # projected copies are in the index now
    col.buildings = array("d")
    gc.collect()

    log("Finding landing zones")
    lz, lz_stats = build_lz(col, proj, cfg, tree, geoms, bufs)
    col.open_areas = []
    del tree, geoms, bufs
    gc.collect()
    hospital_pads, other_pads = link_helipads(col, proj, cfg)
    for hp in other_pads:  # helipads that are not a hospital destination can serve as LZ
        lz.append({"x": hp["x"], "y": hp["y"], "kind": "helipad", "clear": None, "area": None,
                   "ref": hp["ref"], "ele": parse_ele(hp.get("ele"))})
    for hp in hospital_pads:
        hp["ele_m"] = parse_ele(hp.get("ele"))

    slope_rejected = 0
    no_dem_cover = 0
    if dem:
        log("Sampling elevation and slope")
        probe = float(cfg["dem"].get("slope_probe_m", 30))
        max_slope = float(cfg["lz"]["max_slope_deg"])
        if lz:
            e, s = elev_and_slope(dem, proj, [l["x"] for l in lz], [l["y"] for l in lz], probe)
            kept = []
            for l, ei, si in zip(lz, e, s):
                if l["kind"] != "helipad" and not np.isnan(si) and si > max_slope:
                    slope_rejected += 1
                    continue
                if np.isnan(si):
                    no_dem_cover += 1
                if l["ele"] is None:
                    l["ele"] = ei
                l["slope"] = si
                kept.append(l)
            lz = kept
        if hospital_pads:
            xs = [h["x"] for h in hospital_pads]
            ys = [h["y"] for h in hospital_pads]
            e = dem.sample(*proj.lonlat(xs, ys))
            for h, ei in zip(hospital_pads, e):
                if h["ele_m"] is None:
                    h["ele_m"] = ei

    log("Building emergency sites")
    sites, raw_sites = build_sites(col, proj, cfg)
    sites, links = link_sites(sites, lz, cfg)
    if dem and sites:
        e = dem.sample(*proj.lonlat([s["x"] for s in sites], [s["y"] for s in sites]))
        for s, ei in zip(sites, e):
            s["ele"] = ei

    reader = osmium.io.Reader(args.input, osmium.osm.osm_entity_bits.NOTHING)
    header_ts = reader.header().get("osmosis_replication_timestamp") or ""
    reader.close()
    meta = {
        "attribution": ATTRIBUTION,
        "license": "ODbL-1.0",
        "source_file": os.path.basename(args.input),
        "source_timestamp": header_ts or (col.latest_edit.isoformat() if col.latest_edit else ""),
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bounds": json.dumps([round(v, 5) for v in col.bounds]),
        "dem": "yes" if dem else "no",
        "config": json.dumps(cfg, separators=(",", ":")),
        "schema_version": "1",
    }
    if args.output:
        log(f"Writing {args.output}")
    lz_rows, s_rows = write_db(args.output, proj, hospital_pads, other_pads, lz, sites, links, meta)
    bases = collect_bases(col)
    if args.output:
        write_bases(args.output, bases)
    if args.app_json:
        write_app_json(args.app_json, hospital_pads, lz_rows, s_rows, links, bases, meta)
        log(f"App data written to {args.app_json} ({os.path.getsize(args.app_json) / 1e6:.1f} MB)")
    if args.geojson:
        write_geojson(args.geojson, hospital_pads, other_pads, lz_rows, s_rows)
        log(f"Preview written to {args.geojson}")

    by_kind = {}
    for l in lz:
        by_kind[l["kind"]] = by_kind.get(l["kind"], 0) + 1
    site_kinds = {}
    for s in sites:
        site_kinds[s["kind"]] = site_kinds.get(s["kind"], 0) + 1
    print()
    print("Summary")
    print(f"  Hospital helipads : {len(hospital_pads)}  (hospitals in extract: {len(col.hospitals)})")
    print(f"  Possible bases    : {len(bases)}  (airports, heliports and named helipads)")
    print(f"  Landing zones     : {len(lz)}  {dict(sorted(by_kind.items()))}")
    print(f"    open areas checked {lz_stats['areas']}, candidate points {lz_stats['candidates']}, "
          f"rejected: too close to area edge {lz_stats['rejected_inside']}, "
          f"obstacle {lz_stats['rejected_obstacle']}, slope {slope_rejected}")
    if dem and no_dem_cover:
        print(f"    WARNING: {no_dem_cover} LZ outside the DEM tiles, slope not checked (add the missing tiles)")
    if not dem:
        print("    NOTE: no DEM given, slope not checked. Strongly recommended for hilly or mountain regions.")
    print(f"  Emergency sites   : {len(sites)} with an LZ within {cfg['sites']['site_to_lz_max_m']} m "
          f"(from {raw_sites} raw points)  {dict(sorted(site_kinds.items()))}")
    print(f"  Done in {time.time() - t0:.1f} s")


if __name__ == "__main__":
    main()
