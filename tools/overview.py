#!/usr/bin/env python3
"""Build the collection overview COG: hypsometric tint times hillshade.

    python3 -I tools/overview.py [--partial] [--workers 64]

The overview is an 8-bit RGBA COG in Web Mercator (EPSG:3857) for maps of the
whole world (PORTO-FMT-067 to 069). It is a picture, not elevation data.

1. Warp every published DEM COG, read over HTTP through its internal
   overviews, onto one target-aligned EPSG:3857 grid at web-map zoom 7
   (about 1223 m per pixel), as float32. Cached per tile, so reruns resume.
2. Mosaic the warped tiles with a VRT, so the hillshade is computed across
   tile edges and shows no seams.
3. In row strips: shade with Horn's method (azimuth 315, altitude 45). Web
   Mercator stretches ground distances by 1/cos(latitude), so each row's
   slopes are corrected by that factor and relief keeps its strength at high
   latitudes. Colour by the hypsometric ramp below, multiply by the shade
   scaled into [0.45, 1], and set alpha 0 where no tile has data.
4. Write a COG (DEFLATE, 512 px blocks, internal overviews).

Web Mercator ends at about 85.05 degrees, so tiles south of that in
Antarctica are not in the overview. They are in the items.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import gzip
import json
import math
import os
from pathlib import Path

import numpy as np
import yaml
from osgeo import gdal

gdal.UseExceptions()
ROOT = Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text())
CID = CONFIG["collection"]
PUBLIC = CONFIG["upload"]["public_base"]
WORK = Path(CONFIG["scratch"]).parent / "overview"
OUT = Path(CONFIG["staging_dir"]) / CID / f"{CID}_overview.tif"

HALF = 20037508.342789244          # half the EPSG:3857 world width, metres
ZOOM = 7
SIZE = 256 * 2 ** ZOOM             # 32768 px across the world
RES = 2 * HALF / SIZE              # 1222.99 m
AZIMUTH, ALTITUDE = 315.0, 45.0
SHADE_MIN = 0.45

# Hypsometric ramp (metres -> RGB). Ours, chosen for legibility; not from the
# producer. Values between stops are interpolated linearly.
RAMP = [
    (-450, (38, 99, 64)), (0, (52, 128, 70)), (150, (96, 158, 82)), (400, (163, 192, 110)),
    (800, (224, 214, 145)), (1500, (210, 172, 118)), (2500, (178, 130, 92)),
    (3500, (150, 112, 98)), (4500, (176, 162, 160)), (5500, (222, 218, 218)), (7000, (255, 255, 255)),
]

VSI = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR", "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    "GDAL_HTTP_MULTIPLEX": "YES", "GDAL_HTTP_MAX_RETRY": "5", "GDAL_HTTP_RETRY_DELAY": "2",
    "VSI_CACHE": "TRUE",
}


def warp_tile(item_id: str) -> str | None:
    for k, v in VSI.items():
        gdal.SetConfigOption(k, v)
    dst = WORK / "tiles" / f"{item_id}.tif"
    if dst.exists():
        return str(dst)
    src = f"/vsicurl/{PUBLIC}/{CID}/{item_id}/{item_id}_DEM.tif"
    tmp = dst.with_suffix(".tmp.tif")
    try:
        gdal.Warp(str(tmp), src, dstSRS="EPSG:3857", xRes=RES, yRes=RES, targetAlignedPixels=True,
                  resampleAlg="average", outputType=gdal.GDT_Float32, dstNodata=-32767,
                  outputBounds=None, creationOptions=["COMPRESS=DEFLATE", "TILED=YES"],
                  warpOptions=["SAMPLE_GRID=YES"], multithread=False)
    except RuntimeError as exc:
        if "Too many points" in str(exc) or "outside" in str(exc).lower():
            return None  # beyond the Mercator limit
        raise
    os.replace(tmp, dst)
    return str(dst)


def ramp_lut() -> np.ndarray:
    """RGB for every whole metre from -500 to 9000, as a lookup table."""
    z = np.arange(-500, 9001, dtype=np.float64)
    xs = [s for s, _ in RAMP]
    return np.stack([np.interp(z, xs, [c[i] for _, c in RAMP]) for i in range(3)], axis=1).astype(np.uint8)


def shade(dem: np.ndarray, row0: int) -> np.ndarray:
    """Horn hillshade in [0, 1] for rows of the global grid starting at row0.
    dem has one extra row above and below and NaN for no data."""
    rows = np.arange(row0, row0 + dem.shape[0] - 2)
    y = HALF - (rows + 0.5) * RES
    lat = np.degrees(2 * np.arctan(np.exp(y / 6378137.0)) - math.pi / 2)
    ground = RES * np.cos(np.radians(lat))[:, None]  # metres per pixel on the ground
    z = np.where(np.isnan(dem), 0.0, dem)
    p = np.pad(z, ((0, 0), (1, 1)), mode="edge")
    a, b, c = p[:-2, :-2], p[:-2, 1:-1], p[:-2, 2:]
    d, f = p[1:-1, :-2], p[1:-1, 2:]
    g, h, i = p[2:, :-2], p[2:, 1:-1], p[2:, 2:]
    dzdx = ((c + 2 * f + i) - (a + 2 * d + g)) / (8 * ground)
    dzdy = ((g + 2 * h + i) - (a + 2 * b + c)) / (8 * ground)
    slope = np.arctan(np.hypot(dzdx, dzdy))
    aspect = np.arctan2(dzdy, -dzdx)
    zen, azi = math.radians(90 - ALTITUDE), math.radians(360 - AZIMUTH + 90)
    hs = np.cos(zen) * np.cos(slope) + np.sin(zen) * np.sin(slope) * np.cos(azi - aspect)
    return np.clip(hs, 0, 1)


def render(vrt: str, out_tif: Path) -> None:
    src = gdal.Open(vrt)
    gt = src.GetGeoTransform()
    w, h = src.RasterXSize, src.RasterYSize
    row_off = round((HALF - gt[3]) / RES)  # the VRT's first row in the global grid
    drv = gdal.GetDriverByName("GTiff")
    tmp = drv.Create(str(out_tif), w, h, 4, gdal.GDT_Byte,
                     options=["TILED=YES", "BLOCKXSIZE=512", "BLOCKYSIZE=512", "COMPRESS=DEFLATE",
                              "PREDICTOR=2", "BIGTIFF=YES", "SPARSE_OK=TRUE", "PHOTOMETRIC=RGB", "ALPHA=YES"])
    tmp.SetGeoTransform(gt)
    tmp.SetProjection(src.GetProjection())
    band = src.GetRasterBand(1)
    lut = ramp_lut()
    strip = 1024
    for y0 in range(0, h, strip):
        n = min(strip, h - y0)
        top, bot = max(y0 - 1, 0), min(y0 + n + 1, h)
        dem = band.ReadAsArray(0, top, w, bot - top).astype(np.float64)
        dem[dem == -32767] = np.nan
        if top == y0:
            dem = np.vstack([dem[:1], dem])
        if bot == y0 + n:
            dem = np.vstack([dem, dem[-1:]])
        hs = shade(dem, row_off + y0)
        core = dem[1:-1]
        valid = ~np.isnan(core)
        if not valid.any():
            continue
        idx = np.clip(np.nan_to_num(core, nan=0) + 500, 0, len(lut) - 1).astype(np.int32)
        rgb = lut[idx].astype(np.float64) * (SHADE_MIN + (1 - SHADE_MIN) * hs)[..., None]
        for bi in range(3):
            tmp.GetRasterBand(bi + 1).WriteArray(np.where(valid, rgb[..., bi], 0).astype(np.uint8), 0, y0)
        tmp.GetRasterBand(4).WriteArray((valid * 255).astype(np.uint8), 0, y0)
    tmp.FlushCache()
    tmp = None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--partial", action="store_true", help="use only the tiles uploaded so far")
    ap.add_argument("--workers", type=int, default=64)
    args = ap.parse_args()
    (WORK / "tiles").mkdir(parents=True, exist_ok=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(ROOT / "manifest" / "tiles.csv.gz", "rt") as f:
        ids = [r["item_id"] for r in csv.DictReader(f)]
    rdir = Path(CONFIG["results_dir"])
    done = [i for i in ids if (rdir / f"{i}.json").exists()]
    if len(done) != len(ids) and not args.partial:
        raise SystemExit(f"{len(ids) - len(done)} tiles not uploaded yet; pass --partial for a test build")
    # Tiles wholly south of the Mercator limit are skipped (Antarctica, S86 and beyond).
    todo = [i for i in done if not (i[0] == "S" and int(i[1:3]) >= 86)]
    with cf.ProcessPoolExecutor(args.workers) as ex:
        warped = [p for p in ex.map(warp_tile, todo, chunksize=16) if p]
    vrt = str(WORK / "mosaic.vrt")
    gdal.BuildVRT(vrt, warped, srcNodata=-32767, VRTNodata=-32767, resolution="user", xRes=RES, yRes=RES,
                  targetAlignedPixels=True)
    raw = WORK / "rgba.tif"
    render(vrt, raw)
    tmp = OUT.with_suffix(".tmp.tif")
    gdal.Translate(str(tmp), str(raw), format="COG",
                   creationOptions=["COMPRESS=DEFLATE", "PREDICTOR=2", "BLOCKSIZE=512", "BIGTIFF=IF_SAFER",
                                    "OVERVIEW_RESAMPLING=AVERAGE", "NUM_THREADS=ALL_CPUS", "SPARSE_OK=TRUE"])
    os.replace(tmp, OUT)
    ds = gdal.Open(str(OUT))
    info = {"path": str(OUT), "size": OUT.stat().st_size, "shape": [ds.RasterYSize, ds.RasterXSize],
            "geotransform": list(ds.GetGeoTransform()), "tiles": len(warped)}
    (OUT.parent / "overview.json").write_text(json.dumps(info, indent=2))
    print(json.dumps(info))


if __name__ == "__main__":
    main()
