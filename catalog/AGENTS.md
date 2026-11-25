# AGENTS.md: Copernicus DEM GLO-30, release 2024_1

A mirror of the Copernicus DEM at 30 m (GLO-30, DGED format), release 2024_1,
with its quality masks and the TanDEM-X acquisitions behind each tile.
Public root: `https://data.source.coop/portolan-mirrors/copernicus-dem/catalog.json`.
Upstream: [Copernicus DEM on the Copernicus Data Space Ecosystem](https://dataspace.copernicus.eu/explore-data/data-collections/copernicus-contributing-missions/collections-description/COP-DEM),
documented in the [Product Handbook, issue 6.1](https://dataspace.copernicus.eu/sites/default/files/media/files/2026-10/geo1988-copernicusdem-spe-002_producthandbook_i6.1.pdf).
Quotes below are from the handbook unless marked otherwise.

**One rule survives every edit to this file.** Every claim here is either quoted
from a source or measured from the data.

## What this catalog holds

| Collection | What it is |
|---|---|
| [`glo-30-2024-1`](glo-30-2024-1/AGENTS.md) | One item per 1° x 1° tile with the elevation raster (`dem`), four quality masks (`hem`, `edm`, `flm`, `wbm`) and the producer's XML. Plus `items.parquet` (every item as one table) and a global overview COG for display. |
| [`source-acquisitions`](source-acquisitions/AGENTS.md) | The TanDEM-X acquisition footprints each tile's `SRC.kml` lists, as GeoParquet and PMTiles. |

## Licence and required notices

The data is licensed under the Licence for the use of the Copernicus
WorldDEM-30 (COP-DEM-GLO-30-F), pages 22 to 24 of
[`_assets/copernicus-dem-licences.pdf`](_assets/copernicus-dem-licences.pdf).
Article 6 requires these notices. When you distribute the data:

> © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved.

When you distribute data you adapted or modified:

> produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved

And in any licence or notice covering your distribution:

> The organisations in charge of the Copernicus programme by law or by delegation do not incur any liability for any use of the Copernicus WorldDEM-30

Article 6(d) forbids giving "the impression to the General Public that the
user's activities are officially endorsed" by the provider or licensor. This
mirror is not endorsed by ESA, the European Union, DLR or Airbus.

## Uses and limits

- **Includes buildings and trees.** "The Copernicus DEM is a Digital
  Surface Model (DSM) which represents the surface of the Earth including
  buildings, infrastructure and vegetation." Forest canopy and roofs are in
  the heights.
- **Edited.** It "is derived from an edited DSM named WorldDEM, i.e.
  flattening of water bodies and consistent flow of rivers has been included.
  Editing of shore- and coastlines, special features such as airports and
  implausible terrain structures has also been applied." The `edm` and `flm`
  masks say where and how.
- **Accuracy.** Absolute vertical accuracy "< 4m (90% linear error)",
  relative vertical accuracy "< 2m (slope ≤20%)" and "< 4m (slope > 20%)",
  absolute horizontal accuracy "< 6m (90% circular error)". The handbook
  notes these are global means ("Local deviations can occur") and exclude
  Antarctica and Greenland. Per pixel, use `hem`.

## How to read it

The tile URL pattern:

```text
https://data.source.coop/portolan-mirrors/copernicus-dem/glo-30-2024-1/<id>/<id>_<ASSET>.tif
```

with `<ASSET>` one of `DEM`, `HEM`, `EDM`, `FLM`, `WBM`. A tile id is the
south-west corner in whole degrees: `N01_E034` covers 1°N to 2°N and 34°E to
35°E. Find the tile for a point from the item table rather than computing it:

```sql
SELECT id
FROM read_parquet('https://data.source.coop/portolan-mirrors/copernicus-dem/glo-30-2024-1/items.parquet')
WHERE bbox.xmin <= 34.525 AND bbox.xmax > 34.525
  AND bbox.ymin <= 1.125 AND bbox.ymax > 1.125;
-- N01_E034
```

Read one value without downloading the tile; GDAL fetches only the block it
needs:

```bash
gdallocationinfo -valonly -wgs84 \
  /vsicurl/https://data.source.coop/portolan-mirrors/copernicus-dem/glo-30-2024-1/N01_E034/N01_E034_DEM.tif 34.525 1.125
# 4171.7265625
```

Each COG stores its statistics (`gdalinfo -stats` reads them without
scanning) and internal overviews. For a whole region, read the overview of a
tile, or for display the collection overview `glo-30-2024-1_overview.tif`
(EPSG:3857, about 1.2 km, RGBA, not heights).

## Join keys

- All rasters of one item share one grid, so pixel (row, col) is the
  same place in all of them.
- `source-acquisitions.tiles` holds item ids of `glo-30-2024-1`.

## Quirks that produce silently wrong answers

- **Heights are relative to the EGM2008 geoid.** "The vertical reference
  datum is the Earth Gravitational Model 2008 (EGM2008; EPSG 3855)." GPS
  heights are ellipsoidal; convert before comparing.
- **Pixels are points, and tiles are offset by half a pixel.** The rasters are
  "RasterPixelIsPoint", and "The coordinates of the centre of the corner
  pixels of a DEM tile always refer to integer values in latitude and
  longitude." So the north-west pixel of `N01_E034` is centred on (34°E, 2°N),
  and its extent as GDAL reports it runs from 33.99986° to 34.99986° east and
  1.00014° to 2.00014° north. Tiles at 180°W reach up to 0.0014° west of
  the antimeridian; their STAC `bbox` and geometry are clipped at 180°W,
  while `proj:transform` keeps the exact grid.
- **3600, not 3601, pixels per row near the equator.** The producer's tiles
  have one extra row and column that repeat their neighbours' edge ("the
  number of grid posts per geocell is equal to the number of intervals per
  geocell plus one"). This mirror drops the last row and column, so tiles
  abut without overlap. `N01_E034` is 3600 x 3600.
- **Fewer columns away from the equator.** Longitude spacing grows with
  latitude (handbook Table 3): 1.0" up to 50°, 1.5" from 50° to 60°, 2.0"
  to 70°, 3.0" to 80°, 5.0" to 85°, 10.0" beyond. Tiles in those bands have
  proportionally fewer columns. Do not assume square pixels or a fixed width.
- **The DEM has no nodata value,** because GLO-30 is void-filled. The handbook
  gives -32767 as NoData for "EEA-10 only". `hem` uses -32767 for edited
  pixels, `edm` and `flm` use 0 for void, and `wbm` 0 means "No water", a
  value.
- **Release 2024_1, assembled from two copies.** Each tile is the version
  current in 2024_1. Tiles that have not changed since the 2021 release were
  read from the copy on AWS Open Data, the rest from the Copernicus Data Space
  Ecosystem; `copdem:pixel_source` says which. Pixel values are unchanged
  from the source in both cases; only the GeoTIFF encoding differs.

## Structure

Assets and structural links resolve relative to the object that carries them.
The root `catalog.json` has an absolute `self` link. Build scripts and
issue tracker: <https://github.com/portolan-mirrors/copernicus-dem>.
