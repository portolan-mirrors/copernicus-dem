# Copernicus DEM GLO-30 2024_1 mirror

Build scripts and metadata for the Portolan mirror of the Copernicus DEM
GLO-30, release 2024_1, with its quality masks and source acquisitions,
published at
<https://data.source.coop/portolan-mirrors/copernicus-dem/catalog.json>
([portolan-data#81](https://github.com/portolan-sdi/portolan-data/issues/81)).

`catalog/` is the published catalog: STAC JSON, README and AGENTS guides,
thumbnails, the licence. Everything in it publishes, nothing outside it does.
The data (about 1.3 TB of COGs, plus the overview and the footprint files)
lives only in the bucket.

## Pixel sources

The 2024_1 release has 26,475 tiles. Each tile's `eopIdentifier` ends with the
release in which it last changed. The 26,248 tiles last changed in 2019_2 or
2020_2 are identical in the 2021 copy on AWS Open Data
(`s3://copernicus-dem-30m`), so they are read from there. The other 227 last
changed in 2022_1 (188), 2023_1 (38) or 2024_1 (1); 25 of them are not on AWS
at all. They are read from the
Copernicus Data Space Ecosystem, which needs a CDSE account with Copernicus
Contributing Missions access. `manifest/tiles.csv.gz` pins every tile's
source keys, sizes and ETags, so a run never re-lists the upstream.

Every raster is re-encoded losslessly (DEFLATE, 512 px blocks, overviews,
embedded statistics, declared nodata) and checked pixel-for-pixel against the
source before upload. Tiles are trimmed from 3601 to 3600 px where the source
repeats its neighbours' edge.

## Running it

| Step | Command | Notes |
|---|---|---|
| Python env | `make venv` | System GDAL 3.8.4 + pinned boto3/obstore |
| Manifest | `make manifest-check` | Fails if the upstream changed |
| CDSE access | `make cdse-check` | Login in `~/.config/copdem/cdse-login` (mode 600) |
| Transfer | `make bulk` | Resumable; state in `~/copernicus-dem-work/state` |
| Progress | `make status`, `make retry-failed` | |
| Audit | `make audit` | Every object vs its upload record, plus sha256 of 20 random tiles |
| Metadata | `make metadata` | Footprints, overview, STAC, items.parquet, thumbnails, READMEs |
| Gates | `make gate` | Repo tests (links, publish contract, rashid, stac-node-validator), then the catalog grader from `~/unosat/work/collect_facts_fast.py`, a local script not in this repo |
| Publish | `make publish-dry`, then `make publish` | See [Publishing](#publishing) |

Pinned tools are in `config.yaml` (`toolchain`), including tylertoo, built
from a fixed commit by `make tylertoo`.

## Publishing

**Nothing publishes automatically.** CI has read-only access and no bucket
credentials, and merging a pull request uploads nothing. The bucket changes
only when someone runs the commands below from a checkout. So the published
catalog can fall behind `main`: after merging a change under `catalog/`, run
`make publish` from an up-to-date `main`.

### What goes where

Everything lands under `s3://portolan-mirrors/copernicus-dem/`, served at
`https://data.source.coop/portolan-mirrors/copernicus-dem/`. Three routes
upload, and each owns its own files:

| What | Uploaded by | From |
|---|---|---|
| Tile COGs and XML, `glo-30-2024-1/<id>/*` (158,850 objects) | `make bulk` (`tools/bulk.py`), as each tile is encoded | the upstream sources, through `/scratch` |
| Collection-level data: the overview COG, `source-acquisitions.parquet` and `.pmtiles` | `tools/upload_data.py` (first half of `make publish`) | `data_dir` in `catalog.publish.yaml` (`~/copernicus-dem-work/staging`) |
| The catalog: STAC JSON, `items.parquet`, READMEs, AGENTS guides, style, thumbnails, licence | `tools/publish.py` (second half of `make publish`) | `catalog/`, synced 1:1 |

`make publish` uploads the data before the catalog, so a published catalog
never links to a file that is not there yet.

### Credentials

Uploads go through the Source Cooperative gateway with temporary credentials
from the `source-coop` CLI (0.3.0). One-time setup:

1. Install the CLI and log in: `source-coop login`. It caches the login in
   `~/.cache/source-coop/credentials`; do not delete that file, or the
   browser login must be redone.
2. Add this profile to `~/.aws/config`:

   ```ini
   [profile source-coop-proxy]
   credential_process = source-coop creds
   endpoint_url = https://data.source.coop
   region = us-west-2
   ```

3. Run the scripts with `.venv/bin/python` (`make venv`), as the make
   targets do. The apt botocore 1.34 fails to sign through the gateway.

The credentials are short-lived: the first full `make bulk` hit an expired
set 42 minutes in. The run then stops with a `credentials:` message, and
rerunning the same command resumes where it stopped.

### Procedure

1. Rebuild what changed. Docs or metadata only: edit, then `make stac readme`
   (READMEs are generated, so edit their sources: `tools/stac.py`,
   `catalog/*/.portolan/metadata.yaml`, `tools/fix_readmes.py`). Data:
   `make bulk`, `make audit`, `make metadata`.
2. `make gate`. It must pass: repo tests, rashid, stac-node-validator.
3. `make publish-dry`. It lists every file it would upload. Upload is by
   change: a file whose size and MD5 match the bucket's copy is skipped, so a
   docs fix uploads a handful of files, not 26,490.
4. `make publish`.
5. Check the live catalog:

   ```bash
   rashid check catalog --no-data --live --summary
   ```

   With `--live`, rashid reads the full local tree and checks that every
   linked document exists at the public URL. Run it against the directory,
   not the URL: a URL crawl stops at 10,000 files and then reports the
   unread items as broken links.

### Things the scripts never do

- **Delete.** Removing a file from `catalog/` or the staging directory does
  not unpublish it. Delete the object in the bucket yourself if that is
  what you meant.
- **Re-upload unchanged bytes.** After changing a content type in
  `tools/publish.py`, run `tools/publish.py --confirm --force` so existing
  objects get the new header.
- **Publish anything outside `catalog/` and `data_dir`.** That boundary is
  structural; do not move files into `catalog/` to make them publish.

## Docs

- `docs/conformance.md`: known gaps against the Portolan spec, and why.
- `catalog/AGENTS.md`: how to read the published data, with quoted sources.
