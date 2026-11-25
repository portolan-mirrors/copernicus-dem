#!/usr/bin/env python3
"""Gate before the bulk run: are the AWS tiles the 2024_1 tiles?

    .venv/bin/python -I tools/verify_equiv.py [--tiles A,B,...]

The mirror reads every tile whose 2024_1 eopIdentifier says it last changed
in 2019_2 or 2020_2 from the 2021 copy on AWS. That is only right if those
pixels equal the 2024_1 product on CDSE. For six such tiles, one per
longitude-spacing band of handbook Table 3 plus a second equatorial one,
this fetches both copies with bulk.py's own fetch code and compares all five
rasters exactly (NaN equals NaN), after trimming CDSE's last row and column,
which AWS dropped. It also compares the origin, the data type and the nodata
tag, and reports whether the XML and SRC.kml bytes match. Exit 0 only when
every raster matches.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import shutil
import sys
from pathlib import Path

import numpy as np
from osgeo import gdal

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bulk  # noqa: E402
from encode import RASTERS, find  # noqa: E402

gdal.UseExceptions()
WORK = Path(bulk.CONFIG["scratch"]).parent / "verify"
BANDS = [(0, 50), (50, 60), (60, 70), (70, 80), (80, 85)]


def lat_of(item: str) -> int:
    return int(item[1:3]) * (1 if item[0] == "N" else -1)


def pick(rows: list[dict]) -> list[dict]:
    aws = [r for r in rows if r["source"] == "aws"]
    out = []
    for lo, hi in BANDS:
        band = [r for r in aws if lo <= abs(lat_of(r["item_id"])) < hi]
        out.append(band[len(band) // 2])
    eq = [r for r in aws if abs(lat_of(r["item_id"])) < 50 and r["release_changed"] == "2020_2"]
    out.append(eq[len(eq) // 2])
    return out


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def compare(row: dict) -> dict:
    item = row["item_id"]
    base = WORK / item
    shutil.rmtree(base, ignore_errors=True)
    a_dir, c_dir = base / "aws", base / "cdse"
    bulk._init_worker("aws")
    bulk._fetch_aws(row, a_dir)
    bulk._init_worker("cdse")
    bulk._fetch_cdse(row, c_dir)
    res = {"item": item, "release_changed": row["release_changed"], "rasters": {}}
    for asset, (pattern, _, _) in RASTERS.items():
        a = gdal.Open(str(find(a_dir, pattern)))
        c = gdal.Open(str(find(c_dir, pattern)))
        ab, cb = a.GetRasterBand(1), c.GetRasterBand(1)
        aa, ca = ab.ReadAsArray(), cb.ReadAsArray()
        trimmed = ca[: aa.shape[0], : aa.shape[1]]
        res["rasters"][asset] = {
            "aws_shape": list(aa.shape), "cdse_shape": list(ca.shape),
            "cdse_extra": [ca.shape[0] - aa.shape[0], ca.shape[1] - aa.shape[1]],
            "same_origin": np.allclose(a.GetGeoTransform(), c.GetGeoTransform(), rtol=0, atol=1e-12),
            "dtype": [gdal.GetDataTypeName(ab.DataType), gdal.GetDataTypeName(cb.DataType)],
            "nodata": [ab.GetNoDataValue(), cb.GetNoDataValue()],
            "pixels_equal": bool(np.array_equal(aa, trimmed, equal_nan=True)),
        }
    res["xml_bytes_equal"] = sha(find(a_dir, "*.xml")) == sha(find(c_dir, "*.xml"))
    # find() skips PREVIEW/ on purpose (quicklooks), so glob the KML directly.
    ka, kc = (list(d.glob("PREVIEW/*_SRC.kml")) for d in (a_dir, c_dir))
    res["src_kml_bytes_equal"] = len(ka) == len(kc) == 1 and sha(ka[0]) == sha(kc[0])
    res["ok"] = all(r["pixels_equal"] and r["same_origin"] and r["cdse_extra"] == [1, 1]
                    for r in res["rasters"].values())
    shutil.rmtree(base, ignore_errors=True)
    return res


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tiles", help="comma-separated item ids instead of the default pick")
    args = ap.parse_args()
    with gzip.open(bulk.ROOT / "manifest" / "tiles.csv.gz", "rt") as f:
        rows = list(csv.DictReader(f))
    if args.tiles:
        want = set(args.tiles.split(","))
        chosen = [r for r in rows if r["item_id"] in want]
    else:
        chosen = pick(rows)
    results = [compare(r) for r in chosen]
    out = Path(bulk.CONFIG["log_dir"]) / "verify_equiv.json"
    out.write_text(json.dumps(results, indent=2))
    for r in results:
        eq = {k: v["pixels_equal"] for k, v in r["rasters"].items()}
        print(r["item"], r["release_changed"], "OK" if r["ok"] else "MISMATCH", eq,
              "shape", r["rasters"]["dem"]["aws_shape"], "+", r["rasters"]["dem"]["cdse_extra"],
              "xml_same", r["xml_bytes_equal"], "kml_same", r["src_kml_bytes_equal"])
    print(f"details: {out}")
    sys.exit(0 if all(r["ok"] for r in results) else 1)


if __name__ == "__main__":
    main()
