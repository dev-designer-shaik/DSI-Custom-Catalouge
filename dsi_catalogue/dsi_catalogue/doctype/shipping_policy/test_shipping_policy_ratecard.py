"""Frappe-native tests for the rate card in the Frappe layer: the Shipping Policy's `price_source` and
`rate_card` fields, the item facts the card prices from (`item_facts_for`, `build_order(...,
item_facts=True)`), and the price source of the guest method `price_delivery`.

They share the helpers of `test_shipping_policy` (seeding and wiping the policy, the Guest session, the
Error Log guard) and, like those tests, refuse to run on a site that already holds a shipping policy,
because they reset it: run them on erp2, the sandbox.

	bench --site erp.shaik.net run-tests --module dsi_catalogue.dsi_catalogue.doctype.shipping_policy.test_shipping_policy_ratecard

The card used here is a literal: a trimmed copy of the prices in the Shopify export, as the hermetic sample
is. The sample CSV files themselves do not reach erp2 (the push script refuses them).

The items are real published Website Items of the site, chosen by their index key: a full-size fragrance, a
refill and a watch fall in three different profiles of the card. A test that relies on an item's unit weight
gives it that weight, or 0 for none, and puts the old value back when it ends: what the site holds is not
relied on. Nothing here commits.
"""

import copy
import json
import unittest
from contextlib import ExitStack, contextmanager
from unittest import mock

import frappe
from frappe.utils import escape_html

from dsi_catalogue import shipping_engine, shipping_policy
from dsi_catalogue.dsi_catalogue.doctype.shipping_policy.shipping_policy import NOT_JSON, stored_card_problem
from dsi_catalogue.dsi_catalogue.doctype.shipping_policy.test_shipping_policy import (
	CRASH_BOXES,
	EMPTY_POLICY,
	FORMULA_ERROR,
	FRAGILE,
	OFFER_KEYS,
	ONE_BOX,
	POLICY,
	QUOTE,
	RESPONSE_KEYS,
	ShippingPolicyTestCase,
	as_guest,
	error_log_entries,
	first_amount,
	guest_price,
	load_example,
	no_lookups,
	one_of,
	policy_of,
	quote_of,
	rule_of,
	seed_policy,
	unserved_code,
	zone_of,
)
from dsi_catalogue.shipping_policy_data import response_from, snapshot_version
from dsi_catalogue.shipping_rate_card import validate_card

# These tests make their own zones and item sets. Without this, Frappe would first create test records for
# everything the doctypes link to, and leave them on the site.
IGNORE_TEST_RECORD_DEPENDENCIES = ["Shipping Zone", "Shipping Item Set"]


# ---------------------------------------------------------------- the card
def card_zone(name, countries, method, days, *tiers):
	"""A zone of a card; each tier is (from, to, price in USD), or (None, None, price) for a flat price."""
	return {
		"name": name,
		"countries": list(countries),
		"method": method,
		"days": days,
		"tiers": [{"from": low, "to": high, "price": price} for low, high, price in tiers],
	}


EXPRESS = "International Express (4-7 days)"
# The four profiles of the Shopify export, in the order they take an item, with some of their real prices.
CARD = {
	"version": 1,
	"currency": "USD",
	"unit": "lb",
	"profiles": [
		{
			"name": "Dangerous Goods Petite",
			"match": ["F-*-PT", "F-CR-PC-*"],
			"zones": [
				card_zone(
					"GCC petite",
					["KW", "OM", "QA", "SA", "AE"],
					"International Express (1-3 days)",
					3,
					(None, None, 20.0),
				),
				card_zone(
					"International petite",
					["US", "AL", "AD", "AT"],
					"International Shipping (10-15 days)",
					15,
					(2.41, 4.0, 80.0),
					(4.1, 5.0, 100.0),
				),
			],
		},
		{
			"name": "Refill Service",
			"match": ["F-*-RF"],
			"zones": [
				card_zone(
					"USA refill",
					["US"],
					"International Shipping (10-15 days)",
					15,
					(0.01, 2.0, 60.0),
					(2.01, 4.0, 80.0),
				)
			],
		},
		{
			"name": "Dangerous Goods",
			"match": ["F-*"],
			"zones": [
				# The hole between 5 and 18.01 lb is the export's: a weight in it pays the next tier up, 400.
				card_zone(
					"USA",
					["US"],
					EXPRESS,
					7,
					(0.1, 0.6, 35.0),
					(0.7, 2.0, 60.0),
					(2.01, 4.0, 80.0),
					(4.01, 5.0, 100.0),
					(18.01, 20.0, 400.0),
				),
				card_zone(
					"Europe", ["GB", "AL", "AD", "AT"], EXPRESS, 7, (0.01, 2.0, 75.0), (2.01, 4.0, 95.0)
				),
				card_zone("Bahrain", ["BH"], "Free Shipping (1-2 days)", 2, (None, None, 0.0)),
				card_zone("GCC", ["KW", "SA", "AE", "OM"], EXPRESS, 7, (None, None, 35.0)),
			],
		},
		{
			"name": "Rest of the World Allowed Items",
			"match": ["*"],
			"zones": [
				card_zone(
					"Worldwide",
					["AX", "AL", "AU", "CN", "US"],
					EXPRESS,
					7,
					(0.01, 2.0, 40.0),
					(2.01, 4.0, 80.0),
					(4.01, 5.0, 100.0),
				),
				card_zone(
					"GCC allowed",
					["BH", "KW", "OM", "SA", "AE", "QA"],
					"Free Shipping (1-3 days)",
					3,
					(None, None, 0.0),
				),
			],
		},
	],
}

# A pound is 1 / 2.20462262 kg, and a profile's weight is rounded to three decimals before a tier is chosen.
# 0.25 kg is 0.551 lb, 0.5 kg is 1.102 lb, 0.3 kg is 0.661 lb; no weight on file counts as 0.35 kg, 0.772 lb.
GCC_FRAGRANCE = 35.0  # Dangerous Goods to AE: one flat price
US_FRAGRANCES = 60.0  # two fragrances to the USA, 1.102 lb: the 0.7 to 2 lb tier
US_WATCH = 40.0  # a watch of 0.3 kg to the USA (or anywhere in the Worldwide zone): its first tier


def card_policy(**fields) -> dict:
	"""The example policy (its zones and rules) with the card as its price source. `active` is seed_policy's."""
	return {**load_example(), "price_source": "rate_card", "rate_card": copy.deepcopy(CARD), **fields}


def zone_named(policy, name) -> dict:
	return next(zone for zone in policy["zones"] if zone["name"] == name)


