"""Core data models for tile and AOI management."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator
from shapely.geometry import Polygon, box
from shapely.geometry.base import BaseGeometry


class BoundingBox(BaseModel):
    """Bounding box in WGS84 (EPSG:4326)."""

    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @field_validator("min_x", "max_x")
    @classmethod
    def validate_lon(cls, v: float) -> float:
        if not -180 <= v <= 180:
            raise ValueError("Longitude must be between -180 and 180")
        return v

    @field_validator("min_y", "max_y")
    @classmethod
    def validate_lat(cls, v: float) -> float:
        if not -90 <= v <= 90:
            raise ValueError("Latitude must be between -90 and 90")
        return v

    def as_tuple(self) -> tuple[float, float, float, float]:
        """Return as (min_x, min_y, max_x, max_y) tuple."""
        return (self.min_x, self.min_y, self.max_x, self.max_y)

    def to_polygon(self) -> Polygon:
        """Convert to shapely Polygon."""
        return box(self.min_x, self.min_y, self.max_x, self.max_y)


class Tile(BaseModel):
    """Canonical tile representation from TNM."""

    id: str = Field(..., description="Unique tile identifier")
    dataset_id: str = Field(..., description="Dataset identifier extracted from TNM title")
    tile_id: str = Field(..., description="Tile coordinate/ID (e.g., 'x38y448')")
    publication_date: datetime = Field(..., description="Publication date for priority sorting")
    last_updated: datetime = Field(..., description="Last update timestamp (priority fallback)")
    download_url: str = Field(..., description="Direct download URL from TNM")
    bounds_wgs84: BoundingBox = Field(..., description="Bounding box in EPSG:4326")
    priority: int = Field(..., description="Dataset priority (0=newest, 1, 2...)")
    local_path: Optional[str] = Field(
        default=None, description="Local file path after download"
    )

    class Config:
        """Pydantic config."""

        json_schema_extra = {
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


class AOI(BaseModel):
    """Area of Interest geometry and metadata."""

    geometry: BaseGeometry = Field(..., description="Shapely geometry object")
    crs: str = Field(default="EPSG:4326", description="Coordinate reference system")
    buffer_distance: int = Field(default=1000, description="Buffer distance in meters")

    class Config:
        arbitrary_types_allowed = True

    def bounds(self) -> BoundingBox:
        """Get bounding box of AOI."""
        minx, miny, maxx, maxy = self.geometry.bounds
        return BoundingBox(min_x=minx, min_y=miny, max_x=maxx, max_y=maxy)

    def buffered_bounds(self) -> BoundingBox:
        """Get bounding box of buffered AOI.
        
        Buffers in a projected CRS (Web Mercator) to maintain meter-based distance,
        then returns bounding box in WGS84.
        """
        # Reproject to Web Mercator for proper meter-based buffering
        from shapely.geometry import Polygon
        gdf_tmp = __import__('geopandas').GeoDataFrame(
            [{'geometry': self.geometry}], crs=self.crs
        )
        gdf_projected = gdf_tmp.to_crs("EPSG:3857")
        buffered = gdf_projected.iloc[0].geometry.buffer(self.buffer_distance)
        
        # Reproject back to WGS84
        gdf_buffered = __import__('geopandas').GeoDataFrame(
            [{'geometry': buffered}], crs="EPSG:3857"
        )
        gdf_wgs84 = gdf_buffered.to_crs("EPSG:4326")
        buffered_wgs84 = gdf_wgs84.iloc[0].geometry
        
        minx, miny, maxx, maxy = buffered_wgs84.bounds
        return BoundingBox(min_x=minx, min_y=miny, max_x=maxx, max_y=maxy)

    @classmethod
    def from_file(cls, path: str, buffer_distance: int = 1000) -> "AOI":
        """Load AOI from geometry file (shapefile, GeoJSON, GeoPackage, Parquet).
        
        Automatically detects format and uses appropriate reader.
        Converts to WGS84 (EPSG:4326) if needed.
        For Parquet files, uses PyArrow which is required.
        """
        import geopandas as gpd
        from pathlib import Path

        path_obj = Path(path)
        
        # For parquet files, use read_parquet explicitly
        if path_obj.suffix.lower() == '.parquet':
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
        
        return cls(geometry=geometry, buffer_distance=buffer_distance)
