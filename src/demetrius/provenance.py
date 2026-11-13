"""Project attribution carried through the elevation warp."""

import colorsys
from pathlib import Path
from typing import TypedDict
from xml.etree import ElementTree as ET

import numpy as np
from osgeo import gdal

from .models import Tile


class ProjectCategory(TypedDict):
    value: int
    name: str
    dataset_id: str
    color: list[int]


def project_categories(tiles: list[Tile]) -> list[ProjectCategory]:
    """Use existing priorities and dataset names without CRS suffixes."""
    datasets: dict[int, ProjectCategory] = {}
    for tile in tiles:
        category: ProjectCategory = {
            "value": tile.priority,
            "name": tile.workunit if tile.product == "opr" and tile.workunit else tile.dataset_id,
            "dataset_id": tile.dataset_id,
            "color": [],
        }
        if tile.priority < 0 or tile.priority >= 2**24:
            raise ValueError("Project priorities must be nonnegative exact Float32 integers")
        if tile.priority in datasets and datasets[tile.priority]["dataset_id"] != tile.dataset_id:
            raise ValueError(f"Several datasets use priority {tile.priority}")
        datasets[tile.priority] = category
    used = set()
    for index, category in enumerate(sorted(datasets.values(), key=lambda c: c["value"])):
        rgb = tuple(
            round(c * 255) for c in colorsys.hsv_to_rgb((index * 0.618033988749895) % 1, 0.7, 0.9)
        )
        color = (rgb[0] << 16) | (rgb[1] << 8) | rgb[2]
        while color in used:
            color = (color + 1) % 2**24
        used.add(color)
        category["color"] = [color >> 16, (color >> 8) & 255, color & 255, 255]
    return sorted(datasets.values(), key=lambda c: c["value"])


def add_project_band(
    source: Path,
    output: Path,
    priority: int,
    *,
    track_vertical: bool = False,
    sources_only: bool = False,
) -> Path:
    """Build a masked priority source, optionally retaining elevations."""
    with gdal.ExceptionMgr():
        dataset = gdal.Open(str(source))
        if dataset.RasterCount != 1:
            raise ValueError(f"Project tracking requires single-band elevation: {source}")
        vrt = gdal.Translate(str(output), dataset, format="VRT")
        vrt.FlushCache()
        vrt = None
        tree = ET.parse(output)
        root = tree.getroot()
        width, height = dataset.RasterXSize, dataset.RasterYSize
        if sources_only:
            root.remove(root.find("VRTRasterBand"))
        constants = [priority, 0, 1] if track_vertical and not sources_only else [priority]
        for number, constant in enumerate(constants, start=1 if sources_only else 2):
            band = ET.SubElement(
                root,
                "VRTRasterBand",
                dataType="Float64" if track_vertical else "Float32",
                band=str(number),
            )
            ET.SubElement(band, "NoDataValue").text = "-1"
            node = ET.SubElement(band, "ComplexSource")
            ET.SubElement(node, "SourceFilename", relativeToVRT="0").text = str(source.resolve())
            ET.SubElement(node, "SourceBand").text = "1"
            for rect in ("SrcRect", "DstRect"):
                ET.SubElement(node, rect, xOff="0", yOff="0", xSize=str(width), ySize=str(height))
            ET.SubElement(node, "UseMaskBand").text = "true"
            nodata = dataset.GetRasterBand(1).GetNoDataValue()
            if nodata is not None:
                ET.SubElement(node, "NODATA").text = str(nodata)
            ET.SubElement(node, "ScaleRatio").text = "0"
            ET.SubElement(node, "ScaleOffset").text = str(constant)
        # A shared alpha preserves explicit masks alongside intrinsic nodata.
        for target in [root, *root.findall("VRTRasterBand")]:
            for old_mask in target.findall("MaskBand"):
                target.remove(old_mask)
        mask_band = ET.SubElement(
            root,
            "VRTRasterBand",
            dataType="Byte",
            band=str(len(constants) + (1 if sources_only else 2)),
        )
        # ID-only warps consume this via -srcalpha. Omitting ColorInterp
        # prevents GDAL from also automatically adding a destination alpha.
        if not sources_only:
            ET.SubElement(mask_band, "ColorInterp").text = "Alpha"
        mask_source = ET.SubElement(mask_band, "SimpleSource")
        ET.SubElement(mask_source, "SourceFilename", relativeToVRT="0").text = str(source.resolve())
        ET.SubElement(mask_source, "SourceBand").text = "mask,1"
        for rect in ("SrcRect", "DstRect"):
            ET.SubElement(
                mask_source, rect, xOff="0", yOff="0", xSize=str(width), ySize=str(height)
            )
        tree.write(output, encoding="utf-8")
    return output


def elevation_band(source: Path, output: Path) -> Path:
    """Expose only elevations to the unit conversion and final DEM writer."""
    with gdal.ExceptionMgr():
        dataset = gdal.Translate(str(output), str(source), format="VRT", bandList=[1])
        dataset.FlushCache()
        dataset = None
    return output


