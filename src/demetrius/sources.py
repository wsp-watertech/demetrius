"""Abstract interface for tile source implementations."""

from abc import ABC, abstractmethod

from .models import Tile, BoundingBox


class TileSource(ABC):
    """Abstract base class for tile discovery sources.

    Implementations must provide a way to search for tiles within a bounding box
    and return a canonical list of Tile objects. This design enables future
    extensibility for sources like S1M and STAC without changing downstream logic.
    """

    @abstractmethod
    def search(self, aoi_bbox: BoundingBox) -> list[Tile]:
        """Search for tiles intersecting the given bounding box.

        Parameters
        ----------
        aoi_bbox : BoundingBox
            Bounding box to search in EPSG:4326.

        Returns
        -------
        list[Tile]
            Tile objects found in the bounding box.

        Raises
        ------
        ValueError
            If the search fails or returns invalid data.
        """
        pass
