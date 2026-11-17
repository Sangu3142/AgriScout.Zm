import os
import sys

import pytest

# robot/ and desktop/ use flat imports (import config, from api_client import ...),
# like when they run as scripts. Make them work here too.
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROBOT_DIR = os.path.join(REPO_DIR, "robot")
DESKTOP_DIR = os.path.join(REPO_DIR, "desktop")
sys.path.insert(0, ROBOT_DIR)
sys.path.insert(0, DESKTOP_DIR)
# scout/ is a package (from scout import db). Appended, so the flat "config" is still robot's.
sys.path.append(REPO_DIR)


@pytest.fixture
def scout_db(tmp_path):
    """A fresh scout database in a temp folder, loaded from the real seed files."""
    from scout import db
    path = str(tmp_path / "agriscout.db")
    db.setup(path)
    return path
