#!/usr/bin/env python3
"""Encode one Copernicus DEM tile into Portolan-conformant COGs.

    python3 -I tools/encode.py <tile_dir> <out_dir> <item_id>

Runs on the system Python, whose GDAL bindings are built against the system
NumPy (the PyPI NumPy 2 wheels cannot load them).

Input is one tile folder in the DGED layout, either from CDSE or from the AWS
copy (which keeps the same layout): ``*_DEM.tif``, ``AUXFILES/*_{EDM,FLM,HEM,
WBM}.tif``, the product XML, and ``PREVIEW/*_SRC.kml``.

For every raster it:

1. Trims a native DGED grid (3601 rows) by its last row and column, the
   samples it shares with its south and east neighbours. This matches the AWS
   copy, so every tile in the mirror has the same grid.
2. Writes a COG: lossless DEFLATE, 512 px blocks, internal overviews, embedded
   exact statistics, and the nodata the handbook defines (tools/classes.py).
   Masks get an embedded colour table.
3. Re-reads the output and asserts its full-resolution pixels equal the input
   window exactly. A tile that fails this is never reported as done.
4. Hashes the output (sha256 multihash) and records size, shape, transform and
   statistics, so STAC is written later without re-reading any pixels.

It prints one JSON object describing the tile on stdout.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

import numpy as np
from osgeo import gdal

sys.path.insert(0, str(Path(__file__).resolve().parent))
from classes import NODATA, TABLES, rgba  # noqa: E402

gdal.UseExceptions()

RASTERS = {
    "dem": ("*_DEM.tif", 3, "AVERAGE"),
    "hem": ("AUXFILES/*_HEM.tif", 3, "AVERAGE"),
    "edm": ("AUXFILES/*_EDM.tif", 2, "NEAREST"),
    "flm": ("AUXFILES/*_FLM.tif", 2, "NEAREST"),
    "wbm": ("AUXFILES/*_WBM.tif", 2, "NEAREST"),
}
NATIVE_ROWS = 3601


def find(tile_dir: Path, pattern: str) -> Path:
    hits = [p for p in glob.glob(str(tile_dir / pattern)) if "/PREVIEW/" not in p]
    if len(hits) != 1:
        raise RuntimeError(f"{tile_dir}: expected one {pattern}, found {len(hits)}")
    return Path(hits[0])


def multihash_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 << 20), b""):
            h.update(chunk)
    return "1220" + h.hexdigest()


def encode_raster(asset: str, src: Path, dst: Path) -> dict:
    pattern, predictor, resampling = RASTERS[asset]
    ds = gdal.Open(str(src))
    w, h = ds.RasterXSize, ds.RasterYSize
    if h == NATIVE_ROWS:
        win = [0, 0, w - 1, h - 1]
    elif h == NATIVE_ROWS - 1:
        win = [0, 0, w, h]
    else:
        raise RuntimeError(f"{src}: unexpected height {h}")

    nodata = NODATA[asset]
    # A MEM copy carries the colour table (masks) and the nodata tag into the
    # COG driver, which takes both from its source.
    mem = gdal.Translate("", ds, format="MEM", srcWin=win)
    band = mem.GetRasterBand(1)
    if nodata is None:
        band.DeleteNoDataValue()
    else:
        band.SetNoDataValue(nodata)
    if asset in TABLES:
        ct = gdal.ColorTable()
        for value, _, _, hexcode, alpha in TABLES[asset]:
            ct.SetColorEntry(value, rgba(hexcode, alpha))
        band.SetColorTable(ct)
        band.SetColorInterpretation(gdal.GCI_PaletteIndex)
    # A band can hold no valid pixel at all: HEM codes every edited pixel as
    # -32,767, and some tiles are edited throughout. GDAL cannot compute
    # statistics on such a band, so it carries only VALID_PERCENT=0.
    all_nodata = nodata is not None and bool(np.all(band.ReadAsArray() == nodata))
    if all_nodata:
        band.SetMetadataItem("STATISTICS_VALID_PERCENT", "0")
    else:
        band.ComputeStatistics(False)  # exact, written before the COG copy

    tmp = dst.with_suffix(".tmp.tif")
    gdal.Translate(
        str(tmp), mem, format="COG",
        creationOptions=[
            "COMPRESS=DEFLATE", f"PREDICTOR={predictor}", "LEVEL=6",
            "BLOCKSIZE=512", f"OVERVIEW_RESAMPLING={resampling}",
            f"STATISTICS={'NO' if all_nodata else 'YES'}", "NUM_THREADS=1", "BIGTIFF=NO",
        ],
    )

    # Bit-exactness: the full-resolution output equals the source window.
    src_px = ds.GetRasterBand(1).ReadAsArray(*win)
    out = gdal.Open(str(tmp))
    out_px = out.GetRasterBand(1).ReadAsArray()
    if src_px.dtype != out_px.dtype or not np.array_equal(src_px, out_px, equal_nan=True):
        raise RuntimeError(f"{src}: output pixels differ from source")

    ob = out.GetRasterBand(1)
    md = ob.GetMetadata()
    stats = {k.removeprefix("STATISTICS_").lower(): float(v) for k, v in md.items() if k.startswith("STATISTICS_")}
    required = ("valid_percent",) if all_nodata else ("minimum", "maximum", "mean", "stddev", "valid_percent")
    for k in required:
        if k not in stats:
            raise RuntimeError(f"{dst}: statistic {k} missing")
    if all_nodata and stats["valid_percent"] != 0:
        raise RuntimeError(f"{dst}: all-nodata band lost VALID_PERCENT=0")
    if md.get("STATISTICS_APPROXIMATE") == "YES":
        raise RuntimeError(f"{dst}: statistics are approximate")
    info = {
        "href": dst.name,
        "file:size": tmp.stat().st_size,
        "file:checksum": multihash_sha256(tmp),
        "proj:shape": [out.RasterYSize, out.RasterXSize],
        "proj:transform": list(out.GetGeoTransform()),
        "area_or_point": out.GetMetadataItem("AREA_OR_POINT"),
        "data_type": gdal.GetDataTypeName(ob.DataType).lower(),
        "nodata": ob.GetNoDataValue(),
        "statistics": stats,
        "all_nodata": all_nodata,
        "overviews": ob.GetOverviewCount(),
        "source_window": win,
    }
    out = None
    os.replace(tmp, dst)
    return info


def encode_tile(tile_dir: Path, out_dir: Path, item_id: str) -> dict:
    t0 = time.time()
    out_dir.mkdir(parents=True, exist_ok=True)
    result = {"item_id": item_id, "assets": {}}
    for asset, (pattern, _, _) in RASTERS.items():
        src = find(tile_dir, pattern)
        dst = out_dir / f"{item_id}_{asset.upper()}.tif"
        result["assets"][asset] = encode_raster(asset, src, dst)
    # The product XML is copied unchanged; SRC.kml feeds the acquisitions
    # collection and is kept beside the outputs.
    xml = find(tile_dir, "*.xml")
    xml_dst = out_dir / f"{item_id}.xml"
    shutil.copyfile(xml, xml_dst)
    result["assets"]["xml"] = {
        "href": xml_dst.name, "file:size": xml_dst.stat().st_size,
        "file:checksum": multihash_sha256(xml_dst), "source_name": xml.name,
    }
    kmls = glob.glob(str(tile_dir / "PREVIEW/*_SRC.kml"))
    if len(kmls) > 1:
        raise RuntimeError(f"{tile_dir}: {len(kmls)} SRC.kml files")
    kml = Path(kmls[0]) if kmls else None
    if kml:
        shutil.copyfile(kml, out_dir / f"{item_id}_SRC.kml")
    result["src_kml"] = bool(kml)
    result["seconds"] = round(time.time() - t0, 2)
    return result


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__.split("\n\n")[1])
    print(json.dumps(encode_tile(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3])))
