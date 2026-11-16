"""Keep deployable application data under a project-local mount point."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def data_directory():
    directory = (ROOT / (os.environ.get("LABAGENT_DATA_DIR") or "runtime")).resolve()
    if not directory.is_relative_to((ROOT / "runtime").resolve()):
        raise ValueError("LABAGENT_DATA_DIR须位于项目runtime目录内")
    return directory
