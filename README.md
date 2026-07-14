# demetrius

High-resolution DEM assembly from USGS 3DEP data for engineering applications.

**demetrius** is a Python library and CLI tool that:
- Accepts a polygon Area of Interest (AOI)
- Queries the USGS TNM Access API for 3DEP 1m DEM tiles
- Selects the most recent available data
- Downloads only required tiles
- Mosaics them into a single Cloud-Optimized GeoTIFF (COG)

Perfect for hydraulic modeling workflows where data integrity and reproducibility are critical.

## Design Principles

- **Accuracy over convenience** – no resampled or inferred data
- **Deterministic output** – same inputs produce identical outputs
- **Tile-first architecture** – operate at tile level
- **Reproducibility** – full provenance via manifest files
- **Extensibility** – pluggable tile sources (TNM now, S1M/STAC later)

## Installation

```bash
pip install demetrius
```

Or from source:

```bash
git clone https://github.com/your/demetrius.git
cd demetrius
pip install -e ".[dev]"
```

### Requirements

- Python 3.12+
- GDAL command-line tools (`gdalbuildvrt`, `gdalwarp`, `gdal_translate`)

**macOS:**
```bash
brew install gdal
```

**Linux (Ubuntu/Debian):**
```bash
sudo apt-get install gdal-bin
```

