# demetrius

High-resolution DEM assembly from USGS 3DEP data for engineering applications.

```bash
demetrius process --aoi watershed.shp --output dem.tif
```

**demetrius** is a Python library and CLI tool that:
- Accepts a polygon Area of Interest (AOI)
- Queries the USGS TNM Access API for 3DEP 1m DEM tiles
- Selects the most recent available data
- Downloads only required tiles
- Mosaics them into a single Cloud-Optimized GeoTIFF (COG)

Perfect for hydraulic modeling workflows where data integrity and reproducibility are critical.

For overlapping datasets, the newer **LiDAR acquisition period** (ScienceBase
flight Start/End dates) takes precedence over an older one. Datasets without
complete flight dates are placed below dated datasets; their publication date
does not stand in for the acquisition date. `inspect` reports both the LiDAR
flight range and the TNM product publication date separately.

## Installation

**Conda (recommended for GDAL and local datum grids):** from a checkout of
this repository, create the environment specified in `environment.yml`. It
installs Python, GDAL, pyproj, `proj-data`, and the Python dependencies from
conda-forge, then installs demetrius into that environment.

```bash
git clone https://github.com/wsp-watertech/demetrius.git
cd demetrius
conda env create -f environment.yml
conda activate demetrius
export PROJ_DATA="$CONDA_PREFIX/share/proj"
```

Set `PROJ_DATA` **after activation**, so it points to this environment rather
than another installation. To persist it for future activations instead of
exporting it in every shell:

```bash
conda env config vars set -n demetrius PROJ_DATA="$CONDA_PREFIX/share/proj"
conda deactivate
conda activate demetrius
```

For an existing Conda environment created without `environment.yml`, install
the missing native dependencies from conda-forge before installing demetrius:

```bash
conda activate demetrius
conda install -c conda-forge gdal pyproj proj-data
export PROJ_DATA="$CONDA_PREFIX/share/proj"
pip install -e .
```

`pip install demetrius` or `pip install -e ".[dev]"` alone does not install
the GDAL command-line tools or the local PROJ datum grids. If you use pip
without Conda, provide compatible GDAL/PROJ system packages and datum grids.

### System Requirements

- **Python**: 3.12 or higher
- **GDAL**: Command-line tools (`gdalbuildvrt`, `gdalwarp`, `gdal_translate`)
- **PROJ datum grids**: `proj-data` for local NAD83 transformations; see below.

### Container and task execution

The Docker image includes the Conda environment, GDAL command-line tools, and
local PROJ datum grids. Build it locally, then pass a CLI subcommand and its
arguments after the image name:

```bash
docker build -t demetrius .
mkdir -p output
docker run --rm --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$(pwd)/site.geojson,dst=/input/site.geojson,readonly" \
  --mount "type=bind,src=$(pwd)/output,dst=/work" \
  demetrius process --aoi /input/site.geojson --output /work/dem.tif
```

For GitHub releases tagged `v*.*.*`, the release workflow also publishes an
image such as `ghcr.io/wsp-watertech/demetrius:v0.1.0b1`. Use that same
`process ...` argument list as the container command for AWS Batch, ECS
Fargate, or another container task runner. GHCR packages can be private by
default; configure registry credentials for the task or make the package
public before deploying it. The default user is UID 10001;
ensure any output mount is writable by that UID. `/work` is the working
directory; downloads and temporary GDAL files use `/tmp` by default. Size
the task's ephemeral storage for both the downloaded tiles and intermediate
rasters; Fargate's default storage may be too small for large DEMs. Mount
durable storage such as EFS for the AOI and output, or explicitly stage inputs
and upload outputs with a separate task: the CLI does not read or write S3
URLs directly. Output and manifest files left only on ephemeral storage are
lost when the task exits. Set `TMPDIR`, `CPL_TMPDIR`, or `DEMETRIUS_DATA_DIR`
to writable paths if using separate scratch storage.

The image sets `PROJ_DATA` to the bundled Conda grids and disables remote PROJ
grid downloads. If additional grids must be fetched, opt in with
`PROJ_NETWORK=ON` and configure the CA bundle as described below.

## Configuration

### Temporary File Storage

By default, demetrius and GDAL store temporary files in the system temp directory (`/tmp` on Linux/macOS, `%TEMP%` on Windows). To use a different location (e.g., a faster SSD, a local ramdisk, or a directory with more space):

