"""Utilities for accessing bundled projection files."""

from pathlib import Path
from typing import Optional


def get_projection_file(filename: str) -> Optional[Path]:
    """Get path to a bundled projection file.

    Args:
        filename: Name of projection file (e.g., "ffrd.prj")

    Returns:
        Path to projection file if it exists, None otherwise
    """
    import demetrius
    
    pkg_root = Path(demetrius.__file__).parent
    proj_path = pkg_root / "data" / "projections" / filename
    
    return proj_path if proj_path.exists() else None


def load_projection_content(filename: str) -> Optional[str]:
    """Load content of a bundled projection file.

    Args:
        filename: Name of projection file (e.g., "ffrd.prj")

    Returns:
        Content of projection file if it exists, None otherwise
    """
    proj_path = get_projection_file(filename)
    if proj_path:
        return proj_path.read_text()
    return None
