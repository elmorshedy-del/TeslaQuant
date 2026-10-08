import os
from pathlib import Path


def data_root() -> Path:
    return Path(os.environ.get("TESLA_DATA_DIR", "var")).resolve()
