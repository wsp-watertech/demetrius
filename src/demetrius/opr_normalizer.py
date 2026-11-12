"""Validate OPR vertical references and expose meter-valued, horizontal-only VRTs."""

import logging
from collections.abc import Sequence
from pathlib import Path
from xml.etree import ElementTree

import pyproj
from osgeo import gdal

from .elevation_converter import ElevationConverter
from .models import Tile

logger = logging.getLogger(__name__)


def _vertical(crs: pyproj.CRS) -> pyproj.CRS | None:
    if crs.is_vertical and not crs.is_compound:
        return crs
    return next((part for part in crs.sub_crs_list if part.is_vertical), None)


def normalize_opr_tiles(tiles: Sequence[Tile], working_dir: Path) -> list[Tile]:
    """Normalize OPR elevations to meters without altering cached downloads.

    No vertical-datum transformation is attempted. OPR must match the vertical
    datum of the 1m inputs (NAVD88 by product convention if not embedded).
    """
    if not any(tile.product == "opr" for tile in tiles):
        return list(tiles)
    reference = pyproj.CRS("EPSG:5703") if any(t.product == "1m" for t in tiles) else None
    references = []
    with gdal.ExceptionMgr():
        for tile in tiles:
            if tile.product != "1m":
                continue
            dataset = gdal.Open(str(tile.local_path))
            if not dataset.GetProjection():
                raise ValueError(f"No source CRS for {tile.id}")
            vertical = _vertical(pyproj.CRS(dataset.GetProjection()))
            if vertical is not None:
                references.append(vertical)
        if references:
            reference = references[0]
            if any(v.datum != reference.datum for v in references):
                raise ValueError("1m inputs have incompatible vertical datums")
        elif reference is not None:
            logger.warning(
                "Using NAVD88 as the 1m vertical datum convention; no explicit "
                "1m vertical CRS was found"
            )

        normalized = []
        for tile in tiles:
            if tile.product != "opr":
                normalized.append(tile)
                continue
            dataset = gdal.Open(str(tile.local_path))
            if dataset.RasterCount != 1 or not dataset.GetProjection():
                raise ValueError(f"OPR tile {tile.id} must be a single-band DEM with a CRS")
            crs = pyproj.CRS(dataset.GetProjection())
            horizontal = crs.sub_crs_list[0] if crs.is_compound else crs
            if (
                not (horizontal.is_projected or horizontal.is_geographic)
                or len(horizontal.axis_info) != 2
            ):
                raise ValueError(f"OPR tile {tile.id} needs a two-dimensional horizontal CRS")
            embedded = _vertical(crs)
            catalog = pyproj.CRS(tile.vertical_crs) if tile.vertical_crs else None
            if catalog is not None and not catalog.is_vertical:
                raise ValueError(f"Invalid OPR vertical CRS for {tile.id}")
            if embedded is not None and catalog is not None and embedded.datum != catalog.datum:
                raise ValueError(f"Conflicting OPR raster/catalog vertical datums for {tile.id}")
            vertical = embedded or catalog
            if vertical is None:
                raise ValueError(f"Unknown OPR vertical datum for {tile.id}")
            if reference is None:
                reference = vertical
            if vertical.datum != reference.datum:
                raise ValueError(
                    f"Unknown or incompatible OPR vertical datum for {tile.id}; "
                    f"expected {reference.datum}. Supply compatible, documented DEM data; "
                    "automatic vertical-datum conversion is not supported."
                )
            band = dataset.GetRasterBand(1)
            if band.GetScale() not in (None, 1) or band.GetOffset() not in (None, 0):
                raise ValueError(f"Unsupported scaled OPR elevations in {tile.id}")
            unit = band.GetUnitType().lower().strip()
            if unit:
                factor = ElevationConverter.UNIT_TO_METERS.get(unit)
                if factor is None:
                    raise ValueError(f"Unknown OPR elevation unit {unit!r} in {tile.id}")
            else:
                factor = vertical.axis_info[0].unit_conversion_factor
            if (
                embedded is not None
                and abs(factor - embedded.axis_info[0].unit_conversion_factor) > 1e-9
            ):
                raise ValueError(f"Conflicting OPR band/vertical CRS units for {tile.id}")
            path = working_dir / f"{tile.id}_meters.vrt"
            # Removing the vertical CRS after scaling prevents GDAL from applying
            # a second unit conversion or an unintended geoid transformation.
            vrt = gdal.Translate(
                str(path),
                dataset,
                options=[
                    "-of",
                    "VRT",
                    "-ot",
                    "Float32",
                    "-a_srs",
                    horizontal.to_wkt(),
                    "-scale",
                    "0",
                    "1",
                    "0",
                    repr(factor),
                ],
            )
            vrt.GetRasterBand(1).SetUnitType("m")
            vrt.FlushCache()
            vrt = None
            # Some GDAL versions serialize VRT scale ratios with only six
            # significant digits, losing the distinction between foot types.
            tree = ElementTree.parse(path)
            ratio = tree.find(".//ComplexSource/ScaleRatio")
            if ratio is not None:
                ratio.text = repr(factor)
                tree.write(path, encoding="utf-8")
            elif factor != 1:
                raise ValueError(f"Could not encode OPR elevation unit conversion for {tile.id}")
            normalized.append(tile.model_copy(update={"local_path": str(path)}))
            logger.info("Normalized OPR elevations to meters: %s (factor %.12f)", tile.id, factor)
    return normalized
