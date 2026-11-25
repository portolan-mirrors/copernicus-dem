#!/usr/bin/env python3
"""Write the STAC JSON for the glo-30-2024-1 collection and the root catalog.

    python3 -I tools/stac.py [--partial]

Reads only metadata: the pinned manifest and the per-tile results that
tools/bulk.py recorded after each verified upload (size, sha256 multihash,
grid, statistics). It reads no pixels and is idempotent.

Without --partial it refuses to run unless every manifest tile has a result,
so a published collection can never silently miss tiles.

Writes:
  catalog/catalog.json                          root catalog
  catalog/glo-30-2024-1/collection.json         collection
  catalog/glo-30-2024-1/<id>/<id>.json          one item per 1 degree tile
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from classes import NODATA, TABLES, stac_classes  # noqa: E402

CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text())
CAT = ROOT / "catalog"
CID = CONFIG["collection"]
COLL = CAT / CID
PUBLIC = CONFIG["upload"]["public_base"]
REPO = "https://github.com/portolan-mirrors/copernicus-dem"

COG = "image/tiff; application=geotiff; profile=cloud-optimized"
PORTOLAN = "https://schemas.portolan-sdi.org/portolan/v0.3.0/schema.json"
EXT = {
    "file": "https://stac-extensions.github.io/file/v2.1.0/schema.json",
    "proj": "https://stac-extensions.github.io/projection/v2.0.0/schema.json",
    "raster": "https://stac-extensions.github.io/raster/v2.0.0/schema.json",
    "render": "https://stac-extensions.github.io/render/v2.0.0/schema.json",
    "classification": "https://stac-extensions.github.io/classification/v2.0.0/schema.json",
    "processing": "https://stac-extensions.github.io/processing/v1.2.0/schema.json",
    "item_assets": "https://stac-extensions.github.io/item-assets/v1.0.0/schema.json",
    "table": "https://stac-extensions.github.io/table/v1.2.0/schema.json",
    "web_map_links": "https://stac-extensions.github.io/web-map-links/v1.3.0/schema.json",
}
STAGING = Path(CONFIG["staging_dir"])
ACQ = "source-acquisitions"
ATTRIBUTION_ADAPTED = ("produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence and Space "
                       "GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved")

HANDBOOK = ("https://dataspace.copernicus.eu/sites/default/files/media/files/2026-10/"
            "geo1988-copernicusdem-spe-002_producthandbook_i6.1.pdf")
LICENCE_PDF = ("https://dataspace.copernicus.eu/sites/default/files/media/files/2025-06/"
               "copernicus_contributing_mission_data_access_v2_cop_dem_licenses.pdf")
COP_DEM_PAGE = ("https://dataspace.copernicus.eu/explore-data/data-collections/"
                "copernicus-contributing-missions/collections-description/COP-DEM")
CDSE_STAC = "https://stac.dataspace.copernicus.eu/v1/collections/cop-dem-glo-30-dged-cog"
ODATA = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
AWS_HTTP = "https://copernicus-dem-30m.s3.amazonaws.com"
DOI = "https://doi.org/10.5270/ESA-c5d3d65"

# Per-asset descriptions. Quoted text is from Product Handbook issue 6.1.
ASSETS = {
    "dem": {
        "title": "Elevation (DSM)", "roles": ["data"], "unit": "m",
        "band": "elevation",
        "description": ("Digital surface model heights in metres above the EGM2008 geoid (EPSG:3855), float32, "
                        "RasterPixelIsPoint. The handbook gives -32,767 as a NoData value for \"EEA-10 only\"; "
                        "GLO-30 is void-filled and this asset declares no nodata."),
    },
    "hem": {
        "title": "Height Error Mask (HEM)", "roles": ["data", "quality"], "unit": "m",
        "band": "height_error",
        "description": ("\"The Height Error Mask (HEM) represents the corresponding height error for each DEM pixel "
                        "in the form of the standard deviation derived from the interferometric coherence and "
                        "geometrical considerations.\" Edited pixels are coded -32,767 (declared as nodata): "
                        "\"The Editing Mask as well as the Filling Mask support the interpretation of the HEM for "
                        "areas coded with -32,767.\""),
    },
    "edm": {
        "title": "Editing Mask (EDM)", "roles": ["data", "quality"], "band": "editing",
        "description": "Editing applied to each DEM pixel (handbook Table 6). 0 is \"Void (no data)\".",
    },
    "flm": {
        "title": "Filling Mask (FLM)", "roles": ["data", "quality"], "band": "filling",
        "description": ("\"All edited and filled pixels are flagged in this mask. For filled pixels, the fill "
                        "source is specified.\" (handbook 1.2.5.2, Table 7). 0 is \"Void (no data)\"."),
    },
    "wbm": {
        "title": "Water Body Mask (WBM)", "roles": ["data", "quality"], "band": "water_body",
        "description": ("\"The Water Body Mask (WBM) shows all DEM pixels, which are classified as water and edited "
                        "according to the categories Ocean, Lake or River.\" (handbook 1.2.5.4, Table 8). 0 means "
                        "\"No water\" and is a value, not nodata."),
    },
}
METADATA_ASSET = {
    "title": "Product metadata (ISO 19115 XML)", "roles": ["metadata"], "type": "application/xml",
    "description": "The DGED product's XML metadata, copied unchanged from the source tile folder.",
}


def file_fields(path: Path) -> dict:
    """file:size and a sha256 multihash for a local file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return {"file:size": path.stat().st_size, "file:checksum": "1220" + h.hexdigest()}