def write_projects(
    merged: Path,
    dem: Path,
    output: Path,
    categories: list[ProjectCategory],
    *,
    overviews: bool,
) -> Path:
    """Write integer priorities on the final DEM grid with names and colors."""
    with gdal.ExceptionMgr():
        source = gdal.Open(str(merged))
        final = gdal.Open(str(dem))
        if (
            source.RasterCount not in (2, 3, 4, 5)
            or (source.RasterXSize, source.RasterYSize) != (final.RasterXSize, final.RasterYSize)
            or source.GetGeoTransform() != final.GetGeoTransform()
            or source.GetProjection() != final.GetProjection()
        ):
            raise ValueError("Project attribution raster does not match the final DEM grid")
        result = gdal.GetDriverByName("GTiff").Create(
            str(output),
            final.RasterXSize,
            final.RasterYSize,
            1,
            gdal.GDT_Int32,
            options=["TILED=YES", "COMPRESS=DEFLATE", "PREDICTOR=2", "BIGTIFF=IF_SAFER"],
        )
        result.SetGeoTransform(final.GetGeoTransform())
        result.SetProjection(final.GetProjection())
        band = result.GetRasterBand(1)
        band.SetNoDataValue(-1)
        band.SetDescription("Project priority")
        ids = {category["value"] for category in categories}
        for y in range(0, final.RasterYSize, 512):
            height = min(512, final.RasterYSize - y)
            for x in range(0, final.RasterXSize, 512):
                width = min(512, final.RasterXSize - x)
                valid = final.GetRasterBand(1).GetMaskBand().ReadAsArray(x, y, width, height) != 0
                values = source.GetRasterBand(2).ReadAsArray(x, y, width, height)
                if source.RasterCount in (4, 5):
                    # Vertical warps apply z -> scale*z + offset to every band.
                    # Warped constants 0 and 1 recover that transform without
                    # a second warp or changing the elevation operation.
                    zero = source.GetRasterBand(3).ReadAsArray(x, y, width, height)
                    one = source.GetRasterBand(4).ReadAsArray(x, y, width, height)
                    scale = one[valid] - zero[valid]
                    if not np.all(np.isfinite(scale)) or np.any(scale == 0):
                        raise ValueError("Invalid vertical transform in project attribution")
                    values[valid] = (values[valid] - zero[valid]) / scale
                chosen = values[valid]
                if (
                    not np.all(np.isfinite(chosen))
                    or np.any(np.abs(chosen - np.rint(chosen)) > 1e-5)
                    or not set(np.unique(np.rint(chosen)).astype(np.int64).tolist()).issubset(ids)
                ):
                    raise ValueError("Valid DEM pixels contain invalid project attribution")
                integers = np.full(values.shape, -1, dtype=np.int32)
                integers[valid] = np.rint(chosen).astype(np.int32)
                band.WriteArray(integers, x, y)
        band = None
        result.Close()
    return finish_projects(output, categories, overviews=overviews)


def finish_projects(
    output: Path,
    categories: list[ProjectCategory],
    *,
    overviews: bool,
) -> Path:
    """Attach the same categorical metadata to paired and IDs-only outputs."""
    with gdal.ExceptionMgr():
        result = gdal.Open(str(output), gdal.GA_Update)
        band = result.GetRasterBand(1)
        if (
            result.RasterCount != 1
            or band.DataType != gdal.GDT_Int32
            or band.GetNoDataValue() != -1
        ):
            raise ValueError("Source raster must be single-band Int32 with nodata -1")
        band.SetDescription("Project priority")
        ids = {category["value"] for category in categories}
        rat = gdal.RasterAttributeTable()
        rat.SetTableType(gdal.GRTT_THEMATIC)
        for name, kind, usage in [
            ("Value", gdal.GFT_Integer, gdal.GFU_MinMax),
            ("Name", gdal.GFT_String, gdal.GFU_Name),
            ("Red", gdal.GFT_Integer, gdal.GFU_Red),
            ("Green", gdal.GFT_Integer, gdal.GFU_Green),
            ("Blue", gdal.GFT_Integer, gdal.GFU_Blue),
            ("Alpha", gdal.GFT_Integer, gdal.GFU_Alpha),
        ]:
            rat.CreateColumn(name, kind, usage)
        names = [""] * (max(ids, default=-1) + 1)
        for row, category in enumerate(categories):
            rat.SetValueAsInt(row, 0, category["value"])
            rat.SetValueAsString(row, 1, category["name"])
            names[category["value"]] = category["name"]
            for column, color in enumerate(category["color"], start=2):
                rat.SetValueAsInt(row, column, color)
        band.SetCategoryNames(names)
        band.SetDefaultRAT(rat)
        if overviews:
            levels = []
            level = 2
            while max(result.RasterXSize, result.RasterYSize) // level >= 256:
                levels.append(level)
                level *= 2
            if levels:
                result.BuildOverviews("NEAREST", levels)
        result.FlushCache()
        band = None
        result.Close()
        result = None
        reopened = gdal.Open(str(output))
        persisted = reopened.GetRasterBand(1).GetDefaultRAT()
        if persisted is None or persisted.GetRowCount() != len(categories):
            raise ValueError("Project category table was not preserved")
        for row, category in enumerate(categories):
            if (
                persisted.GetValueAsInt(row, 0) != category["value"]
                or persisted.GetValueAsString(row, 1) != category["name"]
                or [persisted.GetValueAsInt(row, col) for col in range(2, 6)] != category["color"]
            ):
                raise ValueError("Project category names or colors were not preserved")
        if reopened.GetRasterBand(1).GetCategoryNames() != names:
            raise ValueError("Project category names were not preserved")
    _write_qgis_style(output.with_suffix(".qml"), categories)
    return output


def _write_qgis_style(path: Path, categories: list[ProjectCategory]) -> None:
    root = ET.Element("qgis", version="3", styleCategories="Symbology")
    pipe = ET.SubElement(root, "pipe")
    renderer = ET.SubElement(pipe, "rasterrenderer", type="paletted", band="1", opacity="1")
    palette = ET.SubElement(renderer, "colorPalette")
    for category in categories:
        red, green, blue, alpha = category["color"]
        ET.SubElement(
            palette,
            "paletteEntry",
            value=str(category["value"]),
            label=category["name"],
            color=f"#{red:02x}{green:02x}{blue:02x}",
            alpha=str(alpha),
        )
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
