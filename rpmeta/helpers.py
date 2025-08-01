import logging
import math
from pathlib import Path

import joblib

logger = logging.getLogger(__name__)


def save_joblib(obj: object, result_dir: Path, filename: str) -> Path:
    """
    Save an object to a file using joblib.

    Args:
        obj: The object to save
        result_dir: The directory to save the object
        filename: The name of the file to save the object to

    Returns:
        The path to the saved file
    """
    if not result_dir.is_dir():
        raise ValueError(f"{result_dir} is not a directory")

    if not result_dir.exists():
        logger.debug("Creating directory: %s", result_dir)
        result_dir.mkdir(parents=True, exist_ok=True)

    path = result_dir / f"{filename}.joblib"
    if path.exists():
        raise ValueError(f"File {path} already exists, won't overwrite it")

    joblib.dump(obj, path)
    logger.info("Saved %s to %s", obj.__class__.__name__, path)
    return path


def to_minutes_rounded(seconds: int) -> int:
    """
    Convert seconds to minutes, rounding up.

    Args:
        seconds: The time in seconds

    Returns:
        The time in minutes, rounded up
    """
    return math.ceil(seconds / 60)
