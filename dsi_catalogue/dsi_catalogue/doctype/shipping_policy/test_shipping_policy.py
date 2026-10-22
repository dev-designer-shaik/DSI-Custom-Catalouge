"""Frappe-native tests for the Shipping Policy: the loader, the policy's own save-time checks and the two
guest methods the website calls.

This module also holds the helpers that every native test of the feature shares:

	load_example()    the example policy of the hermetic fixture, as the loader returns it once it is seeded
	seed_policy()     creates a policy through normal documents (zones, item sets, then the policy)
	wipe_policy()     removes everything the tests made and puts the single back to never saved
	country_name()    the Country name behind an ISO code on this site
	zone_of(), rule_of(), policy_of()    a zone, a rule and a whole policy in the shape the engine reads
	find_items()      real items of the site to order: inside or outside the watches, priced or not
	as_guest()        runs a block as the Guest the website calls the guest methods as

`ShippingPolicyTestCase` runs `wipe_policy` after every test, pass or fail. It refuses to run on a site that
already holds a shipping policy, because the helpers reset it: run these tests on erp2 (the sandbox), never
on a site with a real policy. The hermetic fixture reaches erp2 with tools/erp2_push.sh.

	bench --site erp.shaik.net run-tests --module dsi_catalogue.dsi_catalogue.doctype.shipping_policy.test_shipping_policy
"""

import ast
import inspect
import json
import unittest
import warnings
from contextlib import ExitStack, contextmanager
from unittest import mock

import frappe
import orjson
from frappe.tests.classes.integration_test_case import IntegrationTestCase
from frappe.utils import add_days, add_to_date, flt, now_datetime, nowdate

from dsi_catalogue import shipping_policy
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


def zone_of(name: str, countries=(), **fields) -> dict:
	"""A zone in the shape the engine reads: enabled, listing `countries` (ISO codes), the default fallback."""
	return {
		"name": name,
		"enabled": True,
		"countries": list(countries),
		"not_shipped": False,
		"everywhere_else": False,
		"customer_line": "",
		"not_offered_message": "",
		"fallback": {"method": "policy_default", "amount": 0.0},
		**fields,
	}


def rule_of(zone: str, **fields) -> dict:
	"""A rule in the shape the engine reads: enabled, for every item, no limits, charging the carrier price."""
	limits = (
		"min_order_value",
		"max_order_value",
		"min_weight_kg",
		"max_weight_kg",
		"min_volume_l",
		"max_volume_l",
	)
	return {
		"name": None,
		"enabled": True,
		"label": "",
		"zone": zone,
		"item_set": None,
		**dict.fromkeys(limits, 0.0),
		"method": "carrier_price",
		"formula": "",
		"round_up": False,
		**fields,
	}


def policy_of(zones, rules, **fields) -> dict:
	"""A policy in the shape `seed_policy` takes: no item sets, anything not covered passes the carrier price on."""
	return {
		"active": True,
		"not_covered": "pass_through",
		"default_fallback": {"method": "no_delivery", "amount": 0.0},
		"zones": list(zones),
		"item_sets": [],
		"rules": list(rules),
		**fields,
	}


def find_items(*, watches: bool, priced: bool, count: int = 1) -> list[tuple[str, float]]:
	"""Real items of this site to order, as (code, USD - Online price): inside the watch item groups or outside
	them, priced in USD - Online (exactly one row) or not priced there at all. Skips the test when the site
	has no such items, because what they cost and where they sit is the site's data."""
	group = frappe.db.exists("Item Group", "Watches")
	if not group:
		raise unittest.SkipTest("this site has no Watches item group")
	lft, rgt = frappe.db.get_value("Item Group", group, ["lft", "rgt"])
	inside = "g.lft >= %(lft)s and g.rgt <= %(rgt)s"
	rows = frappe.db.sql(
		f"""
		select i.name, max(p.price_list_rate)
		from `tabItem` i
		join `tabItem Group` g on g.name = i.item_group
		left join `tabItem Price` p
			on p.item_code = i.name and p.price_list = 'USD - Online' and p.selling = 1
		where {"" if watches else "not "}({inside})
		group by i.name
		having count(p.name) = {1 if priced else 0}
		order by i.name
		limit %(count)s
		""",
		{"lft": lft, "rgt": rgt, "count": count},
	)
	if len(rows) < count:
		raise unittest.SkipTest(
			f"this site has fewer than {count} such items (watches={watches}, priced={priced})"
		)
	return [(name, flt(price)) for name, price in rows]


@contextmanager
def as_guest():
	"""Run a block as the Guest the website calls the guest methods as, then give the session back."""
	user = frappe.session.user
	frappe.set_user("Guest")
	try:
		yield
	finally:
		frappe.set_user(user)


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


# ---------------------------------------------------------------- the guest methods
QUOTE = {"service_id": "Aramex BH GCC PPX", "name": "Aramex BH GCC PPX", "amount": 38.20, "boxes_quoted": 1}
ONE_BOX = {"weight_kg": 1.5, "volume_l": 4.0}
# What the guest methods may hand back, and nothing more.
RESPONSE_KEYS = {"policy_active", "zone", "offered", "reason", "message", "offers"}
OFFER_KEYS = {"service_id", "name", "amount", "source", "reason"}


def _listed(value):
	return list(value) if isinstance(value, list | tuple) else value