**Set the `TMPDIR` environment variable before running demetrius:**

```bash
# Use a custom temp directory
export TMPDIR=/fast/ssd/temp
demetrius process --aoi site.shp --output dem.tif
```

**Or in Python:**

```python
import os

os.environ["TMPDIR"] = "/fast/ssd/temp"

from demetrius.pipeline import run_pipeline

result = run_pipeline(...)
```

Both the demetrius library (which uses Python's `tempfile` module) and GDAL (for mosaicking and clipping operations) respect the `TMPDIR` environment variable.

For very large operations, also set `CPL_TMPDIR` to ensure GDAL uses the same temp directory:

```bash
export TMPDIR=/scratch/tmp
export CPL_TMPDIR=/scratch/tmp
demetrius process --aoi site.shp --output dem.tif
```

**Note:** GDAL performs disk space checks before large operations. If you get a "Free disk space available is X GB, whereas Y GB are at least necessary" error despite having adequate space on your temp filesystem, this is likely GDAL's conservative estimate. The check is automatically disabled in demetrius to prevent false failures on properly configured systems.

**Important:** Python's `tempfile` module silently falls back to the system default temp directory (e.g. `/tmp`) if `TMPDIR` points to a directory that doesn't exist yet or isn't writable — it does **not** raise an error. This is easy to miss, especially when running under `nohup` or other non-interactive contexts. demetrius validates `TMPDIR` at pipeline startup and will auto-create it if missing, or fail loudly with a clear error if it cannot be created/written to, rather than silently writing to `/tmp` (which can exhaust disk space on large jobs).

**Performance tip:** For large mosaics or batch processing, using a fast local SSD for temp storage can significantly speed up processing times.

### PROJ datum grids and corporate proxies

Some AOIs require NAD83 grids to reproject to WGS84. The Conda installation
above supplies these grids locally. Confirm pyproj uses that installation:

```bash
python - <<'PY'
import demetrius
from pyproj import Transformer, datadir

print(datadir.get_data_dir())  # Should match $CONDA_PREFIX/share/proj
print(Transformer.from_crs(4269, 4326, always_xy=True).transform(-84, 37.5, errcheck=True))
PY
```

The transformation should return finite coordinates near `(-84, 37.5)`. If
you see `no database context specified`, check that `PROJ_DATA` points to a
directory containing `proj.db`, and avoid mixing pip-installed pyproj with
Conda's PROJ installation. If coordinates become infinite or a remote grid
request fails, confirm `proj-data` is installed in the active environment
(`conda list proj-data`); a CA bundle alone does not install datum grids.

Only if PROJ must download *other* grids through a TLS-intercepting proxy,
configure a PEM bundle containing both corporate and public CA roots:

```bash
export PROJ_NETWORK=ON
export PROJ_CURL_CA_BUNDLE=/path/to/ca-bundle.pem
export SSL_CERT_FILE=/path/to/ca-bundle.pem
export REQUESTS_CA_BUNDLE=/path/to/ca-bundle.pem
```

`PROJ_CURL_CA_BUNDLE` applies to PROJ grid downloads; `SSL_CERT_FILE` and
`REQUESTS_CA_BUNDLE` apply to Python HTTPS clients that contact TNM and
ScienceBase. Set these before running demetrius. Demetrius explicitly selects
`PROJ_DATA` for pyproj at startup because pyproj can otherwise prefer a
bundled database over the environment variable. Do not disable TLS
verification to work around certificate errors.

### Downloaded Tile Storage

By default, demetrius stores downloaded tiles in `~/.demetrius`. To store downloads in a different location (e.g., a scratch space, faster disk, or shared network drive):

**Set the `DEMETRIUS_DATA_DIR` environment variable:**

```bash
# Use a custom data directory
export DEMETRIUS_DATA_DIR=/scratch/demetrius_tiles
demetrius process --aoi site.shp --output dem.tif --project-bounds boundaries.gpkg
```

**Or pass `--data-dir` to the CLI:**

```bash
demetrius process --aoi site.shp --output dem.tif --project-bounds boundaries.gpkg --data-dir /scratch/demetrius_tiles
```

**Or in Python:**

```python
from demetrius.pipeline import run_pipeline

result = run_pipeline(
    aoi, output_path, project_bounds=boundaries, data_dir="/scratch/demetrius_tiles"
)
```

The CLI `--data-dir` option takes precedence over the `DEMETRIUS_DATA_DIR` environment variable.

### TNM API Resilience

The TNM (USGS 3DEP) Access API occasionally returns 504 Server Unavailable errors, especially during high-load periods. demetrius handles this automatically by:

- **Retrying failed requests** up to 5 times with exponential backoff (1s → 2s → 4s → 8s → 16s)
- **Reducing page size** from 50 to 25 items per request to reduce payload size and improve reliability
- **Distinguishing error types**: Retrying on 5xx server errors, failing immediately on 4xx client errors and timeouts after retries

This is handled transparently—no configuration needed. If tile discovery still fails after retries, the error message will indicate the issue clearly.

## Quick Start

### Basic Usage

Process an AOI polygon and generate a DEM:

```bash
demetrius process --aoi site.shp --output dem.tif --project-bounds boundaries.gpkg
```

Required arguments:
- `--aoi`: Path to AOI geometry (shapefile, GeoJSON, or GeoPackage)

By default, demetrius queries the USGS 3DEP Elevation Index's 1-meter project
footprint layer for projects intersecting the AOI. Pass `--project-bounds
boundaries.gpkg` to use a local file instead (and skip that query). A failed
query or an AOI with no project footprints fails explicitly rather than
assuming tile bounding boxes represent actual coverage. For batch processing,
the default lookup runs separately for each AOI.

### Preview Tiles (No Download)

Inspect what tiles would be used without downloading:

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
  
  Dataset Priority Order (oldest first; newer flights overlay older):
    [0] PA_3_County_South_Central_2018_D18
        Tiles: 3
        LiDAR flight dates: 2018-03-01 → 2018-06-15
        Publication dates: 2020-06-15

Coverage Estimate:
  AOI area: 0.00 sq degrees
  Tile coverage: 0.01 sq degrees
  Coverage: 100.0%
======================================================================
```

## Using demetrius as a Python Library

Beyond the CLI, you can use demetrius directly in Python scripts:

### Basic Example

```python
from pathlib import Path
from demetrius.models import AOI
from demetrius.project_boundaries import ProjectBoundaries
from demetrius.tnm import TNMTileSource
from demetrius.filtering import filter_tiles_by_aoi
from demetrius.priority import prioritize_datasets
from demetrius.downloader import TileDownloader
from demetrius.mosaicker import VRTMosaicker
from demetrius.merger import DatasetMerger
from demetrius.reprojector import Reprojector
from demetrius.snapper import Snapper
from demetrius.clipper import Clipper
from demetrius.elevation_converter import ElevationConverter
from demetrius.cog import COGGenerator

# Load AOI and project boundaries
aoi = AOI.from_file("site.shp")
proj_bounds = ProjectBoundaries.from_file("boundaries.gpkg")

# Query TNM for tiles
source = TNMTileSource()
tiles = source.search(aoi.bounds())

# Filter and prioritize
tiles = filter_tiles_by_aoi(tiles, aoi, proj_bounds)
tiles = prioritize_datasets(tiles)

# Download
downloader = TileDownloader()
for tile in tiles:
    downloader.download_tile(tile)

# Mosaic & merge datasets
mosaicker = VRTMosaicker()
mosaic = mosaicker.create_mosaic_vrt(tiles)

merger = DatasetMerger()
merged = merger.merge(mosaic)

# Reproject & snap
reprojector = Reprojector()
snapper = Snapper()
output_crs = "EPSG:32111"
cellsize = 1.0

reprojected = reprojector.reproject(merged, output_crs, cellsize=cellsize)
snapped = snapper.snap(reprojected, cellsize=cellsize)

# Clip to AOI
clipper = Clipper()
clipped = clipper.clip(snapped, aoi)

# Convert units & generate COG
converter = ElevationConverter()
converted = converter.convert(clipped, "converted.tif", output_crs)

cog_gen = COGGenerator()
cog_gen.generate(converted, "dem.tif")
```

### Accessing Cached Data

Downloaded tiles are cached in `~/.demetrius/`:

```python
from demetrius.downloader import TileDownloader

# Get cache directory
cache_dir = TileDownloader.get_default_data_dir()
print(f"Tiles cached in: {cache_dir}")

# List downloaded tiles
for tile_file in cache_dir.glob("**/*.tif"):
    print(tile_file)
```

### Inspect Workflow Without Downloading

```python
from demetrius.inspector import InspectionReport

# Preview tiles that would be downloaded
report = InspectionReport.from_aoi_and_bounds("site.shp", "boundaries.gpkg")
print(report)
# Shows tiles, datasets, coverage %
```

### Working with Manifests

Manifests track tile provenance for reproducibility:

```python
from demetrius.manifest import Manifest

# Load manifest from previous run
manifest = Manifest.load("dem.tif.manifest.json")

# Access tile information
print(f"Tiles used: {len(manifest.tiles)}")
print(f"Buffer distance: {manifest.buffer} m")
print(f"Output CRS: {manifest.output_crs}")

# Inspect original AOI geometry
print(manifest.aoi.bounds())
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

Outputs: `dem.tif.manifest.json` and tiles in `~/.demetrius/`

### Process Only
Use previously downloaded tiles; skip TNM query and download

```bash
demetrius process --aoi site.shp --mode process-only
```

Requires manifest from previous `download-only` run (named `{output}.manifest.json`).

### Batch Processing Multiple Areas
Use the built-in `batch` command to process every polygon in a vector file in
one call. You must tell it which column to use for naming output DEMs via
`--name-field`—no field name (e.g. `shortname`) is assumed:

```bash
demetrius batch \
  --input study_areas.gpkg \
  --name-field name \
  --output-dir ./output_dems \
  --output-crs EPSG:32111 \
  --buffer 500 \
  --cellsize 1.0
```

Or with the FFRD projection defaults applied to every AOI:

```bash
demetrius batch --input study_areas.gpkg --name-field name --ffrd
```

Each DEM gets its own output (`{output-dir}/{name}.tif`) and manifest
(`{output-dir}/{name}.tif.manifest.json`) recording tiles, buffer, cellsize,
and other parameters for reproducibility. Add `--max-workers N` to process
multiple AOIs concurrently.

The same functionality is available as a library call for programmatic use:

```python
from demetrius.batch import batch_process

results = batch_process(
    "study_areas.gpkg",
    name_field="name",
    output_dir="./output_dems",
    output_crs="EPSG:32111",
    buffer=500,
    cellsize=1.0,
)

for result in results:
    if result.status == "success":
        print(f"✓ {result.name}: {result.output_path}")
    else:
        print(f"✗ {result.name}: {result.error}")
```

## Options

```
--aoi PATH                      Path to AOI (shapefile, GeoJSON, GeoPackage)
--output PATH                   Output file path [default: dem.tif]
--output-crs EPSG:CODE          Target CRS (e.g., EPSG:32618) [default: auto-detect]
--ffrd                          Use FFRD custom projection (overrides --output-crs, enforces snapping, defaults to cellsize=4)
--buffer METERS                 Buffer for tile discovery and clipping [default: 0]
--cellsize FLOAT                Output cellsize in target CRS units [default: 1m converted to CRS units]
--project-bounds PATH           Override USGS 1-meter project footprints with a local boundary file
--no-snap                       Disable grid snapping [default: enabled]
--require-full-coverage         Fail if AOI not fully covered [default: False]
--mode {full,download-only,process-only}
                                Processing mode [default: full]
```

### `batch` command options

```
--input PATH                    Path to vector file of AOI polygons
--name-field TEXT                Column to use for naming each output DEM (required)
--output-dir PATH               Output directory for DEMs and manifests [default: ./batch_output]
--max-workers INT               Number of AOIs to process concurrently [default: 1]
                                 (plus all options shared with `process`, applied to every AOI)
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
      "local_path": "/home/user/.demetrius/dataset_PA_3/tile_x38y448.tif"
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

**Prioritization**: Datasets are ranked by the latest LiDAR flight end date,
then start date (oldest first; newer flights overwrite older data where valid).
Datasets without a complete flight range form the base layer. Overlapping
datasets are retained so newer projects can fill or overlay older ones.

**Coverage Validation**: Project footprints are used to check coverage of the
original (unbuffered) AOI. Gaps warn by default; `--require-full-coverage`
turns them into an error. Footprints indicate project extents, not guaranteed
valid raster pixels.

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
demetrius process --aoi site.shp --mode download-only --output dem.tif --project-bounds boundaries.gpkg
# Output: dem.tif.manifest.json, ~/.demetrius/
```

**Stage 2: Processing (offline or different machine)**
```bash
# Copy dem.tif.manifest.json and tiles to processing machine
demetrius process --aoi site.shp --mode process-only --output dem.tif --project-bounds boundaries.gpkg
```
