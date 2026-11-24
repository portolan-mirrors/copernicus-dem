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

## Where the pixels come from

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
| Gates | `make gate` | Repo tests, rashid, catalog grader |
| Publish | `make publish-dry`, then `make publish` | Source Cooperative gateway, profile `source-coop-proxy` |

Uploads go through `https://data.source.coop` with temporary credentials
from the `source-coop` CLI. Pinned tools are in `config.yaml` (`toolchain`),
including tylertoo, built from a fixed commit by `make tylertoo`.

## Docs

- `docs/conformance.md`: known gaps against the Portolan spec, and why.
- `catalog/AGENTS.md`: how to read the published data, with quoted sources.
