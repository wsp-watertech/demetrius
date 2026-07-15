"""Manifest serialization for reproducibility."""

import json
import logging
from pathlib import Path
from typing import Any, Optional

from .models import AOI, BoundingBox, Tile

logger = logging.getLogger(__name__)


class Manifest:
    """Reproducibility manifest for demetrius processing."""

    def __init__(
        self,
        aoi: AOI,
        tiles: list[Tile],
        buffer: int = 0,
        cellsize: Optional[float] = None,
    ):
        """Create manifest from AOI and tiles.

        Parameters
        ----------
        aoi : AOI
            Area of interest.
        tiles : list[Tile]
            Selected and prioritized tiles.
        buffer : int, default=0
            Buffer distance used for discovery.
        cellsize : float | None, optional
            Output cell size in target CRS units.

        Returns
        -------
        None
            Initializes the manifest instance.
        """
        self.aoi = aoi
        self.tiles = tiles
        self.buffer = buffer
        self.cellsize = cellsize

    def to_dict(self) -> dict[str, Any]:
        """Convert manifest to dictionary for serialization.

        Returns
        -------
        dict[str, Any]
            Dictionary representation of the manifest.
        """
        manifest_dict = {
            "aoi": {
                "bounds": {
                    "min_x": float(self.aoi.bounds().min_x),
                    "min_y": float(self.aoi.bounds().min_y),
                    "max_x": float(self.aoi.bounds().max_x),
                    "max_y": float(self.aoi.bounds().max_y),
                },
                "crs": self.aoi.crs,
            },
            "buffer": self.buffer,
            "tile_count": len(self.tiles),
            "tiles": [
                {
                    "dataset_id": tile.dataset_id,
                    "tile_id": tile.tile_id,
                    "priority": tile.priority,
                    "url": tile.download_url,
                    "bounds": {
                        "min_x": float(tile.bounds_wgs84.min_x),
                        "min_y": float(tile.bounds_wgs84.min_y),
                        "max_x": float(tile.bounds_wgs84.max_x),
                        "max_y": float(tile.bounds_wgs84.max_y),
                    },
                    "local_path": tile.local_path,
                }
                for tile in self.tiles
            ],
        }

        if self.cellsize is not None:
            manifest_dict["cellsize"] = self.cellsize

        return manifest_dict

    def save(self, path: Path | str) -> None:
        """Save manifest to JSON file.

        Parameters
        ----------
        path : Path | str
            Path where ``manifest.json`` should be saved.

        Returns
        -------
        None
            Writes the manifest to disk.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2, default=str)

        logger.info(f"Saved manifest to {path}")

    @classmethod
    def load(cls, path: Path | str) -> "Manifest":
        """Load manifest from JSON file.

        Parameters
        ----------
        path : Path | str
            Path to ``manifest.json``.

        Returns
        -------
        Manifest
            Loaded manifest object.
        """
        path = Path(path)

        with open(path) as f:
            data = json.load(f)

        aoi_data = data["aoi"]
        aoi_bounds = aoi_data["bounds"]

        # Reconstruct AOI from bounds (as box polygon)
        from shapely.geometry import box

        geometry = box(
            aoi_bounds["min_x"],
            aoi_bounds["min_y"],
            aoi_bounds["max_x"],
            aoi_bounds["max_y"],
        )
        aoi = AOI(
            geometry=geometry,
            crs=aoi_data.get("crs", "EPSG:4326"),
            buffer=data.get("buffer", data.get("buffer_distance", 0)),
        )

        # Reconstruct tiles
        tiles = []
        for tile_data in data.get("tiles", []):
            bounds = tile_data["bounds"]
            tile = Tile(
                id=f"{tile_data['dataset_id']}_{tile_data['tile_id']}",
                dataset_id=tile_data["dataset_id"],
                tile_id=tile_data["tile_id"],
                publication_date="2021-01-01T00:00:00",  # Dummy date for loaded manifest
                last_updated="2021-01-01T00:00:00",  # Will be overridden if needed
                download_url=tile_data["url"],
                bounds_wgs84=BoundingBox(
                    min_x=bounds["min_x"],
                    min_y=bounds["min_y"],
                    max_x=bounds["max_x"],
                    max_y=bounds["max_y"],
                ),
                priority=tile_data.get("priority", -1),
                local_path=tile_data.get("local_path"),
            )
            tiles.append(tile)

        cellsize = data.get("cellsize")
        return cls(
            aoi=aoi,
            tiles=tiles,
            buffer=data.get("buffer", data.get("buffer_distance", 0)),
            cellsize=cellsize,
        )
