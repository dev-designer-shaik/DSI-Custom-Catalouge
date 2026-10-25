import subprocess
from pathlib import Path

import pytest

from dsi_catalogue.shipping_engine import validate_policy
from dsi_catalogue.shipping_shopify_import import RateCardError, parse_shopify_export, policy_from_export

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "tests/hermetic/fixtures/shopify_shipping_config_sample.csv"


def test_import_produces_editable_rows_and_matching_priority():
	p = policy_from_export(SAMPLE.read_text())
	assert [g["name"] for g in p["item_sets"]][:3] == [
		"Dangerous Goods Petite",
		"Refill Service",
		"Dangerous Goods",
	]
	assert p["rules"][-1]["item_set"] is None
	assert all(r["method"] == "table" and not r["services"] for r in p["rules"])
	assert not validate_policy(p)
	p["rules"][0]["price_table"][0]["price"] = 1.23
	assert p["rules"][0]["price_table"][0]["price"] == 1.23


@pytest.mark.parametrize("bad", [None, "", "no useful columns"])
def test_bad_files_refused(bad):
	with pytest.raises(RateCardError):
		parse_shopify_export(bad)


def test_parity_battery():
	result = subprocess.run(["python3", "tools/parity_check.py"], cwd=ROOT, capture_output=True, text=True)
	assert result.returncode == 0, result.stdout + result.stderr
	assert '"mismatches": 0' in result.stdout
