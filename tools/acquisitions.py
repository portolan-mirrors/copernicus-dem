#!/usr/bin/env python3
"""Build the source-acquisitions collection's GeoParquet and PMTiles.

    python3 -I tools/acquisitions.py [--partial]

Every Copernicus DEM tile ships PREVIEW/*_SRC.kml, one placemark per TanDEM-X
acquisition used to build that tile, plus one attribute-less outline of the
tile itself (dropped here). tools/bulk.py keeps each tile's KML. Each
acquisition placemark carries, in its balloon table:

    Acquisition Set, Acquisition Date, Phase Unwrap, Height of Ambiguity

An acquisition's polygon is its full footprint, not clipped to the tile, and
the same footprint recurs in every tile it contributed to. So the output has
one row per distinct (name, footprint), with the list of tiles that used it.
Values are kept as the KML gives them: the date has minute precision and the
KML states no time zone; Height of Ambiguity is the number as written.

Output (in staging_dir, uploaded with tools/upload_data.py):
    source-acquisitions/source-acquisitions.parquet   GeoParquet (gpio)
    source-acquisitions/source-acquisitions.pmtiles   PMTiles (tylertoo)
and summary.json beside them for tools/stac.py (not uploaded: .json is not
in upload_data.py's suffix allow-list).
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import re
import subprocess
from collections import defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text())
KML_DIR = Path(CONFIG["kml_dir"])
OUT = Path(CONFIG["scratch"]).parent / "source-acquisitions"   # intermediates
FINAL = Path(CONFIG["staging_dir"]) / "source-acquisitions"    # uploaded by tools/upload_data.py
NAME = "source-acquisitions"
TYLERTOO = str(Path(CONFIG["toolchain"]["tylertoo"]["bin"]).expanduser())
# Licence COP-DEM-GLO-30-F, Art. 6(b), the notice for adapted data.
ATTRIBUTION = ("produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH "
               "2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved")

PLACEMARK = re.compile(r"<Placemark>(.*?)</Placemark>", re.S)
FIELD = re.compile(r"<tr><td>(.*?):</td><td>(.*?)</td></tr>")


def parse(path: Path) -> list[dict]:
    out = []
    for p in PLACEMARK.findall(path.read_text(encoding="utf-8")):
        fields = dict(FIELD.findall(p))
        if "Acquisition Set" not in fields:
            continue  # the tile outline
        coords = [tuple(c.split(",")[:2]) for c in re.search(r"<coordinates>(.*?)</coordinates>", p, re.S)[1].split()]
        if coords[0] != coords[-1]:
            coords.append(coords[0])
        if len(re.findall(r"<coordinates>", p)) != 1 or "innerBoundaryIs" in p:
            raise SystemExit(f"{path}: unexpected geometry shape")
        out.append({
            "name": re.search(r"<name>(.*?)</name>", p)[1].strip(),
            "acquisition_set": fields["Acquisition Set"].strip(),
            "acquisition_time": fields["Acquisition Date"].strip(),
            "phase_unwrap": fields["Phase Unwrap"].strip(),
            "height_of_ambiguity": float(fields["Height of Ambiguity"]),
            "wkt": "POLYGON((" + ", ".join(f"{x} {y}" for x, y in coords) + "))",
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--partial", action="store_true")
    args = ap.parse_args()
    with gzip.open(ROOT / "manifest" / "tiles.csv.gz", "rt") as f:
        ids = [r["item_id"] for r in csv.DictReader(f)]
    have = [i for i in ids if (KML_DIR / f"{i}_SRC.kml").exists()]
    if len(have) != len(ids) and not args.partial:
        raise SystemExit(f"{len(ids) - len(have)} tiles have no SRC.kml yet; pass --partial for a test build")

    rows: dict[tuple, dict] = {}
    tiles: dict[tuple, list[str]] = defaultdict(list)
    conflicts = 0
    for tile in have:
        for r in parse(KML_DIR / f"{tile}_SRC.kml"):
            key = (r["name"], hashlib.sha1(r["wkt"].encode()).hexdigest())
            if key in rows:
                a, b = rows[key], r
                if any(a[k] != b[k] for k in ("acquisition_set", "acquisition_time", "phase_unwrap", "height_of_ambiguity")):
                    conflicts += 1  # same footprint, different attributes: keep both
                    key = key + (len(tiles),)
                    rows[key] = r
            else:
                rows[key] = r
            tiles[key].append(tile)

    OUT.mkdir(parents=True, exist_ok=True)
    for d in (OUT, FINAL):  # every output is rebuilt from the KMLs
        for f in d.glob("*") if d.exists() else []:
            if f.is_file():
                f.unlink()
    nd = OUT / "rows.ndjson"
    with open(nd, "w") as f:
        for key, r in rows.items():
            f.write(json.dumps({**r, "acquisition_time": r["acquisition_time"].replace("T", " ") + ":00",
                                "tiles": sorted(tiles[key]), "tile_count": len(tiles[key])}) + "\n")
    raw = OUT / "raw.parquet"
    subprocess.run(["duckdb", "-c", f"""
        COPY (SELECT name, acquisition_set, CAST(acquisition_time AS TIMESTAMP) AS acquisition_time,
                     phase_unwrap, height_of_ambiguity, tiles, tile_count, wkt
              FROM read_json('{nd}', format='newline_delimited', columns={{
                  name:'VARCHAR', acquisition_set:'VARCHAR', acquisition_time:'VARCHAR',
                  phase_unwrap:'VARCHAR', height_of_ambiguity:'DOUBLE', tiles:'VARCHAR[]',
                  tile_count:'INTEGER', wkt:'VARCHAR'}}))
        TO '{raw}' (FORMAT parquet)"""], check=True)
    FINAL.mkdir(parents=True, exist_ok=True)
    pq = FINAL / f"{NAME}.parquet"
    log = open(OUT / "build.log", "w")
    # GeoParquet 2.0 because the spec asks for it when tools can write it
    # (PORTO-FMT-080), plus a bbox covering column (PORTO-FMT-008).
    nobbox = OUT / "nobbox.parquet"
    subprocess.run(["gpio", "convert", "geoparquet", str(raw), str(nobbox), "--wkt-column", "wkt",
                    "--geoparquet-version", "2.0"], check=True, stdout=log, stderr=log)
    subprocess.run(["gpio", "add", "bbox", str(nobbox), str(pq), "--geoparquet-version", "2.0"],
                   check=True, stdout=log, stderr=log)
    subprocess.run(["gpio", "check", "all", str(pq)], check=True, stdout=log, stderr=log)
    pm = FINAL / f"{NAME}.pmtiles"
    # tylertoo, built from the commit pinned in config.yaml (make tylertoo).
    # --size-valve drop: footprints overlap, and the default merge valve would
    # paint one footprint's attributes over its merged neighbours. Dropping
    # keeps every drawn footprint's own attributes. z10 holds the KML's
    # vertices to about 10 m (extent 4096).
    subprocess.run([TYLERTOO, "tiles", str(pq), str(pm), "--force", "--min-zoom", "0", "--max-zoom", "10",
                    "--layer-name", NAME, "--size-valve", "drop", "--name", "Copernicus DEM source acquisitions",
                    "--description", "TanDEM-X acquisition footprints listed in the Copernicus DEM GLO-30 SRC.kml files",
                    "--attribution", ATTRIBUTION, "--report", str(OUT / "tylertoo-report.json")],
                   check=True, stdout=log, stderr=log)
    summary = {"tiles": len(have), "placemarks": sum(len(v) for v in tiles.values()), "rows": len(rows),
               "attribute_conflicts": conflicts,
               "sets": sorted({r["acquisition_set"] for r in rows.values()}),
               "phase_unwrap": sorted({r["phase_unwrap"] for r in rows.values()}),
               "time_range": [min(r["acquisition_time"] for r in rows.values()),
                              max(r["acquisition_time"] for r in rows.values())],
               "parquet_bytes": pq.stat().st_size, "pmtiles_bytes": pm.stat().st_size}
    (FINAL / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
