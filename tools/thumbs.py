#!/usr/bin/env python3
"""Render both collections' thumbnails into catalog/<collection>/thumbnail.png.

    python3 -I tools/thumbs.py [dem|acquisitions ...]

glo-30-2024-1: a 3:2 window of the overview COG (tools/overview.py) at its
native resolution, cut with GDAL. The overview is already the collection's
rendering, so no second style is involved.

source-acquisitions: the collection's default MapLibre style rendered by
chiitiler, through the portolan-thumbnails skill's render_one.sh (framing,
the blank-probe gate, basemap pick). The skill reads the PMTiles next to
collection.json, but the PMTiles is staged outside catalog/ so it can never be
published by accident; so the render runs on a scratch copy of the collection
folder with the PMTiles linked in, and only the PNG comes back.

Needs a running chiitiler for the acquisitions render (the skill's
start_server.sh; chiitiler now reads CHIITILER_PORT etc. from the
environment). Every image must still be looked at before publishing (the
skill's Gate 2); this script only runs Gate 1. Re-run tools/stac.py after, so
file:size and file:checksum match the new bytes.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml
from osgeo import gdal

gdal.UseExceptions()
ROOT = Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text())
CAT = ROOT / "catalog"
STAGING = Path(CONFIG["staging_dir"])
CID = CONFIG["collection"]
T = CONFIG["thumbnails"]
SKILL = Path(os.path.expanduser(T["skill_dir"]))
WORK = Path(CONFIG["scratch"]).parent / "thumbs"
HALF = 20037508.342789244
W, H = 1024, 683


def merc(lon: float, lat: float) -> tuple[float, float]:
    import math
    return (lon * HALF / 180, math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * 6378137.0)


def dem() -> None:
    src = gdal.Open(str(STAGING / CID / f"{CID}_overview.tif"))
    gt = src.GetGeoTransform()
    x, y = merc(*T["dem_center"])
    col, row = int((x - gt[0]) / gt[1]), int((y - gt[3]) / gt[5])
    w, h = W, H
    # Shrink to fit the raster, keeping 3:2 (test builds cover a narrow band).
    while w > 120 and not (col - w // 2 >= 0 and row - h // 2 >= 0 and col + w // 2 <= src.RasterXSize
                           and row + h // 2 <= src.RasterYSize):
        w, h = w - 3, h - 2
    win = [col - w // 2, row - h // 2, w, h]
    out = CAT / CID / "thumbnail.png"
    gdal.Translate(str(out), src, format="PNG", srcWin=win, width=W, height=H, resampleAlg="bilinear")
    out.with_suffix(".png.aux.xml").unlink(missing_ok=True)
    print(json.dumps({"collection": CID, "window_px": win, "out": str(out)}))


def acquisitions() -> None:
    name = "source-acquisitions"
    src_dir = CAT / name
    work = WORK / name
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(src_dir, work, ignore=shutil.ignore_patterns("*.md", "thumbnail.png"))
    (work / f"{name}.pmtiles").symlink_to(STAGING / name / f"{name}.pmtiles")
    coll = json.loads((work / "collection.json").read_text())
    coll["assets"]["thumbnail"] = {"href": "./thumbnail.png", "type": "image/png", "roles": ["thumbnail"]}
    (work / "collection.json").write_text(json.dumps(coll))
    frame = json.loads(subprocess.run(
        ["python3", str(SKILL / "scripts" / "frame.py"), "--center", ",".join(map(str, T["acq_center"])),
         "--zoom", str(T["acq_zoom"]), "--size", "1024"], check=True, capture_output=True, text=True).stdout)
    env = {**os.environ, "WORK": str(WORK / "skill")}
    r = subprocess.run(["bash", str(SKILL / "scripts" / "render_one.sh"), str(work), frame["bbox"], "png"],
                       env=env, capture_output=True, text=True)
    print(r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "", r.stderr[-800:], file=sys.stderr)
    if r.returncode != 0 or "gate1=PASS" not in r.stdout:
        sys.exit(f"{name}: render failed Gate 1")
    shutil.copy(work / "thumbnail.png", src_dir / "thumbnail.png")
    print(json.dumps({"collection": name, "bbox": frame["bbox"], "zoom": frame["zoom"],
                      "out": str(src_dir / "thumbnail.png")}))


if __name__ == "__main__":
    which = sys.argv[1:] or ["dem", "acquisitions"]
    for w in which:
        {"dem": dem, "acquisitions": acquisitions}[w]()
