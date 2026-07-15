"""Dataset and tile ID extraction from TNM responses."""

import re
from typing import Optional


def parse_dataset_and_tile_ids(title: str, url: Optional[str] = None) -> tuple[str, str]:
    """Extract dataset_id and tile_id from TNM product title.

    Expects format: "USGS 1 Meter 18 x38y448 PA_3_County_South_Central_2018_D18"
                                    ^^^^^^^  ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                                    tile_id  dataset_id

    Parameters
    ----------
    title : str
        TNM product title.
    url : str | None, optional
        Download URL used as a fallback if title parsing fails.

    Returns
    -------
    tuple[str, str]
        Extracted ``(dataset_id, tile_id)`` tuple.

    Raises
    ------
    ValueError
        If the identifiers cannot be extracted.
    """
    # Pattern: look for coordinates like x##y## (tile_id)
    # followed by alphabetic/underscore dataset identifier
    tile_match = re.search(r"(x\d+y\d+)", title)
    if not tile_match:
        raise ValueError(f"Could not extract tile_id from title: {title}")

    tile_id = tile_match.group(1)

    # Everything after tile_id until end of string is dataset_id
    # Or until we hit unwanted characters
    remaining = title[tile_match.end() :].strip()

    # Take first "word" (space/underscore separated) that looks like dataset ID
    dataset_match = re.search(r"([A-Z0-9][A-Za-z0-9_\-\.]*)", remaining)
    if not dataset_match:
        # Fallback: try extracting from URL
        if url:
            return _parse_from_url(url), tile_id
        raise ValueError(f"Could not extract dataset_id from title: {title}")

    dataset_id = dataset_match.group(1)
    return dataset_id, tile_id


def _parse_from_url(url: str) -> str:
    """Extract dataset_id from download URL as fallback.

    Notes
    -----
    - https://cloud.sdsc.edu/v1/AUTH_.../DEM/.../<dataset_id>_<tile_id>.tif
    - https://tnmaccess.nationalmap.gov/.../DEM_<dataset_id>.tif

    Parameters
    ----------
    url : str
        Download URL to parse.

    Returns
    -------
    str
        Extracted dataset identifier.

    Raises
    ------
    ValueError
        If the dataset identifier cannot be extracted from the URL.
    """
    # Extract filename
    filename = url.split("/")[-1]

    # Remove .tif/.tiff extension
    filename = re.sub(r"\.(tif+|geotiff?)$", "", filename, flags=re.IGNORECASE)

    # Remove tile_id pattern (x##y##)
    filename = re.sub(r"_x\d+y\d+$", "", filename)

    if not filename or len(filename) < 3:
        raise ValueError(f"Could not extract dataset_id from URL: {url}")

    return filename
