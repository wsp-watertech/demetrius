"""Utilities for accessing bundled projection files."""

from pathlib import Path
from typing import Optional


def get_projection_file(filename: str) -> Optional[Path]:
    """Get path to a bundled projection file.

    Parameters
    ----------
    filename : str
        Name of the projection file, such as ``"ffrd.prj"``.

    Returns
    -------
    Path | None
        Path to the projection file if it exists, otherwise ``None``.
    """
    import demetrius

    pkg_root = Path(demetrius.__file__).parent
    proj_path = pkg_root / "data" / "projections" / filename

    return proj_path if proj_path.exists() else None


def load_projection_content(filename: str) -> Optional[str]:
    """Load content of a bundled projection file.

    Parameters
    ----------
    filename : str
        Name of the projection file, such as ``"ffrd.prj"``.

    Returns
    -------
    str | None
        Content of the projection file if it exists, otherwise ``None``.
    """
    proj_path = get_projection_file(filename)
    if proj_path:
        return proj_path.read_text()
    return None
