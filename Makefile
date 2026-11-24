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

# tylertoo from the pinned commit in config.yaml, in its own root so other
# installs on the machine are untouched.
TYLERTOO_COMMIT := $(shell python3 -I -c "import yaml;print(yaml.safe_load(open('config.yaml'))['toolchain']['tylertoo']['commit'])")
TYLERTOO_ROOT := $(HOME)/copernicus-dem-work/tylertoo-$(shell echo $(TYLERTOO_COMMIT) | cut -c1-10)
.PHONY: tylertoo acquisitions overview stac items-parquet thumbs readme metadata gate audit cdse-check publish-dry publish
tylertoo: $(TYLERTOO_ROOT)/bin/tylertoo
$(TYLERTOO_ROOT)/bin/tylertoo:
	rm -rf /scratch/copdem/build/tylertoo
	git clone -q https://github.com/geoparquet-io/tylertoo /scratch/copdem/build/tylertoo
	git -C /scratch/copdem/build/tylertoo checkout -q $(TYLERTOO_COMMIT)
	CARGO_TARGET_DIR=/scratch/copdem/build/tylertoo-target cargo install --locked --path /scratch/copdem/build/tylertoo/crates/cli --root $(TYLERTOO_ROOT)

acquisitions: tylertoo
	python3 -I tools/acquisitions.py $(ARGS)

overview:
	python3 -I tools/overview.py $(ARGS)

stac:
	python3 -I tools/stac.py $(ARGS)

# Every published item as one stac-geoparquet table (needs the items first).
items-parquet:
	cd catalog && portolan stac-geoparquet -c $(shell python3 -I -c "import yaml;print(yaml.safe_load(open('config.yaml'))['collection'])")

# Thumbnails need a running chiitiler (the portolan-thumbnails skill's
# start_server.sh); look at both images before publishing.
thumbs:
	python3 -I tools/thumbs.py

readme:
	cd catalog && portolan readme
	python3 -I tools/fix_readmes.py

# All metadata from the upload records, in order. stac runs twice: the second
# pass registers items.parquet and the thumbnails with their checksums.
metadata: acquisitions overview stac items-parquet thumbs
	python3 -I tools/stac.py $(ARGS)
	$(MAKE) readme

# The pre-publish gates: repo tests, rashid, and the catalog grader.
gate:
	CI_LIGHT=1 python3 -I tests/run_all.py
	rashid check catalog --no-data --summary
	uv run -q ~/unosat/work/collect_facts_fast.py catalog --repo . --out /scratch/copdem/grade --no-rashid --max-nodes 80 --registry-submission --run-shell

# Bucket against manifest and upload records, plus full hashes of a sample.
audit: venv
	$(PY) tools/audit.py --sample 20 $(ARGS)

# Can the CDSE account download 2024_1 products yet (OData and S3)?
cdse-check: venv
	$(PY) tools/cdse_check.py

# Collection-level data (staging) and then the catalog JSON and docs.
# Dry run first; publish uploads.
publish-dry: venv
	$(PY) tools/upload_data.py
	$(PY) tools/publish.py
publish: venv
	$(PY) tools/upload_data.py --confirm
	$(PY) tools/publish.py --confirm
