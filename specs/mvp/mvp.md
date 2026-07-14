# demetrius - MVP

## Overview

`demetrius` is a Python library and CLI tool for assembling high-resolution (1 m) DEMs from USGS 3DEP data for engineering applications.

The tool:
- Accepts a polygon AOI
- Queries the USGS TNM Access API for 3DEP 1 m DEM tiles
- Selects the most recent available data
- Downloads only required tiles
- Mosaics them into a single Cloud-Optimized GeoTIFF (COG)

This tool is designed for hydraulic modeling workflows where data integrity and reproducibility are critical.

---

## Design Principles

- Accuracy over convenience – no resampled or inferred data  
- Deterministic output – same inputs produce identical outputs  
- Tile-first architecture – operate at tile level  
- Reproducibility – full provenance via manifest files  
- Extensibility – pluggable tile sources (TNM now, S1M/STAC later)  

---

## Inputs

### Required
- `geometry`

### Optional
- `output_crs`
- `cell_size`
- `buffer_distance` (default: 1000 m)
- `mode` (full | download-only | process-only)
- `require_full_coverage` (default: true)

---

## Core Workflow

### Step 1 – AOI Preparation

"""
aoi_original
aoi_buffered = buffer(aoi_original, buffer_distance)
"""

- Use buffered AOI for discovery
- Use original AOI for final clipping only

---

## TNM Access Query Specification (MVP)

### Endpoint

"""
https://tnmaccess.nationalmap.gov/api/v1/products
"""

---

### Query Strategy

- Use **buffered AOI bounding box**
- Must be in EPSG:4326

"""
bbox = (minx, miny, maxx, maxy)
"""

---

### Query Parameters (MVP)

"""
params = {
    "bbox": f"{minx},{miny},{maxx},{maxy}",
    "datasets": "Digital Elevation Model (DEM) 1 meter",
    "prodFormats": "GeoTIFF",
    "outputFormat": "JSON"
}
"""

### Notes:
- Do NOT implement pagination for MVP
- Assume TNM returns all intersecting tiles
- Pagination support will be added later if needed

---

### Expected TNM Response Structure

Each item represents a **single tile**:

"""
{
  "title": "...",
  "publicationDate": "2021-11-18",
  "lastUpdated": "2021-11-22T17:32:57",
  "format": "GeoTIFF",
  "downloadURL": "...",
  "boundingBox": {
    "minX": ...,
    "maxX": ...,
    "minY": ...,
    "maxY": ...
  }
}
"""

---

## Tile Data Model

All downstream logic MUST use this canonical structure:

"""
class Tile:
    id: str
    dataset_id: str
    tile_id: str
    publication_date: datetime
    last_updated: datetime
    download_url: str
    bounds_wgs84: tuple
    priority: int
"""

---

## Tile Parsing

### Required fields

- `downloadURL` → raster URL  
- `boundingBox` → spatial extent  
- `publicationDate` → primary priority metric  

---

### Dataset ID Extraction

From:

"""
USGS 1 Meter 18 x38y448 PA_3_County_South_Central_2018_D18
"""

Extract:

"""
dataset_id = "PA_3_County_South_Central_2018_D18"
tile_id    = "x38y448"
"""

This logic:
- MUST be centralized
- MUST be unit tested

Fallback:
- parse dataset_id from `downloadURL`

---

## Tile Filtering

### Bounding filter (initial)

Use TNM bounding box directly.

### AOI intersection filter

"""
if tile.bounds intersects aoi_buffered:
    keep
else:
    discard
"""

Optional future:
- precise polygon intersection

---

## Dataset Grouping

"""
dataset_id -> list[Tile]
"""

---

## Dataset Priority

Sort datasets by:

1. publication_date (descending)  
2. last_updated (fallback)  

Assign:

"""
priority = 0  # newest dataset
priority = 1
priority = 2
"""

---

## Duplicate Tile Resolution

If same tile appears in multiple datasets:

- Keep tile from highest-priority dataset
- Discard others

---

## Coverage Validation

If require_full_coverage = true:

- AOI must be completely covered by retained tiles
- Buffer gaps allowed
- AOI gaps NOT allowed

---

## Tile Download

Requirements:

- Parallel download
- Retry on failure
- Verify file integrity

Directory structure:

"""
/demetrius_data/
  /dataset_<id>/
    tile_*.tif
manifest.json
"""

---

## Mosaic Workflow

### Per Dataset

- Mosaic tiles in native CRS
- No reprojection yet

---

### Cross-Dataset Merge

- Process datasets from oldest → newest
- Newer overwrites older

Constraints:
- No averaging
- No blending
- Mask-based overwrite only

---

## Multi-CRS Handling

If multiple UTM zones:

1. Mosaic per CRS  
2. Reproject to common CRS  
3. Merge  

---

## Reprojection

If output_crs is provided:

- Reproject raster

Resampling rules:
- Nearest neighbor is forbidden
- Minimum: bilinear

---

## Clip

- Clip to original AOI
- Remove all buffered fringe

---

## Output

- Single Cloud-Optimized GeoTIFF

Requirements:
- internal tiling
- overviews
- compression
- valid nodata

---

## Modes

### full
- Run entire workflow

### download-only
- Run TNM query + tile filtering + download
- Output manifest only

### process-only
- Use local tiles + manifest
- Skip TNM entirely

---

## Manifest Format

"""
{
  "aoi": "...",
  "buffer_distance": 1000,
  "tiles": [
    {
      "dataset_id": "...",
      "tile_id": "...",
      "priority": 0,
      "url": "...",
      "bounds": [...],
      "local_path": "..."
    }
  ]
}
"""

