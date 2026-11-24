#!/usr/bin/env python3
"""Fetch, re-encode, verify and upload every tile in the pinned manifest.

    .venv/bin/python -I tools/bulk.py run [--source aws|cdse|all] [--limit N]
                                 [--tiles N47_E008,...] [--force] [--dry-run]
    .venv/bin/python -I tools/bulk.py status
    .venv/bin/python -I tools/bulk.py retry-failed

Runs in .venv (make venv): the system Python with its apt GDAL 3.8.4 and
NumPy 1.26.4, plus obstore (all object I/O) and boto3 (credentials only),
pinned in requirements-bulk.txt. The apt boto3 1.34 is too old: the Source
Cooperative gateway rejects its PUTs.

Each worker process takes one tile through the whole pipeline:

1. fetch the tile's files from its manifest source into scratch, checking
   size (and MD5 where the source ETag is one);
2. encode with tools/encode.py, which asserts bit-exact pixels and embeds
   statistics;
3. upload each file with a single PUT, then HEAD it and require the size and
   the MD5 ETag to match the local file;
4. return the tile's metadata to the main process, which alone writes state.

A tile is recorded as done only after step 3 succeeds for every file. A crash,
a kill or a machine stop loses only tiles in flight, and a rerun picks up the
rest. Objects of tiles not yet recorded as done may be overwritten (they were
never confirmed); tiles recorded as done are skipped unless --force.

Credential failures (expired SSO or Source Cooperative tokens) stop the run
cleanly instead of failing tiles: refresh the login and run the same command.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import gzip
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import yaml  # noqa: E402

CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text())
# credential_process runs `source-coop creds`, which cargo installs here.
os.environ["PATH"] = f"{Path.home() / '.cargo' / 'bin'}:{os.environ.get('PATH', '')}"
MANIFEST = ROOT / "manifest" / "tiles.csv.gz"
AWS_FILES = ["dem", "edm", "flm", "hem", "wbm", "xml", "src"]
CDSE_WANTED = ("_DEM.tif", "_EDM.tif", "_FLM.tif", "_HEM.tif", "_WBM.tif", "_SRC.kml", ".xml")
CRED_ERRORS = {"ExpiredToken", "ExpiredTokenException", "InvalidAccessKeyId",
               "InvalidClientTokenId", "SignatureDoesNotMatch", "TokenRefreshRequired"}


class CredentialError(RuntimeError):
    """Raised when credentials expired; the run must stop, not fail tiles."""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_toolchain() -> None:
    import boto3
    import numpy
    import obstore
    from osgeo import gdal

    want = CONFIG["toolchain"]
    have = {"gdal": gdal.__version__, "numpy": numpy.__version__, "boto3": boto3.__version__,
            "obstore": obstore.__version__}
    bad = {k: (have[k], str(v)) for k, v in want.items() if have[k] != str(v)}
    if bad:
        sys.exit(f"toolchain mismatch (have, want): {bad}")


# ---------------------------------------------------------------- state


def db() -> sqlite3.Connection:
    d = Path(CONFIG["state_dir"])
    d.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(d / "done.sqlite", timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE IF NOT EXISTS done (item_id TEXT PRIMARY KEY, source TEXT, bytes INTEGER, finished_at TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS failed (item_id TEXT PRIMARY KEY, source TEXT, error TEXT, failed_at TEXT)")
    return con


def load_manifest() -> list[dict]:
    with gzip.open(MANIFEST, "rt") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------- worker side

_clients: dict = {}


def _init_worker(source: str) -> None:
    import boto3
    from obstore.auth.boto3 import Boto3CredentialProvider
    from obstore.store import S3Store

    signal.signal(signal.SIGINT, signal.SIG_IGN)  # the parent handles Ctrl-C
    retry = {"max_retries": 10, "retry_timeout": timedelta(minutes=5)}
    up = CONFIG["upload"]
    # obstore (Rust object_store) measured 27-41% faster than boto3 here. The
    # boto3 provider runs credential_process and refreshes before expiry.
    _clients["upload"] = S3Store(
        up["bucket"], endpoint=up["endpoint_url"], region="us-west-2",
        virtual_hosted_style_request=False, retry_config=retry,
        credential_provider=Boto3CredentialProvider(boto3.Session(profile_name=up["profile"])))
    if source == "aws":
        a = CONFIG["sources"]["aws"]
        _clients["src"] = S3Store(a["bucket"], region=a["region"], skip_signature=True, retry_config=retry)
    else:
        c = CONFIG["sources"]["cdse"]
        _clients["src"] = S3Store(
            c["bucket"], endpoint=c["endpoint_url"], region="default",
            virtual_hosted_style_request=False, retry_config=retry,
            credential_provider=Boto3CredentialProvider(boto3.Session(profile_name=c["profile"])))


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _cred_guard(exc: Exception) -> None:
    text = f"{type(exc).__name__}: {exc}"
    if any(code in text for code in CRED_ERRORS) or "credential" in text.lower() and "provider" in text.lower():
        raise CredentialError(text[:300]) from exc


def _download(store, key: str, path: Path) -> int:
    import obstore

    path.parent.mkdir(parents=True, exist_ok=True)
    data = obstore.get(store, key).bytes()
    path.write_bytes(data)
    return len(data)


def _fetch_aws(row: dict, dest: Path) -> None:
    store = _clients["src"]
    for k in AWS_FILES:
        key = row[f"aws_{k}_key"]
        p = dest / key.split("/", 1)[1]  # keep the DGED layout under the tile dir
        n = _download(store, key, p)
        if n != int(row[f"aws_{k}_size"]):
            raise RuntimeError(f"{key}: size {n} != manifest {row[f'aws_{k}_size']}")
        etag = row[f"aws_{k}_etag"]
        if "-" not in etag and _md5(p) != etag:
            raise RuntimeError(f"{key}: MD5 differs from manifest ETag")


def _fetch_cdse(row: dict, dest: Path) -> None:
    import obstore

    store = _clients["src"]
    prefix = row["cdse_s3"].rstrip("/") + "/"
    got = 0
    for batch in obstore.list(store, prefix):
        for o in batch:
            rel = o["path"][len(prefix):]
            if not rel.endswith(CDSE_WANTED) or rel.endswith("_QL.tif"):
                continue
            if _download(store, o["path"], dest / rel) != o["size"]:
                raise RuntimeError(f"{o['path']}: size mismatch")
            got += 1
    if got == 0:
        raise RuntimeError(f"no files under {prefix} (CCM access?)")


def _upload(path: Path, key: str, content_type: str) -> None:
    import obstore

    store = _clients["upload"]
    md5 = _md5(path)
    obstore.put(store, key, path, attributes={"Content-Type": content_type}, use_multipart=False)
    head = obstore.head(store, key)
    if head["size"] != path.stat().st_size or (head.get("e_tag") or "").strip('"') != md5:
        raise RuntimeError(f"{key}: HEAD after PUT does not match local file")


def process_tile(row: dict, dry_run: bool) -> dict:
    from encode import encode_tile

    item = row["item_id"]
    work = Path(CONFIG["scratch"]) / item
    src_dir, out_dir = work / "in", work / "out"
    t0 = time.time()
    try:
        shutil.rmtree(work, ignore_errors=True)
        (_fetch_aws if row["source"] == "aws" else _fetch_cdse)(row, src_dir)
        t_fetch = time.time()
        result = encode_tile(src_dir, out_dir, item)
        t_enc = time.time()
        if not dry_run:
            prefix = f"{CONFIG['upload']['prefix']}/{CONFIG['collection']}/{item}"
            for asset, info in result["assets"].items():
                ctype = ("image/tiff; application=geotiff; profile=cloud-optimized"
                         if info["href"].endswith(".tif") else "application/xml")
                _upload(out_dir / info["href"], f"{prefix}/{info['href']}", ctype)
        kml = out_dir / f"{item}_SRC.kml"
        if kml.exists():
            kd = Path(CONFIG["kml_dir"])
            kd.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(kml, kd / kml.name)
        result.update({
            "source": row["source"], "release_changed": row["release_changed"], "eop": row["eop"],
            "cdse_name": row["cdse_name"], "start_datetime": row["start_datetime"],
            "end_datetime": row["end_datetime"],
            "timing": {"fetch": round(t_fetch - t0, 1), "encode": round(t_enc - t_fetch, 1),
                       "upload": round(time.time() - t_enc, 1)},
            "dry_run": dry_run,
        })
        return result
    except CredentialError:
        raise
    except Exception as exc:  # noqa: BLE001
        _cred_guard(exc)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- main side


def run(args) -> int:
    check_toolchain()
    con = db()
    done = {r[0] for r in con.execute("SELECT item_id FROM done")}
    rows = load_manifest()
    if args.source != "all":
        rows = [r for r in rows if r["source"] == args.source]
    if args.tiles:
        want = set(args.tiles.split(","))
        rows = [r for r in rows if r["item_id"] in want]
    if args.only_failed:
        failed = {r[0] for r in con.execute("SELECT item_id FROM failed")}
        rows = [r for r in rows if r["item_id"] in failed]
    # --limit takes the first N manifest rows before skipping done ones, so
    # rerunning an interrupted command finishes the same batch.
    if args.limit:
        rows = rows[: args.limit]
    if not args.force:
        rows = [r for r in rows if r["item_id"] not in done]
    by_src = {s: [r for r in rows if r["source"] == s] for s in ("aws", "cdse")}
    print(f"to process: aws {len(by_src['aws'])}, cdse {len(by_src['cdse'])}"
          f" (already done: {len(done)}){'  [dry run: no uploads]' if args.dry_run else ''}", flush=True)
    if not rows:
        return 0

    for d in ("results_dir", "log_dir", "scratch"):
        Path(CONFIG[d]).mkdir(parents=True, exist_ok=True)
    log = open(Path(CONFIG["log_dir"]) / f"bulk-{now().replace(':', '')}.jsonl", "a")
    stop = {"flag": False, "reason": ""}

    def on_sigint(*_):
        stop["flag"], stop["reason"] = True, "interrupted"
        print("\nstopping: no new tiles; in-flight tiles finish or are discarded", flush=True)

    signal.signal(signal.SIGINT, on_sigint)
    pools, futures = [], {}
    for src, todo in by_src.items():
        if not todo:
            continue
        n = min(CONFIG["sources"][src]["workers"], len(todo))
        pool = cf.ProcessPoolExecutor(n, initializer=_init_worker, initargs=(src,))
        pools.append(pool)
        it = iter(todo)
        # Keep at most 2x workers queued, so a stop leaves little to cancel.
        for row in [next(it) for _ in range(min(2 * n, len(todo)))]:
            futures[pool.submit(process_tile, row, args.dry_run)] = (row, pool, it)

    n_ok = n_fail = 0
    bytes_ok = 0
    t0 = time.time()
    total = len(rows)
    tty, last_print = sys.stdout.isatty(), 0.0
    while futures:
        finished, _ = cf.wait(futures, return_when=cf.FIRST_COMPLETED, timeout=5)
        for fut in finished:
            row, pool, it = futures.pop(fut)
            item = row["item_id"]
            try:
                res = fut.result()
                (Path(CONFIG["results_dir"]) / f"{item}.json").write_text(json.dumps(res))
                b = sum(a["file:size"] for a in res["assets"].values())
                if not args.dry_run:
                    con.execute("INSERT OR REPLACE INTO done VALUES (?,?,?,?)", (item, row["source"], b, now()))
                    con.execute("DELETE FROM failed WHERE item_id=?", (item,))
                    con.commit()
                n_ok += 1
                bytes_ok += b
                log.write(json.dumps({"t": now(), "item": item, "ok": True, "bytes": b, **res["timing"]}) + "\n")
            except CredentialError as exc:
                stop["flag"], stop["reason"] = True, f"credentials: {exc}"
                log.write(json.dumps({"t": now(), "item": item, "ok": False, "credential_error": str(exc)}) + "\n")
            except Exception as exc:  # noqa: BLE001
                n_fail += 1
                err = f"{type(exc).__name__}: {exc}"[:500]
                con.execute("INSERT OR REPLACE INTO failed VALUES (?,?,?,?)", (item, row["source"], err, now()))
                con.commit()
                log.write(json.dumps({"t": now(), "item": item, "ok": False, "error": err}) + "\n")
            log.flush()
            if not stop["flag"]:
                nxt = next(it, None)
                if nxt is not None:
                    futures[pool.submit(process_tile, nxt, args.dry_run)] = (nxt, pool, it)
        if stop["flag"]:
            for f in list(futures):
                if f.cancel():
                    futures.pop(f)
        el = time.time() - t0
        line = (f"{n_ok + n_fail}/{total} done ({n_fail} failed)  {bytes_ok / 1e9:.1f} GB"
                f"  {bytes_ok / max(el, 1) / 1e6:.0f} MB/s  {el / 60:.1f} min")
        if tty:
            print(f"\r{line}", end="", flush=True)
        elif time.time() - last_print >= 30 or not futures:
            print(line, flush=True)
            last_print = time.time()
    for p in pools:
        p.shutdown(wait=True, cancel_futures=True)
    print()
    if stop["flag"]:
        print(f"STOPPED ({stop['reason']}). Rerun the same command to resume.")
        return 2
    return 1 if n_fail else 0


def status(_args) -> int:
    con = db()
    rows = load_manifest()
    done = dict(con.execute("SELECT item_id, bytes FROM done").fetchall())
    failed = con.execute("SELECT item_id, error FROM failed").fetchall()
    for src in ("aws", "cdse"):
        ids = [r["item_id"] for r in rows if r["source"] == src]
        d = [i for i in ids if i in done]
        print(f"{src}: {len(d)}/{len(ids)} done, {sum(done[i] for i in d) / 1e9:.1f} GB")
    print(f"failed: {len(failed)}")
    for item, err in failed[:20]:
        print(f"  {item}: {err}")
    out = Path(CONFIG["state_dir"]) / "failed.csv"
    with open(out, "w", newline="") as f:
        csv.writer(f).writerows([("item_id", "error"), *failed])
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--source", choices=["aws", "cdse", "all"], default="all")
    r.add_argument("--limit", type=int)
    r.add_argument("--tiles")
    r.add_argument("--force", action="store_true", help="reprocess tiles already recorded as done")
    r.add_argument("--dry-run", action="store_true", help="fetch and encode, but upload nothing")
    r.set_defaults(func=run, only_failed=False)
    rf = sub.add_parser("retry-failed")
    rf.set_defaults(func=run, only_failed=True, source="all", limit=None, tiles=None, force=False, dry_run=False)
    sub.add_parser("status").set_defaults(func=status)
    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