# ---------------------------------------------------------------- real items
# (an index key LIKE pattern, and the patterns it must not match) for each kind of item
FRAGRANCE = ("{F-%", ("{F-%-PT}", "{F-%-RF}", "{F-CR-PC-%"))  # the Dangerous Goods profile
REFILL = ("{F-%-RF}", ())  # the Refill Service profile
WATCH = ("{P-%", ())  # nothing else matches it: the catch-all profile


def website_items(kind, count=1) -> list[tuple[str, str]]:
	"""(item code, index key) of published Website Items of a `kind` above, by name. Skips the test when the
	site has fewer, because what the items are and how they are keyed is the site's data."""
	like, unlike = kind
	conditions = ["custom_index_key like %(like)s"]
	conditions += [f"custom_index_key not like %(unlike{n})s" for n in range(len(unlike))]
	params = {"like": like, "count": count, **{f"unlike{n}": pattern for n, pattern in enumerate(unlike)}}
	rows = frappe.db.sql(
		"select item_code, custom_index_key from `tabWebsite Item` "
		f"where published = 1 and {' and '.join(conditions)} order by name limit %(count)s",
		params,
	)
	if len(rows) < count:
		raise unittest.SkipTest(f"this site has fewer than {count} published Website Items keyed like {like}")
	return [(code, key) for code, key in rows]


def weigh(test, item_code, kilos):
	"""Give an Item a unit weight until the test ends."""
	was = frappe.db.get_value("Item", item_code, "weight_per_unit")
	test.addCleanup(frappe.db.set_value, "Item", item_code, "weight_per_unit", was, update_modified=False)
	frappe.db.set_value("Item", item_code, "weight_per_unit", kilos, update_modified=False)


def rekey(test, item_code, field, value):
	"""Change a field of the item's Website Item until the test ends."""
	name = frappe.db.get_value("Website Item", {"item_code": item_code}, "name")
	was = frappe.db.get_value("Website Item", name, field)
	test.addCleanup(frappe.db.set_value, "Website Item", name, field, was, update_modified=False)
	frappe.db.set_value("Website Item", name, field, value, update_modified=False)


# ---------------------------------------------------------------- watching what the code reads
@contextmanager
def recorded_queries():
	"""The SQL a block runs, as a list that fills while the block runs."""
	queries = []
	real = frappe.db.__class__.sql

	def sql(db, *args, **kwargs):
		result = real(db, *args, **kwargs)
		queries.append(str(db.last_query))
		return result

	with mock.patch.object(frappe.db.__class__, "sql", sql):
		yield queries


@contextmanager
def boxes_and_quotes_unread():
	"""Fail the test when the code cleans the posted cartons or quotes."""
	with ExitStack() as stack:
		for name in ("clean_boxes", "clean_quotes"):
			stack.enter_context(
				mock.patch.object(shipping_policy, name, side_effect=AssertionError(f"{name} was read"))
			)
		yield


def carrier_tables_read(queries) -> list[str]:
	return [query for query in queries if "tabShipping Service" in query or "tabShipping Provider" in query]


def inside(value):
	"""Every key and every value inside a response, however deep."""
	if isinstance(value, dict):
		for key, item in value.items():
			yield key
			yield from inside(item)
	elif isinstance(value, list | tuple):
		for item in value:
			yield from inside(item)
	else:
		yield value


def sentences(problems) -> list[str]:
	"""How the policy's save refuses with these sentences: escaped, because people typed some of the words."""
	return [escape_html(problem) for problem in problems]


