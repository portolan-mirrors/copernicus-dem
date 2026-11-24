# Copernicus DEM GLO-30 2024_1 mirror. Every stage is a target; each reads
# committed inputs (manifest/, config.yaml) and is safe to rerun.
PY := .venv/bin/python -I

.PHONY: venv manifest manifest-check sample bulk bulk-aws bulk-cdse status retry-failed

venv: .venv/.ok
.venv/.ok: requirements-bulk.txt
	uv venv -q --clear --python /usr/bin/python3 --system-site-packages .venv
	uv pip install -q --python .venv/bin/python -r requirements-bulk.txt
	touch $@

# Rebuild the pinned manifest from the CDSE catalogue and the AWS bucket.
manifest:
	uv run -q --with boto3 tools/manifest.py

# Fail if the upstream no longer matches the committed manifest.
manifest-check:
	uv run -q --with boto3 tools/manifest.py --check

# Fetch and encode ARGS-selected tiles without uploading (e.g. ARGS="--limit 64").
sample: venv
	$(PY) tools/bulk.py run --dry-run $(ARGS)

bulk: venv
	$(PY) tools/bulk.py run --source all $(ARGS)
bulk-aws: venv
	$(PY) tools/bulk.py run --source aws $(ARGS)
bulk-cdse: venv
	$(PY) tools/bulk.py run --source cdse $(ARGS)

status: venv
	$(PY) tools/bulk.py status

retry-failed: venv
	$(PY) tools/bulk.py retry-failed