**Windows:**
Download from [OSGeo4W](https://trac.osgeo.org/osgeo4w/) or use conda:
```bash
conda install gdal
```

## Quick Start

### Basic Usage

```bash
demetrius process --aoi site.shp --output dem.tif
```

### Inspect Tiles

Preview what tiles would be downloaded without downloading:

```bash
demetrius inspect --aoi site.shp
```

Output:
```
======================================================================
demetrius Inspection Report
======================================================================

Area of Interest:
  Bounds: (-74.4500, 40.0500) → (-74.4100, 40.0900)
  Buffer distance: 0 m

Tiles Discovered:
  Total: 3 tile(s)
  Datasets: 1
  
  Dataset Priority Order (newest first):
    [0] PA_3_County_South_Central_2018_D18
        Tiles: 3
        Publication dates: 2018-06-15 → 2018-06-15

Coverage Estimate:
  AOI area: 0.00 sq degrees
  Tile coverage: 0.01 sq degrees
  Coverage: 100.0%
======================================================================
```

## Processing Modes

### Full Pipeline (default)
Runs complete workflow: discover → download → mosaic → reproject → clip → COG

```bash
demetrius process --aoi site.shp --output dem.tif
```

### Download Only
Stop after downloading tiles; saves manifest for later processing

```bash
demetrius process --aoi site.shp --mode download-only
```

Outputs: `manifest.json` and tiles in `~/.demetrius_data/`

### Process Only
Use previously downloaded tiles; skip TNM query and download

```bash
demetrius process --aoi site.shp --mode process-only
```

Requires manifest from previous `download-only` run.

## Options

```
--aoi PATH                      Path to AOI (shapefile, GeoJSON, GeoPackage)
--output PATH                   Output file path [default: dem.tif]
--output-crs EPSG:CODE          Target CRS (e.g., EPSG:32618) [default: auto-detect]
--buffer METERS                 Buffer for tile discovery and clipping [default: 0]
--cellsize FLOAT                Output cellsize in target CRS units [default: 1m converted to CRS units]
--no-snap                       Disable grid snapping [default: enabled]
--require-full-coverage         Fail if AOI not fully covered [default: True]
--mode {full,download-only,process-only}
                                Processing mode [default: full]
```

### Default Cellsize and Grid Snapping

By default, DEMs are generated with:
- **Cellsize**: 1 meter, automatically converted to output CRS units
  - UTM zones (meters): 1.0 meter
  - State Plane (feet): 3.28083... feet
  - Other units: automatic conversion
- **Grid snapping**: Enabled, aligning to multiples of the cellsize

This means all DEMs (even with different CRS selections) have equivalent 1-meter resolution in their native units. The actual pixel size appears different in different CRS (3.28... feet vs 1m) but represents the same geographic resolution.

To override:
- `--cellsize VALUE`: Use explicit cellsize instead of 1m default
- `--no-snap`: Disable grid snapping (rarely needed)

Both cellsize resampling and grid snapping use the same computed value for consistent alignment.

## AOI Geometry Formats

- **Shapefiles**: `.shp` + `.shx` + `.dbf`
- **GeoJSON**: `.geojson`
- **GeoPackage**: `.gpkg`
- **GIS formats**: Any format readable by geopandas (WKT, etc.)

Multi-feature files automatically unionize to single geometry.

## Output

### Cloud-Optimized GeoTIFF (COG)

Final output is COG-compliant GeoTIFF with:
- Internal tiling (512x512)
- Overviews for efficient zoom
- DEFLATE compression
- Valid NODATA values
- Georeferencing in output CRS

### Manifest

JSON file recording all inputs and selected tiles for reproducibility:

```json
{
  "aoi": {
    "bounds": {"min_x": -74.45, "min_y": 40.05, "max_x": -74.41, "max_y": 40.09},
    "crs": "EPSG:4326"
  },
  "buffer": 1000,
  "cellsize": 10.5,
  "tile_count": 3,
  "tiles": [
    {
      "dataset_id": "PA_3_County_South_Central_2018_D18",
      "tile_id": "x38y448",
      "priority": 0,
      "url": "https://...",
      "bounds": {...},
      "local_path": "/home/user/.demetrius_data/dataset_PA_3/tile_x38y448.tif"
    },
    ...
  ]
}
```

Note: `cellsize` is only included if specified; otherwise the field is omitted.

## How It Works

### 1. AOI Preparation

Original geometry is buffered by `--buffer` distance for tile discovery AND for final clipping.

The buffer is applied in the output CRS coordinate space to ensure accurate meter-based (or relevant units) buffering. This means:
- If no `--output-crs` is specified, it's auto-detected based on initial unbuffered tile discovery
- The buffered geometry is then used for both tile filtering and final clip operations

### 1b. Output Resolution (Cellsize)

If `--cellsize` is specified, the final DEM will be resampled to that resolution during reprojection. The cellsize units are in the target CRS units (e.g., meters for UTM zones, feet for State Plane feet zones).

If `--cellsize` is not specified, native tile resolution is preserved (typically 1 meter for USGS 3DEP 1m DEM).

### 2. TNM Query

Tiles are discovered using [USGS TNM Access API](https://tnmaccess.nationalmap.gov/):

```
https://tnmaccess.nationalmap.gov/api/v1/products
?bbox=minx,miny,maxx,maxy
&datasets=Digital Elevation Model (DEM) 1 meter
&prodFormats=GeoTIFF
```

### 3. Tile Selection

**Filtering**: Tiles are filtered to those intersecting the buffered AOI.

**Prioritization**: Datasets are ranked by `publication_date` (newest first). When the same tile appears in multiple datasets, only the newest version is kept.

**Coverage Validation**: Original (unbuffered) AOI must be fully covered. Fails with `--require-full-coverage=true`.

### 4. Parallel Download

Tiles are downloaded in parallel (configurable worker pool) with automatic retry on failure.

### 5. Mosaicking

Tiles are merged using GDAL Virtual Raster (VRT) for efficiency:

```bash
gdalbuildvrt dataset_priority_0.vrt tile1.tif tile2.tif ...
```

No in-memory raster loading; efficient for thousands of tiles.

### 6. Multi-CRS Handling

If tiles span multiple UTM zones, each zone is mosaicked separately, then reprojected to common zone.

### 7. Reprojection

All tiles reprojected to target CRS (auto-detected or user-specified), with optional resampling to specified cellsize:

```bash
gdalwarp -t_srs EPSG:32618 -r bilinear -multi ...
# With cellsize:
gdalwarp -t_srs EPSG:32618 -tr 10.0 10.0 -r bilinear -multi ...
```

- **Resampling**: Minimum bilinear (nearest neighbor forbidden)
- **Multi-threaded**: Uses all available CPU cores
- **Cellsize**: Optional target resolution (applied via `-tr` flag)

### 8. Clipping

Final raster clipped to original AOI using shapely geometry:

```bash
gdalwarp -cutline aoi.geojson -crop_to_cutline ...
```

### 9. Grid Snapping (Optional, Default Enabled)

If snapping is enabled (default), the raster is snapped to a regular grid where all pixel boundaries are exact multiples of the snap distance:

```bash
# Snapping distance = 1m converted to output CRS units
gdalwarp -te <snapped_minx> <snapped_miny> <snapped_maxx> <snapped_maxy> ...
```

**CRS-Aware Snapping:**
- **Meters (UTM/projected)**: Snap distance is 1.0 meter
- **US Survey Feet (State Plane)**: Snap distance is 3.28083... feet
- **Other units**: Automatically converted from 1 meter

This ensures:
- Pixel boundaries align to multiples of snap distance in the output CRS
- Consistent reproducible output across runs
- Compatibility with downstream grid-based processing
- CRS-native coordinate precision

Snapping can be disabled with `--no-snap` or overridden with explicit `--cellsize`.

### 10. COG Output

Result converted to Cloud-Optimized GeoTIFF:

```bash
gdal_translate -of COG -co COMPRESS=DEFLATE ...
```

## Examples

### Statewide DEM with Auto-CRS

```bash
demetrius process \
  --aoi pennsylvania.shp \
  --output pa_dem.tif
```

### Multi-county with Specific CRS

```bash
demetrius process \
  --aoi counties.geojson \
  --output counties_dem.tif \
  --output-crs EPSG:2272 \
  --buffer 5000
```

### Resampled DEM with Specific Resolution

```bash
demetrius process \
  --aoi site.shp \
  --output dem_10m.tif \
  --cellsize 10.0
```

With this command, the output DEM will be resampled to 10-meter resolution in the target CRS units, and snapped to multiples of 10.0.

### Snapping with CRS-Specific Units

```bash
# EPSG:32111 (meters) - snaps to multiples of 1.0 meter
demetrius process \
  --aoi site.shp \
  --output dem_utm.tif \
  --output-crs EPSG:32111

# EPSG:2286 (US survey feet) - snaps to multiples of 3.28... feet
demetrius process \
  --aoi site.shp \
  --output dem_feet.tif \
  --output-crs EPSG:2286
```

Both commands snap to the equivalent of 1 meter, but in their respective CRS units. This ensures consistent, reproducible grid alignment regardless of CRS.

### Two-stage processing (separate network/compute)

**Stage 1: Discovery & Download (on server with network)**
```bash
demetrius process --aoi site.shp --mode download-only
# Output: manifest.json, ~/.demetrius_data/
```

**Stage 2: Processing (offline or different machine)**
```bash
# Copy manifest and tiles to processing machine
demetrius process --aoi site.shp --mode process-only --output dem.tif
```

## Troubleshooting

### "gdalbuildvrt not found"

GDAL command-line tools not installed or not in PATH.

**Solution**: Install GDAL (see Installation section)

### "AOI has uncovered areas"

Tiles don't fully cover the requested area. Options:

1. Increase `--buffer-distance` to pull more tiles
2. Use `--require-full-coverage=false` to proceed anyway
3. Check for data gaps in that region

### "Failed to download tile after 3 attempts"

Network error or TNM API issue. Check:

1. Internet connection
2. TNM API status: https://tnmaccess.nationalmap.gov/
3. Disk space for downloads

### Memory errors with large AOIs

This shouldn't happen - demetrius uses file-backed GDAL operations, not in-memory rasters. If it does:

1. Check available disk space for working directory
2. Reduce AOI size if > 100,000 sq km
3. Report as bug with AOI bounds

## Design Decisions

### Why VRTs instead of merged rasters?

VRTs (Virtual Raster) are memory-efficient XML files that reference tile files without duplicating pixel data. This allows demetrius to handle hundreds or thousands of tiles without loading any into RAM.

### Why strict priority overwrite (no blending)?

Data integrity. In hydraulic modeling, a single blended value from two DEMs introduces unknown uncertainty. We use newest data completely, avoiding statistical mixing.

### Why require full coverage?

Partial coverage hides data gaps that would silently propagate to analysis results. Better to fail loudly so users can address the gap explicitly.

## Architecture

```
User Input (AOI)
    ↓
[TNM Discovery] → Query USGS API
    ↓
[Tile Filtering] → Intersect buffered AOI
    ↓
[Prioritization] → Sort by publication date
    ↓
[Coverage Check] → Validate original AOI covered
    ↓
[Parallel Download] → Get tiles from TNM
    ↓
[VRT Mosaicking] → Per-dataset VRTs
    ↓
[Dataset Merge] → Priority-based overwrite
    ↓
[Reprojection] → gdalwarp to target CRS
    ↓
[Clipping] → Remove buffer, keep only AOI
    ↓
[COG Generation] → Final cloud-optimized GeoTIFF
    ↓
Output (dem.tif) + Manifest (manifest.json)
```

## Development

```bash
# Clone and install in editable mode
git clone <repo>
cd demetrius
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Run linters
black src tests
mypy src
ruff check src

# Build documentation
sphinx-build -b html docs docs/_build
```

## Future Enhancements

- **Seamless 1m (S1M)** integration for national coverage
- **STAC-based** discovery for cloud-native data
- **Cloud-native COG** streaming (no local download)
- **Parallel processing** with Dask for multi-node scaling
- **Hydroconditioning** for hydraulic model integration
- **Vertical datum transforms** (NAVD88 ↔ ellipsoid)
- **Bathymetry** integration (water surface elevation)

## License

MIT License - See LICENSE file

## Citation

If you use demetrius in research, please cite:

```bibtex
@software{demetrius,
  title={demetrius: High-Resolution DEM Assembly from USGS 3DEP},
  author={Your Name},
  year={2024},
  url={https://github.com/your/demetrius}
}
```

## Support

- **Issues**: GitHub Issues
- **Documentation**: https://demetrius.readthedocs.io
- **Community**: GitHub Discussions

## Contributing

Contributions welcome! See CONTRIBUTING.md for guidelines.