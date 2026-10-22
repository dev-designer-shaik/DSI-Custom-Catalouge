"""Frappe-native tests for the Shipping Policy: the loader and the policy's own save-time checks.

This module also holds the helpers that every native test of the feature shares:

	load_example()    the example policy of the hermetic fixture, as the loader returns it once it is seeded
	seed_policy()     creates a policy through normal documents (zones, item sets, then the policy)
	wipe_policy()     removes everything the tests made and puts the single back to never saved
	country_name()    the Country name behind an ISO code on this site

`ShippingPolicyTestCase` runs `wipe_policy` after every test, pass or fail. It refuses to run on a site that
already holds a shipping policy, because the helpers reset it: run these tests on erp2 (the sandbox), never
on a site with a real policy. The hermetic fixture reaches erp2 with tools/erp2_push.sh.

	bench --site erp.shaik.net run-tests --module dsi_catalogue.dsi_catalogue.doctype.shipping_policy.test_shipping_policy
"""

import json

import frappe
from frappe.tests.classes.integration_test_case import IntegrationTestCase

from dsi_catalogue.shipping_policy import load_policy
from dsi_catalogue.shipping_policy_data import rows_from_policy, snapshot_version

# These tests make their own zones and item sets. Without this, Frappe would first create test records
# for everything those link to (and every record they link to in turn), and leave them on the site.
IGNORE_TEST_RECORD_DEPENDENCIES = ["Shipping Zone", "Shipping Item Set"]

POLICY = "Shipping Policy"
FIXTURE = ("..", "tests", "hermetic", "fixtures", "shipping_policy_example.json")

# The policy of a site that has never had one: off, nothing set, no zones, no item sets, no rules.
EMPTY_POLICY = {
	"active": False,
	"not_covered": "pass_through",
	"default_fallback": {"method": "no_delivery", "amount": 0.0},
	"zones": [],
	"item_sets": [],
	"rules": [],
}

# The Item Groups load_example() had to create because the site had none of that name.
_made_item_groups = []


# ---------------------------------------------------------------- helpers shared by the native tests
def country_name(code: str) -> str:
	"""The name of the Country with this ISO code on this site (the codes are stored in lower case)."""
	return frappe.db.get_value("Country", {"code": code.lower()}, "name")


def _country_names() -> dict:
	rows = frappe.get_all("Country", fields=["name", "code"])
	return {row["code"].upper(): row["name"] for row in rows if row["code"]}


def _stored_item_group(name: str) -> str:
	"""The Item Group `name` as this site stores it (erp2 has WATCHES where the fixture says Watches). If the
	site has no such group, one is made and remembered, so that `wipe_policy` removes it."""
	if stored := frappe.db.exists("Item Group", name):
		return stored
	group = {"doctype": "Item Group", "item_group_name": name, "parent_item_group": "All Item Groups"}
	frappe.get_doc(group).insert()
	_made_item_groups.append(name)
	return name


def load_example() -> dict:
	"""The example policy of tests/hermetic/fixtures/shipping_policy_example.json, ready to seed.

	It is the policy `load_policy()` returns once `seed_policy` has saved it (apart from the rule names, which
	are the database's): item groups and items carry the name this site stores, and the zones and item sets
	are sorted by name, the order the loader gives them in. A group the site lacks is created and remembered
	for cleanup; an item the site lacks is left as it is, and seeding it fails loudly.
	"""
	with open(frappe.get_app_path("dsi_catalogue", *FIXTURE), encoding="utf-8") as fixture:
		policy = json.load(fixture)
	for item_set in policy["item_sets"]:
		item_set["item_groups"] = [_stored_item_group(group) for group in item_set["item_groups"]]
		item_set["items"] = [frappe.db.exists("Item", code) or code for code in item_set["items"]]
	policy["zones"].sort(key=lambda zone: zone["name"])
	policy["item_sets"].sort(key=lambda item_set: item_set["name"])
	return policy


def seed_policy(policy: dict, active: bool = False) -> None:
	"""Save `policy` as documents: its zones, then its item sets, then the Shipping Policy with its rules.

	`active` decides whether the policy is on; the policy's own `active` is not used. The documents go through
	their normal save, so every check runs. A rule's `name` is dropped: Frappe takes a child row that carries
	a name for an existing row, and would update a row that is not there instead of inserting one.
	"""
	rows = rows_from_policy(policy, _country_names())
	for zone in rows["zones"]:
		frappe.get_doc({"doctype": "Shipping Zone", **zone}).insert()
	for item_set in rows["item_sets"]:
		frappe.get_doc({"doctype": "Shipping Item Set", **item_set}).insert()
	row = rows["policy"]
	rules = [{key: value for key, value in rule.items() if key != "name"} for rule in row["rules"]]
	frappe.get_doc(POLICY).update({**row, "rules": rules, "active": 1 if active else 0}).save()


