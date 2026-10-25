"""Compare the retired Shopify engine with ordinary policy rows. Real prices are never saved."""

import argparse
import importlib
import itertools
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dsi_catalogue.shipping_engine import resolve
from dsi_catalogue.shipping_shopify_import import policy_from_export


def main():
	parser = argparse.ArgumentParser()
	parser.add_argument(
		"config", nargs="?", default=str(ROOT / "tests/hermetic/fixtures/shopify_shipping_config_sample.csv")
	)
	parser.add_argument("--not-delivered")
	args = parser.parse_args()
	config = Path(args.config).read_text(encoding="utf-8-sig")
	closed = Path(args.not_delivered).read_text(encoding="utf-8-sig") if args.not_delivered else None
	with tempfile.TemporaryDirectory() as tmp:
		pkg = Path(tmp) / "prior_shipping"
		pkg.mkdir()
		(pkg / "__init__.py").touch()
		for mod in ("shipping_rate_card", "shipping_formula", "shipping_engine"):
			old = subprocess.check_output(
				["git", "show", f"d2364e8:dsi_catalogue/{mod}.py"], cwd=ROOT, text=True
			)
			(pkg / (mod + ".py")).write_text(old.replace("dsi_catalogue.", "prior_shipping."))
		sys.path.insert(0, tmp)
		old = importlib.import_module("prior_shipping.shipping_rate_card")
		parsed = old.parse_shopify_export(config, closed)
		policy = policy_from_export(config, closed)
		policy["active"] = True
		countries = sorted(
			{c for p in parsed["card"]["profiles"] for z in p["zones"] for c in z["countries"]}
			| {"BH", "SA", "AE", "KW", "OM", "QA", "ZZ", "AQ", "US", "GB"}
		)
		cases = 0
		mismatches = []
		codes = ["F-CR-OSA-M", "F-CR-OSA-M-PT", "F-CR-OSA-M-RF", "F-CR-PC-1", "W-A", "P-A"]
		for country, code, qty, weight in itertools.product(
			countries, codes, range(1, 5), [0.1, 0.35, 0.9, 1, 2.5]
		):
			items = [
				{"item_code": "sample", "index_key": code, "qty": qty, "weight_kg": weight, "groups": []}
			]
			order = {"country_code": country, "items": items, "directory": None}
			before = old.price_card(parsed["card"], order)
			after = resolve(policy, order)
			if country in parsed["not_delivered"]:
				before = {"priced": False}
			old_amount = before.get("total")
			new_amount = after["offers"][0]["amount"] if after["offers"] else None
			if bool(before.get("priced")) != after["offered"] or (
				after["offered"] and old_amount != new_amount
			):
				mismatches.append(
					(
						country,
						code,
						qty,
						weight,
						old_amount,
						new_amount,
						before.get("reason"),
						after["reason"],
					)
				)
			cases += 1
		print(json.dumps({"cases": cases, "mismatches": len(mismatches)}))
		for mismatch in mismatches[:8]:
			print(mismatch)
		return bool(mismatches)


if __name__ == "__main__":
	sys.exit(main())
