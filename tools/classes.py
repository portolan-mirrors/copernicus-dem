"""Pixel codes of the Copernicus DEM quality masks.

Every value and meaning is quoted from the Copernicus DEM Product Handbook,
issue 6.1 (09.10.2026), Tables 6, 7 and 8:
https://dataspace.copernicus.eu/sites/default/files/media/files/2026-10/geo1988-copernicusdem-spe-002_producthandbook_i6.1.pdf

Colours are ours. They are written into each mask COG as a colour table and
into STAC as ``color_hint``, so the two always agree (PORTO-FMT-054). Alpha 0
marks values drawn transparent: the void code, and "no water" in WBM so the
mask overlays cleanly.
"""
from __future__ import annotations

# (value, name, title, RRGGBB, alpha)
EDM = [
    (0, "void", "Void (no data)", "000000", 0),
    (1, "not_edited", "Not edited", "E0E0E0", 255),
    (2, "infill", "Infill of external elevation data", "E69F00", 255),
    (3, "interpolated", "Interpolated pixels", "56B4E9", 255),
    (4, "smoothed", "Smoothed pixels", "009E73", 255),
    (5, "airport", "Airport editing", "F0E442", 255),
    (6, "raised_negative", "Raised negative elevation pixels", "D55E00", 255),
    (7, "flattened", "Flattened pixels", "CC79A7", 255),
    (8, "ocean", "Ocean pixels", "08306B", 255),
    (9, "lake", "Lake pixels", "2171B5", 255),
    (10, "river", "River pixels", "6BAED6", 255),
    (11, "shoreline", "Shoreline pixels", "FDD0A2", 255),
    (12, "morphed", "Morphed pixels (series of pixels manually set)", "8C510A", 255),
    (13, "shifted", "Shifted pixels", "762A83", 255),
]

FLM = [
    (0, "void", "Void (no data)", "000000", 0),
    (1, "edited", "Edited (except filled pixels)", "BDBDBD", 255),
    (2, "not_edited_not_filled", "Not edited / not filled", "F0F0F0", 255),
    (3, "aster", "ASTER", "E41A1C", 255),
    (4, "srtm90", "SRTM90", "377EB8", 255),
    (5, "srtm30", "SRTM30", "4DAF4A", 255),
    (6, "gmted2010", "GMTED2010", "984EA3", 255),
    (7, "srtm30plus", "SRTM30plus", "FF7F00", 255),
    (8, "tsx_radargrammetric", "TerraSAR-X Radargrammetric DEM", "FFFF33", 255),
    (9, "aw3d30", "AW3D30", "A65628", 255),
    (100, "norway_dem", "Norway DEM", "F781BF", 255),
    (101, "dsm05_spain", "DSM05 Spain", "1B9E77", 255),
    (102, "norway_dem_v2", "Norway DEM v2", "E7298A", 255),
]

WBM = [
    (0, "no_water", "No water", "FFFFFF", 0),
    (1, "ocean", "Ocean", "08306B", 255),
    (2, "lake", "Lake", "2171B5", 255),
    (3, "river", "River", "6BAED6", 255),
]

TABLES = {"edm": EDM, "flm": FLM, "wbm": WBM}

# Nodata per asset. Handbook Table 1 gives -32,767 as a DEM NoData value for
# "EEA-10 only"; GLO-30 is void-filled, so the DEM declares none. HEM codes
# edited pixels as -32,767 (handbook 1.2.5.3). EDM and FLM code 0 as "Void (no
# data)". WBM 0 means "No water", which is a value, not an absence.
NODATA = {"dem": None, "hem": -32767.0, "edm": 0, "flm": 0, "wbm": None}


def rgba(hexcode: str, alpha: int) -> tuple[int, int, int, int]:
    return (int(hexcode[0:2], 16), int(hexcode[2:4], 16), int(hexcode[4:6], 16), alpha)


def stac_classes(asset: str) -> list[dict]:
    """``classification:classes`` for a mask, with ``nodata: true`` exactly on
    the class whose value equals the asset's nodata (PORTO-FMT-057)."""
    nd = NODATA[asset]
    out = []
    for value, name, title, hexcode, _ in TABLES[asset]:
        c = {"value": value, "name": name, "title": title, "color_hint": hexcode}
        if nd is not None and value == nd:
            c["nodata"] = True
        out.append(c)
    return out
