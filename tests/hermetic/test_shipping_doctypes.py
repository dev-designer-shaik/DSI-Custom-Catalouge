"""Storage, permissions, deployment boundary, and the compact page's structure."""

import json
import re
import subprocess
from pathlib import Path

import pytest

from dsi_catalogue.shipping_engine import LABELS

ROOT = Path(__file__).resolve().parents[2]
DT = ROOT / "dsi_catalogue/dsi_catalogue/doctype"
PAGE = ROOT / "dsi_catalogue/dsi_catalogue/page/shipping_policy_wizard"


def test_only_current_fields_and_options_are_stored():
	for name in ("shipping_policy", "shipping_policy_rule", "shipping_zone", "shipping_item_set"):
		doc = json.loads((DT / name / (name + ".json")).read_text())
		fields = {f["fieldname"]: f for f in doc["fields"]}
		assert not (
			{
				"price_source",
				"rate_card",
				"round_up",
				"min_order_value",
				"default_fallback_method",
				"everywhere_else",
				"enabled" if name == "shipping_policy_rule" else "no_such_field",
			}
			& fields.keys()
		)
		assert doc["field_order"] == list(fields)
	rule = json.loads((DT / "shipping_policy_rule/shipping_policy_rule.json").read_text())
	method = next(f for f in rule["fields"] if f["fieldname"] == "method")
	assert method["options"].splitlines() == list(LABELS["method"])


def test_manager_permissions_and_child_doctype():
	for name in ("shipping_policy", "shipping_zone", "shipping_item_set"):
		doc = json.loads((DT / name / (name + ".json")).read_text())
		assert {p["role"] for p in doc["permissions"]} == {"System Manager", "Website Manager"}
	child = json.loads((DT / "shipping_item_set_pattern/shipping_item_set_pattern.json").read_text())
	assert child["istable"] == 1 and child["fields"][0]["fieldname"] == "pattern"


def test_manifest_and_reloaded_child_order():
	listed = {
		l
		for l in (ROOT / "tools/shipping_policy_files.txt").read_text().splitlines()
		if l and not l.startswith("#")
	}
	actual = {str(p.relative_to(ROOT)) for p in (ROOT / "dsi_catalogue").glob("shipping_*.py")}
	actual |= {
		str(p.relative_to(ROOT))
		for p in DT.glob("shipping_*/*")
		if p.is_file() and p.suffix in (".py", ".json", ".js")
	}
	actual |= {str(p.relative_to(ROOT)) for p in PAGE.iterdir() if p.is_file()}
	actual.add("tests/hermetic/fixtures/shipping_policy_example.json")
	assert listed == actual
	script = (ROOT / "tools/erp2_push.sh").read_text()
	assert script.index("\tshipping_item_set_pattern\n") < script.index("\tshipping_item_set\n")
	dry = subprocess.run(
		["bash", "tools/erp2_push.sh", "--dry-run"], cwd=ROOT, capture_output=True, text=True
	)
	assert dry.returncode == 0, dry.stderr
	assert "erp2-sandbox" in dry.stdout


def test_no_tabs_no_unsafe_dom_and_bounded_css():
	js = "\n".join(p.read_text() for p in PAGE.glob("*.js"))
	assert not re.search(r"\.innerHTML\s*=|\.outerHTML\s*=|insertAdjacentHTML", js)
	assert 'role:"tab"' not in js and "step_rates" not in js
	assert (PAGE / "shipping_policy_wizard.css").stat().st_size < 15000
	assert "Formula" in js and "carrier_price" in js
