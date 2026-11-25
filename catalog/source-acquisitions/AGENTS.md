# AGENTS.md: Copernicus DEM source acquisitions

The TanDEM-X acquisition footprints listed in the `SRC.kml` file of every
Copernicus DEM GLO-30 tile, as one GeoParquet table. The
[catalog guide](../AGENTS.md) covers the source and licence.

The Product Handbook (issue 6.1, section 1.2.5.6) describes the source file as
"a vector file (kml) containing the information of the data scenes used for
DEM processing. This file includes information about the acquisition ID, data
scene number, data acquisition date and time as well as information about the
height of ambiguity of the data scenes." The handbook says nothing more about
these fields. Everything below that is not quoted was measured from the table.

## Files

- `source-acquisitions.parquet`: GeoParquet 2.0, EPSG:4326, with a `bbox`
  covering column. One row per distinct footprint.
- `source-acquisitions.pmtiles`: the same footprints as vector tiles, layer
  `source-acquisitions`, zoom 0 to 10, with every column above except `geometry` as a tile attribute.
- `styles/default.json`: MapLibre style, coloured as the producer's KML files
  colour the same sets.

## Columns

| Column | Type | Meaning |
|---|---|---|
| `name` | string | Placemark name from the KML, such as `1020979_02`. Not unique (see below). |
| `acquisition_set` | string | "Acquisition Set" from the KML: `First acquisition`, `Second acquisition`, `First crossing acquisition`, `Second crossing acquisition`, `First desert acquisition`, `Second desert acquisition`, `Additional acquisition`, `Other acquisition`. Not defined in the handbook. |
| `acquisition_time` | timestamp | "Acquisition Date", to the minute. No time zone given in the KML. |
| `phase_unwrap` | string | "Phase Unwrap": `multi-baseline` or `single-baseline`. |
| `height_of_ambiguity` | double | "Height of Ambiguity" as written. No unit given in the KML. |
| `tiles` | string[] | Item ids of the `glo-30-2024-1` tiles whose KML lists this footprint. |
| `tile_count` | int | `len(tiles)`. |
| `geometry` | polygon | The footprint as given, not clipped to any tile. |

## Recipes

Every acquisition behind one DEM tile, joined on the item id:

```sql
SELECT name, acquisition_set, acquisition_time, height_of_ambiguity
FROM read_parquet('https://data.source.coop/portolan-mirrors/copernicus-dem/source-acquisitions/source-acquisitions.parquet')
WHERE list_contains(tiles, 'N01_E034')
ORDER BY acquisition_time;
```

The acquisitions under a point, using the bbox column to prune before the
exact test:

```sql
INSTALL spatial;
LOAD spatial;
SELECT name, acquisition_set, acquisition_time
FROM read_parquet('https://data.source.coop/portolan-mirrors/copernicus-dem/source-acquisitions/source-acquisitions.parquet')
WHERE bbox.xmin <= 34.525 AND bbox.xmax >= 34.525 AND bbox.ymin <= 1.125 AND bbox.ymax >= 1.125
  AND ST_Contains(geometry, ST_Point(34.525, 1.125));
```

## Join keys

`tiles` holds item ids of the `glo-30-2024-1` collection (for example
`N01_E034`), so `list_contains(tiles, id)` or `UNNEST(tiles)` joins a footprint to the
tiles it built. A footprint can list several tiles, and a tile has many
footprints.

## Quirks that produce silently wrong answers

- **`name` is not a key.** Some names appear with different footprints in
  different tiles' KML files, and each distinct footprint is its own row. Group
  by `name` only if you mean to merge those.
- **Footprints overlap, and are not clipped.** A footprint runs beyond the
  tiles that list it. Summing areas double-counts.
- **`height_of_ambiguity` is often negative.** That is the value the KML
  gives. The sign is not explained in the handbook; do not take its absolute
  value without a source.
- **Being listed is not the same as covering a pixel.** A tile's KML lists
  the scenes used for the tile; the handbook does not say every listed scene
  contributes to every pixel inside its footprint.
