"""Project boundary coverage from FESM or similar sources."""

import logging
from pathlib import Path

import geopandas as gpd
from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry

logger = logging.getLogger(__name__)


class ProjectBoundaries:
    """Load and query actual DEM coverage from project footprints.
    
    The project boundaries file (typically FESM data) contains the true coverage
    areas of DEM projects, which may not align with TNM tile boundaries. This
    allows us to detect gaps and validate coverage more accurately.
    """

    def __init__(self, gdf: gpd.GeoDataFrame):
        """Initialize with GeoDataFrame of project boundaries.
        
        Args:
            gdf: GeoDataFrame with project boundaries (must have geometry column)
        
        Raises:
            ValueError: If GeoDataFrame is empty or has no geometry
        """
        if gdf.empty:
            raise ValueError("Project boundaries GeoDataFrame is empty")
        if "geometry" not in gdf.columns:
            raise ValueError("GeoDataFrame must have 'geometry' column")
        
        # Ensure WGS84
        if gdf.crs and gdf.crs.to_epsg() != 4326:
            logger.info(f"Converting project boundaries from {gdf.crs} to EPSG:4326")
            gdf = gdf.to_crs("EPSG:4326")
        
        self.gdf = gdf.reset_index(drop=True)
        self.spatial_index = self.gdf.sindex
        logger.info(f"Loaded {len(self.gdf)} project boundaries")

    @classmethod
    def from_file(cls, path: str) -> "ProjectBoundaries":
        """Load project boundaries from file.
        
        Supports GeoJSON, Shapefile, GeoPackage, or Parquet formats.
        
        Args:
            path: Path to geometry file
            
        Returns:
            ProjectBoundaries instance
            
        Raises:
            FileNotFoundError: If file doesn't exist
            ValueError: If file can't be read or is empty
        """
        import warnings
        
        path_obj = Path(path)
        
        if not path_obj.exists():
            raise FileNotFoundError(f"Project boundaries file not found: {path}")
        
        logger.info(f"Loading project boundaries from {path}")
        
        try:
            # Suppress GDAL warnings when reading GeoParquet via PyArrow
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", category=RuntimeWarning)
                
                if path_obj.suffix.lower() == ".parquet":
                    gdf = gpd.read_parquet(path)
                else:
                    gdf = gpd.read_file(path)
        except Exception as e:
            raise ValueError(f"Failed to read project boundaries: {e}")
        
        return cls(gdf)

    def get_coverage_for_geometry(self, geometry: BaseGeometry) -> BaseGeometry:
        """Get union of all project boundaries that intersect a geometry.
        
        Args:
            geometry: Query geometry (typically tile or AOI)
            
        Returns:
            Union of intersecting project boundaries, or empty geometry if none
        """
        # Use spatial index for efficiency
        candidates = self.spatial_index.intersection(geometry.bounds)
        intersecting = self.gdf.iloc[list(candidates)]
        
        if intersecting.empty:
            return BaseGeometry()
        
        return intersecting.geometry.unary_union

    def coverage_fraction(self, geometry: BaseGeometry) -> float:
        """Calculate fraction of geometry covered by project boundaries.
        
        Args:
            geometry: Query geometry
            
        Returns:
            Float between 0 and 1 representing coverage fraction
        """
        if geometry.area == 0:
            return 0.0
        
        coverage = self.get_coverage_for_geometry(geometry)
        if coverage.is_empty:
            return 0.0
        
        intersection = geometry.intersection(coverage)
        return intersection.area / geometry.area

    def covers_geometry(self, geometry: BaseGeometry, min_coverage: float = 0.95) -> bool:
        """Check if project boundaries cover a geometry.
        
        Args:
            geometry: Query geometry
            min_coverage: Minimum coverage fraction (0-1) to consider as covered
            
        Returns:
            True if covered above threshold, False otherwise
        """
        return self.coverage_fraction(geometry) >= min_coverage

    def get_uncovered_regions(self, geometry: BaseGeometry) -> list[BaseGeometry]:
        """Get regions of geometry not covered by project boundaries.
        
        Args:
            geometry: Query geometry
            
        Returns:
            List of uncovered polygons (may be empty if fully covered)
        """
        coverage = self.get_coverage_for_geometry(geometry)
        if coverage.is_empty:
            return [geometry]
        
        difference = geometry.difference(coverage)
        if difference.is_empty:
            return []
        
        # Handle different geometry types
        if difference.geom_type == "GeometryCollection":
            return [g for g in difference.geoms if g.geom_type in ("Polygon", "MultiPolygon")]
        elif difference.geom_type in ("Polygon", "MultiPolygon"):
            return [difference]
        else:
            return []

    def intersects_coverage(self, geometry: BaseGeometry) -> bool:
        """Check if geometry intersects any project boundaries.
        
        Args:
            geometry: Query geometry
            
        Returns:
            True if any intersection exists
        """
        coverage = self.get_coverage_for_geometry(geometry)
        return not coverage.is_empty
