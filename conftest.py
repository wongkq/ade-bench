"""Repo-level pytest configuration.

Adds the repo root and the scripts/ directory to sys.path so that tests
can `import ade_bench` and `from scripts import ...` without requiring
the package to be installed in editable mode.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

# Ensure repo root is first so `ade_bench` resolves to the source tree.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# scripts/ is force-included by hatch but not auto-added to sys.path; tests
# under tests/trace/ want `from scripts import mock_llm_server`.
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))