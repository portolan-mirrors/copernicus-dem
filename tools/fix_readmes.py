#!/usr/bin/env python3
"""Repair what `portolan readme` (portolan-cli 0.9.0) writes, after it runs.

- Root child links come out as `catalog/<id>/`; point them at `<id>/README.md`.
- Collection READMEs link the licence as `./_assets/...`, which only resolves
  from the root; from a collection it is `../_assets/...`.
- The glo-30-2024-1 Files table lists every tile file (six per item). Collapse
  them into one row per asset, with the count and total size summed exactly
  from the items' file:size.

    python3 -I tools/fix_readmes.py
"""
import json
import re
from collections import defaultdict
from pathlib import Path

CAT = Path(__file__).resolve().parent.parent / "catalog"
PUBLIC = "https://data.source.coop/portolan-mirrors/copernicus-dem"
TILE_ROW = re.compile(r"^\| [NS]\d\d_[EW]\d{3}/[NS]\d\d_[EW]\d{3}(_[A-Z]{3}\.tif|\.xml) \| [^|]* \| [^|]* \|\n", re.M)


def sizes(coll: Path) -> dict[str, list[int]]:
    out = defaultdict(list)
    for item in coll.glob("*/*.json"):
        for key, a in json.loads(item.read_text())["assets"].items():
            out[key].append(a["file:size"])
    return out


def collapse(text: str, coll: Path) -> str:
    m = TILE_ROW.search(text)
    if not m:
        return text
    first = m.start()
    text = TILE_ROW.sub("", text)
    rows = ""
    for key, suffix in (("dem", "_DEM.tif"), ("hem", "_HEM.tif"), ("edm", "_EDM.tif"), ("flm", "_FLM.tif"),
                        ("wbm", "_WBM.tif"), ("metadata", ".xml")):
        s = sizes(coll)[key]
        rows += (f"| `<id>/<id>{suffix}` ({len(s):,} files, asset `{key}`; checksums in each item) "
                 f"| {sum(s) / 1e9:,.1f} GB total | - |\n")
    return text[:first] + rows + text[first:]


# portolan readme writes Title Case headings; the org prose rules want
# sentence case (portolan-ops norms/prose.md, Portolan-Mechanics.Headings).
HEADINGS = {"## Processing Notes": "## Processing notes", "## Quick Start": "## Quick start",
            "## STAC Metadata": "## STAC metadata", "## Spatial Coverage": "## Spatial coverage",
            "## Temporal Coverage": "## Temporal coverage"}


for readme in CAT.rglob("README.md"):
    rel = readme.parent.relative_to(CAT).as_posix()
    text_orig = readme.read_text()
    text = text_orig
    for a, b in HEADINGS.items():
        text = re.sub(rf"^{re.escape(a)}$", b, text, flags=re.M)
    if rel == ".":
        fixed = re.sub(r"\]\(catalog/([^)/]+)/\)", r"](\1/README.md)", text)
    else:
        fixed = text.replace("](./_assets/", "](../_assets/")
        # The quick start reads a data file by relative path, but data files
        # live only in the bucket; point it at the public URL.
        fixed = re.sub(r'read_parquet\("\./([^"/]+\.parquet)"\)',
                       lambda m: f'read_parquet("{PUBLIC}/{rel}/{m.group(1)}")', fixed)
        if (readme.parent / "collection.json").exists() and any(readme.parent.glob("*/*.json")):
            fixed = collapse(fixed, readme.parent)
    if fixed != text_orig:
        readme.write_text(fixed)
        print("fixed", readme.relative_to(CAT), f"({len(text_orig.splitlines())} -> {len(fixed.splitlines())} lines)")