def guest_price(country="AE", items=(), boxes=(ONE_BOX,), quotes=(QUOTE,), **more):
	"""`price_delivery` as the website calls it: a Guest, one carton and one quote unless told otherwise."""
	with as_guest():
		result = shipping_policy.price_delivery(
			country, _listed(items), _listed(boxes), _listed(quotes), **more
		)
	orjson.dumps(result)  # what Frappe sends back: it raises on text it cannot write, which would be a 500
	return result


def guest_summary(language=None):
	with as_guest():
		result = shipping_policy.public_summary(language)
	orjson.dumps(result)
	return result


def quote_of(amount, **fields):
	return {**QUOTE, "amount": amount, **fields}


def first_amount(result):
	return result["offers"][0]["amount"]


def one_of(item_code, qty=1):
	return [{"item_code": item_code, "qty": qty}]


@contextmanager
def no_lookups():
	"""Fail the test when the code reads item groups, prices or the carrier tables."""
	with ExitStack() as stack:
		for name in ("item_groups_for", "order_value_for", "country_is_served", "_carrier_countries"):
			stack.enter_context(
				mock.patch.object(shipping_policy, name, side_effect=AssertionError(f"{name} was read"))
			)
		yield


@contextmanager
def error_log_entries(title):
	"""The Error Log entries with this title that a block writes, as a function that lists them.

	The Error Log table is MyISAM: nothing rolls a row back, so a test that makes Frappe write one would leave
	it on the site for good. Each entry written inside the block is deleted when the block ends (a delete in
	that table is immediate too, so no commit is needed).
	"""
	known = set(frappe.get_all("Error Log", {"method": title}, pluck="name"))

	def new():
		rows = frappe.get_all("Error Log", {"method": title}, ["name", "error"])
		return [row for row in rows if row["name"] not in known]

	try:
		yield new
	finally:
		if names := [row["name"] for row in new()]:
			frappe.db.delete("Error Log", {"name": ["in", names]})


def make_translation(test, language, source, translated):
	"""A Translation record. Deleting it clears the translation cache too, so it is deleted when the test ends."""
	doc = frappe.get_doc(
		{"doctype": "Translation", "language": language, "source_text": source, "translated_text": translated}
	).insert()
	test.addCleanup(
		frappe.delete_doc,
		"Translation",
		doc.name,
		force=True,
		ignore_permissions=True,
		delete_permanently=True,
	)


def make_carrier(
	test, country, *, max_weight_kg=0, service_enabled=1, provider_enabled=1, scope="International"
):
	"""A Shipping Provider and one Shipping Service of it that lists `country`. Both go when the test ends."""
	provider = frappe.get_doc(
		{
			"doctype": "Shipping Provider",
			"provider_name": "ZZ Test Provider",
			"enabled": provider_enabled,
			"api_type": "REST",
			"base_url_sandbox": "https://example.invalid/",
		}
	).insert()
	test.addCleanup(
		frappe.delete_doc,
		"Shipping Provider",
		provider.name,
		force=True,
		ignore_permissions=True,
		delete_permanently=True,
	)
	service = frappe.get_doc(
		{
			"doctype": "Shipping Service",
			"service_name": "ZZ Test Service",
			"shipping_provider": provider.name,
			"scope": scope,
			"enabled": service_enabled,
			"max_weight_kg": max_weight_kg,
			"countries": [{"country_code": country}],
		}
	).insert()
	test.addCleanup(
		frappe.delete_doc,
		"Shipping Service",
		service.name,
		force=True,
		ignore_permissions=True,
		delete_permanently=True,
	)
	return provider, service


def unserved_code() -> str:
	"""An ISO code of this site's Country list that no enabled service of an enabled provider lists."""
	served = shipping_policy.served_country_codes()
	return next(code for code in sorted(_country_names()) if code not in served)


