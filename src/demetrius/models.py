"""Core data models for tile and AOI management."""

import logging
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator
from shapely.geometry import Polygon, box
from shapely.geometry.base import BaseGeometry

logger = logging.getLogger(__name__)


class BoundingBox(BaseModel):
    """Bounding box in WGS84 (EPSG:4326)."""

    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @field_validator("min_x", "max_x")
    @classmethod
    def validate_lon(cls, v: float) -> float:
        """Validate longitude values.

        Parameters
        ----------
        v : float
            Longitude value to validate.

        Returns
        -------
        float
            Validated longitude value.

        Raises
        ------
        ValueError
            If the longitude is outside the valid range.
        """
        if not -180 <= v <= 180:
            raise ValueError("Longitude must be between -180 and 180")
        return v

    @field_validator("min_y", "max_y")
    @classmethod
    def validate_lat(cls, v: float) -> float:
        """Validate latitude values.

        Parameters
        ----------
        v : float
            Latitude value to validate.

        Returns
        -------
        float
            Validated latitude value.

        Raises
        ------
        ValueError
            If the latitude is outside the valid range.
        """
        if not -90 <= v <= 90:
            raise ValueError("Latitude must be between -90 and 90")
        return v

    def as_tuple(self) -> tuple[float, float, float, float]:
        """Return the bounds as a tuple.

        Returns
        -------
        tuple[float, float, float, float]
            Tuple in ``(min_x, min_y, max_x, max_y)`` order.
        """
        return (self.min_x, self.min_y, self.max_x, self.max_y)

    def to_polygon(self) -> Polygon:
        """Convert the bounding box to a shapely polygon.

        Returns
        -------
        Polygon
            Polygon representing the bounding box extent.
        """
        return box(self.min_x, self.min_y, self.max_x, self.max_y)


