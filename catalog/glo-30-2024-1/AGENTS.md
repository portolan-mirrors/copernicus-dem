# AGENTS.md: Copernicus DEM GLO-30, release 2024_1

One item per 1° x 1° tile, each with five rasters and the producer's XML
metadata. The [catalog guide](../AGENTS.md) covers the source, licence and
the traps every asset shares. Values and quotes below are from the
[Product Handbook, issue 6.1](https://dataspace.copernicus.eu/sites/default/files/media/files/2026-10/geo1988-copernicusdem-spe-002_producthandbook_i6.1.pdf)
unless a query shows where they were measured.

## Assets

| Asset | File | Type | Nodata | What a pixel is |
|---|---|---|---|---|
| `dem` | `<id>_DEM.tif` | float32 | none | Height in metres above the EGM2008 geoid (EPSG:3855). A surface model: "including buildings, infrastructure and vegetation". |
| `hem` | `<id>_HEM.tif` | float32 | -32767 | Height error: "the corresponding height error for each DEM pixel in the form of the standard deviation". -32767 marks edited pixels. |
| `edm` | `<id>_EDM.tif` | uint8 | 0 | Editing Mask, handbook Table 6. |
| `flm` | `<id>_FLM.tif` | uint8 | 0 | Filling Mask, handbook Table 7: which source filled an edited pixel. |
| `wbm` | `<id>_WBM.tif` | uint8 | none | Water Body Mask, handbook Table 8. 0 is "No water", a value. |
| `metadata` | `<id>.xml` | XML | | The producer's product metadata, unchanged. |

Mask values, from the handbook tables (also in each asset's
`classification:classes`):

- **EDM:** 0 Void (no data), 1 Not edited, 2 Infill of external elevation
  data, 3 Interpolated pixels, 4 Smoothed pixels, 5 Airport editing, 6 Raised
  negative elevation pixels, 7 Flattened pixels, 8 Ocean pixels, 9 Lake
  pixels, 10 River pixels, 11 Shoreline pixels, 12 Morphed pixels (series of
  pixels manually set), 13 Shifted pixels.
- **FLM:** 0 Void (no data), 1 Edited (except filled pixels), 2 Not edited /
  not filled, 3 ASTER, 4 SRTM90, 5 SRTM30, 6 GMTED2010, 7 SRTM30plus,
  8 TerraSAR-X Radargrammetric DEM, 9 AW3D30, 100 Norway DEM, 101 DSM05 Spain,
  102 Norway DEM v2.
- **WBM:** 0 No water, 1 Ocean, 2 Lake, 3 River.

## Recipes

All five values at one point. On the summit area of Mount Elgon, tile
`N01_E034`:

```bash
B=https://data.source.coop/portolan-mirrors/copernicus-dem/glo-30-2024-1
for a in DEM HEM EDM FLM WBM; do
  gdallocationinfo -valonly -wgs84 /vsicurl/$B/N01_E034/N01_E034_$a.tif 34.525 1.125
done
# 4171.7265625
# 0.425464361906052
# 1
# 2
# 0
```

The same on Lake Victoria (33.0, 0.1), tile `N00_E033`, shows an edited
pixel:

```bash
for a in DEM HEM EDM FLM WBM; do
  gdallocationinfo -valonly -wgs84 /vsicurl/$B/N00_E033/N00_E033_$a.tif 33.0 0.1
done
# 1133
# -32767
# 9
# 1
# 2
```

The lake surface was flattened to one height (EDM 9, Lake pixels), so HEM has
no error estimate there (-32767), and the WBM says Lake (2).

## Per-item properties

- `copdem:release` is `2024_1` on every item. `copdem:release_changed` is the
  release in which the tile last changed, from the suffix of the producer's
  `eopIdentifier` (kept as `copdem:eop_identifier`).
- `copdem:pixel_source` says which copy the bytes were read from: `aws` (the
  2021 release on AWS Open Data, used where the tile has not changed since)
  or `cdse` (the Copernicus Data Space Ecosystem). The pixels are the same
  either way; see the catalog guide.
- `start_datetime` and `end_datetime` are the acquisition period the
  producer's catalogue gives for the tile.
