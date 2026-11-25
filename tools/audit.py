#!/usr/bin/env python3
"""Check the bucket against the manifest and the per-tile upload records.

    .venv/bin/python -I tools/audit.py [--sample 20] [--partial]

1. Lists every object under <prefix>/<collection>/ through the Source
   Cooperative gateway.
2. For every manifest tile: each of its six files (five COGs and the XML) is
   present with exactly the file:size recorded when bulk.py uploaded it.
   Reports missing objects, size mismatches, and objects nobody recorded.
3. Downloads --sample random tiles' files in full and checks each sha256
   against the recorded file:checksum (the same value the items publish).

Without --partial, a manifest tile with no upload record is an error. Exit 0
only when everything matches.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import random
import sys
from pathlib import Path

import boto3
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text())
UP = CONFIG["upload"]
CID = CONFIG["collection"]
RESULTS = Path(CONFIG["results_dir"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sample", type=int, default=20, help="tiles to download and hash in full")
    ap.add_argument("--partial", action="store_true", help="only audit tiles that have an upload record")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    with gzip.open(ROOT / "manifest" / "tiles.csv.gz", "rt") as f:
        ids = [r["item_id"] for r in csv.DictReader(f)]
    expected: dict[str, tuple[int, str]] = {}
    unrecorded = []
    for i in ids:
        p = RESULTS / f"{i}.json"
        if not p.exists():
            unrecorded.append(i)
            continue
        for a in json.loads(p.read_text())["assets"].values():
            expected[f"{UP['prefix']}/{CID}/{i}/{a['href']}"] = (a["file:size"], a["file:checksum"])

    s3 = boto3.Session(profile_name=UP["profile"]).client("s3", endpoint_url=UP["endpoint_url"])
    found: dict[str, int] = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=UP["bucket"], Prefix=f"{UP['prefix']}/{CID}/"):
        for o in page.get("Contents", []):
            found[o["Key"]] = o["Size"]

    missing = [k for k in expected if k not in found]
    wrong = [(k, expected[k][0], found[k]) for k in expected if k in found and found[k] != expected[k][0]]
    tile_dir = f"{UP['prefix']}/{CID}/"
    extra = [k for k in found if k not in expected and k.count("/") > tile_dir.count("/")
             and not (args.partial and k[len(tile_dir):].split("/")[0] in unrecorded)]

    rng = random.Random(args.seed)
    done = sorted({k.split("/")[-2] for k in expected})
    bad_hash = []
    for tile in rng.sample(done, min(args.sample, len(done))):
        for k, (size, checksum) in expected.items():
            if f"/{tile}/" not in k:
                continue
            h = hashlib.sha256()
            for chunk in s3.get_object(Bucket=UP["bucket"], Key=k)["Body"].iter_chunks(1 << 22):
                h.update(chunk)
            if "1220" + h.hexdigest() != checksum:
                bad_hash.append(k)

    report = {
        "manifest_tiles": len(ids), "tiles_with_record": len(ids) - len(unrecorded),
        "objects_expected": len(expected), "objects_listed": len(found),
        "missing": len(missing), "size_mismatch": len(wrong), "unrecorded_objects": len(extra),
        "sampled_tiles": min(args.sample, len(done)), "hash_mismatch": len(bad_hash),
    }
    print(json.dumps(report, indent=2))
    for label, rows in (("missing", missing), ("size", wrong), ("extra", extra), ("hash", bad_hash)):
        for r in rows[:20]:
            print(label, r)
    ok = not (missing or wrong or extra or bad_hash or (unrecorded and not args.partial))
    if unrecorded and not args.partial:
        print(f"{len(unrecorded)} manifest tiles have no upload record")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