# ---------------------------------------------------------------- the tests
class TestRateCardInTheFrappeLayer(ShippingPolicyTestCase):
	def test_the_card_these_tests_use_is_a_valid_card(self):
		self.assertEqual(validate_card(CARD), [])

	# -------------------------------------------------------------- the fields, the loader and the version
	def test_a_site_that_never_had_a_policy_reads_as_the_carrier_with_no_card(self):
		loaded = shipping_policy.load_policy()
		self.assertEqual((loaded["price_source"], loaded["rate_card"]), ("carrier", None))
		self.assertEqual(loaded, EMPTY_POLICY)
		off = guest_price("AE")
		self.assertEqual(
			(
				off["policy_active"],
				off["price_source"],
				first_amount(off),
				off["offers"][0]["estimated_days"],
			),
			(False, "carrier", 38.2, None),
		)

	def test_load_policy_reads_the_price_source_and_the_card(self):
		seed_policy(card_policy(), active=True)
		loaded = shipping_policy.load_policy()
		self.assertEqual((loaded["price_source"], loaded["rate_card"]), ("rate_card", CARD))
		stored = frappe.db.get_single_value(POLICY, "rate_card")
		self.assertIsInstance(stored, str, "the card is kept as JSON text")
		self.assertEqual(json.loads(stored), CARD)
		self.assertEqual(
			frappe.db.get_single_value(POLICY, "price_source"), "Rate card", "as the Select's label"
		)
		# A draft's values are read without saving anything.
		draft = shipping_policy.load_policy(overrides={"price_source": "Carrier", "rate_card": ""})
		self.assertEqual((draft["price_source"], draft["rate_card"]), ("carrier", None))
		self.assertEqual(shipping_policy.load_policy(), loaded)

	def test_the_policy_version_follows_the_card_and_not_how_it_is_written(self):
		seed_policy(card_policy(), active=False)
		version = snapshot_version(shipping_policy.load_policy())
		frappe.get_doc(POLICY).save()
		self.assertEqual(
			snapshot_version(shipping_policy.load_policy()), version, "a save that changes nothing"
		)
		compact = frappe.get_doc(POLICY)
		compact.rate_card = json.dumps(CARD)  # the same card, without the indentation
		compact.save()
		self.assertEqual(
			snapshot_version(shipping_policy.load_policy()), version, "the same card in other words"
		)
		cheaper = copy.deepcopy(CARD)
		cheaper["profiles"][0]["zones"][0]["tiers"][0]["price"] = 19.0
		changed = frappe.get_doc(POLICY)
		changed.rate_card = json.dumps(cheaper)
		changed.save()
		with_new_price = snapshot_version(shipping_policy.load_policy())
		self.assertNotEqual(with_new_price, version, "a price of the card changed")
		frappe.get_doc(POLICY).update({"price_source": "Carrier"}).save()
		self.assertNotEqual(
			snapshot_version(shipping_policy.load_policy()), with_new_price, "so did the source"
		)

	def test_the_price_source_is_a_select_the_form_holds_to(self):
		seed_policy(
			policy_of([zone_of("Gulf", ["AE"])], [rule_of("Gulf")], price_source="carrier"), active=False
		)
		for value in ("Gold", "rate card", "Rate Card"):
			with self.subTest(value=value):
				policy = frappe.get_doc(POLICY)
				policy.price_source = value
				with self.assertRaises(frappe.ValidationError):
					policy.save()
		self.assertEqual(frappe.db.get_single_value(POLICY, "price_source"), "Carrier")

	def test_the_zone_and_the_policy_default_can_choose_the_rate_card(self):
		policy = card_policy(default_fallback={"method": "rate_card", "amount": 0.0})
		zone_named(policy, "GCC")["fallback"] = {"method": "rate_card", "amount": 0.0}
		seed_policy(policy, active=True)
		self.assertEqual(frappe.db.get_value("Shipping Zone", "GCC", "fallback_method"), "Use the rate card")
		self.assertEqual(frappe.db.get_single_value(POLICY, "default_fallback_method"), "Use the rate card")
		loaded = shipping_policy.load_policy()
		self.assertEqual(loaded["default_fallback"]["method"], "rate_card")
		self.assertEqual(zone_named(loaded, "GCC")["fallback"]["method"], "rate_card")

	# -------------------------------------------------------------- the policy refuses a bad card
	def test_policy_refuses_invalid_rate_card_json_and_card_problems_in_plain_words(self):
		seed_policy(
			policy_of([zone_of("Gulf", ["AE"])], [rule_of("Gulf")], price_source="carrier"), active=True
		)
		before = shipping_policy.load_policy()
		not_json = "The stored rate card is not valid JSON. Import the rates again in the wizard."
		not_a_card = (
			"The stored rate card is not a rate card: it has to be a JSON object. "
			"Import the rates again in the wizard."
		)

		def refused(**fields):
			policy = frappe.get_doc(POLICY)
			policy.update(fields)
			with self.assertRaises(frappe.ValidationError) as caught:
				policy.save()
			self.assertIsInstance(caught.exception.args[0], list, "a list of sentences, as the other checks")
			self.assertEqual(shipping_policy.load_policy(), before, "nothing was saved")
			return caught.exception.args[0]

		for text in ("{not json", "{", '{"version": 1,}', "<b>card</b>", "'{}'", "NaN NaN"):
			with self.subTest(text=text):
				self.assertEqual(refused(rate_card=text), [not_json])
		for text in ("[]", '["a"]', '"text"', "5", "true", "null", "[{}]"):
			with self.subTest(text=text):
				self.assertEqual(refused(rate_card=text), [not_a_card])
		# JSON the policy can read but that is no usable card: every problem of the card, in its own words.
		for card in ({}, {"version": 1}, {"version": 2, "currency": "EUR", "unit": "kg", "profiles": [1]}):
			with self.subTest(card=card):
				messages = refused(rate_card=json.dumps(card))
				self.assertEqual(
					messages, sentences(f"Rate card: {problem}" for problem in validate_card(card))
				)
				self.assertGreaterEqual(len(messages), 2)
		self.assertIn("Rate card: The rate card has no profiles.", refused(rate_card='{"version": 1}'))
		# The card is checked whether or not it is used: the price source is still the carrier here.
		self.assertEqual(frappe.db.get_single_value(POLICY, "price_source"), "Carrier")
		# The price source cannot be the rate card while there is none.
		self.assertEqual(
			refused(price_source="Rate card"),
			["The price source is the rate card, but there is no rate card yet."],
		)
		# What people typed in a card is escaped in the sentence that names it.
		named = copy.deepcopy(CARD)
		named["profiles"][0]["name"] = "<b>Bold & co</b>"
		named["profiles"][0]["zones"] = []
		text = " ".join(refused(rate_card=json.dumps(named)))
		self.assertIn("&lt;b&gt;Bold &amp; co&lt;/b&gt;", text)
		self.assertNotIn("<", text)
		# A good card is saved, as the text it was given.
		good = frappe.get_doc(POLICY)
		good.update({"rate_card": json.dumps(CARD), "price_source": "Rate card"})
		good.save()
		self.assertEqual(frappe.db.get_single_value(POLICY, "rate_card"), json.dumps(CARD))
		self.assertEqual(shipping_policy.load_policy()["rate_card"], CARD)
		self.assertEqual(shipping_policy.load_policy()["price_source"], "rate_card")
		# No text, and text of nothing but blanks, mean no card: fine while the carrier is the source.
		for blank in ("", "  \n\t"):
			with self.subTest(blank=blank):
				cleared = frappe.get_doc(POLICY)
				cleared.update({"rate_card": blank, "price_source": "Carrier"})
				cleared.save()
				self.assertIsNone(shipping_policy.load_policy()["rate_card"])

	def test_a_stored_card_holding_nan_or_an_infinity_is_not_valid_json(self):
		# Python reads NaN, Infinity and -Infinity in JSON text, but they are not JSON: a browser refuses text
		# that holds one, and the card could never be written back. Saved on, such text is refused as text
		# that is not JSON; saved off it is kept as it was typed (nothing is checked), and read as no card.
		seed_policy(
			policy_of([zone_of("Gulf", ["AE"])], [rule_of("Gulf")], price_source="carrier"), active=True
		)
		before = shipping_policy.load_policy()
		for constant in ("NaN", "Infinity", "-Infinity"):
			text = json.dumps({**CARD, "note": float(constant)})
			self.assertIn(constant, text)
			with self.subTest(constant=constant):
				self.assertEqual(stored_card_problem(text), NOT_JSON)
				policy = frappe.get_doc(POLICY)
				policy.rate_card = text
				with self.assertRaises(frappe.ValidationError) as caught:
					policy.save()
				self.assertEqual(caught.exception.args[0], [NOT_JSON])
				self.assertEqual(shipping_policy.load_policy(), before, "nothing was saved")
		self.assertIsNone(stored_card_problem(json.dumps(CARD)), "the same card without it is fine")
		frappe.get_doc(POLICY).update({"active": 0, "rate_card": text}).save()
		self.assertEqual(frappe.db.get_single_value(POLICY, "rate_card"), text)
		self.assertIsNone(shipping_policy.load_policy()["rate_card"])

	def test_a_zone_that_uses_the_rate_card_needs_one_when_the_policy_is_saved(self):
		zone = zone_of("Gulf", ["AE"], fallback={"method": "rate_card", "amount": 0.0})
		with self.assertRaises(frappe.ValidationError) as caught:
			seed_policy(policy_of([zone], [rule_of("Gulf")]), active=True)
		self.assertEqual(
			caught.exception.args[0],
			sentences(
				[
					'Zone "Gulf" uses the rate card when the carrier can\'t quote, but there is no rate card yet.'
				]
			),
		)

	def test_a_policy_that_is_off_keeps_a_card_it_cannot_read_and_is_not_switched_on_with_it(self):
		# While the policy is off nothing of its content is checked, the stored card included: a card that is not
		# JSON is kept as it was typed (the loader reads it as no card, and an off policy prices nothing). Saving
		# the policy on is refused until the card is fixed.
		seed_policy(
			policy_of([zone_of("Gulf", ["AE"])], [rule_of("Gulf")], price_source="carrier"), active=True
		)
		frappe.get_doc(POLICY).update({"active": 0, "rate_card": "{not json"}).save()
		self.assertEqual(frappe.db.get_single_value(POLICY, "rate_card"), "{not json")
		self.assertIsNone(shipping_policy.load_policy()["rate_card"])
		policy = frappe.get_doc(POLICY)
		policy.active = 1
		with self.assertRaises(frappe.ValidationError) as caught:
			policy.save()
		self.assertEqual(
			caught.exception.args[0],
			["The stored rate card is not valid JSON. Import the rates again in the wizard."],
		)
		self.assertFalse(shipping_policy.load_policy()["active"])

	# -------------------------------------------------------------- item facts
	def test_item_facts_read_the_index_key_and_the_unit_weight(self):
		[(fragrance, key)] = website_items(FRAGRANCE)
		[(watch, watch_key)] = website_items(WATCH)
		weigh(self, fragrance, 0.25)
		weigh(self, watch, 0)
		facts = shipping_policy.item_facts_for([fragrance, watch, "NO-SUCH-ITEM", fragrance])
		self.assertEqual(facts[fragrance], {"index_key": key, "weight_kg": 0.25})
		self.assertEqual(facts[watch], {"index_key": watch_key, "weight_kg": 0.0}, "no weight on file")
		self.assertEqual(facts["NO-SUCH-ITEM"], {"index_key": "", "weight_kg": 0.0})
		self.assertEqual(set(facts), {fragrance, watch, "NO-SUCH-ITEM"}, "every text asked about is a key")
		self.assertTrue(
			key.startswith("{F-") and key.endswith("}"), "as stored: the card takes the braces off"
		)
		# Any publish state, and a key that is blank is no key.
		rekey(self, fragrance, "published", 0)
		self.assertEqual(shipping_policy.item_facts_for([fragrance])[fragrance]["index_key"], key)
		rekey(self, fragrance, "custom_index_key", "  ")
		self.assertEqual(shipping_policy.item_facts_for([fragrance])[fragrance]["index_key"], "")
		# Text that is no item code, and text the database cannot hold, are unknown items and no errors.
		odd = ["a\x00b", "\ud800", "O'Brien", "100%", "' OR 1=1 -- ", "x" * 500]
		unknown = {"index_key": "", "weight_kg": 0.0}
		self.assertEqual(
			shipping_policy.item_facts_for([None, 5, ["B"], {}, *odd]), {code: unknown for code in odd}
		)
		self.assertEqual(shipping_policy.item_facts_for([]), {})

	def test_item_facts_match_the_item_code_as_the_item_groups_do(self):
		# The database matches names without regard to case; both lookups keep only the code as it was asked.
		lettered = [(code, key) for code, key in website_items(FRAGRANCE, 10) if code != code.swapcase()]
		if not lettered:
			self.skipTest("no fragrance has letters in its item code to change the case of")
		code, key = lettered[0]
		facts = shipping_policy.item_facts_for([code, code.swapcase()])
		self.assertEqual(facts[code]["index_key"], key)
		self.assertEqual(facts[code.swapcase()], {"index_key": "", "weight_kg": 0.0})
		groups = shipping_policy.item_groups_for([code, code.swapcase()])
		self.assertTrue(groups[code])
		self.assertEqual(groups[code.swapcase()], [])

	def test_item_facts_use_at_most_two_queries(self):
		real = [code for code, _ in website_items(FRAGRANCE, 2) + website_items(WATCH)]
		codes = [*real, "NO-SUCH-ITEM", *[f"NO-SUCH-ITEM-{n}" for n in range(196)]]
		items = [{"item_code": code, "qty": 1.0} for code in codes]
		shipping_policy.item_facts_for(codes)  # once to warm Frappe's own metadata
		with self.assertQueryCount(2):
			facts = shipping_policy.item_facts_for(codes)
		self.assertTrue(all(facts[code]["index_key"] for code in real), "and they did read the keys")
		with recorded_queries() as queries:
			shipping_policy.item_facts_for(codes)
		self.assertEqual(len(queries), 2)
		self.assertTrue("`tabWebsite Item`" in queries[0] and "`tabItem`" in queries[1], queries)
		with self.assertQueryCount(0):
			shipping_policy.item_facts_for([])
			shipping_policy.item_facts_for([None, 5, "\ud800"])  # nothing the database could be asked
		# Through build_order the facts add two queries to the ones an order reads anyway.
		shipping_policy.build_order("AE", items, [ONE_BOX], [QUOTE])
		shipping_policy.build_order("AE", items, [ONE_BOX], [QUOTE], item_facts=True)
		with recorded_queries() as plain:
			shipping_policy.build_order("AE", items, [ONE_BOX], [QUOTE])
		with recorded_queries() as with_facts:
			shipping_policy.build_order("AE", items, [ONE_BOX], [QUOTE], item_facts=True)
		self.assertEqual(len(with_facts) - len(plain), 2)

	def test_build_order_without_item_facts_is_what_it_was(self):
		[(fragrance, key)] = website_items(FRAGRANCE)
		weigh(self, fragrance, 0.25)
		items = [{"item_code": fragrance, "qty": 2.0}, {"item_code": "NO-SUCH-ITEM", "qty": 1.0}]
		args = (" ae ", items, [ONE_BOX], [QUOTE])
		plain = shipping_policy.build_order(*args)
		self.assertEqual(plain, shipping_policy.build_order(*args, item_facts=False), "False is the default")
		self.assertEqual(set(plain), {"country_code", "items", "order_value", "boxes", "quotes", "served"})
		self.assertEqual([set(line) for line in plain["items"]], [{"item_code", "qty", "groups"}] * 2)
		with_facts = shipping_policy.build_order(*args, item_facts=True)
		self.assertEqual(with_facts["items"][0], {**plain["items"][0], "index_key": key, "weight_kg": 0.25})
		self.assertEqual(with_facts["items"][1], {**plain["items"][1], "index_key": "", "weight_kg": 0.0})
		self.assertEqual({**with_facts, "items": None}, {**plain, "items": None}, "nothing else differs")
		typed = shipping_policy.build_order(*args, order_value=125.5, item_facts=True)
		self.assertEqual(typed["order_value"], 125.5, "a typed value still wins")

	# -------------------------------------------------------------- rate-card mode
	def test_rate_card_mode_prices_a_guest_order_end_to_end(self):
		[(fragrance, _)] = website_items(FRAGRANCE)
		[(watch, _)] = website_items(WATCH)
		weigh(self, fragrance, 0.25)
		weigh(self, watch, 0.3)
		seed_policy(card_policy(), active=True)

		def priced(country, items):
			return guest_price(country, items=items, boxes=None, quotes=None)  # the items alone

		gulf = priced("AE", one_of(fragrance))
		self.assertEqual(
			gulf,
			{
				"policy_active": True,
				"price_source": "rate_card",
				"zone": "GCC",
				"offered": True,
				"reason": None,
				"message": "",
				"offers": [
					{
						"service_id": "rate-card",
						"name": EXPRESS,
						"amount": GCC_FRAGRANCE,
						"source": "rate_card",
						"reason": None,
						"estimated_days": 7,
					}
				],
			},
		)
		self.assertEqual(set(gulf), RESPONSE_KEYS)
		self.assertEqual(set(gulf["offers"][0]), OFFER_KEYS)
		# A guest can never say what an item is or what it weighs: the ERP's product code and unit weight
		# price it, and whatever else is posted with an item is dropped as it is cleaned. A forged code that
		# would make the fragrance a free item of the catch-all profile in the Gulf, and a forged weight
		# that would be too heavy for the USA, change nothing.
		forged = {"index_key": "{P-ZZ-1}", "weight_kg": 99, "weight_per_unit": 99, "groups": [], "price": 0}
		lie = [{**one_of(fragrance)[0], **forged}]
		self.assertEqual(priced("AE", lie), gulf)
		self.assertEqual(priced("US", lie), priced("US", one_of(fragrance)))
		# The control: had they been read, the answers would differ.
		policy = shipping_policy.load_policy()
		believed = {"country_code": "AE", "items": [{**lie[0], "qty": 1.0}], "served": True}
		self.assertEqual(first_amount(shipping_engine.resolve(policy, believed)), 0.0)
		believed["country_code"] = "US"
		self.assertEqual(shipping_engine.resolve(policy, believed)["reason"], "too_heavy")
		# Two profiles in one cart add up, each in its zone of the card; the zone named is the policy's.
		mixed = priced("US", one_of(fragrance, 2) + one_of(watch))
		self.assertEqual((mixed["zone"], mixed["offered"], mixed["reason"]), ("Rest of world", True, None))
		self.assertEqual(first_amount(mixed), US_FRAGRANCES + US_WATCH)
		self.assertEqual((mixed["offers"][0]["name"], mixed["offers"][0]["estimated_days"]), (EXPRESS, 7))
		self.assertEqual(len(mixed["offers"]), 1, "one delivery method, whatever the profiles")
		# Free is still an offer; the slower promise names the method and sets the days.
		free = priced("BH", one_of(fragrance) + one_of(watch))
		self.assertEqual((free["zone"], free["offered"], first_amount(free)), ("Bahrain", True, 0.0))
		self.assertEqual(
			(free["offers"][0]["name"], free["offers"][0]["estimated_days"]), ("Free Shipping (1-3 days)", 3)
		)
		# A Not shipped zone still refuses, in the zone's own words.
		self.assertEqual(
			priced("RU", one_of(fragrance)),
			{
				"policy_active": True,
				"price_source": "rate_card",
				"zone": "Not shipped",
				"offered": False,
				"reason": "not_shipped",
				"message": "We don't deliver to this country.",
				"offers": [],
			},
		)
		# What the card cannot price is not offered, for the card's reason, in the standard words.
		refusals = {
			"a country the card does not list": (
				priced("FR", one_of(fragrance)),
				"not_covered",
				"Rest of world",
			),
			"a weight above the top tier": (
				priced("US", one_of(fragrance, 40)),
				"too_heavy",
				"Rest of world",
			),
			"no items": (priced("AE", []), "not_offered", "GCC"),
			"a destination that is no country": (priced("UAE", one_of(fragrance)), "not_served", None),
		}
		for what, (answer, reason, zone) in refusals.items():
			with self.subTest(what):
				self.assertEqual((answer["offered"], answer["reason"], answer["zone"]), (False, reason, zone))
				self.assertEqual((answer["offers"], answer["price_source"]), ([], "rate_card"))
				self.assertEqual(answer["message"], shipping_engine.DEFAULT_MESSAGES[reason])
		self.assertEqual(
			shipping_engine.DEFAULT_MESSAGES["too_heavy"],
			"This order is heavier than our delivery rates cover.",
		)
		self.assertEqual(
			first_amount(priced(" ae ", one_of(fragrance))), GCC_FRAGRANCE, "a country as written"
		)

	def test_rate_card_mode_needs_no_boxes_or_quotes_and_never_calls_the_carrier(self):
		[(fragrance, _)] = website_items(FRAGRANCE)
		weigh(self, fragrance, 0.25)
		seed_policy(card_policy(), active=True)
		items = json.dumps(one_of(fragrance))
		with as_guest(), no_lookups(), boxes_and_quotes_unread(), recorded_queries() as queries:
			# Nothing about the cartons or the quotes is read, not even to find that it is no JSON.
			garbage = shipping_policy.price_delivery("AE", items, "this is not json", "[{")
			absent = shipping_policy.price_delivery("AE", items)
			absurd = shipping_policy.price_delivery("AE", items, [{"weight_kg": -1}], [{"amount": -5}])
		self.assertEqual((garbage["offered"], first_amount(garbage)), (True, GCC_FRAGRANCE))
		self.assertEqual(absent, garbage)
		self.assertEqual(absurd, garbage, "the carrier's side of the order changes nothing")
		self.assertEqual(
			carrier_tables_read(queries), [], "the carrier's tables are not read: no served lookup"
		)
		# The control: the carrier's own price path does read them, so this test could fail.
		frappe.db.set_single_value(POLICY, "price_source", "Carrier")
		with recorded_queries() as queries:
			guest_price("AE", items=one_of(fragrance))
		self.assertNotEqual(carrier_tables_read(queries), [])
		# The items are still checked, and refused in plain words as for the carrier.
		frappe.db.set_single_value(POLICY, "price_source", "Rate card")
		for bad in ("[{", [{"item_code": "A", "qty": -1}], [{"item_code": "A\x00B", "qty": 1}]):
			with self.subTest(bad=bad), self.assertRaises(frappe.ValidationError):
				guest_price("AE", items=bad, boxes=None, quotes=None)

	def test_rate_card_mode_prices_a_country_no_carrier_serves(self):
		served = shipping_policy.served_country_codes()
		country = next((code for code in ("AX", "AL", "AU", "CN") if code not in served), None)
		if country is None:
			self.skipTest("the carriers of this site serve every country of the card's Worldwide zone")
		[(watch, _)] = website_items(WATCH)
		weigh(self, watch, 0.3)
		seed_policy(card_policy(), active=True)
		priced = guest_price(country, items=one_of(watch), boxes=None, quotes=None)
		self.assertEqual((priced["offered"], first_amount(priced)), (True, US_WATCH))
		# The carrier's own rule is unchanged: where nobody delivers there is no delivery.
		frappe.db.set_single_value(POLICY, "price_source", "Carrier")
		refused = guest_price(country, items=one_of(watch))
		self.assertEqual((refused["offered"], refused["reason"]), (False, "not_served"))

	def test_rate_card_mode_prices_what_build_order_with_item_facts_would(self):
		# `price_delivery` builds a leaner order than `build_order` (no item groups, order value or `served`
		# lookup) and the wizard's Try an order builds the full one: the engine must price both the same.
		[(fragrance, _), (other, _)] = website_items(FRAGRANCE, 2)
		[(watch, _)] = website_items(WATCH)
		[(refill, _)] = website_items(REFILL)
		weigh(self, fragrance, 0.25)
		weigh(self, watch, 0.3)
		seed_policy(card_policy(), active=True)
		orders = {
			"a fragrance to the Gulf": ("AE", one_of(fragrance)),
			"two profiles to the USA": ("US", one_of(fragrance, 2) + one_of(watch)),
			"free in Bahrain": ("BH", one_of(fragrance) + one_of(watch)),
			"an item the ERP does not know": ("US", one_of("NO-SUCH-ITEM") + one_of(other)),
			"a refill the card cannot send to the Gulf": ("AE", one_of(refill)),
			"too heavy": ("US", one_of(fragrance, 40)),
			"not shipped": ("RU", one_of(fragrance)),
			"a country nobody serves": (unserved_code(), one_of(fragrance)),
			"no country": ("", one_of(fragrance)),
			"no items": ("AE", []),
		}
		policy = shipping_policy.load_policy()
		for what, (country, items) in orders.items():
			with self.subTest(what):
				full = shipping_policy.build_order(country, items, [ONE_BOX], [QUOTE], item_facts=True)
				expected = response_from(shipping_engine.resolve(policy, full), lambda text: text)
				self.assertEqual(guest_price(country, items=items, boxes=None, quotes=None), expected)

	# -------------------------------------------------------------- the card as a fallback
	def test_carrier_mode_fallback_uses_the_rate_card_when_no_quote(self):
		[(fragrance, _)] = website_items(FRAGRANCE)
		weigh(self, fragrance, 0.25)
		policy = card_policy(price_source="carrier", default_fallback={"method": "rate_card", "amount": 0.0})
		zone_named(policy, "GCC")["fallback"] = {"method": "rate_card", "amount": 0.0}
		seed_policy(policy, active=True)
		# No quote where a carrier serves the country: the GCC zone's own choice is the card.
		fallback = guest_price("AE", items=one_of(fragrance), quotes=[])
		self.assertEqual(
			fallback["price_source"], "carrier", "the policy's source, not where the price came from"
		)
		self.assertEqual(
			(fallback["zone"], fallback["offered"], fallback["reason"]), ("GCC", True, "fallback_offer")
		)
		self.assertEqual(
			fallback["offers"],
			[
				{
					"service_id": "rate-card",
					"name": EXPRESS,
					"amount": GCC_FRAGRANCE,
					"source": "rate_card",
					"reason": "fallback_offer",
					"estimated_days": 7,
				}
			],
		)
		# A zone that defers to the policy default gets the card from there.
		bahrain = guest_price("BH", items=one_of(fragrance), quotes=[])
		self.assertEqual(
			(bahrain["offered"], first_amount(bahrain), bahrain["reason"]), (True, 0.0, "fallback_offer")
		)
		self.assertEqual(bahrain["offers"][0]["name"], "Free Shipping (1-2 days)")
		# With a quote the carrier's price goes through the rules as before, and promises no days.
		quoted = guest_price("AE", items=one_of(fragrance), quotes=[QUOTE])
		self.assertEqual(
			(quoted["reason"], first_amount(quoted), quoted["offers"][0]["source"]), (None, 39.0, "rule")
		)
		self.assertIsNone(quoted["offers"][0]["estimated_days"])
		# A quote for one carton of two is no quote.
		two_boxes = [ONE_BOX, {"weight_kg": 2.0, "volume_l": 3.0}]
		partial = guest_price("AE", items=one_of(fragrance), boxes=two_boxes, quotes=[quote_of(10.0)])
		self.assertEqual((partial["reason"], first_amount(partial)), ("fallback_offer", GCC_FRAGRANCE))
		# Where no carrier delivers there is no fallback at all, the card included.
		unserved = guest_price(unserved_code(), items=one_of(fragrance), quotes=[])
		self.assertEqual(
			(unserved["offered"], unserved["reason"], unserved["offers"]), (False, "not_served", [])
		)

	def test_the_rate_card_fallback_prices_a_formula_error_and_logs_it_once(self):
		[(fragrance, _)] = website_items(FRAGRANCE)
		weigh(self, fragrance, 0.25)
		zone = zone_of("Zone 1", ["AE"], fallback={"method": "rate_card", "amount": 0.0})
		rule = rule_of("Zone 1", label="Weight formula", method="formula", formula=FRAGILE)
		policy = policy_of([zone], [rule], price_source="carrier", rate_card=copy.deepcopy(CARD))
		seed_policy(policy, active=True)
		with error_log_entries(FORMULA_ERROR) as new_entries:
			crashed = guest_price("AE", items=one_of(fragrance), boxes=CRASH_BOXES)
			self.assertEqual((crashed["reason"], crashed["offered"]), ("formula_error", True))
			offer = crashed["offers"][0]
			self.assertEqual(
				(offer["service_id"], offer["source"], offer["reason"]),
				("rate-card", "rate_card", "formula_error"),
			)
			self.assertEqual((offer["amount"], offer["estimated_days"]), (GCC_FRAGRANCE, 7))
			self.assertEqual(crashed["price_source"], "carrier")
			guest_price("AE", items=one_of(fragrance), boxes=CRASH_BOXES)
			self.assertEqual(len(new_entries()), 1, "the throttled log, as for the other fallbacks")
		with error_log_entries(FORMULA_ERROR) as new_entries:
			fine = guest_price("AE", items=one_of(fragrance), boxes=[{"weight_kg": 10.0, "volume_l": 1.0}])
			self.assertEqual((fine["reason"], first_amount(fine)), (None, 2.0), "10 / (10 - 5)")
			self.assertEqual(new_entries(), [])

	def test_a_formula_error_is_logged_when_the_card_fallback_cannot_price_the_order_either(self):
		# The rule's formula fails (the carton weighs 5 kg) and the zone's fallback, the rate card, cannot
		# price the order either: ten fragrances with no weight on file are 3.5 kg, 7.716 lb, and the card's
		# zone for AE here stops at 2 lb. The customer gets the card's refusal, and the failed formula is
		# noted once, as when the fallback prices the order.
		[(fragrance, _)] = website_items(FRAGRANCE)
		weigh(self, fragrance, 0)
		card = copy.deepcopy(CARD)
		zone_named(card["profiles"][2], "GCC")["tiers"] = [{"from": 0.01, "to": 2.0, "price": 35.0}]
		zone = zone_of("Zone 1", ["AE"], fallback={"method": "rate_card", "amount": 0.0})
		rule = rule_of("Zone 1", label="Weight formula", method="formula", formula=FRAGILE)
		seed_policy(policy_of([zone], [rule], price_source="carrier", rate_card=card), active=True)
		rule_row = shipping_policy.load_policy()["rules"][0]["name"]
		with error_log_entries(FORMULA_ERROR) as new_entries:
			refused = guest_price("AE", items=one_of(fragrance, 10), boxes=CRASH_BOXES)
			self.assertEqual(
				(refused["offered"], refused["reason"], refused["offers"]), (False, "too_heavy", [])
			)
			[entry] = new_entries()
			# It went to the fallback, which priced nothing: the entry does not say the fallback applied.
			said = 'Rule 1 ("Weight formula") could not be priced, so the order went to the zone\'s fallback.'
			self.assertEqual(entry["error"].split("\n")[0], said)
			self.assertEqual(
				(entry["reference_doctype"], entry["reference_name"]), ("Shipping Policy Rule", rule_row)
			)

	def test_item_facts_are_read_only_when_a_fallback_may_use_the_card(self):
		[(fragrance, _)] = website_items(FRAGRANCE)
		policy = card_policy(price_source="carrier")  # the card is there but no fallback is the card
		seed_policy(policy, active=True)

		def facts_read():
			with mock.patch.object(
				shipping_policy, "item_facts_for", wraps=shipping_policy.item_facts_for
			) as read:
				guest_price("AE", items=one_of(fragrance), quotes=[QUOTE])
			return read.call_count

		self.assertEqual(facts_read(), 0, "a carrier policy with no rate-card fallback reads no item facts")
		saved = frappe.get_doc(POLICY)
		saved.default_fallback_method = "Use the rate card"
		saved.save()
		self.assertEqual(facts_read(), 1, "the policy default is the card")
		saved.default_fallback_method = "Don't offer delivery"
		saved.save()
		self.assertEqual(facts_read(), 0)
		bahrain = frappe.get_doc("Shipping Zone", "Bahrain")
		bahrain.fallback_method = "Use the rate card"
		bahrain.save()
		self.assertEqual(facts_read(), 1, "an enabled zone's fallback is the card")
		bahrain.enabled = 0
		bahrain.save()
		self.assertEqual(facts_read(), 0, "a zone that is off does not count")

	# -------------------------------------------------------------- the policy off, odd settings
	def test_policy_off_is_unchanged_in_rate_card_mode(self):
		[(fragrance, _)] = website_items(FRAGRANCE)
		weigh(self, fragrance, 0.25)
		quotes = [
			{"service_id": "A", "name": "Whole", "amount": 12.5, "boxes_quoted": 2},
			{"service_id": "C", "amount": 9.99},
		]
		expected = {
			"policy_active": False,
			"price_source": "rate_card",
			"zone": None,
			"offered": True,
			"reason": None,
			"message": "",
			"offers": [
				{
					"service_id": service_id,
					"name": name,
					"amount": amount,
					"source": "carrier",
					"reason": None,
					"estimated_days": None,
				}
				for service_id, name, amount in (("A", "Whole", 12.5), ("C", "C", 9.99))
			],
		}
		seed_policy(card_policy(), active=False)

		def priced():
			# Nothing about the items, the cartons or the carriers is read while the policy is off.
			with (
				as_guest(),
				no_lookups(),
				mock.patch.object(
					shipping_policy, "item_facts_for", side_effect=AssertionError("facts read")
				),
			):
				return shipping_policy.price_delivery("AE", "this is not even json", "[{", quotes)

		self.assertEqual(priced(), expected, "the card is the source, but the policy is off")
		with self.assertRaises(frappe.ValidationError):  # a bad quote is still refused in plain words
			guest_price(quotes=[{"service_id": "A", "amount": -1}])
		# Switched on, the same policy prices from the card; off again, it is the carrier's price once more.
		frappe.db.set_single_value(POLICY, "active", 1)
		on = guest_price("AE", items=one_of(fragrance), boxes=None, quotes=quotes)
		self.assertEqual(
			(on["policy_active"], on["price_source"], first_amount(on)), (True, "rate_card", 35.0)
		)
		frappe.db.set_single_value(POLICY, "active", 0)
		self.assertEqual(priced(), expected)
		frappe.db.set_single_value(POLICY, "rate_card", "")  # and with no card at all
		self.assertEqual(priced(), expected)

	def test_an_unknown_price_source_in_the_database_prices_nothing(self):
		[(fragrance, _)] = website_items(FRAGRANCE)
		seed_policy(card_policy(), active=True)
		frappe.db.set_single_value(POLICY, "price_source", "Gold")
		answer = guest_price("AE", items=one_of(fragrance))
		self.assertEqual(
			(answer["offered"], answer["reason"], answer["offers"], answer["price_source"]),
			(False, "not_covered", [], "carrier"),
		)

	def test_a_card_that_cannot_price_an_order_is_an_ordinary_refusal_not_a_log_entry(self):
		[(fragrance, _)] = website_items(FRAGRANCE)
		[(refill, _)] = website_items(REFILL)
		weigh(self, fragrance, 0)  # 40 of them at the default weight are 14 kg: above the card's 20 lb
		seed_policy(card_policy(), active=True)
		with (
			error_log_entries(FORMULA_ERROR) as new_entries,
			mock.patch.object(frappe, "log_error") as log_error,
			mock.patch.object(shipping_policy, "_log_formula_error") as log_formula_error,
		):
			heavy = guest_price("US", items=one_of(fragrance, 40), boxes=None, quotes=None)
			uncovered = guest_price("AE", items=one_of(refill), boxes=None, quotes=None)
			self.assertEqual((heavy["reason"], uncovered["reason"]), ("too_heavy", "not_covered"))
			self.assertEqual(new_entries(), [])
			log_error.assert_not_called()
			log_formula_error.assert_not_called()

	# -------------------------------------------------------------- default weights
	def test_unknown_items_and_missing_weights_use_the_default_weight(self):
		[(fragrance, key)] = website_items(FRAGRANCE)
		[(watch, _)] = website_items(WATCH)
		weigh(self, fragrance, 0)
		weigh(self, watch, 0)
		seed_policy(card_policy(), active=True)

		def price(items):
			return first_amount(guest_price("US", items=items, boxes=None, quotes=None))

		# With no weight on file, each unit counts as 0.35 kg, 0.772 lb: 60 in the USA zone of Dangerous
		# Goods (the 0.7 to 2 lb tier) and 40 in the Worldwide zone of the catch-all profile.
		self.assertEqual(price(one_of(fragrance)), 60.0)
		self.assertEqual(price(one_of(watch)), 40.0)
		self.assertEqual(
			price(one_of("NO-SUCH-ITEM")), 40.0, "an unknown item has no key: the catch-all profile"
		)
		self.assertEqual(price(one_of(fragrance, 3)), 80.0, "3 x 0.35 kg = 2.315 lb: the next tier")
		self.assertEqual(
			price(one_of(fragrance) + one_of("NO-SUCH-ITEM")), 100.0, "each profile on its own weight"
		)
		# A weight on file is used instead; zero and negative weights are 'no weight'.
		weigh(self, fragrance, 0.2)
		self.assertEqual(price(one_of(fragrance)), 35.0, "0.2 kg = 0.441 lb: the first tier")
		for no_weight in (0, -1.5):
			with self.subTest(weight=no_weight):
				weigh(self, fragrance, no_weight)
				self.assertEqual(price(one_of(fragrance)), 60.0)
		# An item whose Website Item has no key is as unknown as one that has no Website Item.
		weigh(self, fragrance, 0.2)
		rekey(self, fragrance, "custom_index_key", "")
		self.assertEqual(price(one_of(fragrance)), 40.0)
		rekey(self, fragrance, "custom_index_key", key)
		self.assertEqual(price(one_of(fragrance)), 35.0)
		# The staff can see which line was guessed: the engine's receipt says so. The guest never sees it.
		weigh(self, fragrance, 0)
		weigh(self, watch, 0.3)
		lines = [{"item_code": fragrance, "qty": 1.0}, {"item_code": watch, "qty": 1.0}]
		order = shipping_policy.build_order("US", lines, [], [], item_facts=True)
		trace = shipping_engine.resolve(shipping_policy.load_policy(), order)["offers"][0]["trace"]
		receipt = [step["text"] for step in trace if step["kind"] == "rate_card"]
		self.assertEqual(len(receipt), 2)
		self.assertTrue(receipt[0].endswith("(weight estimated)"), receipt)
		self.assertFalse(receipt[1].endswith("(weight estimated)"), receipt)

	# -------------------------------------------------------------- the card never reaches a customer
	def test_guest_response_never_contains_card_contents(self):
		[(fragrance, _)] = website_items(FRAGRANCE)
		weigh(self, fragrance, 0.25)
		secrets = (
			"ZZ-PROFILE-SECRET",
			"ZZ-PATTERN-SECRET",
			"ZZ-ZONE-SECRET",
			"ZZ-OTHER-ZONE-SECRET",
			"ZZ-OTHER-METHOD-SECRET",
			"ZZ-CATCHALL-SECRET",
			"F-*",
			"777.77",
			"888.88",
		)
		card = {
			"version": 1,
			"currency": "USD",
			"unit": "lb",
			"profiles": [
				{
					"name": "ZZ-PROFILE-SECRET",
					"match": ["F-*", "ZZ-PATTERN-SECRET-*"],
					"zones": [
						card_zone(
							"ZZ-ZONE-SECRET",
							["AE"],
							"Express (4-7 days)",
							7,
							(0.01, 2.0, 123.45),
							(2.01, 4.0, 777.77),
						),
						card_zone(
							"ZZ-OTHER-ZONE-SECRET",
							["KW"],
							"ZZ-OTHER-METHOD-SECRET (9-10 days)",
							10,
							(None, None, 888.88),
						),
					],
				},
				{
					"name": "ZZ-CATCHALL-SECRET",
					"match": ["*"],
					"zones": [
						card_zone("ZZ-ZONE-SECRET", ["AE"], "Express (4-7 days)", 7, (None, None, 5.5))
					],
				},
			],
		}
		self.assertEqual(validate_card(card), [])
		policy = card_policy(rate_card=card)
		zone_named(policy, "GCC")["fallback"] = {"method": "rate_card", "amount": 0.0}
		seed_policy(policy, active=True)

		def alone(country, items):
			return guest_price(country, items=items, boxes=None, quotes=None)

		answers = {
			"priced": alone("AE", one_of(fragrance)),
			"too heavy": alone("AE", one_of(fragrance, 40)),
			"not covered": alone("FR", one_of(fragrance)),
			"not shipped": alone("RU", one_of(fragrance)),
			"nothing to price": alone("AE", []),
		}
		frappe.db.set_single_value(POLICY, "price_source", "Carrier")
		answers["carrier, priced by the fallback"] = guest_price("AE", items=one_of(fragrance), quotes=[])
		answers["carrier, priced by a rule"] = guest_price("AE", items=one_of(fragrance), quotes=[QUOTE])
		self.assertEqual(answers["priced"]["offers"][0]["amount"], 123.45)
		self.assertEqual(answers["carrier, priced by the fallback"]["offers"][0]["amount"], 123.45)
		self.assertEqual(
			answers["priced"]["offers"][0]["name"], "Express (4-7 days)", "the method names the offer"
		)
		for what, answer in answers.items():
			with self.subTest(what):
				self.assertEqual(set(answer), RESPONSE_KEYS)
				for offer in answer["offers"]:
					self.assertEqual(set(offer), OFFER_KEYS)
				everything = json.dumps(list(inside(answer)), ensure_ascii=False)
				for secret in secrets:
					self.assertNotIn(secret, everything)
		# The control: the engine's own result does hold the card's words (its receipt), so this could fail.
		order = shipping_policy.build_order("AE", one_of(fragrance), [], [], item_facts=True)
		kept = {**shipping_policy.load_policy(), "price_source": "rate_card"}
		trace = " ".join(step["text"] for step in shipping_engine.resolve(kept, order)["offers"][0]["trace"])
		self.assertIn("ZZ-PROFILE-SECRET", trace)
		self.assertIn("ZZ-ZONE-SECRET", trace)
