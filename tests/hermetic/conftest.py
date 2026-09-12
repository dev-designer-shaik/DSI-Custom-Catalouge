"""Hermetic bootstrap: repo root AND the app dir (the package is nested) on
sys.path; pure modules import no frappe."""

import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / "dsi_catalogue"))
