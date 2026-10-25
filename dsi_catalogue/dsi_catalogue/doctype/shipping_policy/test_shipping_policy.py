"""Native sandbox checks. Every test rolls back its own writes."""

import copy
import os
import unittest
from unittest.mock import patch

import frappe

from dsi_catalogue import shipping_policy as api
from dsi_catalogue import shipping_policy_wizard as wizard
from dsi_catalogue.shipping_policy_data import snapshot_version


def empty():
	return {
		"active": False,
		"fallback": {"method": "no_delivery", "amount": 0},
		"zones": [],
		"item_sets": [],
		"rules": [],
	}


def sample():
	p = empty()
	p["active"] = True
	p["zones"] = [
		{
			"name": "One-page test USA",
			"countries": ["US"],
			"enabled": True,
			"not_shipped": False,
			"customer_line": "",
			"not_offered_message": "",
		}
	]
	p["item_sets"] = [
		{"name": "One-page test products", "items": [], "item_groups": [], "patterns": ["TEST-*"]}
	]
	p["rules"] = [
		{
			"zone": "One-page test USA",
			"item_set": "One-page test products",
			"method": "table",
			"services": [],
			"price_table": [{"up_to_lb": None, "price": 7.25}],
			"formula": "",
			"label": "Delivery",
			"days": 4,
		}
	]
	return p


class ShippingPolicyTestCase(unittest.TestCase):
	def setUp(self):
		if os.environ.get("SHIPPING_POLICY_SANDBOX_TESTS") != "1":
			self.skipTest("Explicit sandbox test flag required")
		self.user = frappe.session.user
		frappe.set_user("Administrator")
		frappe.db.savepoint("onepage_test")

	def tearDown(self):
		frappe.db.rollback(save_point="onepage_test")
		frappe.set_user(self.user)

	def apply(self, p):
		return wizard.apply_policy(p, snapshot_version(api.load_policy()))


class TestShippingPolicy(ShippingPolicyTestCase):
	def test_save_and_read_preserves_all_options(self):
		p = sample()
		self.apply(p)
		actual = api.load_policy()
		self.assertEqual(actual["rules"][0]["price_table"], p["rules"][0]["price_table"])
		self.assertEqual(actual["item_sets"][0]["patterns"], ["TEST-*"])

	def test_stale_save_refused(self):
		self.apply(sample())
		with self.assertRaises(frappe.TimestampMismatchError):
			wizard.apply_policy(sample(), "stale")

	def test_invalid_save_writes_nothing(self):
		before = api.load_policy()
		p = sample()
		p["rules"][0]["price_table"][0]["price"] = -1
		with self.assertRaises(frappe.ValidationError):
			self.apply(p)
		self.assertEqual(api.load_policy(), before)

	def test_failure_after_writes_rolls_back(self):
		before = api.load_policy()
		original = wizard._write

		def fail(*args):
			original(*args)
			raise RuntimeError("injected after write")

		with patch.object(wizard, "_write", fail), self.assertRaises(RuntimeError):
			self.apply(sample())
		self.assertEqual(api.load_policy(), before)

	def test_guest_cannot_manage(self):
		frappe.set_user("Guest")
		for fn, args in [
			(wizard.wizard_state, ()),
			(wizard.apply_policy, (sample(), "x")),
			(wizard.search_products, ()),
			(wizard.preview_changes, (sample(),)),
		]:
			with self.assertRaises(frappe.PermissionError):
				fn(*args)

	def test_formula_save_and_public_service_offers(self):
		p = sample()
		p["rules"][0].update(method="formula", formula="carrier_price * 1.2 + 2")
		self.apply(p)
		order = {
			"country_code": "US",
			"items": [
				{"item_code": "TEST-1", "index_key": "TEST-1", "qty": 1, "groups": [], "weight_kg": 0.35}
			],
			"order_value": 20,
			"directory": [
				{
					"name": "A",
					"label": "Example A",
					"enabled": True,
					"countries": ["US"],
					"provider": "Provider",
					"carrier_code": "TEST",
					"max_weight_kg": 0,
					"scope": "International",
					"estimated_days": 4,
				}
			],
		}
		with patch.object(api, "build_order", return_value=copy.deepcopy(order)):
			first = api.price_delivery("US", [{"item_code": "TEST-1", "qty": 1}])
			self.assertTrue(first["needs_quotes"])
			quoted = {
				"0": {
					"boxes": [{"weight_kg": 1, "volume_l": 1}],
					"quotes": [{"service_id": "A", "amount": 10, "boxes_quoted": 1}],
				}
			}
			second = api.price_delivery(
				"US", [{"item_code": "TEST-1", "qty": 1}], quoted, first["plan_token"]
			)
			self.assertEqual(second["offers"][0]["amount"], 14)
			self.assertEqual(second["offers"][0]["carrier_code"], "TEST")
			self.assertNotIn("trace", second["offers"][0])
			self.assertNotIn("formula", second["offers"][0])

	def test_stale_quote_plan_rejected(self):
		p = sample()
		p["rules"][0]["method"] = "carrier"
		self.apply(p)
		order = {
			"country_code": "US",
			"items": [
				{"item_code": "TEST-1", "index_key": "TEST-1", "qty": 1, "groups": [], "weight_kg": 0.35}
			],
			"directory": None,
		}
		with patch.object(api, "build_order", return_value=order):
			result = api.price_delivery(
				"US", [{"item_code": "TEST-1", "qty": 1}], {"0": {"boxes": [], "quotes": []}}, "stale"
			)
			self.assertEqual(result["reason"], "stale_plan")

	def test_product_search_filters_and_caps(self):
		result = wizard.search_products(limit=1000)
		self.assertLessEqual(len(result["rows"]), 100)
		if result["rows"]:
			first = result["rows"][0]
			found = wizard.search_products(text=first["item_code"])
			self.assertIn(first["item_code"], [r["item_code"] for r in found["rows"]])

	def test_directory_lists_dynamic_provider_data(self):
		state = wizard.wizard_state()
		if state["services"]:
			self.assertTrue(
				all(s["provider"] in {p["name"] for p in state["providers"]} for s in state["services"])
			)

	def test_group_order_persists_without_rows(self):
		p = sample()
		p["item_sets"].insert(
			0, {"name": "ZZ first empty card", "items": [], "item_groups": [], "patterns": ["X-*"]}
		)
		self.apply(p)
		self.assertEqual(api.load_policy()["item_sets"][0]["name"], "ZZ first empty card")
		p["item_sets"].reverse()
		self.apply(p)
		self.assertEqual(api.load_policy()["item_sets"][0]["name"], "One-page test products")

	def test_guest_service_directory_has_only_booking_identity(self):
		frappe.set_user("Guest")
		for service in api.delivery_services():
			self.assertEqual(set(service), {"name", "provider", "carrier_code"})
			self.assertTrue(service["name"])
