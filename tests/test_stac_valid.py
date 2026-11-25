#!/usr/bin/env python3
"""STAC validity, via stac-node-validator, as the Portolan registry runs it.

Validates every STAC object under the published directory against the STAC
core schema and the schemas of the extensions each object declares. rashid
(tests/test_conformance.py) asks whether the catalog conforms to Portolan;
this asks whether the JSON is valid STAC. The registry gate runs exactly these
two, so passing both here means the registry's deterministic validation passes.

The validator release is pinned in tests/stac-node-validator/package.json and
its lockfile, copied from portolan-sdi/portolan-registry
(scripts/stac-node-validator, commit 9d27149, Apache-2.0), together with
report.cjs, which calls the library and prints the findings as JSON. Install
it once with:

    npm ci --prefix tests/stac-node-validator --no-audit --no-fund

A finding with schema "skipped" is a JSON file that is not a STAC object
(a MapLibre style, versions.json); those are listed, not failed.

Run: python3 tests/test_stac_valid.py
"""
import json
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from publish import load_config  # noqa: E402

SNV = ROOT / "tests" / "stac-node-validator"
INSTALL = "npm ci --prefix tests/stac-node-validator --no-audit --no-fund"


def fail(message: str) -> None:
    """Report the problem, name the fix, and exit non-zero."""
    print(f"error  {message}")
    print(f"       install stac-node-validator with: {INSTALL}")
    raise SystemExit(1)


if shutil.which("node") is None:
    fail("node is not installed, so this gate checks nothing")
if not (SNV / "node_modules" / "stac-node-validator").is_dir():
    fail("stac-node-validator is not installed, so this gate checks nothing")

BASE = ROOT / load_config()["publish_dir"]
proc = subprocess.run(["node", str(SNV / "report.cjs"), str(BASE)], capture_output=True, text=True)
if proc.returncode != 0:
    print(f"error  stac-node-validator exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()[-500:]}")
    raise SystemExit(1)
report = json.loads(proc.stdout)

skipped = [f["path"] for f in report["findings"] if f["schema"] == "skipped"]
errors = [f for f in report["findings"] if f["schema"] != "skipped"]
for path in skipped:
    print(f"note   {path}: not a STAC object, not validated")
if errors:
    for f in errors[:50]:
        print(f"error  {f['path']}: [{f['schema']}] {f['message']}")
    by_schema = Counter(f["schema"] for f in errors)
    print(f"FAILED: {len(errors)} finding(s) across {len({f['path'] for f in errors})} file(s): {dict(by_schema)}")
    raise SystemExit(1)
print(f"OK: {report['files_checked'] - len(skipped)} STAC object(s) valid")