def wipe_policy() -> None:
	"""Remove every zone and item set, the item groups `load_example` made, and everything the single holds.

	The single goes back to never saved: no stored values, so inactive, with the default settings and no rules.
	"""
	frappe.db.delete("Shipping Policy Rule", {"parent": POLICY, "parenttype": POLICY})
	frappe.db.delete("Singles", {"doctype": POLICY})
	frappe.clear_document_cache(POLICY, POLICY)
	for doctype in ("Shipping Zone", "Shipping Item Set"):
		for name in frappe.get_all(doctype, pluck="name"):
			frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
	while _made_item_groups:
		frappe.delete_doc("Item Group", _made_item_groups.pop(), force=True, ignore_permissions=True)


def refuse_a_site_with_a_policy() -> None:
	"""Stop a test that would wipe a real policy: the helpers reset the zones, item sets and the single."""
	policy = frappe.get_doc(POLICY)
	held = {
		"zones": frappe.db.count("Shipping Zone"),
		"item sets": frappe.db.count("Shipping Item Set"),
		"rules": len(policy.rules),
		"an active policy": 1 if policy.active else 0,
	}
	if any(held.values()):
		listed = ", ".join(f"{count} {what}" for what, count in held.items() if count)
		raise RuntimeError(
			f"Refusing to run: this site already holds a shipping policy ({listed}) and these tests reset it. "
			"Run them on the erp2 sandbox, or remove the policy first."
		)


class ShippingPolicyTestCase(IntegrationTestCase):
	"""The base of the shipping native tests: a clean site before each test, and a clean site after it."""

	def setUp(self):
		super().setUp()
		refuse_a_site_with_a_policy()
		self.addCleanup(wipe_policy)