class TestGuestMethods(ShippingPolicyTestCase):
	def test_guest_can_price_and_read_the_summary_but_not_the_tables(self):
		seed_policy(load_example(), active=True)
		with as_guest():
			priced = shipping_policy.price_delivery("AE", [], [ONE_BOX], [QUOTE])
			summary = shipping_policy.public_summary()
			self.assertTrue(priced["offered"])
			self.assertEqual(
				[zone["name"] for zone in summary["zones"]],
				["Bahrain", "GCC", "Not shipped", "Rest of world"],
			)
			with warnings.catch_warnings():
				# Frappe's own permission check for a Guest still hands its internal query the `limit_page_length`
				# that Frappe deprecates from v17. The warning is Frappe's, not ours: keep it out of the output.
				warnings.filterwarnings("ignore", message=r"(?s).*limit_page_length.*")
				for doctype in ("Shipping Zone", "Shipping Item Set"):
					with self.assertRaises(frappe.PermissionError):
						frappe.get_list(doctype)
				self.assertFalse(frappe.get_doc(POLICY).has_permission("read"))
			# The two methods are open to guests, and nothing else of the module is.
			for method in (shipping_policy.price_delivery, shipping_policy.public_summary):
				frappe.is_whitelisted(method)  # raises PermissionError for a guest when it is not open
			for helper in (
				shipping_policy.build_order,
				shipping_policy.load_policy,
				shipping_policy.translate,
			):
				with self.assertRaises(frappe.PermissionError):
					frappe.is_whitelisted(helper)

	def test_policy_off_returns_the_quotes_unchanged(self):
		quotes = [
			{"service_id": "A", "name": "Whole", "amount": 12.5, "boxes_quoted": 2},
			{"service_id": "B", "name": "Short", "amount": 7, "boxes_quoted": 1},
			{"service_id": "C", "amount": 9.99},
		]
		cartons = [{"weight_kg": 1, "volume_l": 2}, {"weight_kg": 0.5, "volume_l": None}]  # a null measure
		expected = {
			"policy_active": False,
			"zone": None,
			"offered": True,
			"reason": None,
			"message": "",
			"offers": [
				{"service_id": "A", "name": "Whole", "amount": 12.5, "source": "carrier", "reason": None},
				{"service_id": "B", "name": "Short", "amount": 7.0, "source": "carrier", "reason": None},
				{"service_id": "C", "name": "C", "amount": 9.99, "source": "carrier", "reason": None},
			],
		}

		def priced():
			# Nothing about the items, the cartons or the carriers is read while the policy is off.
			with as_guest(), no_lookups():
				return shipping_policy.price_delivery("AE", "this is not even json", cartons, quotes)

		self.assertEqual(priced(), expected, "a policy that was never saved")
		seed_policy(load_example(), active=False)
		self.assertEqual(priced(), expected, "the rules are there but the policy is off")
		# A bad quote is still refused with a plain sentence.
		surrogate = '[{"service_id": "A\\ud800B", "amount": 1.5}]'  # JSON text that holds a lone surrogate
		for bad in ([{"service_id": "A", "amount": -1}], "[{", [{"amount": 5}], {"amount": 1}, surrogate):
			with self.subTest(bad=bad), self.assertRaises(frappe.ValidationError):
				guest_price(quotes=bad)

	def test_example_policy_prices_a_gcc_perfume_order_at_39(self):
		seed_policy(load_example(), active=True)
		[(perfume, _)] = find_items(watches=False, priced=True)
		result = guest_price("AE", items=one_of(perfume))  # one quote, 38.20
		self.assertEqual(
			result,
			{
				"policy_active": True,
				"zone": "GCC",
				"offered": True,
				"reason": None,
				"message": "",
				"offers": [
					{
						"service_id": "Aramex BH GCC PPX",
						"name": "Aramex BH GCC PPX",
						"amount": 39.0,
						"source": "rule",
						"reason": None,
					}
				],
			},
		)
		self.assertEqual(set(result), RESPONSE_KEYS, "no trace, no rule number, nothing else")
		self.assertEqual(set(result["offers"][0]), OFFER_KEYS)

	def test_example_policy_in_each_zone_with_real_items_and_carrier_services(self):
		example = load_example()
		seed_policy(example, active=True)
		[(perfume, _)] = find_items(watches=False, priced=True)
		[(watch, _)] = find_items(watches=True, priced=True)
		self.assertEqual(first_amount(guest_price(items=one_of(watch))), 0.0, "all watches: free in the Gulf")
		self.assertEqual(
			first_amount(guest_price(items=one_of(watch) + one_of(perfume))),
			39.0,
			"a mixed order does not get the rule meant for watches",
		)
		self.assertEqual(first_amount(guest_price("BH", items=one_of(perfume))), 0.0, "free in Bahrain")
		russia = guest_price("RU", items=one_of(perfume))
		self.assertEqual(
			(russia["offered"], russia["reason"], russia["zone"]), (False, "not_shipped", "Not shipped")
		)
		self.assertEqual(russia["message"], "We don't deliver to this country.")
		self.assertEqual(russia["offers"], [])
		zoned = {code for zone in example["zones"] for code in zone["countries"]}
		far = next(code for code in shipping_policy.served_country_codes() if code not in zoned)
		self.assertEqual(guest_price(far, quotes=[quote_of(20.0)])["zone"], "Rest of world")
		self.assertEqual(first_amount(guest_price(far, quotes=[quote_of(20.0)])), 45.0, "the minimum")
		self.assertEqual(first_amount(guest_price(far, quotes=[quote_of(100.0)])), 110.0, "10 percent on top")

	def test_two_calls_around_a_policy_change_see_the_change(self):
		seed_policy(load_example(), active=True)
		[(perfume, _)] = find_items(watches=False, priced=True)
		order = one_of(perfume)
		self.assertEqual(first_amount(guest_price(items=order)), 39.0)
		# The way an administrator changes it: the Gulf rule stops rounding up.
		policy = frappe.get_doc(POLICY)
		next(rule for rule in policy.rules if rule.label == "Gulf at the carrier price").round_up = 0
		policy.save()
		self.assertEqual(first_amount(guest_price(items=order)), 38.2)
		# Behind the code's back, straight in the database: the Gulf zone goes off, so AE has no zone of its own.
		frappe.db.set_value("Shipping Zone", "GCC", "enabled", 0)
		changed = guest_price(items=order)
		self.assertEqual((changed["zone"], first_amount(changed)), ("Rest of world", 45.0))
		frappe.db.set_single_value(POLICY, "active", 0)
		off = guest_price(items=order)
		self.assertEqual(
			(off["policy_active"], first_amount(off)), (False, 38.2), "the quote, as it was posted"
		)

	def test_the_quote_path_uses_no_cache(self):
		tree = ast.parse(inspect.getsource(shipping_policy))
		used = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
		used |= {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
		used |= {
			alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names
		}
		banned = {"get_cached_doc", "get_cached_value", "cache", "lru_cache", "site_cache", "request_cache"}
		self.assertEqual(used & banned, set())

	def test_unknown_and_unpriced_items_count_as_zero_and_a_groupless_item_matches_only_by_code(self):
		seed_policy(load_example(), active=True)
		[(priced, price)] = find_items(watches=False, priced=True)
		[(watch, _)] = find_items(watches=True, priced=True)
		[(unpriced, _)] = find_items(watches=True, priced=False)
		value = shipping_policy.order_value_for
		self.assertEqual(value([]), 0.0)
		self.assertEqual(value(one_of("NO-SUCH-ITEM", 3)), 0.0, "an unknown item")
		self.assertEqual(value(one_of(unpriced, 2)), 0.0, "an item with no price in USD - Online")
		mixed = one_of(priced, 2) + one_of(unpriced) + one_of("NO-SUCH-ITEM")
		self.assertEqual(value(mixed), flt(2 * price, 2), "only what has a price is counted")
		groups = shipping_policy.item_groups_for(["NO-SUCH-ITEM", priced, watch])
		self.assertEqual(groups["NO-SUCH-ITEM"], [])
		self.assertIn("WATCHES", groups[watch], "a group above the item's own counts")
		self.assertNotIn("WATCHES", groups[priced])
		for code in (priced, watch):
			self.assertEqual(
				groups[code][0], frappe.db.get_value("Item", code, "item_group"), "its own group first"
			)
			self.assertEqual(groups[code][-1], "All Item Groups", "the whole chain, up to the root")
		# The order still prices with items the ERP does not know or has no price for.
		odd = one_of("NO-SUCH-ITEM") + one_of(unpriced)
		self.assertEqual(first_amount(guest_price(items=odd)), 39.0)
		# An item with no group at all is in a selected set only when the set lists it by code.
		original = frappe.db.get_value("Item", unpriced, "item_group")
		self.addCleanup(frappe.db.set_value, "Item", unpriced, "item_group", original, update_modified=False)
		frappe.db.set_value("Item", unpriced, "item_group", "", update_modified=False)
		self.assertEqual(shipping_policy.item_groups_for([unpriced]), {unpriced: []})
		self.assertEqual(first_amount(guest_price(items=one_of(unpriced))), 39.0, "no group: not a watch")
		watches = frappe.get_doc("Shipping Item Set", "Watches")
		watches.append("items", {"item": unpriced})
		watches.save()
		self.assertEqual(first_amount(guest_price(items=one_of(unpriced))), 0.0, "listed by code: free")

	def test_order_with_no_weights_or_boxes_still_prices(self):
		policy = policy_of(
			[zone_of("Gulf", ["AE"])],
			[
				rule_of("Gulf", label="Heavy", min_weight_kg=0.5, method="free"),
				rule_of("Gulf", label="Big", min_volume_l=1.0, method="free"),
				rule_of("Gulf", label="Light and small", max_weight_kg=1.0, max_volume_l=5.0),
			],
		)
		seed_policy(policy, active=True)
		no_cartons = guest_price(boxes=[], quotes=[quote_of(38.2, boxes_quoted=0)])
		self.assertEqual(
			(no_cartons["offered"], first_amount(no_cartons)), (True, 38.2), "no cartons: any quote is whole"
		)
		self.assertEqual(no_cartons["offers"][0]["source"], "rule", "the limits were compared against 0")
		empty = guest_price(boxes=[{"weight_kg": 0, "volume_l": 0}], items=[])
		self.assertEqual(first_amount(empty), 38.2, "a carton that weighs nothing, and no items")
		heavy = guest_price(boxes=[{"weight_kg": 0.5, "volume_l": 0}])
		self.assertEqual(first_amount(heavy), 0.0, "0.5 kg is where the first rule starts: from includes it")

	def test_a_service_that_did_not_quote_every_carton_is_dropped_while_the_policy_is_on(self):
		seed_policy(load_example(), active=True)
		two_boxes = [ONE_BOX, {"weight_kg": 2.0, "volume_l": 3.0}]
		whole = quote_of(38.2, service_id="whole", name="Whole", boxes_quoted=2)
		short = quote_of(10.0, service_id="short", name="Short", boxes_quoted=1)
		unsaid = {"service_id": "unsaid", "name": "Unsaid", "amount": 5.0}  # no boxes_quoted: 0
		both = guest_price(boxes=two_boxes, quotes=[short, whole, unsaid])
		self.assertEqual([(o["service_id"], o["amount"]) for o in both["offers"]], [("whole", 39.0)])
		only_short = guest_price(boxes=two_boxes, quotes=[short, unsaid])
		self.assertEqual(
			(only_short["reason"], only_short["offers"][0]["service_id"], first_amount(only_short)),
			("fallback_offer", "policy-fallback", 35.0),
			"no whole quote from a country the carrier serves: the Gulf fallback",
		)
		frappe.db.set_single_value(POLICY, "active", 0)
		echoed = guest_price(boxes=two_boxes, quotes=[short, whole, unsaid])
		self.assertEqual(
			[o["service_id"] for o in echoed["offers"]], ["short", "whole", "unsaid"], "off: all kept"
		)

	def test_translated_zone_line_and_message_follow_the_language(self):
		seed_policy(load_example(), active=True)
		line = "Gulf delivery, priced by the carrier. Watches ship free."
		message = "We don't deliver to this country."
		arabic_line = "التوصيل إلى الخليج بسعر شركة الشحن. الساعات مجانًا."
		arabic_message = "لا نوصل إلى هذا البلد."
		make_translation(self, "ar", line, arabic_line)
		make_translation(self, "ar", message, arabic_message)

		def lines(language=None):
			return {zone["name"]: zone["customer_line"] for zone in guest_summary(language)["zones"]}

		self.assertEqual(lines("ar")["GCC"], arabic_line)
		self.assertEqual(
			lines("ar")["Bahrain"], "Free delivery in Bahrain.", "no translation for it: English"
		)
		self.assertEqual(lines()["GCC"], line)
		self.assertEqual(lines("en")["GCC"], line)
		self.assertEqual(lines("fr")["GCC"], line, "a language that has no translation of it: English")
		self.assertEqual(lines("AR")["GCC"], arabic_line, "matched the way the database matches names")
		for language, expected in (("ar", arabic_line), ("fr", line), ("xx", line), (None, line), (5, line)):
			self.assertEqual(shipping_policy.translate(line, language), expected, language)
		[(perfume, _)] = find_items(watches=False, priced=True)
		blocked = guest_price("RU", items=one_of(perfume), language="ar")
		self.assertEqual((blocked["reason"], blocked["message"]), ("not_shipped", arabic_message))
		self.assertEqual(guest_price("RU", items=one_of(perfume))["message"], message)
		# Whatever else a guest sends as the language means English, and is never an error. The last of these
		# is "ar" written in fullwidth letters.
		odd = [
			"xx",
			"",
			"  ",
			"ar ",
			" ar",
			"a r",
			"ar;--",
			"ar' OR '1'='1",
			"\ud800",
			"\x00",
			"\uff41\uff52",
		]
		odd += [123, 4.5, True, ["ar"], {"ar": 1}, "x" * 10_000, None]
		with mock.patch.object(frappe, "_", wraps=frappe._) as translate:
			for language in odd:
				with self.subTest(language=language):
					self.assertEqual(lines(language)["GCC"], line)
					self.assertEqual(guest_price("RU", language=language)["message"], message)
			translate.assert_not_called()  # Frappe keeps the translations of every language it is asked about
			lines("ar")
			translate.assert_called()

	def test_oversized_or_negative_input_is_refused(self):
		seed_policy(load_example(), active=True)
		item = {"item_code": "MARKER-ITEM", "qty": 1}
		refused = {
			"negative quantity": {"items": [{**item, "qty": -1}]},
			"zero quantity": {"items": [{**item, "qty": 0}]},
			"quantity too big for a float": {"items": [{**item, "qty": 10**400}]},
			"quantity that is text": {"items": [{**item, "qty": "1; DROP TABLE"}]},
			"too many items": {"items": [item] * 201},
			"an item that is not an object": {"items": ["MARKER-ITEM"]},
			"items that are not a list": {"items": {"MARKER": 1}},
			"items that are not json": {"items": "[{MARKER"},
			"negative weight": {"boxes": [{"weight_kg": -1, "volume_l": 1}]},
			"a carton with no volume": {"boxes": [{"weight_kg": 1}]},
			"too many cartons": {"boxes": [ONE_BOX] * 51},
			"negative amount": {"quotes": [quote_of(-0.01)]},
			"amount of a billion and one": {"quotes": [quote_of(1_000_000_001)]},
			"amount of 1e300": {"quotes": [quote_of(1e300)]},
			"half a carton quoted": {"quotes": [quote_of(5.0, boxes_quoted=1.5)]},
			"too many quotes": {"quotes": [QUOTE] * 21},
			"NaN as an amount": {"quotes": '[{"service_id": "A", "amount": NaN}]'},
			# Text that is not plain, which the cleaners refuse. A lone surrogate is JSON that Frappe cannot write
			# back (it would have been a bare 500); a control character has no place in an id.
			"item code with a lone surrogate": {"items": [{**item, "item_code": "A\ud800"}]},
			"service id with a lone surrogate": {"quotes": [quote_of(5.0, service_id="A\ud800B")]},
			"service name with a lone surrogate": {"quotes": [quote_of(5.0, name="N\ud83d")]},
			"a lone surrogate sent as JSON text": {"quotes": '[{"service_id": "A\\ud800B", "amount": 1.5}]'},
			"item code with a NUL": {"items": [{**item, "item_code": "A\x00B"}]},
			"service id with an escape character": {"quotes": [quote_of(5.0, service_id="A\x1b[0mB")]},
			"service name with a tab": {"quotes": [quote_of(5.0, name="N\tB")]},
		}
		for what, bad in refused.items():
			with self.subTest(what):
				with (
					mock.patch.object(frappe, "log_error") as log,
					self.assertRaises(frappe.ValidationError) as caught,
				):
					guest_price(**bad)
				message = str(caught.exception)
				self.assertTrue(message.endswith("."), f"a plain sentence: {message}")
				self.assertNotIn("MARKER", message, "what was sent is not repeated")
				self.assertNotIn("Traceback", message)
				log.assert_not_called()  # a refusal is the guest's mistake, not an error of ours

	def test_unserved_country_is_not_offered_even_with_a_fallback(self):
		served = shipping_policy.served_country_codes()
		self.assertTrue(served, "erp2 has enabled Shipping Services")
		unserved, listed = unserved_code(), served[0]
		self.assertNotIn(unserved, served)
		fixed = {"method": "fixed", "amount": 25.0}
		policy = policy_of(
			[
				zone_of("Far", [unserved], fallback=fixed, not_offered_message="Not a place we reach."),
				zone_of("Near", [listed], fallback=fixed),
			],
			[rule_of("Far"), rule_of("Near")],
		)
		seed_policy(policy, active=True)
		for quotes in ([QUOTE], []):  # with a carrier quote, and with none: no fallback offer either
			with self.subTest(quotes=quotes):
				far = guest_price(unserved, quotes=quotes)
				self.assertEqual((far["offered"], far["reason"], far["offers"]), (False, "not_served", []))
				self.assertEqual(far["message"], "Delivery is not available to this destination.")
		near = guest_price(listed)
		self.assertEqual((near["zone"], near["offered"], first_amount(near)), ("Near", True, 38.2))
		nothing_quoted = guest_price(listed, quotes=[])
		self.assertEqual(
			(
				nothing_quoted["reason"],
				first_amount(nothing_quoted),
				nothing_quoted["offers"][0]["service_id"],
			),
			("fallback_offer", 25.0, "policy-fallback"),
			"where a carrier serves the country the fallback does apply",
		)

	def test_served_means_an_enabled_service_of_an_enabled_provider_within_its_weight_limit(self):
		country = unserved_code()
		served = shipping_policy.country_is_served
		self.assertFalse(served(country, 0))
		self.assertTrue(served("AE", 0) and served("BH", 250), "erp2's own Aramex services, no weight limit")
		provider, service = make_carrier(self, country, max_weight_kg=10)
		self.assertTrue(served(country, 0), "no cartons")
		self.assertTrue(served(country, 10), "the limit is at least the heaviest carton")
		self.assertFalse(served(country, 10.001))
		self.assertTrue(served(f" {country.lower()} ", 5), "a code as people write it")
		self.assertIn(country, shipping_policy.served_country_codes())
		frappe.db.set_value("Shipping Service", service.name, "scope", "Domestic")
		self.assertTrue(served(country, 5), "the scope is not looked at, as in the carrier path")
		frappe.db.set_value("Shipping Service", service.name, "max_weight_kg", 0)
		self.assertTrue(served(country, 5000), "a limit of 0 is no limit")
		frappe.db.set_value("Shipping Service", service.name, "enabled", 0)
		self.assertFalse(served(country, 0), "a disabled service serves nobody")
		self.assertNotIn(country, shipping_policy.served_country_codes())
		frappe.db.set_value("Shipping Service", service.name, "enabled", 1)
		frappe.db.set_value("Shipping Provider", provider.name, "enabled", 0)
		self.assertFalse(served(country, 0), "a service of a disabled provider serves nobody")
		self.assertNotIn(country, shipping_policy.served_country_codes())
		frappe.db.set_value("Shipping Provider", provider.name, "enabled", 1)
		self.assertTrue(served(country, 0))

	def test_a_code_that_is_not_two_letters_is_not_served(self):
		for code in ("", " ", "A", "UAE", "A1", "ÄE", "ae\x00", None, 123, 4.5, ["AE"], {"AE": 1}, True):
			with self.subTest(code=code):
				self.assertFalse(shipping_policy.country_is_served(code, 0))

	def test_country_is_served_never_raises_on_an_odd_weight(self):
		for weight in (None, "x", "5", [], {}, float("nan"), float("inf"), -1, True):
			with self.subTest(weight=weight):
				self.assertIsInstance(shipping_policy.country_is_served("AE", weight), bool)

	def test_without_the_carrier_tables_every_country_is_served(self):
		country = unserved_code()  # before the tables go: with them gone there is no such country
		with mock.patch.object(shipping_policy, "_carrier_tables_exist", return_value=False):
			self.assertTrue(shipping_policy.country_is_served(country, 999))
			self.assertFalse(shipping_policy.country_is_served("UAE", 0), "a code that is no country is not")
			every = shipping_policy.served_country_codes()
			self.assertEqual(
				every, sorted({code.upper() for code in frappe.get_all("Country", pluck="code") if code})
			)
			self.assertEqual(len(every), frappe.db.count("Country"))

	def test_served_country_codes_are_sorted_upper_case_codes(self):
		codes = shipping_policy.served_country_codes()
		self.assertEqual(codes, sorted(set(codes)))
		self.assertTrue(all(len(code) == 2 and code.isalpha() and code.isupper() for code in codes), codes)
		self.assertLessEqual({"AE", "BH"}, set(codes))

	def test_country_codes_as_people_send_them(self):
		seed_policy(load_example(), active=True)
		for sent in (" ae ", "AE", "ae", "\tAe\n"):
			with self.subTest(sent=sent):
				result = guest_price(sent)
				self.assertEqual((result["zone"], first_amount(result)), ("GCC", 39.0))
		for sent in ("", "UAE", "A", "A1", "ÄE", "ae\x00", None, 123, 4.5, ["AE"], {"AE": 1}):
			with self.subTest(sent=sent):
				result = guest_price(sent)
				self.assertEqual(
					(result["offered"], result["reason"], result["offers"]), (False, "not_served", [])
				)

	def test_a_formula_error_is_logged_without_customer_data(self):
		policy = policy_of(
			[zone_of("Gulf", ["AE"], fallback={"method": "fixed", "amount": 35.0})],
			[rule_of("Gulf", label="Weight formula", method="formula", formula="10 / (weight_kg - 5)")],
		)
		seed_policy(policy, active=True)  # saves: at the checker's sample weight the formula gives a number
		[(perfume, _)] = find_items(watches=False, priced=True)
		crash = {"boxes": [{"weight_kg": 5.0, "volume_l": 1.0}], "items": one_of(perfume)}
		with mock.patch.object(frappe, "log_error") as log:
			result = guest_price(**crash)
		self.assertEqual(
			(result["reason"], result["offered"], first_amount(result)), ("formula_error", True, 35.0)
		)
		self.assertEqual(result["offers"][0]["service_id"], "policy-fallback")
		self.assertEqual(set(result), RESPONSE_KEYS)
		log.assert_called_once()
		logged = log.call_args.kwargs
		self.assertEqual(logged["title"], "Shipping policy formula error")
		for named in ("Rule 1", "Weight formula", "10 / (weight_kg - 5)", "Gulf", "AE"):
			self.assertIn(named, logged["message"])
		for private in (perfume, "Aramex", "38.2", "1.5"):
			self.assertNotIn(private, logged["message"], "nothing about the customer's order")
		# Unmocked, the Error Log really gets one entry, and it carries the same words.
		with error_log_entries("Shipping policy formula error") as new_entries:
			guest_price(**crash)
			[entry] = new_entries()
			for named in ("Rule 1", "Weight formula", "10 / (weight_kg - 5)", "Gulf", "AE"):
				self.assertIn(named, entry["error"])
		# A call that prices normally logs nothing.
		with mock.patch.object(frappe, "log_error") as log:
			guest_price(boxes=[{"weight_kg": 2.0, "volume_l": 1.0}], items=one_of(perfume))
		log.assert_not_called()

	def test_build_order_assembles_the_order_the_engine_reads(self):
		[(perfume, price)] = find_items(watches=False, priced=True)
		[(watch, watch_price)] = find_items(watches=True, priced=True)
		items = [
			{"item_code": perfume, "qty": 2.0},
			{"item_code": watch, "qty": 1.0},
			{"item_code": "NO-SUCH-ITEM", "qty": 1.0},
		]
		boxes = [{"weight_kg": 1.5, "volume_l": 4.0}, {"weight_kg": 2.5, "volume_l": 1.0}]
		order = shipping_policy.build_order(" ae ", items, boxes, [QUOTE])
		self.assertEqual(set(order), {"country_code", "items", "order_value", "boxes", "quotes", "served"})
		self.assertEqual(order["country_code"], " ae ", "as given: the engine normalises it")
		self.assertEqual([line["item_code"] for line in order["items"]], [perfume, watch, "NO-SUCH-ITEM"])
		self.assertEqual([line["qty"] for line in order["items"]], [2.0, 1.0, 1.0])
		self.assertIn("WATCHES", order["items"][1]["groups"])
		self.assertEqual(order["items"][2]["groups"], [])
		self.assertEqual(order["order_value"], flt(2 * price + watch_price, 2))
		self.assertEqual((order["boxes"], order["quotes"]), (boxes, [QUOTE]))
		self.assertTrue(order["served"])
		typed = shipping_policy.build_order("AE", items, boxes, [QUOTE], order_value=0.0)
		self.assertEqual(typed["order_value"], 0.0, "a typed value wins, a typed zero included")
		self.assertEqual(
			shipping_policy.build_order("AE", items, boxes, [QUOTE], order_value=125.5)["order_value"], 125.5
		)
		self.assertFalse(shipping_policy.build_order(unserved_code(), items, boxes, [QUOTE])["served"])
		self.assertTrue(shipping_policy.build_order("AE", [], [], [])["served"], "no cartons, no items")
		# The weight limit looks at the heaviest carton.
		country = unserved_code()
		make_carrier(self, country, max_weight_kg=2.0)
		self.assertFalse(
			shipping_policy.build_order(country, items, boxes, [QUOTE])["served"], "2.5 kg is over 2"
		)
		self.assertTrue(shipping_policy.build_order(country, items, boxes[:1], [QUOTE])["served"])

	def test_the_lookups_read_the_database_once_for_the_whole_order(self):
		[(perfume, _)] = find_items(watches=False, priced=True)
		codes = [perfume, "NO-SUCH-ITEM", *[f"NO-SUCH-ITEM-{n}" for n in range(200)]]
		items = [{"item_code": code, "qty": 1.0} for code in codes]
		shipping_policy.item_groups_for(codes)  # once to warm Frappe's own metadata
		shipping_policy.order_value_for(items)
		with self.assertQueryCount(1):
			shipping_policy.item_groups_for(codes)
		with self.assertQueryCount(1):
			shipping_policy.order_value_for(items)
		with self.assertQueryCount(2):
			shipping_policy.country_is_served("AE", 3.0)
		with self.assertQueryCount(2):
			shipping_policy.served_country_codes()

	def test_the_price_used_is_valid_today_then_latest_start_then_latest_change(self):
		[(item, _)] = find_items(watches=True, priced=False)  # an item with no price row at all
		today = nowdate()
		names = []

		def remove_prices():
			if names:
				frappe.db.delete("Item Price", {"name": ["in", names]})
			names.clear()

		def priced_at(*rows, **ignored):
			"""Replace the item's prices by these (rate, valid from, valid up to, minutes after now) rows.

			`ignored` rows are made to look better than any other (valid today, latest start, latest change)
			but are in another price list or are not selling prices: {"another list": {"price_list": ...}}.
			"""
			remove_prices()
			rows = [(*row, {}) for row in rows]
			rows += [(99, add_days(today, -1), None, 60, fields) for fields in ignored.values()]
			for position, (rate, valid_from, valid_upto, minutes, fields) in enumerate(rows):
				doc = frappe.get_doc(
					{
						"doctype": "Item Price",
						"item_code": item,
						"price_list": "USD - Online",
						"selling": 1,
						"currency": "USD",
						"price_list_rate": rate,
						"valid_from": valid_from,
						"valid_upto": valid_upto,
						**fields,
					}
				)
				# The first row has the name that sorts last: only the change time can put a later row first.
				doc.name = f"{9 - position}-{frappe.generate_hash(length=10)}"
				doc.owner = doc.modified_by = frappe.session.user
				doc.creation = doc.modified = add_to_date(now_datetime(), minutes=minutes)
				doc.docstatus = doc.idx = 0
				doc.db_insert()  # straight into the table: no document hooks, no Version rows
				names.append(doc.name)
			return shipping_policy.order_value_for(one_of(item, 3))

		self.addCleanup(remove_prices)
		old, soon, ended = add_days(today, -30), add_days(today, 30), add_days(today, -60)
		self.assertEqual(
			priced_at(
				(10, old, None, 0),
				(20, soon, None, 0),
				(30, add_days(today, -90), ended, 0),
				**{"another list": {"price_list": "USD - Land Retail"}, "not selling": {"selling": 0}},
			),
			30.0,
			"valid today; another list and a buying price do not count",
		)
		self.assertEqual(
			priced_at((40, add_days(today, -10), None, 0), (10, old, None, 0)), 120.0, "later start"
		)
		self.assertEqual(
			priced_at((10, old, None, 0), (50, old, add_days(today, 5), 5)), 150.0, "later change"
		)
		self.assertEqual(
			priced_at((20, soon, None, 0), (30, add_days(today, -90), ended, 0)), 60.0, "none valid today"
		)
		later = (99, soon, None, 0)  # starts later than the price under test, and is not valid yet
		self.assertEqual(priced_at((12.5, None, None, 0), later), 37.5, "no dates at all is valid")
		self.assertEqual(
			priced_at((12.5, today, today, 0), later), 37.5, "starts and ends today: valid today"
		)

	def test_public_summary_is_empty_while_off_and_shows_only_what_a_customer_may_see(self):
		self.assertEqual(guest_summary(), {"active": False, "zones": []})
		seed_policy(load_example(), active=False)
		with mock.patch.object(shipping_policy, "_country_names", side_effect=AssertionError("read")):
			self.assertEqual(guest_summary(), {"active": False, "zones": []}, "zones exist, nothing is said")
		frappe.db.set_single_value(POLICY, "active", 1)
		summary = guest_summary()
		self.assertEqual(set(summary), {"active", "zones"})
		by_name = {zone["name"]: zone for zone in summary["zones"]}
		self.assertEqual(
			by_name["GCC"],
			{
				"name": "GCC",
				"countries": [
					{"code": code, "name": country_name(code)} for code in ("AE", "SA", "KW", "OM", "QA")
				],
				"customer_line": "Gulf delivery, priced by the carrier. Watches ship free.",
				"delivers": True,
			},
		)
		self.assertEqual(by_name["Rest of world"]["countries"], [], "Everywhere else lists no countries")
		self.assertFalse(by_name["Not shipped"]["delivers"])
		for zone in summary["zones"]:
			self.assertEqual(set(zone), {"name", "countries", "customer_line", "delivers"})
		text = json.dumps(summary)
		for private in (
			"max(carrier_price",
			"35",
			"Gulf at the carrier price",
			"formula",
			"fallback",
			"rule",
		):
			self.assertNotIn(private, text)
		frappe.db.set_value("Shipping Zone", "GCC", "enabled", 0)
		self.assertNotIn(
			"GCC", [zone["name"] for zone in guest_summary()["zones"]], "a disabled zone is not shown"
		)

	def test_arguments_may_be_json_text_as_a_form_post_sends_them(self):
		seed_policy(load_example(), active=True)
		[(perfume, _)] = find_items(watches=False, priced=True)
		with as_guest():
			from_text = shipping_policy.price_delivery(
				"AE", json.dumps(one_of(perfume)), json.dumps([ONE_BOX]), json.dumps([QUOTE]), "en"
			)
			empty = shipping_policy.price_delivery("AE", "", "  ", json.dumps([QUOTE]))
		self.assertEqual(from_text, guest_price(items=one_of(perfume)))
		self.assertEqual(first_amount(empty), 39.0, "empty text is an empty list")

	def test_odd_item_codes_are_unknown_items_not_errors(self):
		seed_policy(load_example(), active=True)
		# Plain text that is odd to a database, and text the cleaners refuse (a NUL, an escape character, a lone
		# surrogate): the method never passes the second kind on, but the lookups are plain functions that
		# anybody may call with anything, so they cope with both.
		plain = ["O'Brien", "back\\slash", "100%", "x" * 140, "' OR 1=1 -- ", "日本 é"]
		odd = [*plain, "a\x00b", "\x1b[0m", "\ud800"]
		self.assertEqual(shipping_policy.item_groups_for(odd), {code: [] for code in odd})
		self.assertEqual(
			shipping_policy.order_value_for([{"item_code": code, "qty": 2.0} for code in odd]), 0.0
		)
		# Lines that are no items at all are skipped, not trusted.
		junk = [None, "text", 5, {}, {"item_code": 5, "qty": 1}, {"item_code": "A", "qty": "abc"}, {"qty": 1}]
		self.assertEqual(shipping_policy.order_value_for(junk), 0.0)
		self.assertEqual(shipping_policy.item_groups_for([None, 5, "A", ["B"]]), {"A": []})
		for code in plain:
			with self.subTest(code=code):
				self.assertEqual(first_amount(guest_price(items=one_of(code))), 39.0)