def thumbnail_asset(coll_dir: Path, what: str) -> dict:
    """The thumbnail asset, when tools/thumbs.sh has rendered one."""
    p = coll_dir / "thumbnail.png"
    if not p.exists():
        return {}
    return {"thumbnail": {"href": "./thumbnail.png", "type": "image/png", "roles": ["thumbnail"],
                          "title": "Thumbnail", "description": f"{what}, rendered by tools/thumbs.sh.",
                          **file_fields(p)}}


def save(p: Path, d: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")


def iso(t: str) -> str:
    """OData's 2011-07-30T17:47:44.000000Z -> 2011-07-30T17:47:44Z."""
    return t.split(".")[0].rstrip("Z") + "Z"


def stac_transform(gt: list[float]) -> list[float]:
    """GDAL geotransform order -> STAC proj:transform (affine a..f) order."""
    return [gt[1], gt[2], gt[0], gt[4], gt[5], gt[3]]


def bbox_of(shape: list[int], gt: list[float]) -> list[float]:
    h, w = shape
    x0, y0 = gt[0], gt[3]
    # Pixel centres sit on whole degrees (RasterPixelIsPoint), so W180 tiles
    # reach half a pixel past the antimeridian (to -180.0014 at 10"). STAC
    # bboxes must stay within +-180; clamp the footprint, not proj:transform.
    return [max(round(x0, 9), -180.0), max(round(y0 + h * gt[5], 9), -90.0),
            min(round(x0 + w * gt[1], 9), 180.0), min(round(y0, 9), 90.0)]


def band(asset: str, info: dict) -> dict:
    a = ASSETS[asset]
    # GDAL names uint8 "byte"; STAC's data_type enum says "uint8".
    b = {"name": a["band"], "data_type": {"byte": "uint8"}.get(info["data_type"], info["data_type"]),
         "raster:sampling": "point"}
    if info["nodata"] is not None:
        b["nodata"] = info["nodata"] if asset not in TABLES else int(info["nodata"])
    if "unit" in a:
        b["unit"] = a["unit"]
    b["statistics"] = {k: info["statistics"][k] for k in ("minimum", "maximum", "mean", "stddev", "valid_percent")
                       if k in info["statistics"]}
    return b


def colormap(asset: str) -> dict:
    return {str(v): list(int(h[i:i + 2], 16) for i in (0, 2, 4)) + [a] for v, _, _, h, a in TABLES[asset]}


def item_renders(res: dict) -> dict:
    st = res["assets"]["dem"]["statistics"]
    r = {"dem": {"title": "Elevation", "assets": ["dem"], "rescale": [[st["minimum"], st["maximum"]]],
                 "colormap_name": "terrain", "resampling": "bilinear"}}
    for k in TABLES:
        r[k] = {"title": ASSETS[k]["title"], "assets": [k], "colormap": colormap(k), "resampling": "nearest"}
        if NODATA[k] is not None:
            r[k]["nodata"] = NODATA[k]
    return r


def make_item(row: dict, res: dict) -> dict:
    iid = row["item_id"]
    dem = res["assets"]["dem"]
    shape, gt = dem["proj:shape"], dem["proj:transform"]
    for k in ASSETS:  # every raster shares the DEM grid
        # 358 source HEMs carry float noise in the geotransform (at most
        # 2.8e-14 deg, about 3 nm), kept byte-exact; tolerate it, not more.
        t = res["assets"][k]["proj:transform"]
        if res["assets"][k]["proj:shape"] != shape or any(abs(a - b) > 1e-9 for a, b in zip(t, gt)):
            raise SystemExit(f"{iid}: {k} grid differs from dem")
    bb = bbox_of(shape, gt)
    src = row["source"]
    lineage = (
        f"Release 2024_1 tile {row['cdse_name']} ({row['eop']}). "
        + (f"Pixels read from the AWS Open Data copy s3://copernicus-dem-30m (2021 release, already trimmed to "
           f"{shape[1]}x{shape[0]}); the tile last changed in release {row['release_changed']}, which that copy "
           "holds. " if src == "aws" else
           "Pixels read from the CDSE eodata archive and trimmed by the last row and column, the samples shared "
           "with the south and east neighbours. ")
        + "Re-encoded losslessly (DEFLATE) with embedded statistics; every output was checked pixel-for-pixel "
          "against its source.")
    assets = {}
    for k, a in ASSETS.items():
        info = res["assets"][k]
        d = {"href": f"./{info['href']}", "type": COG, "title": a["title"], "description": a["description"],
             "roles": a["roles"], "file:size": info["file:size"], "file:checksum": info["file:checksum"]}
        if info["nodata"] is not None:
            d["nodata"] = int(info["nodata"]) if k in TABLES else info["nodata"]
        if k in TABLES:
            d["classification:classes"] = stac_classes(k)
        d["bands"] = [band(k, info)]
        assets[k] = d
    x = res["assets"]["xml"]
    assets["metadata"] = {"href": f"./{x['href']}", **METADATA_ASSET,
                          "file:size": x["file:size"], "file:checksum": x["file:checksum"]}
    links = [
        {"rel": "root", "href": "../../catalog.json", "type": "application/json"},
        {"rel": "parent", "href": "../collection.json", "type": "application/json"},
        {"rel": "collection", "href": "../collection.json", "type": "application/json"},
        {"rel": "via", "href": f"{ODATA}?$filter=" + quote(f"Name eq '{row['cdse_name']}'"),
         "type": "application/json", "title": "Source product in the CDSE catalogue (OData)"},
    ]
    if src == "aws":
        links.append({"rel": "via", "href": f"{AWS_HTTP}/{row['aws_dem_key']}", "type": COG,
                      "title": "DEM tile in the AWS Open Data copy that the pixels were read from"})
    return {
        "type": "Feature", "stac_version": "1.1.0",
        "stac_extensions": [EXT["file"], EXT["proj"], EXT["raster"], EXT["classification"],
                            EXT["render"], EXT["processing"]],
        "id": iid,
        "geometry": {"type": "Polygon", "coordinates": [[[bb[0], bb[1]], [bb[2], bb[1]], [bb[2], bb[3]],
                                                         [bb[0], bb[3]], [bb[0], bb[1]]]]},
        "bbox": bb,
        "properties": {
            "title": f"Copernicus DEM GLO-30 tile {iid}",
            "datetime": None,
            "start_datetime": iso(row["start_datetime"]),
            "end_datetime": iso(row["end_datetime"]),
            "proj:code": "EPSG:4326",
            "proj:shape": shape,
            "proj:transform": stac_transform(gt),
            "copdem:release": "2024_1",
            "copdem:release_changed": row["release_changed"],
            "copdem:eop_identifier": row["eop"],
            "copdem:source_product": row["cdse_name"],
            "copdem:pixel_source": src,
            "processing:lineage": lineage,
            "processing:software": {"GDAL": CONFIG["toolchain"]["gdal"]},
            "renders": item_renders(res),
        },
        "links": links,
        "assets": assets,
        "collection": CID,
    }


def item_assets() -> dict:
    out = {}
    for k, a in ASSETS.items():
        d = {"type": COG, "title": a["title"], "description": a["description"], "roles": a["roles"]}
        if NODATA[k] is not None:
            d["nodata"] = NODATA[k]
        if k in TABLES:
            d["classification:classes"] = stac_classes(k)
        b = {"name": a["band"], "data_type": "float32" if k in ("dem", "hem") else "uint8", "raster:sampling": "point"}
        if NODATA[k] is not None:
            b["nodata"] = NODATA[k]
        if "unit" in a:
            b["unit"] = a["unit"]
        d["bands"] = [b]
        out[k] = d
    out["metadata"] = dict(METADATA_ASSET)
    return out


def make_collection(items: list[dict], now: str) -> dict:
    bbs = [i["bbox"] for i in items]
    starts = sorted(i["properties"]["start_datetime"] for i in items)
    ends = sorted(i["properties"]["end_datetime"] for i in items)
    old = json.loads((COLL / "collection.json").read_text()) if (COLL / "collection.json").exists() else {}
    keep_assets = {k: v for k, v in old.get("assets", {}).items() if k in ("geoparquet-items",)}
    keep_assets.update(thumbnail_asset(COLL, "A window of the overview COG"))
    if "geoparquet-items" in keep_assets:
        keep_assets["geoparquet-items"]["description"] = (
            "Every item of this collection as one stac-geoparquet file, one row per tile, for querying the "
            "whole collection (for example with DuckDB) without fetching the item JSON files. Rebuilt with "
            "portolan stac-geoparquet whenever the items change.")
    ov = STAGING / CID / f"{CID}_overview.tif"
    if ov.exists():
        keep_assets["overview"] = {
            "href": f"./{ov.name}", "type": COG, "roles": ["visual", "overview"],
            "title": "Global hillshaded elevation overview (COG)",
            "description": ("The whole collection in one 8-bit RGB + alpha COG in Web Mercator at about 1.2 km, for "
                             "maps that should not open 26,475 items. Elevation tinted with a hypsometric ramp and "
                             "multiplied by a hillshade. Alpha is 0 where no tile has data. Not elevation data: "
                             "read the item COGs for heights. Web Mercator stops near 85 degrees, so most of "
                             "Antarctica is not in it. " + ATTRIBUTION_ADAPTED + "."),
            "proj:code": "EPSG:3857",
            "start_datetime": starts[0], "end_datetime": ends[-1],
            "processing:lineage": "tools/overview.py: DEM COGs warped to EPSG:3857 (average), Horn hillshade "
                                  "(azimuth 315, altitude 45), hypsometric tint, 8-bit RGBA.",
            **file_fields(ov),
        }
    links = [
        {"rel": "root", "href": "../catalog.json", "type": "application/json"},
        {"rel": "parent", "href": "../catalog.json", "type": "application/json"},
        *({"rel": "item", "href": f"./{i['id']}/{i['id']}.json", "type": "application/geo+json",
           "title": f"Tile {i['id']}"} for i in items),
        {"rel": "license", "href": "../_assets/copernicus-dem-licences.pdf", "type": "application/pdf",
         "title": "Licence for the use of the Copernicus WorldDEM-30 (COP-DEM-GLO-30-F, pages 22-24)"},
        {"rel": "license", "href": LICENCE_PDF, "type": "application/pdf",
         "title": "The same licence document at its CDSE source"},
        {"rel": "via", "href": COP_DEM_PAGE, "type": "text/html", "title": "Copernicus DEM on the Copernicus Data Space Ecosystem"},
        {"rel": "canonical", "href": CDSE_STAC, "type": "application/json",
         "title": "CDSE STAC collection cop-dem-glo-30-dged-cog (an older COG copy, not release 2024_1)"},
        {"rel": "cite-as", "href": DOI, "type": "text/html", "title": "Copernicus DEM, doi:10.5270/ESA-c5d3d65"},
        {"rel": "describedby", "href": HANDBOOK, "type": "application/pdf", "title": "Copernicus DEM Product Handbook, issue 6.1"},
        {"rel": "describedby", "href": "./README.md", "type": "text/markdown", "title": "Collection README"},
        {"rel": "agents", "href": "./AGENTS.md", "type": "text/markdown", "title": "Collection agent guide"},
    ]
    # Collections inherit the root metadata.yaml unless they override a field.
    meta = {**yaml.safe_load((CAT / ".portolan" / "metadata.yaml").read_text()),
            **yaml.safe_load((COLL / ".portolan" / "metadata.yaml").read_text())}
    return {
        "type": "Collection", "id": CID, "stac_version": "1.1.0",
        "stac_extensions": [EXT["file"], EXT["proj"], EXT["raster"], EXT["classification"],
                            *([EXT["render"], EXT["processing"]] if "overview" in keep_assets else []),
                            EXT["item_assets"], PORTOLAN],
        "title": meta["title"],
        "description": " ".join(meta["description"].split()),
        "keywords": meta.get("keywords", []),
        "license": "other",
        "providers": meta["providers"],
        "extent": {
            "spatial": {"bbox": [[min(b[0] for b in bbs), min(b[1] for b in bbs),
                                  max(b[2] for b in bbs), max(b[3] for b in bbs)]]},
            "temporal": {"interval": [[starts[0], ends[-1]]]},
        },
        "summaries": {
            "proj:code": ["EPSG:4326"],
            "copdem:release": ["2024_1"],
            "copdem:release_changed": sorted({i["properties"]["copdem:release_changed"] for i in items}),
            "copdem:pixel_source": sorted({i["properties"]["copdem:pixel_source"] for i in items}),
        },
        "item_assets": item_assets(),
        **({"renders": {"overview": {"title": "Hillshaded elevation", "assets": ["overview"]}}}
           if "overview" in keep_assets else {}),
        "assets": keep_assets,
        "links": links,
        "updated": now,
    }


# Columns of source-acquisitions.parquet. Quoted text is from the KML balloon
# tables or Product Handbook 6.1, section 1.2.5.6; the rest is what
# tools/acquisitions.py does.
ACQ_COLUMNS = [
    {"name": "name", "type": "string",
     "description": "The placemark name from SRC.kml, such as 1020979_02. The handbook says the file holds "
                    "\"the acquisition ID, data scene number\"; it does not define how the name encodes them."},
    {"name": "acquisition_set", "type": "string",
     "description": "\"Acquisition Set\" from the KML balloon: First, Second, First crossing, Second crossing, First desert, Second desert, "
                    "Additional or Other acquisition. The handbook does not define these sets."},
    {"name": "acquisition_time", "type": "timestamp",
     "description": "\"Acquisition Date\" from the KML balloon, to the minute. The KML gives no time zone."},
    {"name": "phase_unwrap", "type": "string",
     "description": "\"Phase Unwrap\" from the KML balloon: multi-baseline or single-baseline."},
    {"name": "height_of_ambiguity", "type": "number",
     "description": "\"Height of Ambiguity\" from the KML balloon, as written. The KML gives no unit."},
    {"name": "tiles", "type": "array",
     "description": "Item ids (grid cells, such as N00_E006) of the glo-30-2024-1 tiles whose SRC.kml lists this footprint."},
    {"name": "tile_count", "type": "integer", "description": "Length of tiles."},
    {"name": "geometry", "type": "geometry",
     "description": "The acquisition footprint polygon as given in the KML, EPSG:4326. Not clipped to any tile."},
    {"name": "bbox", "type": "object", "description": "Bounding box covering column (xmin, ymin, xmax, ymax)."},
]


def acquisitions_collection(now: str) -> bool:
    d = STAGING / ACQ
    pq, pm = d / f"{ACQ}.parquet", d / f"{ACQ}.pmtiles"
    if not (pq.exists() and pm.exists()):
        return False
    summ = json.loads((d / "summary.json").read_text())
    out = subprocess.run(["duckdb", "-no-agent", "-json", "-c",
                          f"select min(bbox.xmin) a, min(bbox.ymin) b, max(bbox.xmax) c, max(bbox.ymax) d, "
                          f"count(*) n from '{pq}'"], check=True, capture_output=True, text=True).stdout
    b = json.loads(out)[0]
    if b["n"] != summ["rows"]:
        sys.exit(f"{pq}: {b['n']} rows, summary says {summ['rows']}")
    t0, t1 = (t.replace("T", " ") for t in summ["time_range"])
    acq = CAT / ACQ
    meta = {**yaml.safe_load((CAT / ".portolan" / "metadata.yaml").read_text()),
            **yaml.safe_load((acq / ".portolan" / "metadata.yaml").read_text())}
    style = acq / "styles" / "default.json"
    assets = {
        "data": {"href": f"./{pq.name}", "type": "application/vnd.apache.parquet", "roles": ["data"],
                 "title": "Acquisition footprints (GeoParquet)",
                 "description": f"{summ['rows']:,} footprints from {summ['tiles']:,} tiles' SRC.kml files, "
                                "GeoParquet 2.0 with a bbox covering column, Hilbert-sorted.",
                 "table:row_count": summ["rows"], **file_fields(pq)},
        "tiles": {"href": f"./{pm.name}", "type": "application/vnd.pmtiles", "roles": ["visual"],
                  "title": "Acquisition footprints (PMTiles)",
                  "description": f"Vector tiles of the GeoParquet, z0 to z10, layer {ACQ}, built with tylertoo "
                                 f"{CONFIG['toolchain']['tylertoo']['commit'][:10]}.",
                  **file_fields(pm)},
        "style": {"href": "./styles/default.json", "type": "application/vnd.mapbox.style+json",
                  "roles": ["style", "default"], "title": "Footprints by acquisition set",
                  "description": "MapLibre style over the PMTiles, in the colours the producer's SRC.kml files use.",
                  **file_fields(style)},
    }
    assets.update(thumbnail_asset(acq, "Footprints near Mount Kenya in the default style"))
    coll = {
        "type": "Collection", "id": ACQ, "stac_version": "1.1.0",
        "stac_extensions": [EXT["file"], EXT["table"], EXT["web_map_links"], PORTOLAN],
        "title": meta["title"], "description": " ".join(meta["description"].split()),
        "keywords": meta.get("keywords", []), "license": "other", "providers": meta["providers"],
        "extent": {"spatial": {"bbox": [[b["a"], b["b"], b["c"], b["d"]]]},
                   "temporal": {"interval": [[f"{t0}:00Z".replace(" ", "T"), f"{t1}:00Z".replace(" ", "T")]]}},
        "table:columns": ACQ_COLUMNS,
        "table:primary_geometry": "geometry",
        "assets": assets,
        "links": [
            {"rel": "root", "href": "../catalog.json", "type": "application/json"},
            {"rel": "parent", "href": "../catalog.json", "type": "application/json"},
            {"rel": "pmtiles", "href": f"./{pm.name}", "type": "application/vnd.pmtiles", "pmtiles:layers": [ACQ]},
            {"rel": "related", "href": f"../{CID}/collection.json", "type": "application/json",
             "title": "Copernicus DEM GLO-30, release 2024_1 (the tiles these acquisitions built)"},
            {"rel": "license", "href": "../_assets/copernicus-dem-licences.pdf", "type": "application/pdf",
             "title": "Licence for the use of the Copernicus WorldDEM-30 (COP-DEM-GLO-30-F, pages 22-24)"},
            {"rel": "via", "href": COP_DEM_PAGE, "type": "text/html",
             "title": "Copernicus DEM on the Copernicus Data Space Ecosystem"},
            {"rel": "cite-as", "href": DOI, "type": "text/html", "title": "Copernicus DEM, doi:10.5270/ESA-c5d3d65"},
            {"rel": "describedby", "href": HANDBOOK, "type": "application/pdf",
             "title": "Copernicus DEM Product Handbook, issue 6.1 (section 1.2.5.6)"},
            {"rel": "describedby", "href": "./README.md", "type": "text/markdown", "title": "Collection README"},
            {"rel": "agents", "href": "./AGENTS.md", "type": "text/markdown", "title": "Collection agent guide"},
        ],
        "updated": now,
    }
    save(acq / "collection.json", coll)
    return True


def root_catalog(now: str) -> None:
    r = json.loads((CAT / "catalog.json").read_text())
    meta = yaml.safe_load((CAT / ".portolan" / "metadata.yaml").read_text())
    r.update({
        "stac_extensions": [PORTOLAN], "id": "copernicus-dem", "title": meta["title"],
        "description": " ".join(meta["description"].split()), "updated": now,
    })
    keep = [lnk for lnk in r["links"] if lnk["rel"] in ("root", "describedby", "agents")]
    children = [{"rel": "child", "href": f"./{CID}/collection.json", "type": "application/json",
                 "title": "Copernicus DEM GLO-30, release 2024_1"}]
    if (CAT / "source-acquisitions" / "collection.json").exists():
        children.append({"rel": "child", "href": "./source-acquisitions/collection.json",
                         "type": "application/json", "title": "TanDEM-X source acquisitions"})
    r["links"] = keep + children + [
        {"rel": "self", "href": f"{PUBLIC}/catalog.json", "type": "application/json"},
        {"rel": "via", "href": COP_DEM_PAGE, "type": "text/html", "title": "Copernicus DEM on the Copernicus Data Space Ecosystem"},
        {"rel": "license", "href": "./_assets/copernicus-dem-licences.pdf", "type": "application/pdf",
         "title": "Licence for the use of the Copernicus WorldDEM-30 (pages 22-24)"},
        {"rel": "vcs", "href": REPO, "type": "text/html", "title": "Build scripts and metadata"},
        {"rel": "issues", "href": f"{REPO}/issues", "type": "text/html", "title": "Report a problem"},
    ]
    save(CAT / "catalog.json", r)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--partial", action="store_true", help="write items for the tiles done so far")
    args = ap.parse_args()
    with gzip.open(ROOT / "manifest" / "tiles.csv.gz", "rt") as f:
        rows = list(csv.DictReader(f))
    rdir = Path(CONFIG["results_dir"])
    have = [r for r in rows if (rdir / f"{r['item_id']}.json").exists()]
    if len(have) != len(rows) and not args.partial:
        sys.exit(f"{len(rows) - len(have)} of {len(rows)} tiles have no result; run bulk first or pass --partial")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    items = []
    for row in have:
        res = json.loads((rdir / f"{row['item_id']}.json").read_text())
        if res.get("dry_run"):
            sys.exit(f"{row['item_id']}: result is from a dry run")
        it = make_item(row, res)
        save(COLL / it["id"] / f"{it['id']}.json", it)
        items.append(it)
    save(COLL / "collection.json", make_collection(items, now))
    acq = acquisitions_collection(now)
    root_catalog(now)
    print(f"wrote {len(items)} items, collection {CID}, {'collection ' + ACQ + ', ' if acq else ''}root catalog")


if __name__ == "__main__":
    main()