---

## TileSource Abstraction (Future-Proofing)

All TNM logic MUST be isolated.

### Interface

"""
class TileSource:
    def search(self, aoi_bbox) -> listpass
"""

---

### TNM Implementation (MVP)

"""
class TNMTileSource(TileSource):
    def search(self, aoi_bbox):
        # query TNM API
        # parse tiles
        # return list[Tile]
"""

---

### Future Sources

"""
class S1MTileSource(TileSource):
    pass

class STACTileSource(TileSource):
    pass
"""

---

## Critical Constraint

ALL downstream processing MUST operate on:

"""
list[Tile]
"""

NOT on:
- TNM JSON
- dataset-specific structures

---

## Debug Command

"""
demetrius inspect --aoi site.gpkg
"""

Outputs:
- tiles found
- datasets identified
- priority order
- CRS distribution

---

## Non-Goals (MVP)

- No lidar point cloud processing  
- No hydroconditioning  
- No vertical datum transforms  
- No bathymetry  

---

## Future Extensions

- Seamless 1 m (S1M) integration  
- STAC-based discovery  
- Cloud-native COG streaming  
- Parallel processing (Dask)  
- Integration with hydraulic models  

## Large AOI Processing Requirements

### Scope

`demetrius` MUST support **very large Areas of Interest (AOIs)**, including:

- thousands of 1 m DEM tiles
- multi-county or statewide extents
- multi-UTM-zone coverage
- datasets totaling hundreds of gigabytes

This requirement fundamentally drives the processing architecture.

---

## Core Constraint

The system MUST NOT rely on:

- in-memory raster mosaics
- naive raster merging approaches (e.g., rasterio.merge for full AOIs)
- full-resolution intermediate rasters written unnecessarily

Instead, the system MUST use:

- **streaming workflows**
- **virtual raster constructs (VRT)**
- **GDAL-based file-driven processing**

---

## Processing Architecture (Required)

`demetrius` is a **pipeline orchestrator**, not a raster processing engine.

- Tile discovery and selection are handled in Python
- Large-scale raster operations are delegated to GDAL

---

## Required Tools

The implementation MUST use:

- GDAL command-line utilities or equivalent bindings for:
  - mosaicking (VRT)
  - reprojection
  - clipping
  - COG generation

---

## Mosaic Strategy

### Requirement

Tiles MUST be mosaicked using **Virtual Raster (VRT)** workflows instead of materialized rasters.

---

### Per-Dataset Mosaic

For each dataset:

"""
gdalbuildvrt dataset_<priority>.vrt tile1.tif tile2.tif ...
"""

Properties:

- no pixel data duplication
- minimal memory usage
- scalable to thousands of tiles

---

## Cross-Dataset Merge Strategy

### Requirement

Dataset priority MUST be enforced during mosaic.

Newer data MUST overwrite older data.

---

### Implementation Approach

Datasets are applied in order:

"""
oldest dataset → newest dataset
"""

Two acceptable strategies:

1. Iterative warp-based overwrite (simpler)
2. Layered VRT with priority control (preferred for performance)

Constraints:

- no averaging of overlapping datasets
- no blending between datasets
- strict overwrite semantics

---

## Reprojection Strategy

### Requirement

Reprojection MUST be performed using GDAL, not in-memory Python operations.

---

### Required Command

"""
gdalwarp \
  -t_srs <output_crs> \
  -r bilinear \
  -multi \
  -wo NUM_THREADS=ALL_CPUS \
  input.vrt \
  output.tif
"""

---

### Constraints

- nearest neighbor resampling is forbidden
- minimum resampling method: bilinear
- must support multi-threaded execution

---

## Clipping Strategy

Clipping MUST be performed using GDAL with AOI geometry:

"""
gdalwarp \
  -cutline aoi.geojson \
  -crop_to_cutline \
  input.tif \
  clipped.tif
"""

---

## Output Strategy (COG)

Final output MUST be a Cloud-Optimized GeoTIFF (COG).

---

### Required Command

"""
gdal_translate \
  -of COG \
  -co COMPRESS=DEFLATE \
  -co BLOCKSIZE=512 \
  input.tif \
  dem.tif
"""

---

## Intermediate Data Management

To support large AOIs, the system MUST:

- use **VRTs for intermediate mosaics**
- avoid writing large intermediate GeoTIFFs unless required
- use temporary working directories for:
  - VRTs
  - intermediate warped rasters

---

## Parallelization

### Download

- must use parallel downloads (thread-based)

### Processing

- GDAL must be configured to:
  - use all available CPU cores
  - stream processing internally

---

## Memory Management

The system MUST:

- never load full AOI rasters into memory
- rely on GDAL chunking/windowing
- operate in a file-backed pipeline

---

## Performance Considerations

Primary bottlenecks:

1. disk I/O  
2. network download  
3. reprojection cost  

The system should optimize for:

- minimizing intermediate writes
- avoiding redundant raster reads
- maximizing sequential disk access

---

## Failure Modes

The system MUST detect and fail early if:

- AOI requires an infeasible number of tiles (configurable threshold)
- GDAL operations fail (non-zero exit code)
- insufficient disk space for intermediate processing

---

## Design Implication

The processing flow is:

"""
Tiles → VRT (per dataset)
     → priority merge
     → reprojection (GDAL)
     → clip
     → COG output
"""

---

## Future Compatibility

This architecture MUST remain compatible with:

- Seamless 1 m DEM (S1M)
- STAC-based data sources
- Cloud-native COG streaming workflows

The core invariant:

"""
All processing operates on tile-level inputs
All heavy raster operations are delegated to GDAL
"""