# ---------------------------------------------------------------- the tests
class TestShippingPolicy(ShippingPolicyTestCase):
	def messages_of(self, error):
		"""The sentences of a refusal that `ShippingPolicy.validate` raised as a list."""
		self.assertIsInstance(error.args[0], list)
		return error.args[0]

	def test_a_site_that_never_had_a_policy_loads_an_empty_inactive_one(self):
		self.assertEqual(load_policy(), EMPTY_POLICY)

	def test_loader_returns_the_example_policy_after_saving_it(self):
		example = load_example()
		seed_policy(example, active=True)
		loaded = load_policy()
		names = [rule["name"] for rule in loaded["rules"]]
		self.assertTrue(all(names), "a saved rule keeps the name of its row")
		self.assertEqual(len(set(names)), len(names))
		for rule in loaded["rules"]:
			rule["name"] = None  # the fixture has no row names; the database makes them
		self.assertEqual(loaded, example)

	def test_active_is_what_seed_policy_was_told_not_what_the_policy_says(self):
		seed_policy(load_example(), active=False)
		self.assertFalse(load_policy()["active"])
		frappe.get_doc(POLICY).update({"active": 1}).save()
		self.assertTrue(load_policy()["active"])

	def test_loader_reads_the_database_on_every_call(self):
		seed_policy(load_example())
		first = load_policy()
		rule_row = first["rules"][0]["name"]
		frappe.db.set_value("Shipping Zone", "GCC", "customer_line", "Changed behind the loader's back")
		frappe.db.set_value("Shipping Policy Rule", rule_row, "label", "Renamed behind the loader's back")
		second = load_policy()
		self.assertNotEqual(second, first)
		gcc = next(zone for zone in second["zones"] if zone["name"] == "GCC")
		self.assertEqual(gcc["customer_line"], "Changed behind the loader's back")
		self.assertEqual(second["rules"][0]["label"], "Renamed behind the loader's back")

	def test_overrides_replace_fields_of_the_policy_row_and_nothing_is_saved(self):
		seed_policy(load_example(), active=True)
		saved = load_policy()
		draft = load_policy(overrides={"active": 0, "not_covered": "Don't offer delivery", "rules": []})
		self.assertFalse(draft["active"])
		self.assertEqual(draft["not_covered"], "no_delivery")
		self.assertEqual(draft["rules"], [])
		self.assertEqual(draft["zones"], saved["zones"], "the zones are always the saved ones")
		self.assertEqual(load_policy(), saved)

	def test_overrides_may_be_the_unsaved_document_and_its_rules_are_not_lost(self):
		seed_policy(load_example())
		policy = frappe.get_doc(POLICY)
		policy.append("rules", {"zone": "GCC", "label": "Added, not saved", "method": "Free"})
		as_dict = load_policy(overrides=policy.as_dict())
		self.assertEqual(len(as_dict["rules"]), 5)
		self.assertEqual(load_policy(overrides=policy), as_dict, "the document itself gives the same policy")
		self.assertEqual(load_policy(overrides={"rules": list(policy.rules)})["rules"], as_dict["rules"])
		self.assertEqual(len(load_policy()["rules"]), 4, "nothing was saved")

	def test_a_save_that_changes_nothing_keeps_the_snapshot_version(self):
		seed_policy(load_example())
		before = snapshot_version(load_policy())
		frappe.get_doc(POLICY).save()
		self.assertEqual(snapshot_version(load_policy()), before)

	def test_a_new_rule_row_starts_disabled(self):
		seed_policy(load_example())
		policy = frappe.get_doc(POLICY)
		policy.append("rules", {"zone": "Bahrain", "method": "Free"})
		policy.save()
		self.assertEqual(frappe.get_doc(POLICY).rules[-1].enabled, 0)

	def test_policy_refuses_an_inverted_band_and_a_bad_formula_with_plain_messages(self):
		seed_policy(load_example())
		policy = frappe.get_doc(POLICY)
		policy.append("rules", {"zone": "GCC", "min_weight_kg": 5, "max_weight_kg": 2})
		policy.append("rules", {"zone": "GCC", "method": "Custom formula", "formula": "carrier_price ** 2"})
		with self.assertRaises(frappe.ValidationError) as caught:
			policy.save()
		messages = self.messages_of(caught.exception)
		# Both rows are new, so both are disabled: a rule is checked whether or not it is switched on.
		self.assertTrue(
			messages[0].startswith("Rule 5 has its limits on weight the wrong way round"), messages
		)
		self.assertTrue(messages[1].startswith("Rule 6 has a formula that cannot be used."), messages)
		self.assertEqual(len(messages), 2)
		self.assertEqual(len(frappe.get_doc(POLICY).rules), 4, "nothing was saved")

	def test_policy_refuses_a_custom_formula_with_no_formula(self):
		seed_policy(load_example())
		policy = frappe.get_doc(POLICY)
		policy.append("rules", {"zone": "GCC", "method": "Custom formula"})
		with self.assertRaises(frappe.ValidationError) as caught:
			policy.save()
		self.assertEqual(
			self.messages_of(caught.exception), ["Rule 5 charges by formula but has no formula."]
		)

	def test_messages_escape_what_people_typed_and_what_the_checker_says(self):
		seed_policy(load_example())
		# The example already has an enabled Everywhere else zone. The form refuses a second one; a draft
		# that the wizard has validated is let through, and the policy's own check then names both.
		zone = frappe.get_doc({"doctype": "Shipping Zone", "zone_name": "Gulf & Co", "everywhere_else": 1})
		zone.flags.policy_draft_validated = True
		zone.insert()
		policy = frappe.get_doc(POLICY)
		policy.append("rules", {"zone": "GCC", "method": "Custom formula", "formula": "carrier_price ** 2"})
		with self.assertRaises(frappe.ValidationError) as caught:
			policy.save()
		text = " ".join(self.messages_of(caught.exception))
		self.assertIn("&quot;Gulf &amp; Co&quot;", text)
		self.assertIn("(&lt; &lt;= &gt; &gt;=", text, "the checker's own sentence is escaped too")
		self.assertNotIn("<", text)

	def test_only_system_manager_can_use_the_doctypes_and_no_custom_permission_was_added(self):
		for doctype in ("Shipping Zone", "Shipping Item Set", POLICY):
			rows = frappe.get_all("DocPerm", filters={"parent": doctype}, fields=["role", "read", "write"])
			self.assertEqual(
				[(row["role"], row["read"], row["write"]) for row in rows], [("System Manager", 1, 1)]
			)
			self.assertEqual(frappe.db.count("Custom DocPerm", {"parent": doctype}), 0, doctype)

	def test_wipe_policy_puts_the_site_back_as_it_was(self):
		seed_policy(load_example(), active=True)
		self.assertNotEqual(load_policy(), EMPTY_POLICY)
		wipe_policy()
		self.assertEqual(load_policy(), EMPTY_POLICY)
		self.assertEqual(frappe.db.count("Shipping Zone"), 0)
		self.assertEqual(frappe.db.count("Shipping Item Set"), 0)
		self.assertEqual(frappe.db.count("Shipping Policy Rule"), 0)

	def test_a_site_that_already_holds_a_policy_is_refused(self):
		seed_policy(load_example())
		with self.assertRaises(RuntimeError) as caught:
			refuse_a_site_with_a_policy()
		self.assertIn("already holds a shipping policy", str(caught.exception))
