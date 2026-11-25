#!/usr/bin/env python3
"""Build the pinned tile manifest for the Copernicus DEM GLO-30 2024_1 mirror.

    uv run tools/manifest.py            # build manifest/tiles.csv.gz + summary
    uv run tools/manifest.py --check    # rebuild and fail if it differs

Two upstream listings go in:

1. The CDSE OData catalogue (anonymous). It is the authority on what release
   2024_1 contains: one product per 1 degree tile, its S3 path, size,
   acquisition interval and ``eopIdentifier``. The identifier's suffix
   (``...:VE47Q-2020_2``) is the release in which the tile last changed.
2. The AWS Open Data bucket ``s3://copernicus-dem-30m`` (anonymous). It holds
   the 2021 release as COGs, trimmed to 3600 px.

A tile is sourced from AWS when it last changed in a release that AWS holds
(``AWS_RELEASES``) and AWS has every file we need for it. Every other tile is
sourced from CDSE. The pixel identity of AWS and CDSE copies of "unchanged"
tiles is not assumed here; ``tools/verify_equiv.py`` tests it.

The manifest is committed. ``tools/bulk.py`` processes exactly this file and
never lists the upstream itself, so a run is reproducible from git.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import gzip
import io
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ODATA = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
RELEASE = "COP-DEM_GLO-30-DGED/2024_1"
AWS_BUCKET = "copernicus-dem-30m"
# Releases whose content the AWS bucket carries. The bucket is the 2021
# release; no 2024_1 tile carries a 2021_1 suffix, so the last content
# change AWS can hold is 2020_2.
AWS_RELEASES = {"2019_1", "2019_2", "2020_1", "2020_2", "2021_1"}
# The files each item needs on AWS, by asset key: a regex on the object key.
# The DGED layout on CDSE is read from the product itself by tools/bulk.py.
ASSETS = {
    "dem": r"/Copernicus_DSM_COG_10_[^/]+_DEM\.tif$",
    "edm": r"/AUXFILES/[^/]+_EDM\.tif$",
    "flm": r"/AUXFILES/[^/]+_FLM\.tif$",
    "hem": r"/AUXFILES/[^/]+_HEM\.tif$",
    "wbm": r"/AUXFILES/[^/]+_WBM\.tif$",
    "xml": r"/Copernicus_DSM_10_[^/]+\.xml$",
    "src": r"/PREVIEW/[^/]+_SRC\.kml$",
}
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "manifest" / "tiles.csv.gz"
SUMMARY = ROOT / "manifest" / "summary.json"
EXPECTED = {"tiles": 26475, "aws": 26248, "cdse": 227}


def odata_window(start: str, end: str) -> list[dict]:
    """All 2024_1 products whose ContentDate starts in [start, end)."""
    flt = (
        "startswith(Name,'DEM1_SAR_DGE_30') and Attributes/OData.CSC.StringAttribute/any("
        f"att:att/Name eq 'dataset' and att/OData.CSC.StringAttribute/Value eq '{RELEASE}')"
        f" and ContentDate/Start ge {start} and ContentDate/Start lt {end}"
    )
    rows, skip = [], 0
    while True:
        q = urllib.parse.urlencode({
            "$filter": flt, "$top": 1000, "$skip": skip,
            "$orderby": "ContentDate/Start asc", "$expand": "Attributes",
        })
        for attempt in range(6):
            try:
                with urllib.request.urlopen(f"{ODATA}?{q}", timeout=180) as r:
                    page = json.load(r)["value"]
                break
            except Exception:  # noqa: BLE001 - network retry
                if attempt == 5:
                    raise
                time.sleep(2 ** attempt)
        for p in page:
            a = {x["Name"]: x["Value"] for x in p["Attributes"]}
            rows.append({
                "grid": a["gridId"],
                "cdse_name": p["Name"],
                "cdse_s3": p["S3Path"].removeprefix("/eodata/"),
                "cdse_size": p["ContentLength"],
                "eop": a["eopIdentifier"],
                "release_changed": a["eopIdentifier"].rsplit("-", 1)[1],
                "start_datetime": a["beginningDateTime"],
                "end_datetime": a["endingDateTime"],
            })
        if len(page) < 1000:
            return rows
        skip += 1000


def list_odata() -> dict[str, dict]:
    # Page by month. Some products carry a placeholder ContentDate
    # (2020-11-12), so the window range runs through 2021.
    months = [(y, m) for y in range(2008, 2022) for m in range(1, 13)]

    def win(ym):
        y, m = ym
        nxt = (y + (m == 12), m % 12 + 1)
        return odata_window(f"{y}-{m:02d}-01T00:00:00.000Z", f"{nxt[0]}-{nxt[1]:02d}-01T00:00:00.000Z")

    out: dict[str, dict] = {}
    with cf.ThreadPoolExecutor(8) as ex:
        for rows in ex.map(win, months):
            for r in rows:
                if r["grid"] in out and out[r["grid"]]["cdse_name"] != r["cdse_name"]:
                    sys.exit(f"two 2024_1 products for {r['grid']}")
                out[r["grid"]] = r
    return out


def list_aws() -> dict[str, dict]:
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config

    s3 = boto3.client("s3", region_name="eu-central-1", config=Config(signature_version=UNSIGNED))
    pat = re.compile(r"^Copernicus_DSM_COG_10_([NS]\d\d)_00_([EW]\d\d\d)_00_DEM/")
    tiles: dict[str, dict] = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=AWS_BUCKET):
        for o in page.get("Contents", []):
            m = pat.match(o["Key"])
            if not m:
                continue
            grid = f"{m[1]}_{m[2]}"
            for asset, rx in ASSETS.items():
                if re.search(rx, o["Key"]):
                    t = tiles.setdefault(grid, {})
                    t[f"aws_{asset}_key"] = o["Key"]
                    t[f"aws_{asset}_size"] = o["Size"]
                    t[f"aws_{asset}_etag"] = o["ETag"].strip('"')
    return tiles


def build() -> tuple[list[dict], dict]:
    cdse = list_odata()
    aws = list_aws()
    rows = []
    for grid in sorted(cdse):
        r = dict(cdse[grid])
        a = aws.get(grid, {})
        complete = all(f"aws_{k}_key" in a for k in ASSETS)
        r["source"] = "aws" if (r["release_changed"] in AWS_RELEASES and complete) else "cdse"
        r.update(a)
        r["item_id"] = grid
        rows.append(r)
    unknown = sorted(set(aws) - set(cdse))
    summary = {
        "release": RELEASE,
        "tiles": len(rows),
        "aws": sum(r["source"] == "aws" for r in rows),
        "cdse": sum(r["source"] == "cdse" for r in rows),
        "cdse_bytes_raw": sum(r["cdse_size"] for r in rows if r["source"] == "cdse"),
        "aws_bytes_cog": sum(
            r.get(f"aws_{k}_size", 0) for r in rows if r["source"] == "aws" for k in ASSETS
        ),
        "by_release_changed": {},
        "cdse_reasons": {
            "absent_from_aws": sum(1 for r in rows if r["grid"] not in aws),
            "incomplete_on_aws": sum(
                1 for r in rows
                if r["grid"] in aws and not all(f"aws_{k}_key" in aws[r["grid"]] for k in ASSETS)
            ),
            "changed_after_aws": sum(
                1 for r in rows if r["grid"] in aws and r["release_changed"] not in AWS_RELEASES
            ),
        },
        "aws_tiles_not_in_release": unknown,
    }
    for r in rows:
        k = r["release_changed"]
        summary["by_release_changed"][k] = summary["by_release_changed"].get(k, 0) + 1
    return rows, summary


def serialize(rows: list[dict]) -> bytes:
    cols = sorted({k for r in rows for k in r}, key=lambda c: (not c.startswith(("grid", "item")), c))
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    # mtime=0 keeps the gzip bytes deterministic, so git sees real changes only.
    return gzip.compress(buf.getvalue().encode(), mtime=0)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="fail if the upstream differs from the committed manifest")
    args = ap.parse_args()
    rows, summary = build()
    data = serialize(rows)
    for k, v in EXPECTED.items():
        if summary[k] != v:
            print(f"WARNING: {k} = {summary[k]}, expected {v}", file=sys.stderr)
    if args.check:
        same = OUT.exists() and OUT.read_bytes() == data
        print("manifest matches upstream" if same else "manifest DIFFERS from upstream")
        sys.exit(0 if same else 1)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_bytes(data)
    SUMMARY.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k != "aws_tiles_not_in_release"}, indent=2))


if __name__ == "__main__":
    main()
