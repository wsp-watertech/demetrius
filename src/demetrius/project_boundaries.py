"""Project boundary coverage from FESM or similar sources."""

import logging
from pathlib import Path

import geopandas as gpd
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

        Parameters
        ----------
        gdf : gpd.GeoDataFrame
            GeoDataFrame with project boundaries. It must contain a geometry
            column.

        Returns
        -------
        None
            Initializes the project boundaries instance.

        Raises
        ------
        ValueError
            If the GeoDataFrame is empty or has no geometry column.
        """
        if gdf.empty:
            raise ValueError("Project boundaries GeoDataFrame is empty")
        if "geometry" not in gdf.columns:
            raise ValueError("GeoDataFrame must have 'geometry' column")

        # Clean invalid geometries BEFORE CRS conversion (buffer(0) fixes self-intersecting polygons)
        invalid_count = (~gdf.geometry.is_valid).sum()
        if invalid_count > 0:
            logger.info(f"Cleaning {invalid_count} invalid geometries")
            gdf["geometry"] = gdf.geometry.apply(
                lambda geom: geom.buffer(0) if not geom.is_valid else geom
            )
            # Re-check after buffer - if still invalid, try convex hull
            still_invalid = ~gdf.geometry.is_valid
            if still_invalid.any():
                logger.info(
                    f"Applying convex_hull to {still_invalid.sum()} geometries still invalid after buffer"
                )
                gdf.loc[still_invalid, "geometry"] = gdf.loc[still_invalid, "geometry"].apply(
                    lambda geom: geom.convex_hull
                )

        # Ensure WGS84
        if gdf.crs and gdf.crs.to_epsg() != 4326:
            logger.info(f"Converting project boundaries from {gdf.crs} to EPSG:4326")
            try:
                gdf_converted = gdf.to_crs("EPSG:4326")
                # Validate that conversion didn't produce invalid geometries (e.g., infinity coordinates)
                invalid_after_conversion = (~gdf_converted.geometry.is_valid).sum()
                bad_bounds = gdf_converted.geometry.apply(
                    lambda g: (
                        any(b == float("inf") or b == float("-inf") for b in g.bounds)
                        if g.bounds
                        else False
                    )
                ).sum()
                if invalid_after_conversion > 0 or bad_bounds > 0:
                    logger.warning(
                        f"CRS conversion to EPSG:4326 produced {invalid_after_conversion} invalid geometries and {bad_bounds} with inf bounds. Keeping geometries in original CRS ({gdf.crs})."
                    )
                else:
                    gdf = gdf_converted
            except Exception as e:
                logger.warning(
                    f"CRS conversion failed: {e}. Keeping geometries in original CRS ({gdf.crs})."
                )

        self.gdf = gdf.reset_index(drop=True)
        self.spatial_index = self.gdf.sindex
        logger.info(f"Loaded {len(self.gdf)} project boundaries")

    @classmethod
    def from_file(cls, path: str) -> "ProjectBoundaries":
        """Load project boundaries from file.

        Supports GeoJSON, Shapefile, GeoPackage, or Parquet formats.

        Parameters
        ----------
        path : str
            Path to the geometry file.

        Returns
        -------
        ProjectBoundaries
            Loaded project boundaries instance.

        Raises
        ------
        FileNotFoundError
            If the file does not exist.
        ValueError
            If the file cannot be read or is empty.
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

        Parameters
        ----------
        geometry : BaseGeometry
            Query geometry, typically a tile or AOI.

        Returns
        -------
        BaseGeometry
            Union of intersecting project boundaries, or an empty geometry
            collection if none intersect.
        """
        # Use spatial index for efficiency
        candidates = self.spatial_index.intersection(geometry.bounds)
        intersecting = self.gdf.iloc[list(candidates)]

        if intersecting.empty:
            from shapely.geometry import GeometryCollection

            return GeometryCollection()

        return intersecting.geometry.unary_union

    def coverage_fraction(self, geometry: BaseGeometry) -> float:
        """Calculate fraction of geometry covered by project boundaries.

        Parameters
        ----------
        geometry : BaseGeometry
            Query geometry.

        Returns
        -------
        float
            Coverage fraction between 0 and 1.
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

        Parameters
        ----------
        geometry : BaseGeometry
            Query geometry.
        min_coverage : float, default=0.95
            Minimum coverage fraction required to consider the geometry
            covered.

        Returns
        -------
        bool
            ``True`` if the geometry is covered above the threshold, otherwise
            ``False``.
        """
        return self.coverage_fraction(geometry) >= min_coverage

    def get_uncovered_regions(self, geometry: BaseGeometry) -> list[BaseGeometry]:
        """Get regions of geometry not covered by project boundaries.

        Parameters
        ----------
        geometry : BaseGeometry
            Query geometry.

        Returns
        -------
        list[BaseGeometry]
            Uncovered polygons, or an empty list if fully covered.
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

        Parameters
        ----------
        geometry : BaseGeometry
            Query geometry.

        Returns
        -------
        bool
            ``True`` if any intersection exists.
        """
        coverage = self.get_coverage_for_geometry(geometry)
        return not coverage.is_empty