class Tile(BaseModel):
    """Canonical tile representation from TNM."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "USGS_1m_PA_3_County_South_Central_2018_D18_x38y448",
                "dataset_id": "PA_3_County_South_Central_2018_D18",
                "tile_id": "x38y448",
                "publication_date": "2021-11-18T00:00:00",
                "last_updated": "2021-11-22T17:32:57",
                "download_url": "https://example.com/tile.tif",
                "bounds_wgs84": {
                    "min_x": -74.5,
                    "min_y": 40.0,
                    "max_x": -74.4,
                    "max_y": 40.1,
                },
                "priority": 0,
            }
        }
    )

    id: str = Field(..., description="Unique tile identifier")
    dataset_id: str = Field(..., description="Dataset identifier extracted from TNM title")
    tile_id: str = Field(..., description="Tile coordinate/ID (e.g., 'x38y448')")
    publication_date: datetime = Field(..., description="Publication date for priority sorting")
    last_updated: datetime = Field(..., description="Last update timestamp (priority fallback)")
    download_url: str = Field(..., description="Direct download URL from TNM")
    bounds_wgs84: BoundingBox = Field(..., description="Bounding box in EPSG:4326")
    priority: int = Field(..., description="Dataset priority (0=newest, 1, 2...)")
    local_path: Optional[str] = Field(default=None, description="Local file path after download")


class AOI(BaseModel):
    """Area of Interest geometry and metadata."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    geometry: BaseGeometry = Field(..., description="Shapely geometry object")
    crs: str = Field(default="EPSG:4326", description="Coordinate reference system")
    buffer: int = Field(default=0, description="Buffer distance in meters")

    def bounds(self) -> BoundingBox:
        """Get the AOI bounding box.

        Returns
        -------
        BoundingBox
            Bounding box of the AOI geometry.
        """
        minx, miny, maxx, maxy = self.geometry.bounds
        return BoundingBox(min_x=minx, min_y=miny, max_x=maxx, max_y=maxy)

    def buffered_bounds(self, output_crs: Optional[str] = None) -> BoundingBox:
        """Get bounding box of buffered AOI.

        Buffers in Web Mercator to maintain meter-equivalent distance for TNM
        discovery, then returns bounding box in WGS84. The buffer distance is
        interpreted as being in output_crs units (or meters if output_crs is
        not specified).

        Parameters
        ----------
        output_crs : str | None
            The target output CRS. Used to convert buffer from CRS units to
            meters for Web Mercator projection. If None, buffer is assumed
            to already be in meters.

        Returns
        -------
        BoundingBox
            Bounding box of the buffered AOI in WGS84.
        """
        if self.buffer == 0:
            return self.bounds()

        # If output CRS is provided, convert buffer from CRS units to meters
        buffer_in_meters = self.buffer
        if output_crs is not None:
            try:
                from .snapper import Snapper
                snapper = Snapper()
                meters_to_crs_units = snapper.get_conversion_factor_for_snapping(output_crs)
                # Invert the conversion: if 1 meter = X CRS units, then buffer_in_crs_units / X = meters
                buffer_in_meters = self.buffer / meters_to_crs_units
            except Exception as e:
                logger.warning(
                    f"Failed to convert buffer from {output_crs} units to meters: {e}. "
                    "Assuming buffer is already in meters."
                )

        # Reproject to Web Mercator for buffering
        gdf = __import__("geopandas").GeoDataFrame([{"geometry": self.geometry}], crs=self.crs)
        gdf_projected = gdf.to_crs("EPSG:3857")
        buffered = gdf_projected.iloc[0].geometry.buffer(buffer_in_meters)

        # Reproject back to WGS84
        gdf_buffered = __import__("geopandas").GeoDataFrame(
            [{"geometry": buffered}], crs="EPSG:3857"
        )
        gdf_wgs84 = gdf_buffered.to_crs("EPSG:4326")
        buffered_wgs84 = gdf_wgs84.iloc[0].geometry

        minx, miny, maxx, maxy = buffered_wgs84.bounds
        return BoundingBox(min_x=minx, min_y=miny, max_x=maxx, max_y=maxy)

    def buffered_geometry(self) -> BaseGeometry:
        """Get buffered AOI geometry in WGS84.

        Returns the original geometry if buffer is 0.
        Buffers in Web Mercator for global consistency, then returns in WGS84.

        Returns
        -------
        BaseGeometry
            Buffered AOI geometry in WGS84.
        """
        if self.buffer == 0:
            return self.geometry

        import geopandas as gpd

        # Reproject to Web Mercator for buffering
        gdf = gpd.GeoDataFrame([{"geometry": self.geometry}], crs=self.crs)
        gdf_projected = gdf.to_crs("EPSG:3857")
        buffered = gdf_projected.iloc[0].geometry.buffer(self.buffer)

        # Reproject back to WGS84
        gdf_buffered = gpd.GeoDataFrame([{"geometry": buffered}], crs="EPSG:3857")
        gdf_wgs84 = gdf_buffered.to_crs("EPSG:4326")
        return gdf_wgs84.iloc[0].geometry

    def buffered_geometry_in_crs(self, target_crs: str) -> BaseGeometry:
        """Get buffered AOI geometry in a specific CRS.

        Reprojects to target CRS, buffers with the specified distance in
        target CRS units, then returns geometry in target CRS. The buffer
        is assumed to be in target_crs units.

        Parameters
        ----------
        target_crs : str
            Target CRS, for example ``"EPSG:32111"`` for UTM.

        Returns
        -------
        BaseGeometry
            Buffered geometry in the target CRS. Returns the reprojected
            original geometry if ``buffer`` is 0.
        """
        import geopandas as gpd

        # Reproject to target CRS
        gdf = gpd.GeoDataFrame([{"geometry": self.geometry}], crs=self.crs)
        try:
            gdf_projected = gdf.to_crs(target_crs)
            geom_projected = gdf_projected.iloc[0].geometry
        except Exception as e:
            # If reprojection fails (datum transformation issue), fall back to Web Mercator buffering
            logger.warning(
                f"Reprojection to {target_crs} failed: {e}. Falling back to Web Mercator buffering."
            )
            return self.buffered_geometry()

        # Check for invalid geometry after reprojection (e.g., coordinates with Infinity)
        try:
            # Try to validate by getting bounds
            bounds = geom_projected.bounds
            if any(b == float("inf") or b == float("-inf") for b in bounds):
                logger.warning(
                    f"Invalid coordinates after reprojection to {target_crs}. Falling back to Web Mercator buffering."
                )
                return self.buffered_geometry()
        except Exception as e:
            logger.warning(
                f"Geometry validation failed after reprojection: {e}. Falling back to Web Mercator buffering."
            )
            return self.buffered_geometry()

        # Apply buffer if specified (buffer is already in target_crs units)
        if self.buffer == 0:
            return geom_projected

        # Buffer in target CRS with lower resolution if needed to avoid errors
        try:
            buffered = geom_projected.buffer(self.buffer)
        except Exception as e:
            # Try with lower resolution if default fails
            logger.warning(f"Buffer with default resolution failed: {e}, trying with resolution=8")
            try:
                buffered = geom_projected.buffer(self.buffer, resolution=8)
            except Exception as e2:
                # If buffering still fails, fall back to Web Mercator buffering
                logger.warning(
                    f"Buffering with lower resolution also failed: {e2}. Falling back to Web Mercator buffering."
                )
                return self.buffered_geometry()

        # Validate buffered geometry (but don't try to fix with buffer(0) if it fails)
        if not buffered.is_valid:
            try:
                buffered = buffered.buffer(0)
            except Exception as e:
                logger.warning(
                    f"Could not fix invalid buffered geometry: {e}. Falling back to Web Mercator buffering."
                )
                return self.buffered_geometry()

        return buffered

    @classmethod
    def from_file(cls, path: str, buffer: int = 0) -> "AOI":
        """Load AOI from geometry file (shapefile, GeoJSON, GeoPackage, Parquet).

        Automatically detects format and uses appropriate reader.
        Converts to WGS84 (EPSG:4326) if needed.
        For Parquet files, uses PyArrow which is required.

        Parameters
        ----------
        path : str
            Path to the geometry file.
        buffer : int, default=0
            Buffer distance in meters.

        Returns
        -------
        AOI
            Loaded AOI instance.

        Raises
        ------
        ValueError
            If the file contains no geometries.
        """
        import geopandas as gpd
        from pathlib import Path

        path_obj = Path(path)

        # For parquet files, use read_parquet explicitly
        if path_obj.suffix.lower() == ".parquet":
            gdf = gpd.read_parquet(path)
        else:
            gdf = gpd.read_file(path)

        if gdf.empty:
            raise ValueError(f"No geometries found in {path}")

        # Convert to WGS84 if not already
        if gdf.crs and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs("EPSG:4326")

        if len(gdf) > 1:
            geometry = gdf.unary_union
        else:
            geometry = gdf.iloc[0].geometry

        return cls(geometry=geometry, buffer=buffer)
