"""Pins the shipping pricing engine: which zone and rule an order meets, what it is charged, and what
happens when the carrier cannot quote.

Most cases run against the example policy in fixtures/shipping_policy_example.json: four zones
(Bahrain, GCC, Rest of world, Not shipped), a Watches item set, and four rules in this order:

	0  Bahrain, every item, free
	1  GCC, the Watches set, free
	2  GCC, every item, the carrier price, rounded up
	3  Rest of world, every item, max(carrier_price * 1.1, 45), rounded up

The last sections are about the interim rate card: the same policy with the rate card as its price source
(or as a fallback), holding the trimmed Shopify export of fixtures/shopify_shipping_config_sample.csv.
"""

import ast
import copy
import json
import math
import random
import string
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from dsi_catalogue import shipping_engine
from dsi_catalogue.shipping_engine import (
	DEFAULT_MESSAGES,
	FALLBACK_SERVICE_ID,
	FALLBACK_SERVICE_NAME,
	LABELS,
	RATE_CARD_SERVICE_ID,
	REASONS,
	normalize_country,
	resolve,
	round_up_dollar,
	rule_matches,
	rule_previews,
	select_zone,
	to_cents,
	validate_policy,
)
from dsi_catalogue.shipping_rate_card import parse_shopify_export, validate_card

FIXTURE = Path(__file__).parent / "fixtures" / "shipping_policy_example.json"
SAMPLE_CARD_CSV = Path(__file__).parent / "fixtures" / "shopify_shipping_config_sample.csv"

POLICY_KEYS = {
	"active",
	"price_source",
	"rate_card",
	"not_covered",
	"default_fallback",
	"zones",
	"item_sets",
	"rules",
}
ZONE_KEYS = {
	"name",
	"enabled",
	"countries",
	"not_shipped",
	"everywhere_else",
	"customer_line",
	"not_offered_message",
	"fallback",
}
RULE_KEYS = {
	"name",
	"enabled",
	"label",
	"zone",
	"item_set",
	"min_order_value",
	"max_order_value",
	"min_weight_kg",
	"max_weight_kg",
	"min_volume_l",
	"max_volume_l",
	"method",
	"formula",
	"round_up",
}
RESULT_KEYS = {"policy_active", "price_source", "zone", "rule", "offered", "reason", "message", "offers"}
OFFER_KEYS = {"service_id", "name", "amount", "source", "reason", "estimated_days", "trace"}
STEP_KEYS = {"kind", "text", "amount"}
STEP_KINDS = {"carrier", "rule", "formula", "round", "fallback", "rate_card", "result"}


# ---------------------------------------------------------------- helpers
def example():
	"""A fresh copy of the example policy, so a test can change it freely."""
	return json.loads(FIXTURE.read_text(encoding="utf-8"))


def make_rule(zone="GCC", **changes):
	"""A rule with every key: for every item, no limits, the carrier price, not rounded up."""
	rule = {
		"name": None,
		"enabled": True,
		"label": "",
		"zone": zone,
		"item_set": None,
		"min_order_value": 0.0,
		"max_order_value": 0.0,
		"min_weight_kg": 0.0,
		"max_weight_kg": 0.0,
		"min_volume_l": 0.0,
		"max_volume_l": 0.0,
		"method": "carrier_price",
		"formula": "",
		"round_up": False,
	}
	return {**rule, **changes}


def make_item(code="PERFUME-1", groups=("Fragrances",), qty=1):
	return {"item_code": code, "qty": qty, "groups": list(groups)}


def make_quote(amount=38.2, service_id="svc", name="Express", boxes_quoted=1):
	return {"service_id": service_id, "name": name, "amount": amount, "boxes_quoted": boxes_quoted}


def make_order(country="AE", *, items=None, order_value=310.0, boxes=None, quotes=None, served=True):
	"""The sample order: one perfume, 310 dollars, one carton of 2.4 kg and 6 litres, one 38.20 quote."""
	boxes = [{"weight_kg": 2.4, "volume_l": 6.0}] if boxes is None else boxes
	return {
		"country_code": country,
		"items": [make_item()] if items is None else items,
		"order_value": order_value,
		"boxes": boxes,
		"quotes": [make_quote(boxes_quoted=len(boxes))] if quotes is None else quotes,
		"served": served,
	}


def with_rules(*rules):
	"""The example policy with its rules replaced by these."""
	policy = example()
	policy["rules"] = list(rules)
	return policy


def without_zone(policy, name):
	policy["zones"] = [zone for zone in policy["zones"] if zone["name"] != name]
	return policy


def only_offer(result):
	(offer,) = result["offers"]
	return offer


def amount_of(result):
	return only_offer(result)["amount"]


def price(policy, carrier_amount, country="AE", **order):
	"""What the customer pays for one carrier quote of `carrier_amount`."""
	return amount_of(resolve(policy, make_order(country, quotes=[make_quote(carrier_amount)], **order)))


def check_shape(result):
	"""A result has the documented shape, whatever the policy or the order."""
	assert set(result) == RESULT_KEYS, result
	assert isinstance(result["policy_active"], bool)
	assert result["price_source"] in ("carrier", "rate_card")
	assert isinstance(result["offered"], bool)
	assert result["offered"] == bool(result["offers"])
	assert result["reason"] is None or result["reason"] in REASONS
	assert isinstance(result["message"], str)
	assert result["rule"] is None or (isinstance(result["rule"], int) and result["rule"] >= 0)
	for offer in result["offers"]:
		assert set(offer) == OFFER_KEYS, offer
		assert offer["source"] in ("carrier", "rule", "fallback", "rate_card")
		assert offer["reason"] is None or offer["reason"] in REASONS
		days = offer["estimated_days"]
		assert days is None or (type(days) is int and days >= 0), days
		assert offer["trace"], "an offer always explains itself"
		for step in offer["trace"]:
			assert set(step) == STEP_KEYS, step
			assert step["kind"] in STEP_KINDS
			assert isinstance(step["text"], str) and step["text"]
			assert step["amount"] is None or isinstance(step["amount"], (int, float))


def holds(order, **limits):
	"""Do the conditions of a rule with these limits hold for the order?"""
	return rule_matches(make_rule(**limits), order, [])


# ---------------------------------------------------------------- the example and the constants
def test_the_example_policy_carries_every_key_of_the_policy_shape():
	"""Task 3 compares a round trip through ERP rows against this file, so no key may be left out."""
	policy = example()
	assert set(policy) == POLICY_KEYS
	assert set(policy["default_fallback"]) == {"method", "amount"}
	for zone in policy["zones"]:
		assert set(zone) == ZONE_KEYS
		assert set(zone["fallback"]) == {"method", "amount"}
	for item_set in policy["item_sets"]:
		assert set(item_set) == {"name", "item_groups", "items"}
	for rule in policy["rules"]:
		assert set(rule) == RULE_KEYS


def test_the_example_policy_is_the_one_the_plan_describes():
	policy = example()
	assert (policy["active"], policy["not_covered"]) == (True, "pass_through")
	assert policy["default_fallback"] == {"method": "no_delivery", "amount": 0.0}
	by_name = {zone["name"]: zone for zone in policy["zones"]}
	assert list(by_name) == ["Bahrain", "GCC", "Rest of world", "Not shipped"]
	assert by_name["Bahrain"]["countries"] == ["BH"]
	assert by_name["GCC"]["countries"] == ["AE", "SA", "KW", "OM", "QA"]
	assert by_name["GCC"]["fallback"] == {"method": "fixed", "amount": 35.0}
	assert by_name["Bahrain"]["fallback"]["method"] == "policy_default"
	assert by_name["Rest of world"]["fallback"]["method"] == "policy_default"
	assert (by_name["Rest of world"]["everywhere_else"], by_name["Rest of world"]["countries"]) == (True, [])
	assert by_name["Not shipped"]["countries"] == ["RU", "BY"]
	assert by_name["Not shipped"]["not_shipped"] is True
	assert by_name["Not shipped"]["not_offered_message"] == "We don't deliver to this country."
	assert policy["item_sets"] == [{"name": "Watches", "item_groups": ["Watches"], "items": []}]
	assert [(r["zone"], r["item_set"], r["method"], r["round_up"]) for r in policy["rules"]] == [
		("Bahrain", None, "free", False),
		("GCC", "Watches", "free", False),
		("GCC", None, "carrier_price", True),
		("Rest of world", None, "formula", True),
	]
	assert policy["rules"][3]["formula"] == "max(carrier_price * 1.1, 45)"
	assert all(rule["name"] is None for rule in policy["rules"])


def test_labels_are_the_select_strings_mapped_to_codes_in_the_order_the_doctypes_list_them():
	# What the first four tables held before the rate card stays where it was; the entries the rate card added
	# are pinned in test_labels_keep_their_order_and_gain_the_new_entries.
	assert list(LABELS)[:4] == ["method", "zone_fallback", "default_fallback", "not_covered"]
	assert list(LABELS["method"].items()) == [
		("Carrier price", "carrier_price"),
		("Free", "free"),
		("Custom formula", "formula"),
	]
	assert list(LABELS["zone_fallback"].items())[:4] == [
		("Use the policy default", "policy_default"),
		("Don't offer delivery", "no_delivery"),
		("Charge a fixed amount", "fixed"),
		("Free", "free"),
	]
	assert list(LABELS["default_fallback"].items())[:3] == [
		("Don't offer delivery", "no_delivery"),
		("Charge a fixed amount", "fixed"),
		("Free", "free"),
	]
	assert list(LABELS["not_covered"].items()) == [
		("Pass the carrier price through", "pass_through"),
		("Don't offer delivery", "no_delivery"),
	]
	# a straight apostrophe, so the text typed in a Select option matches
	for labels in LABELS.values():
		assert all(label.isascii() for label in labels)


def test_reasons_service_and_messages():
	assert REASONS == (
		"not_served",
		"not_shipped",
		"not_offered",
		"not_covered",
		"fallback_offer",
		"formula_error",
		"too_heavy",
	)
	assert DEFAULT_MESSAGES["too_heavy"] == "This order is heavier than our delivery rates cover."
	assert (FALLBACK_SERVICE_ID, FALLBACK_SERVICE_NAME) == ("policy-fallback", "Standard delivery")
	assert RATE_CARD_SERVICE_ID == "rate-card"
	assert set(DEFAULT_MESSAGES) == set(REASONS)
	for message in DEFAULT_MESSAGES.values():
		assert message.isascii() and message[0].isupper() and message.endswith(".")
	assert len(set(DEFAULT_MESSAGES.values())) == len(REASONS), "each reason reads differently"


def test_the_engine_is_pure_python_that_never_evaluates_text():
	source = Path(shipping_engine.__file__).read_text(encoding="utf-8")
	imported = set()
	called = set()
	for node in ast.walk(ast.parse(source)):
		if isinstance(node, ast.Import):
			imported.update(alias.name.split(".")[0] for alias in node.names)
		elif isinstance(node, ast.ImportFrom):
			imported.add((node.module or "").split(".")[0])
		elif isinstance(node, ast.Name) and node.id in {"eval", "exec", "compile", "__import__"}:
			called.add(node.id)
	assert "frappe" not in imported
	assert called == set()


# ---------------------------------------------------------------- country codes and zones
@pytest.mark.parametrize(
	("raw", "expected"),
	[
		("AE", "AE"),
		(" ae ", "AE"),
		("ae", "AE"),
		("\tqa\n", "QA"),
		("UAE", ""),
		("", ""),
		("  ", ""),
		("A", ""),
		("A E", ""),
		("A1", ""),
		("12", ""),
		("A-", ""),
		(None, ""),
		(5, ""),
		(["AE"], ""),
		# 'dotless i' and 'sharp s' are not letters A-Z, even though they upper-case to ASCII letters
		(chr(0x131) + "i", ""),
		(chr(0xDF) + "a", ""),
		(chr(0xE4) + "e", ""),
	],
)
def test_normalize_country(raw, expected):
	assert normalize_country(raw) == expected


def test_country_code_forms():
	policy = example()
	for form in ("AE", " ae ", "ae", "\tAe\n"):
		assert resolve(policy, make_order(form))["zone"] == "GCC", form
	# anything that is not two letters is not a country: not served, and the Everywhere-else zone
	# does not catch it
	for form in ("UAE", "", " ", "A", "1A", None, 7, "A-E"):
		result = resolve(policy, make_order(form))
		check_shape(result)
		assert (result["offered"], result["reason"], result["zone"], result["rule"]) == (
			False,
			"not_served",
			None,
			None,
		)
		assert result["offers"] == []


def test_select_zone_finds_the_enabled_zone_that_lists_the_country():
	policy = example()
	assert select_zone(policy, "AE") is policy["zones"][1]
	assert select_zone(policy, " ae ") is policy["zones"][1]
	assert select_zone(policy, "BH")["name"] == "Bahrain"
	assert select_zone(policy, "RU")["name"] == "Not shipped"
	# a code in no zone goes to the enabled Everywhere-else zone
	assert select_zone(policy, "JP")["name"] == "Rest of world"
	# ...but a value that is not a country goes nowhere
	for junk in ("", "UAE", None, 5):
		assert select_zone(policy, junk) is None


def test_select_zone_skips_disabled_zones_and_everywhere_else_needs_to_be_enabled():
	policy = example()
	policy["zones"][1]["enabled"] = False  # GCC off: AE has no zone of its own
	assert select_zone(policy, "AE")["name"] == "Rest of world"
	policy["zones"][2]["enabled"] = False  # Everywhere else off too
	assert select_zone(policy, "AE") is None
	assert select_zone(policy, "JP") is None
	assert select_zone(policy, "BH")["name"] == "Bahrain"


def test_a_listed_country_beats_everywhere_else_whatever_the_order_of_the_zones():
	policy = example()
	policy["zones"].reverse()  # Not shipped, Rest of world, GCC, Bahrain
	assert select_zone(policy, "AE")["name"] == "GCC"
	assert select_zone(policy, "JP")["name"] == "Rest of world"
	# an Everywhere-else zone's own country list is ignored: it already catches everything not elsewhere,
	# so even when it is listed first it does not take a country that another zone lists
	policy["zones"][1]["countries"] = ["AE", "JP"]  # Rest of world, now first in the list
	assert select_zone(policy, "AE")["name"] == "GCC"
	assert select_zone(policy, "JP")["name"] == "Rest of world"


def test_select_zone_is_forgiving_about_how_the_policy_spells_a_country():
	policy = example()
	policy["zones"][0]["countries"] = [" bh ", None, 7, "UAE"]
	assert select_zone(policy, "BH")["name"] == "Bahrain"
	policy["zones"][1]["countries"] = None
	assert select_zone(policy, "AE")["name"] == "Rest of world"


def test_the_first_listing_wins_when_a_draft_repeats_a_country():
	"""validate_policy refuses this; resolve must still pick one zone and not crash."""
	policy = example()
	policy["zones"][0]["countries"].append("AE")  # Bahrain is listed before GCC
	assert select_zone(policy, "AE")["name"] == "Bahrain"
	assert resolve(policy, make_order("AE"))["zone"] == "Bahrain"


# ---------------------------------------------------------------- the priced examples
def test_gcc_perfume_order_rounds_the_carrier_price_up():
	result = resolve(example(), make_order())
	check_shape(result)
	assert result["policy_active"] is True
	assert result["zone"] == "GCC"
	assert result["rule"] == 2
	assert result["offered"] is True
	assert result["reason"] is None
	assert result["message"] == ""
	offer = only_offer(result)
	assert (offer["service_id"], offer["name"]) == ("svc", "Express")
	assert offer["amount"] == 39.0
	assert type(offer["amount"]) is float
	assert (offer["source"], offer["reason"]) == ("rule", None)
	assert [(step["kind"], step["amount"]) for step in offer["trace"]] == [
		("carrier", 38.2),
		("rule", None),
		("round", 0.8),
		("result", 39.0),
	]
	texts = {step["kind"]: step["text"] for step in offer["trace"]}
	assert "38.20" in texts["carrier"]
	assert "0.80" in texts["round"]
	assert "39.00" in texts["result"]
	assert "Rule 3" in texts["rule"] and "GCC" in texts["rule"]


def test_selected_items_rule_needs_every_item_in_the_set():
	policy = example()
	watch = make_item("WATCH-1", ["Watches"])
	perfume = make_item()
	only_a_watch = resolve(policy, make_order(items=[watch]))
	assert (only_a_watch["rule"], amount_of(only_a_watch)) == (1, 0.0)
	two_watches = resolve(
		policy, make_order(items=[watch, make_item("WATCH-2", ["Automatic", "Watches"], qty=2)])
	)
	assert (two_watches["rule"], amount_of(two_watches)) == (1, 0.0)
	mixed = resolve(policy, make_order(items=[watch, perfume]))
	assert (mixed["rule"], amount_of(mixed)) == (2, 39.0)
	assert amount_of(resolve(policy, make_order(items=[perfume]))) == 39.0


def test_item_without_groups_matches_a_set_only_by_code():
	policy = example()
	groupless = make_item("WATCH-9", [])
	assert resolve(policy, make_order(items=[groupless]))["rule"] == 2
	policy["item_sets"][0]["items"] = ["WATCH-9"]
	assert resolve(policy, make_order(items=[groupless]))["rule"] == 1
	# an item that has no "groups" key at all behaves the same
	assert resolve(policy, make_order(items=[{"item_code": "WATCH-9", "qty": 1}]))["rule"] == 1
	assert resolve(policy, make_order(items=[make_item("WATCH-8", [])]))["rule"] == 2


def test_bahrain_is_free_for_any_order():
	policy = example()
	watch, perfume = make_item("WATCH-1", ["Watches"]), make_item()
	for items in ([perfume], [watch], [perfume, watch]):
		result = resolve(policy, make_order("BH", items=items, quotes=[make_quote(12.5)]))
		check_shape(result)
		assert (result["zone"], result["rule"]) == ("Bahrain", 0)
		offer = only_offer(result)
		assert offer["amount"] == 0.0
		assert math.copysign(1.0, offer["amount"]) == 1.0
		assert (offer["source"], offer["reason"]) == ("rule", None)
	free = only_offer(resolve(policy, make_order("BH", quotes=[make_quote(12.5)])))
	assert [(step["kind"], step["amount"]) for step in free["trace"]] == [
		("carrier", 12.5),
		("rule", None),
		("result", 0.0),
	]


def test_rest_of_world_formula_with_minimum_and_round_up():
	policy = example()
	cheap = resolve(policy, make_order("US", quotes=[make_quote(38.2)]))
	assert (cheap["zone"], cheap["rule"], amount_of(cheap)) == ("Rest of world", 3, 45.0)
	# 60.00 * 1.1 is 66.00000000000001 in binary floating point: that is 66.00, not 67
	assert amount_of(resolve(policy, make_order("US", quotes=[make_quote(60.0)]))) == 66.0
	kinds = [step["kind"] for step in only_offer(cheap)["trace"]]
	assert kinds == ["carrier", "rule", "formula", "result"]
	formula = only_offer(cheap)["trace"][2]
	assert formula["amount"] == 45.0
	assert "max(carrier_price * 1.1, 45)" in formula["text"] and "45.00" in formula["text"]


def test_not_shipped_zone_returns_its_message():
	policy = example()
	for code in ("RU", "BY"):
		result = resolve(policy, make_order(code))
		check_shape(result)
		assert result["policy_active"] is True
		assert result["zone"] == "Not shipped"
		assert result["rule"] is None
		assert (result["offered"], result["reason"]) == (False, "not_shipped")
		assert result["message"] == "We don't deliver to this country."
		assert result["offers"] == []
	# no quotes needed to refuse, and no fallback however it is set
	assert resolve(policy, make_order("RU", quotes=[]))["reason"] == "not_shipped"
	policy["zones"][3]["not_offered_message"] = "  "
	assert resolve(policy, make_order("RU"))["message"] == DEFAULT_MESSAGES["not_shipped"]
	policy["zones"][3]["not_offered_message"] = "Sorry, not to Russia."
	assert resolve(policy, make_order("RU"))["message"] == "Sorry, not to Russia."
	# a text box adds a newline, which is not part of the message
	policy["zones"][3]["not_offered_message"] = " Sorry, not to Russia.\n"
	assert resolve(policy, make_order("RU"))["message"] == "Sorry, not to Russia."


def test_country_in_no_zone_goes_to_everywhere_else_then_not_covered():
	policy = example()
	japan = resolve(policy, make_order("JP", quotes=[make_quote(38.2)]))
	assert (japan["zone"], japan["rule"], amount_of(japan)) == ("Rest of world", 3, 45.0)

	without_zone(policy, "Rest of world")
	passed = resolve(policy, make_order("JP", quotes=[make_quote(107.610), make_quote(20.0, "b", "Slow")]))
	check_shape(passed)
	assert (passed["zone"], passed["rule"], passed["offered"], passed["reason"], passed["message"]) == (
		None,
		None,
		True,
		None,
		"",
	)
	assert [
		(o["service_id"], o["name"], o["amount"], o["source"], o["reason"]) for o in passed["offers"]
	] == [
		("svc", "Express", 107.61, "carrier", None),
		("b", "Slow", 20.0, "carrier", None),
	]
	assert [step["kind"] for step in passed["offers"][0]["trace"]] == ["carrier", "result"]

	policy["not_covered"] = "no_delivery"
	refused = resolve(policy, make_order("JP"))
	check_shape(refused)
	assert (refused["zone"], refused["rule"], refused["offered"], refused["reason"]) == (
		None,
		None,
		False,
		"not_covered",
	)
	assert refused["message"] == DEFAULT_MESSAGES["not_covered"]
	assert refused["offers"] == []


def test_a_zone_whose_rules_do_not_hold_falls_to_anything_not_covered():
	policy = example()
	policy["rules"] = [rule for rule in policy["rules"] if rule["zone"] != "GCC"]  # GCC has no rule left
	passed = resolve(policy, make_order("AE", quotes=[make_quote(107.610)]))
	assert (passed["zone"], passed["rule"], passed["reason"], passed["message"]) == ("GCC", None, None, "")
	offer = only_offer(passed)
	assert (offer["amount"], offer["source"], offer["reason"]) == (107.61, "carrier", None)

	policy["not_covered"] = "no_delivery"
	refused = resolve(policy, make_order("AE"))
	assert (refused["zone"], refused["offered"], refused["reason"]) == ("GCC", False, "not_covered")
	assert refused["message"] == DEFAULT_MESSAGES["not_covered"]
	policy["zones"][1]["not_offered_message"] = "Please write to us for a delivery price."
	assert resolve(policy, make_order("AE"))["message"] == "Please write to us for a delivery price."

	# a rule that exists but whose band leaves the order out behaves the same
	policy["rules"] = [make_rule("GCC", min_order_value=500.0)]
	assert resolve(policy, make_order("AE", order_value=310.0))["reason"] == "not_covered"
	assert resolve(policy, make_order("AE", order_value=500.0))["rule"] == 0


def test_inactive_policy_returns_quotes_unchanged():
	policy = example()
	quotes = [
		make_quote(38.2, "a", "Express"),
		make_quote(107.615, "b", "Economy", boxes_quoted=0),  # a partial quote is kept
		make_quote(38.999999999, "c", "Slow"),
		make_quote(41, "d", "Whole dollars"),
	]
	order = make_order("AE", quotes=quotes)
	active = resolve(policy, order)
	assert [o["service_id"] for o in active["offers"]] == ["a", "c", "d"]  # (the control: it prices when on)
	assert [o["amount"] for o in active["offers"]] == [39.0, 39.0, 41.0]

	policy["active"] = False
	result = resolve(policy, order)
	check_shape(result)
	assert result["policy_active"] is False
	assert (result["zone"], result["rule"], result["offered"], result["reason"], result["message"]) == (
		None,
		None,
		True,
		None,
		"",
	)
	assert [
		(o["service_id"], o["name"], o["amount"], o["source"], o["reason"]) for o in result["offers"]
	] == [
		("a", "Express", 38.2, "carrier", None),
		("b", "Economy", 107.615, "carrier", None),
		("c", "Slow", 38.999999999, "carrier", None),
		("d", "Whole dollars", 41, "carrier", None),
	]
	assert type(result["offers"][3]["amount"]) is int  # not even the type changes
	assert [step["kind"] for step in result["offers"][0]["trace"]] == ["carrier", "result"]
	# off means off: the country does not have to be served or in any zone
	for country, served in (("RU", True), ("AE", False), ("", True), ("JP", True)):
		other = resolve(policy, make_order(country, quotes=quotes, served=served))
		assert [o["amount"] for o in other["offers"]] == [38.2, 107.615, 38.999999999, 41]
		assert other["policy_active"] is False
	# and with nothing quoted there is nothing to offer
	nothing = resolve(policy, make_order("AE", quotes=[]))
	check_shape(nothing)
	assert (nothing["offered"], nothing["reason"], nothing["offers"]) == (False, None, [])


def test_unserved_country_gets_no_delivery_and_no_fallback():
	policy = example()
	for quotes in ([], [make_quote()]):
		result = resolve(policy, make_order("AE", quotes=quotes, served=False))
		check_shape(result)
		assert (result["offered"], result["reason"], result["offers"]) == (False, "not_served", [])
		assert result["message"] == DEFAULT_MESSAGES["not_served"]
		assert (result["zone"], result["rule"]) == (None, None)  # stops before a zone is looked at
	# the zone's own message is for the zone's own refusals, not for this one
	policy["zones"][1]["not_offered_message"] = "Custom words."
	assert resolve(policy, make_order("AE", served=False))["message"] == DEFAULT_MESSAGES["not_served"]
	# an order that does not say whether it is served is not served
	order = make_order("AE")
	del order["served"]
	assert resolve(policy, order)["reason"] == "not_served"


def test_carrier_unavailable_uses_the_zone_fallback():
	policy = example()
	gcc = resolve(policy, make_order("AE", quotes=[]))
	check_shape(gcc)
	assert (gcc["zone"], gcc["rule"], gcc["offered"], gcc["reason"], gcc["message"]) == (
		"GCC",
		None,
		True,
		"fallback_offer",
		"",
	)
	offer = only_offer(gcc)
	assert (offer["service_id"], offer["name"], offer["amount"], offer["source"], offer["reason"]) == (
		FALLBACK_SERVICE_ID,
		FALLBACK_SERVICE_NAME,
		35.0,
		"fallback",
		"fallback_offer",
	)
	assert [(step["kind"], step["amount"]) for step in offer["trace"]] == [
		("fallback", 35.0),
		("result", 35.0),
	]

	# Bahrain says "use the policy default", which ships as "don't offer delivery"
	bahrain = resolve(policy, make_order("BH", quotes=[]))
	check_shape(bahrain)
	assert (bahrain["zone"], bahrain["offered"], bahrain["reason"], bahrain["offers"]) == (
		"Bahrain",
		False,
		"not_offered",
		[],
	)
	assert bahrain["message"] == DEFAULT_MESSAGES["not_offered"]
	policy["default_fallback"] = {"method": "fixed", "amount": 12.0}
	fixed = resolve(policy, make_order("BH", quotes=[]))
	assert (fixed["reason"], amount_of(fixed)) == ("fallback_offer", 12.0)
	assert only_offer(fixed)["service_id"] == FALLBACK_SERVICE_ID
	policy["default_fallback"] = {"method": "free", "amount": 99.0}
	free = resolve(policy, make_order("BH", quotes=[]))
	assert (free["offered"], free["reason"], amount_of(free)) == (True, "fallback_offer", 0.0)

	# a zone can decide for itself, and then the policy default is not consulted
	policy["zones"][0]["fallback"] = {"method": "no_delivery", "amount": 5.0}
	assert resolve(policy, make_order("BH", quotes=[]))["reason"] == "not_offered"
	policy["zones"][0]["fallback"] = {"method": "fixed", "amount": 7.005}
	assert amount_of(resolve(policy, make_order("BH", quotes=[]))) == 7.01
	policy["zones"][1]["fallback"] = {"method": "free", "amount": 35.0}
	assert amount_of(resolve(policy, make_order("AE", quotes=[]))) == 0.0

	# the zone's own message goes with a declined fallback
	policy["zones"][0]["fallback"] = {"method": "no_delivery", "amount": 0.0}
	policy["zones"][0]["not_offered_message"] = "Please contact us."
	assert resolve(policy, make_order("BH", quotes=[]))["message"] == "Please contact us."


def test_partial_quotes_are_dropped_when_active():
	boxes = [{"weight_kg": 1.2, "volume_l": 3.0}, {"weight_kg": 1.2, "volume_l": 3.0}]
	service_a = make_quote(30.0, "A", "Service A", boxes_quoted=1)
	service_b = make_quote(50.0, "B", "Service B", boxes_quoted=2)
	both = resolve(example(), make_order("AE", boxes=boxes, quotes=[service_a, service_b]))
	check_shape(both)
	assert [(o["service_id"], o["amount"]) for o in both["offers"]] == [("B", 50.0)]
	only_a = resolve(example(), make_order("AE", boxes=boxes, quotes=[service_a]))
	assert (only_a["reason"], only_a["rule"], amount_of(only_a)) == ("fallback_offer", None, 35.0)
	assert only_offer(only_a)["service_id"] == FALLBACK_SERVICE_ID
	# a service that quoted more cartons than there are still counts as whole
	over = make_quote(44.0, "C", "Service C", boxes_quoted=5)
	assert [
		o["service_id"] for o in resolve(example(), make_order("AE", boxes=boxes, quotes=[over]))["offers"]
	] == ["C"]
	# every service partial, and the country has no carrier fallback of its own: not offered
	policy = example()
	policy["zones"][1]["fallback"]["method"] = "policy_default"
	assert resolve(policy, make_order("AE", boxes=boxes, quotes=[service_a]))["reason"] == "not_offered"


def test_unusable_quote_amounts_are_dropped_rather_than_priced():
	quotes = [
		make_quote(float("nan"), "nan"),
		make_quote(float("inf"), "inf"),
		make_quote(-5.0, "negative"),
		make_quote(None, "none"),
		make_quote("38.2", "text"),
		make_quote(True, "bool"),
		make_quote(0.0, "free", "Carrier promotion"),
	]
	result = resolve(example(), make_order("AE", quotes=quotes))
	check_shape(result)
	assert [(o["service_id"], o["amount"]) for o in result["offers"]] == [("free", 0.0)]


def test_things_that_are_not_quotes_are_ignored():
	order = make_order("AE", quotes=[None, 5, "x", make_quote(38.2, "ok")])
	assert [offer["service_id"] for offer in resolve(example(), order)["offers"]] == ["ok"]
	off = example()
	off["active"] = False
	assert [offer["service_id"] for offer in resolve(off, order)["offers"]] == ["ok"]


def test_each_service_is_priced_with_its_own_quote():
	quotes = [
		make_quote(38.2, "a", "Express"),
		make_quote(60.0, "b", "Economy"),
		make_quote(20.0, "c", "Slow"),
	]
	result = resolve(example(), make_order("US", quotes=quotes))
	check_shape(result)
	assert result["rule"] == 3
	assert [(o["service_id"], o["name"], o["amount"]) for o in result["offers"]] == [
		("a", "Express", 45.0),
		("b", "Economy", 66.0),
		("c", "Slow", 45.0),
	]


# ---------------------------------------------------------------- rule conditions
@pytest.mark.parametrize(
	("order_value", "banded"), [(499.99, False), (500.0, True), (999.99, True), (1000.0, False)]
)
def test_bands_include_the_lower_edge_and_exclude_the_upper(order_value, banded):
	policy = with_rules(
		make_rule(method="free", min_order_value=500.0, max_order_value=1000.0),
		make_rule(method="carrier_price"),
	)
	result = resolve(policy, make_order("AE", order_value=order_value))
	assert result["rule"] == (0 if banded else 1)
	assert amount_of(result) == (0.0 if banded else 38.2)
	assert holds(make_order(order_value=order_value), min_order_value=500.0, max_order_value=1000.0) is banded


@pytest.mark.parametrize(
	("low", "high", "measure"),
	[("min_weight_kg", "max_weight_kg", "weight_kg"), ("min_volume_l", "max_volume_l", "volume_l")],
)
@pytest.mark.parametrize(
	("total", "banded"), [(499.99, False), (500.0, True), (999.99, True), (1000.0, False)]
)
def test_weight_and_volume_bands(low, high, measure, total, banded):
	policy = with_rules(
		make_rule(method="free", **{low: 500.0, high: 1000.0}), make_rule(method="carrier_price")
	)
	boxes = [{"weight_kg": 1.0, "volume_l": 1.0, measure: total}]
	result = resolve(policy, make_order("AE", boxes=boxes))
	assert result["rule"] == (0 if banded else 1)


def test_band_totals_add_up_the_cartons_and_ignore_float_noise():
	# 0.7 + 0.1 is 0.7999999999999999 in floating point, which is below an edge of 0.8 that it equals
	# on paper: it would miss a lower edge of 0.8 and slip under an upper edge of 0.8
	boxes = [{"weight_kg": 0.7, "volume_l": 0.7}, {"weight_kg": 0.1, "volume_l": 0.1}]
	order = make_order("AE", boxes=boxes)
	assert holds(order, min_weight_kg=0.8, max_weight_kg=0.9)
	assert holds(order, min_volume_l=0.8, max_volume_l=0.9)
	assert not holds(order, max_weight_kg=0.8)  # "up to 0.8" leaves 0.8 out
	assert not holds(order, max_volume_l=0.8)
	# the total is what is compared, not each carton
	assert holds(make_order(boxes=[{"weight_kg": 3.0, "volume_l": 1.0}] * 2), min_weight_kg=6.0)
	assert not holds(make_order(boxes=[{"weight_kg": 3.0, "volume_l": 1.0}] * 2), max_weight_kg=6.0)


BAND_KEYS = (
	"min_order_value",
	"max_order_value",
	"min_weight_kg",
	"max_weight_kg",
	"min_volume_l",
	"max_volume_l",
)
# What a draft or a stored policy might hold where a limit goes, that is no number the engine can read.
UNREADABLE_LIMITS = ["abc", "5", " ", True, False, [], {}, float("nan"), float("inf"), float("-inf"), 10**400]


@pytest.mark.parametrize("key", BAND_KEYS)
@pytest.mark.parametrize("value", UNREADABLE_LIMITS, ids=repr)
def test_a_limit_that_cannot_be_read_means_the_rule_never_holds(key, value):
	"""An unreadable limit is not "no limit": that would let the rule price orders it was never meant for (free,
	perhaps). Where a setting cannot be read the answer is "no delivery", not a free one."""
	rule = make_rule(method="free", **{key: value})
	huge = make_order("AE", order_value=1e9, boxes=[{"weight_kg": 500.0, "volume_l": 800.0}])
	tiny = make_order("AE", order_value=0.0, boxes=[], quotes=[make_quote(boxes_quoted=0)])
	for order in (huge, tiny, make_order("AE")):
		assert not rule_matches(rule, order, [])
	# the order falls to Anything not covered, as it does when no rule holds ...
	policy = with_rules(rule)
	result = resolve(policy, make_order("AE"))
	assert (result["rule"], amount_of(result), only_offer(result)["source"]) == (None, 38.2, "carrier")
	policy["not_covered"] = "no_delivery"
	result = resolve(policy, make_order("AE"))
	assert (result["offered"], result["reason"], result["rule"]) == (False, "not_covered", None)
	# ... or to the next rule, which is free to hold
	later = with_rules(rule, make_rule(method="formula", formula="7"))
	result = resolve(later, make_order("AE"))
	assert (result["rule"], amount_of(result)) == (1, 7.0)
	# and the preview says the same
	assert [p["matches"] for p in rule_previews(later, make_order("AE"))] == [False, True]


@pytest.mark.parametrize("key", BAND_KEYS)
@pytest.mark.parametrize("empty", [None, "", 0, 0.0])
def test_an_empty_or_zero_limit_is_still_no_limit(key, empty):
	rule = make_rule(method="free", **{key: empty})
	assert rule_matches(rule, make_order("AE"), [])
	assert rule_matches(rule, make_order("AE", order_value=0.0, boxes=[], quotes=[]), [])


@pytest.mark.parametrize("value", UNREADABLE_LIMITS, ids=repr)
def test_an_unreadable_limit_is_reported_once_and_is_left_out_of_the_wrong_way_round_check(value):
	"""validate_policy still says "not a number" for it, and it cannot also make a band look inverted."""
	for low_key, high_key in (
		("min_order_value", "max_order_value"),
		("min_weight_kg", "max_weight_kg"),
		("min_volume_l", "max_volume_l"),
	):
		for unreadable, other in ((low_key, high_key), (high_key, low_key)):
			policy = example()
			policy["rules"][2].update({unreadable: value, other: 5.0})
			problems = validate_policy(policy)
			assert len(problems) == 1, (unreadable, problems)
			assert "Rule 3" in problems[0] and "not a number" in problems[0], problems


@pytest.mark.parametrize("empty", [0.0, 0, None, "", "absent"])
def test_zero_means_no_limit(empty):
	rule = make_rule(method="free")
	for key in (
		"min_order_value",
		"max_order_value",
		"min_weight_kg",
		"max_weight_kg",
		"min_volume_l",
		"max_volume_l",
	):
		if empty == "absent":
			del rule[key]
		else:
			rule[key] = empty
	policy = with_rules(rule, make_rule())
	huge = make_order("AE", order_value=1e9, boxes=[{"weight_kg": 500.0, "volume_l": 800.0}])
	tiny = make_order("AE", order_value=0.0, boxes=[], quotes=[make_quote(boxes_quoted=0)])
	for order in (huge, tiny, make_order("AE")):
		assert resolve(policy, order)["rule"] == 0
		assert rule_matches(rule, order, policy["item_sets"])


def test_a_band_can_have_only_one_edge():
	for value, only_from, only_up_to in (
		(0.0, False, True),
		(499.99, False, True),
		(500.0, True, False),
		(1e9, True, False),
	):
		order = make_order(order_value=value)
		assert holds(order, min_order_value=500.0) is only_from, value
		assert holds(order, max_order_value=500.0) is only_up_to, value


def test_first_matching_rule_wins_disabled_rules_and_other_zones_are_skipped():
	policy = with_rules(
		make_rule("Bahrain", method="free"),  # another zone
		make_rule("GCC", enabled=False, method="free"),  # disabled
		make_rule("GCC", method="formula", formula="7"),  # the first that holds
		make_rule("GCC", method="free"),  # holds too, but never reached
	)
	result = resolve(policy, make_order("AE"))
	assert (result["rule"], amount_of(result)) == (2, 7.0)
	policy["rules"][2]["enabled"] = False
	result = resolve(policy, make_order("AE"))
	assert (result["rule"], amount_of(result)) == (3, 0.0)
	# a rule whose zone is spelled differently is for a different zone: the names are compared exactly,
	# so nothing holds for GCC and the quote passes through unchanged
	policy["rules"][3]["zone"] = "gcc"
	result = resolve(policy, make_order("AE"))
	assert (result["rule"], amount_of(result), only_offer(result)["source"]) == (None, 38.2, "carrier")
	# a rule that holds only for another band is skipped in favour of the next
	policy = with_rules(
		make_rule(method="free", min_order_value=1000.0), make_rule(method="formula", formula="9")
	)
	assert resolve(policy, make_order("AE", order_value=310.0))["rule"] == 1


def test_rule_matches_judges_the_item_set_by_group_or_code_ignoring_case_and_spaces():
	sets = example()["item_sets"]  # Watches: the group Watches
	selected = make_rule(item_set="Watches")

	def order_of(*items):
		return make_order("AE", items=list(items))

	assert rule_matches(selected, order_of(make_item("W1", ["watches"])), sets)
	assert rule_matches(selected, order_of(make_item("W1", ["  WATCHES "])), sets)
	assert rule_matches(selected, order_of(make_item("W1", ["Automatic", "Watches"])), sets)
	assert not rule_matches(selected, order_of(make_item("W1", ["Watches"]), make_item()), sets)
	assert not rule_matches(selected, order_of(make_item("W1", ["Straps"])), sets)
	# an order with no items does not pick up a selected-items rule; a rule for every item takes any order
	assert not rule_matches(selected, order_of(), sets)
	assert rule_matches(make_rule(), order_of(), sets)
	assert rule_matches(make_rule(item_set=""), order_of(make_item()), sets)
	assert rule_matches(make_rule(item_set=None), order_of(make_item()), sets)
	# a set that does not exist never matches
	assert not rule_matches(make_rule(item_set="Pens"), order_of(make_item()), sets)
	assert not rule_matches(selected, order_of(make_item("W1", ["Watches"])), [])
	# by code
	by_code = [{"name": "Codes", "item_groups": [], "items": [" w-1 "]}]
	assert rule_matches(make_rule(item_set="Codes"), order_of(make_item("W-1", [])), by_code)
	assert not rule_matches(make_rule(item_set="Codes"), order_of(make_item("W-2", [])), by_code)
	# a set with nothing in it, or only blanks, holds no item
	for blank in ({"item_groups": [], "items": []}, {"item_groups": [""], "items": ["  "]}):
		empty = [{"name": "Empty", **blank}]
		assert not rule_matches(make_rule(item_set="Empty"), order_of(make_item("", [""])), empty)
		assert not rule_matches(make_rule(item_set="Empty"), order_of(make_item()), empty)


def test_rule_matches_judges_the_conditions_only():
	"""Whether a rule is enabled and in the right zone is for the caller, which has the zone to hand."""
	order = make_order("AE")
	assert rule_matches(make_rule("Nowhere", enabled=False), order, [])
	assert not rule_matches(make_rule("Nowhere", enabled=False, min_order_value=1000.0), order, [])


def test_formula_variables_are_the_order_totals():
	policy = example()
	order = make_order(
		"AE",
		items=[make_item(qty=2), make_item("P2", qty=3)],
		order_value=250.0,
		boxes=[{"weight_kg": 1.25, "volume_l": 2.5}, {"weight_kg": 0.75, "volume_l": 3.5}],
		quotes=[make_quote(20.0, boxes_quoted=2)],
	)
	expected = {
		"weight_kg": 2.0,  # the cartons' weights added up
		"volume_l": 6.0,  # the cartons' volumes added up
		"boxes": 2.0,  # the number of cartons
		"item_count": 5.0,  # the quantities added up: 2 + 3
		"order_value": 250.0,
		"carrier_price": 20.0,
	}
	for name, value in expected.items():
		policy["rules"][2] = make_rule(method="formula", formula=name)
		assert amount_of(resolve(policy, order)) == value, name
	policy["rules"][2] = make_rule(method="formula", formula="weight_kg * 6 + 8")
	assert amount_of(resolve(policy, order)) == 20.0


# ---------------------------------------------------------------- failures, money and edges
def test_a_failing_formula_goes_to_the_fallback_never_to_zero():
	# validate_policy would refuse this formula (it divides by zero on the sample order, whose value is
	# 310), so it is built by hand and priced directly
	policy = example()
	policy["rules"][2] = make_rule(method="formula", formula="carrier_price / (order_value - 310)")
	result = resolve(policy, make_order("AE", order_value=310.0))
	check_shape(result)
	assert (result["zone"], result["rule"], result["offered"], result["reason"], result["message"]) == (
		"GCC",
		2,  # the rule that held but could not be priced
		True,
		"formula_error",
		"",
	)
	offer = only_offer(result)
	assert (offer["service_id"], offer["name"], offer["amount"]) == (
		FALLBACK_SERVICE_ID,
		FALLBACK_SERVICE_NAME,
		35.0,
	)
	assert (offer["source"], offer["reason"]) == ("fallback", "formula_error")
	assert [step["kind"] for step in offer["trace"]] == ["fallback", "result"]
	assert "divide by zero" in offer["trace"][0]["text"] and "Rule 3" in offer["trace"][0]["text"]
	# the same formula prices when it can
	assert amount_of(resolve(policy, make_order("AE", order_value=320.0))) == 3.82


def test_a_failing_formula_with_a_declined_or_free_fallback():
	policy = example()
	policy["rules"][2] = make_rule(method="formula", formula="carrier_price / (order_value - 310)")
	policy["zones"][1]["not_offered_message"] = "Custom words."  # not for this reason
	policy["zones"][1]["fallback"] = {"method": "no_delivery", "amount": 0.0}
	declined = resolve(policy, make_order("AE", order_value=310.0))
	check_shape(declined)
	assert (declined["offered"], declined["reason"], declined["offers"]) == (False, "formula_error", [])
	assert declined["message"] == DEFAULT_MESSAGES["formula_error"]
	assert (declined["zone"], declined["rule"]) == ("GCC", 2)
	policy["zones"][1]["fallback"] = {"method": "free", "amount": 0.0}
	free = resolve(policy, make_order("AE", order_value=310.0))
	assert (free["reason"], amount_of(free), only_offer(free)["reason"]) == (
		"formula_error",
		0.0,
		"formula_error",
	)
	policy["zones"][1]["fallback"] = {"method": "policy_default", "amount": 0.0}
	policy["default_fallback"] = {"method": "fixed", "amount": 12.0}
	assert amount_of(resolve(policy, make_order("AE", order_value=310.0))) == 12.0


@pytest.mark.parametrize(
	"formula",
	[
		"carrier_price > 5",  # a yes/no answer
		"carrier_price ** 2",  # not in the language
		"unknown_name",
		"",
		"   ",
		"carrier_price * 1e308 * 10",  # overflows to infinity
		"carrier_price / 0",
		"1 if",
	],
)
def test_any_formula_that_cannot_be_calculated_is_a_formula_error(formula):
	policy = example()
	policy["rules"][2] = make_rule(method="formula", formula=formula)
	result = resolve(policy, make_order("AE"))
	check_shape(result)
	assert (result["reason"], amount_of(result)) == ("formula_error", 35.0)


def test_a_formula_that_fails_for_one_service_sends_the_whole_order_to_the_fallback():
	policy = example()
	policy["rules"][2] = make_rule(method="formula", formula="10 / (carrier_price - 40)")
	quotes = [make_quote(38.2, "a"), make_quote(40.0, "b"), make_quote(50.0, "c")]
	result = resolve(policy, make_order("AE", quotes=quotes))
	assert result["reason"] == "formula_error"
	assert [(o["service_id"], o["amount"]) for o in result["offers"]] == [(FALLBACK_SERVICE_ID, 35.0)]
	# without the troublesome service the others price, each with its own quote
	result = resolve(policy, make_order("AE", quotes=[quotes[0], quotes[2]]))
	assert [(o["service_id"], o["amount"]) for o in result["offers"]] == [("a", 0.0), ("c", 1.0)]


def test_a_rule_that_does_not_say_how_to_charge_cannot_price_and_goes_to_the_fallback():
	policy = example()
	policy["rules"][2] = make_rule(method="discount")
	result = resolve(policy, make_order("AE"))
	assert (result["reason"], amount_of(result)) == ("formula_error", 35.0)


def test_a_fallback_that_cannot_be_read_offers_no_delivery_rather_than_a_free_one():
	policy = example()
	for fallback in (
		{"method": "fixed", "amount": -5.0},
		{"method": "fixed", "amount": None},
		{"method": "fixed"},
		{"method": "fixed", "amount": "abc"},
		{"method": "fixed", "amount": float("nan")},
		{"method": "surprise", "amount": 5.0},
	):
		policy["zones"][1]["fallback"] = fallback
		result = resolve(policy, make_order("AE", quotes=[]))
		check_shape(result)
		assert (result["offered"], result["reason"]) == (False, "not_offered"), fallback
	# a fixed amount of zero is a real choice (free), and a missing method is the zone's default
	policy["zones"][1]["fallback"] = {"method": "fixed", "amount": 0.0}
	assert amount_of(resolve(policy, make_order("AE", quotes=[]))) == 0.0
	policy["zones"][1]["fallback"] = {}
	assert resolve(policy, make_order("AE", quotes=[]))["reason"] == "not_offered"  # the policy default


def test_without_a_zone_and_without_quotes_the_policy_default_decides():
	policy = without_zone(example(), "Rest of world")
	nothing = resolve(policy, make_order("JP", quotes=[]))  # pass-through has nothing to pass
	check_shape(nothing)
	assert (nothing["zone"], nothing["offered"], nothing["reason"]) == (None, False, "not_offered")
	policy["default_fallback"] = {"method": "fixed", "amount": 12.0}
	fixed = resolve(policy, make_order("JP", quotes=[]))
	assert (fixed["zone"], fixed["reason"], amount_of(fixed)) == (None, "fallback_offer", 12.0)
	policy["default_fallback"] = {"method": "free", "amount": 12.0}
	assert amount_of(resolve(policy, make_order("JP", quotes=[]))) == 0.0
	# "don't offer" for anything not covered is checked first
	policy["not_covered"] = "no_delivery"
	assert resolve(policy, make_order("JP", quotes=[]))["reason"] == "not_covered"
	# an unserved country is still unserved
	assert resolve(policy, make_order("JP", quotes=[], served=False))["reason"] == "not_served"


def test_the_zones_own_message_replaces_the_default_for_three_reasons_only():
	policy = example()
	for zone in policy["zones"]:
		zone["not_offered_message"] = "Custom words."
	policy["zones"][1]["fallback"] = {"method": "no_delivery", "amount": 0.0}
	policy["rules"][2] = make_rule(method="formula", formula="1 if")
	assert resolve(policy, make_order("RU"))["message"] == "Custom words."  # not_shipped
	assert resolve(policy, make_order("AE", quotes=[]))["message"] == "Custom words."  # not_offered
	policy["rules"] = []
	policy["not_covered"] = "no_delivery"
	assert resolve(policy, make_order("AE"))["message"] == "Custom words."  # not_covered
	policy["rules"] = [make_rule(method="formula", formula="1 if")]
	failed = resolve(policy, make_order("AE"))
	assert failed["reason"] == "formula_error" and failed["message"] == DEFAULT_MESSAGES["formula_error"]
	assert resolve(policy, make_order("AE", served=False))["message"] == DEFAULT_MESSAGES["not_served"]
	# a result that offers delivery has no message, whichever offer it is
	assert resolve(example(), make_order("AE", quotes=[]))["message"] == ""
	assert resolve(example(), make_order("AE"))["message"] == ""


def test_money_edges():
	policy = example()  # GCC rule 3 (index 2): the carrier price, rounded up
	assert price(policy, 38.999999999) == 39.0
	assert price(policy, 39.0) == 39.0
	assert price(policy, 0) == 0.0
	assert price(policy, 0.004) == 0.0  # to cents first (0.00), then up: zero stays zero
	assert price(policy, 66.00000000000001) == 66.0
	assert price(policy, 66.005) == 67.0  # half a cent rounds up to 66.01, which is not whole
	# a formula that goes below zero prices at zero, and not at minus zero
	for round_up in (True, False):
		policy["rules"][2] = make_rule(method="formula", formula="carrier_price - 100", round_up=round_up)
		amount = price(policy, 38.2)
		assert amount == 0.0 and math.copysign(1.0, amount) == 1.0
		assert price(policy, 138.2) == (39.0 if round_up else 38.2)
	policy["rules"][2] = make_rule(method="formula", formula="0 * -1", round_up=True)
	assert math.copysign(1.0, price(policy, 38.2)) == 1.0
	# a pass-through price is rounded to cents, half up, from the decimal a person reads
	policy["rules"][2] = make_rule()
	assert price(policy, 107.610) == 107.61
	assert price(policy, 107.614) == 107.61
	assert price(policy, 107.615) == 107.62
	assert price(policy, 1.005) == 1.01
	# with neither GCC nor Everywhere else, AE is in no zone and the carrier's price passes through
	without_zone(without_zone(policy, "GCC"), "Rest of world")
	assert price(policy, 107.610) == 107.61
	assert price(policy, 2.675) == 2.68


def test_empty_boxes_still_price():
	order = make_order("AE", boxes=[], quotes=[make_quote(38.2, boxes_quoted=0)])
	result = resolve(example(), order)
	check_shape(result)
	assert (result["rule"], amount_of(result)) == (2, 39.0)
	# with no cartons a quote does not have to say how many it covered
	unsaid = {"service_id": "svc", "name": "Express", "amount": 38.2}
	assert amount_of(resolve(example(), make_order("AE", boxes=[], quotes=[unsaid]))) == 39.0
	# weight and volume compare as zero, so an upper band holds and a lower one does not
	assert holds(order, max_weight_kg=5.0, max_volume_l=5.0)
	assert not holds(order, min_weight_kg=1.0)
	assert not holds(order, min_volume_l=1.0)
	# a formula sees no weight, no volume and no cartons
	policy = example()
	policy["rules"][2] = make_rule(method="formula", formula="weight_kg + volume_l + boxes")
	assert amount_of(resolve(policy, order)) == 0.0


def test_an_order_with_no_items_still_prices_under_a_rule_for_every_item():
	result = resolve(example(), make_order("AE", items=[]))
	assert (result["rule"], amount_of(result)) == (2, 39.0)  # the Watches rule (1) does not pick it up


# ---------------------------------------------------------------- rounding functions
@pytest.mark.parametrize(
	("amount", "cents"),
	[
		(1.005, 1.01),  # half a cent goes up, read as the decimal 1.005 and not the binary approximation
		(2.675, 2.68),
		(38.2, 38.2),
		(107.610, 107.61),
		(107.614999, 107.61),
		(107.615, 107.62),
		(0.004, 0.0),
		(0.005, 0.01),
		(0, 0.0),
		(0.0, 0.0),
		(5, 5.0),
		(66.00000000000001, 66.0),
		(-1.5, 0.0),  # never negative
		(-0.0, 0.0),
		(-0.004, 0.0),
	],
)
def test_to_cents(amount, cents):
	result = to_cents(amount)
	assert result == cents
	assert type(result) is float
	assert math.copysign(1.0, result) == 1.0


@pytest.mark.parametrize(
	("amount", "dollars"),
	[
		(38.2, 39.0),
		(38.999999999, 39.0),
		(39.0, 39.0),
		(39, 39.0),
		(0, 0.0),
		(0.0, 0.0),
		(0.004, 0.0),  # cents first: 0.00, which is already whole
		(0.005, 1.0),
		(66.00000000000001, 66.0),
		(66.004, 66.0),
		(66.005, 67.0),
		(-3, 0.0),
		(-0.0, 0.0),
	],
)
def test_round_up_dollar(amount, dollars):
	result = round_up_dollar(amount)
	assert result == dollars
	assert type(result) is float
	assert math.copysign(1.0, result) == 1.0


@pytest.mark.parametrize("amount", [1e300, sys.float_info.max, 1e22, 123456789012345678.0, 5e-324, 1e-300])
def test_rounding_never_raises_for_a_finite_float(amount):
	"""Decimal.quantize raises past 28 digits unless it is given room, and a guest can post 1e300."""
	assert to_cents(amount) == pytest.approx(amount)
	assert round_up_dollar(amount) == pytest.approx(amount, abs=1.0)
	assert type(to_cents(amount)) is float and type(round_up_dollar(amount)) is float


@pytest.mark.parametrize(
	"junk",
	[
		float("nan"),
		float("inf"),
		float("-inf"),
		None,
		"38.2",
		True,
		[1],
		# an integer or a Decimal too big for a float: it is no finite number, and never an infinity or an
		# InvalidOperation (10**5000 has more digits than str() will make of an integer)
		10**350,
		-(10**350),
		10**500,
		10**5000,
		Decimal("1e400"),
		Decimal("-1e400"),
		Decimal("sNaN"),
	],
	ids=lambda junk: "huge" if isinstance(junk, int) and abs(junk) > 10**300 else repr(junk),
)
def test_rounding_refuses_what_is_not_a_finite_number(junk):
	with pytest.raises(ValueError):
		to_cents(junk)
	with pytest.raises(ValueError):
		round_up_dollar(junk)


HUGE_AMOUNTS = [10**350, -(10**350), 10**400, 10**5000, Decimal("1e400")]


@pytest.mark.parametrize(
	"amount", HUGE_AMOUNTS, ids=["10**350", "-(10**350)", "10**400", "10**5000", "1e400"]
)
def test_a_quote_too_big_for_a_float_is_handed_back_when_off_and_dropped_when_on(amount):
	"""Off, the quotes come back as posted, whatever they hold, and writing the trace line for one must not
	raise. On, it is no usable price (a service that gave no price), like a NaN."""
	big, fine = make_quote(amount, "big", "Huge"), make_quote(38.2, "ok", "Express")
	order = make_order("AE", quotes=[big, fine])
	off = {**example(), "active": False}
	result = resolve(
		off, order
	)  # (not check_shape: it wants int and float amounts, and this hands back a Decimal)
	assert (result["policy_active"], result["offered"]) == (False, True)
	assert [o["service_id"] for o in result["offers"]] == ["big", "ok"]
	assert result["offers"][0]["amount"] is amount, "handed back exactly as it was posted"
	for text in (step["text"] for offer in result["offers"] for step in offer["trace"]):
		assert isinstance(text, str) and text
	# on, only the quote that is a price is priced (GCC: the carrier price rounded up)
	result = resolve(example(), order)
	check_shape(result)
	assert [(o["service_id"], o["amount"]) for o in result["offers"]] == [("ok", 39.0)]
	# and with nothing else to quote, it is as if the carrier had given no price (here GCC's fixed fallback)
	alone = resolve(example(), make_order("AE", quotes=[big]))
	check_shape(alone)
	assert alone == resolve(example(), make_order("AE", quotes=[make_quote(float("nan"))]))
	assert (alone["reason"], amount_of(alone)) == ("fallback_offer", 35.0)


# str() and an f-string refuse an integer of more than 4300 digits, and a draft or a stored policy may hold
# anything, so every sentence the engine writes about a value of the policy has to cope with one.
VAST = 10**5000
TOO_LARGE_TO_SHOW = "a number too large to show"


def test_a_policy_holding_a_vast_integer_anywhere_is_priced_previewed_and_checked_without_raising():
	orders = [
		make_order("AE"),
		make_order("BH"),
		make_order("US", quotes=[make_quote(60.0)]),
		make_order("AE", items=[make_item("W", ["Watches"])]),
		make_order("JP", quotes=[]),
		make_order("AE", served=False),
	]
	places = 0
	for base in (
		example(),
		card_policy(),
		card_policies(sample_card())["carrier with the card as its fallback"],
	):
		for path in list(paths_in(base)):
			policy = copy.deepcopy(base)
			follow(policy, path[:-1])[path[-1]] = VAST
			places += 1
			for order in orders:
				check_shape(resolve(policy, order))
				for preview in rule_previews(policy, order):
					assert set(preview) == {"index", "in_zone", "matches", "amount"}
			problems = validate_policy(policy)
			assert all(isinstance(problem, str) and problem for problem in problems), path
	assert places > 300, "every place of three policies was tried"


def test_a_vast_integer_is_shown_as_a_phrase_in_the_sentences_that_name_it():
	"""One policy holds it in each place a sentence quotes a value: a rule's label, zone and item set, the
	countries of an Everywhere-else zone, a country, and the zone or item set a rule names."""
	policy = example()
	policy["zones"][1]["name"] = VAST  # GCC, now named by the number (its rule 3 names it too)
	policy["rules"][1]["zone"] = VAST
	policy["rules"][2].update(zone=VAST, label=VAST)
	policy["rules"][1]["item_set"] = VAST
	policy["item_sets"][0]["name"] = VAST
	result = resolve(policy, make_order("AE"))
	check_shape(result)
	text = " ".join(step["text"] for step in only_offer(result)["trace"])
	assert f'Rule 3 ("{TOO_LARGE_TO_SHOW}") applies: {TOO_LARGE_TO_SHOW}, every item' in text, text
	watches = resolve(policy, make_order("AE", items=[make_item("W", ["Watches"])]))
	assert f"selected items ({TOO_LARGE_TO_SHOW})" in " ".join(
		step["text"] for step in only_offer(watches)["trace"]
	)
	# the sentences of validate_policy
	policy = example()
	policy["rules"][0]["zone"] = VAST  # a zone that does not exist
	policy["rules"][1]["item_set"] = VAST  # an item set that does not exist
	policy["zones"][1]["countries"].append(VAST)  # not a country code
	policy["zones"][2]["countries"].append(VAST)  # Rest of world is Everywhere else, so it cannot list any
	problems = validate_policy(policy)
	assert len(problems) == 5, (
		problems
	)  # (the Everywhere-else zone gets two: not a code, and not allowed to list)
	assert [TOO_LARGE_TO_SHOW in problem for problem in problems] == [True] * 5, problems
	assert any(problem.startswith('Zone "Rest of world" is set to Everywhere else') for problem in problems)


def test_an_item_whose_code_or_group_is_a_vast_integer_matches_no_item_set():
	"""It has no text to compare, so it names nothing: it neither raises nor matches a blank entry."""
	sets = [{"name": "Odd", "item_groups": [VAST, ""], "items": [VAST, "  "]}]
	selected = make_rule(item_set="Odd")
	for item in (make_item(VAST, [VAST]), make_item("", [""]), make_item(None, [None])):
		assert not rule_matches(selected, make_order("AE", items=[item]), sets), item
	policy = example()
	policy["item_sets"][0]["item_groups"] = [VAST]
	problems = validate_policy(policy)
	assert len(problems) == 1 and "Watches" in problems[0] and "no item group and no item" in problems[0]


# ---------------------------------------------------------------- previews
def test_rule_previews_give_each_rule_its_price_for_the_sample_order():
	previews = rule_previews(example(), make_order("AE"))
	assert [preview["index"] for preview in previews] == [0, 1, 2, 3]
	assert all(set(preview) == {"index", "in_zone", "matches", "amount"} for preview in previews)
	assert previews[0]["in_zone"] is False and previews[0]["amount"] is None
	assert previews[1] == {"index": 1, "in_zone": True, "matches": False, "amount": None}
	assert previews[2] == {"index": 2, "in_zone": True, "matches": True, "amount": 39.0}
	assert previews[3]["in_zone"] is False and previews[3]["amount"] is None
	# the Rest of world rule is the one that shows a price for a US order
	us = rule_previews(example(), make_order("US", quotes=[make_quote(38.2)]))
	assert [(p["in_zone"], p["amount"]) for p in us] == [
		(False, None),
		(False, None),
		(False, None),
		(True, 45.0),
	]


def test_rule_previews_price_every_rule_that_would_apply_not_only_the_winner():
	policy = with_rules(make_rule(method="free"), make_rule(round_up=True))
	previews = rule_previews(policy, make_order("AE"))
	assert [(p["in_zone"], p["matches"], p["amount"]) for p in previews] == [
		(True, True, 0.0),
		(True, True, 39.0),
	]
	assert resolve(policy, make_order("AE"))["rule"] == 0  # (only the first is used)


def test_rule_previews_show_no_price_for_a_rule_that_cannot_apply():
	policy = with_rules(
		make_rule(enabled=False, method="free"),
		make_rule(method="formula", formula="carrier_price / 0"),
		make_rule("Nowhere"),
		make_rule(item_set="Pens"),
	)
	previews = rule_previews(policy, make_order("AE"))
	assert previews[0] == {"index": 0, "in_zone": True, "matches": False, "amount": None}
	assert previews[1] == {"index": 1, "in_zone": True, "matches": True, "amount": None}  # cannot be priced
	assert previews[2]["in_zone"] is False
	assert previews[3] == {"index": 3, "in_zone": True, "matches": False, "amount": None}
	# a rule that names no zone, or a country in no zone, is in no zone
	assert rule_previews(with_rules(make_rule(None)), make_order("AE"))[0]["in_zone"] is False
	assert (
		rule_previews(
			without_zone(with_rules(make_rule("Rest of world")), "Rest of world"), make_order("JP")
		)[0]["in_zone"]
		is False
	)


def test_rule_previews_follow_what_resolve_would_do_with_the_order():
	policy = example()

	def rows(order):
		return [preview["amount"] for preview in rule_previews(policy, order)]

	assert rows(make_order("AE"))[2] == 39.0
	assert rows(make_order("AE", served=False)) == [None] * 4  # not served: no rule would be used
	assert rows(make_order("AE", quotes=[])) == [None] * 4  # the carrier cannot quote: rules are not used
	partial = make_quote(30.2, "a", boxes_quoted=0)
	assert rows(make_order("AE", quotes=[partial])) == [None] * 4
	# the first usable quote is the one that is priced
	assert rows(make_order("AE", quotes=[partial, make_quote(50.2, "b"), make_quote(10.2, "c")]))[2] == 51.0
	# a Not shipped zone never prices, even for a rule written for it
	policy["rules"].append(make_rule("Not shipped", method="free"))
	previews = rule_previews(policy, make_order("RU"))
	assert previews[4] == {"index": 4, "in_zone": True, "matches": True, "amount": None}
	# the policy being off does not matter: a draft is previewed before it goes live
	policy["active"] = False
	assert rows(make_order("AE"))[2] == 39.0


def test_pricing_and_checking_leave_the_policy_and_the_order_alone():
	policy = example()
	order = make_order("AE", quotes=[make_quote(38.2), make_quote(60.0, "b")])
	policy_before, order_before = copy.deepcopy(policy), copy.deepcopy(order)
	resolve(policy, order)
	rule_previews(policy, order)
	validate_policy(policy)
	for country in ("BH", "RU", "US", "ZZ", ""):
		resolve(policy, {**order, "country_code": country})
	policy["active"] = False
	resolve(policy, order)
	policy["active"] = True
	assert policy == policy_before
	assert order == order_before


# ---------------------------------------------------------------- traces
def test_traces_tell_the_story_of_each_price():
	"""The Try an order panel prints these lines, so each outcome has its own sequence of steps."""
	policy = example()

	def story(result, index=0):
		return [(step["kind"], step["amount"]) for step in result["offers"][index]["trace"]]

	# a rule that rounds, but finds the price already whole, shows no round step
	whole = resolve(policy, make_order("AE", quotes=[make_quote(39.0)]))
	assert story(whole) == [("carrier", 39.0), ("rule", None), ("result", 39.0)]
	# a formula that rounds shows the formula, then the rounding
	policy["rules"][3]["formula"] = "carrier_price * 1.1"
	rounded = resolve(policy, make_order("US", quotes=[make_quote(40.0)]))
	assert story(rounded) == [("carrier", 40.0), ("rule", None), ("formula", 44.0), ("result", 44.0)]
	rounded = resolve(policy, make_order("US", quotes=[make_quote(40.5)]))
	assert story(rounded) == [
		("carrier", 40.5),
		("rule", None),
		("formula", 44.55),
		("round", 0.45),
		("result", 45.0),
	]
	# a formula below zero says so
	policy["rules"][3]["formula"] = "carrier_price - 100"
	negative = only_offer(resolve(policy, make_order("US", quotes=[make_quote(38.2)])))
	assert [(s["kind"], s["amount"]) for s in negative["trace"]] == [
		("carrier", 38.2),
		("rule", None),
		("formula", 0.0),
		("result", 0.0),
	]
	assert "-61.80" in negative["trace"][2]["text"]
	# the rule line names the rule, its zone, its items and how it charges
	rule_line = only_offer(resolve(example(), make_order("AE", items=[make_item("W", ["Watches"])])))[
		"trace"
	][1]["text"]
	assert "Rule 2" in rule_line and "GCC" in rule_line and "Watches" in rule_line and "free" in rule_line
	labelled = example()
	labelled["rules"][2]["label"] = "Gulf at cost"
	assert "Gulf at cost" in only_offer(resolve(labelled, make_order("AE")))["trace"][1]["text"]


# ---------------------------------------------------------------- an inconsistent policy never raises
def broken_policies():
	"""Policies a half-finished draft or a careless caller could hold. Pricing must cope with all of them."""

	def mutated(change):
		policy = example()
		change(policy)
		return policy

	cases = {
		"no policy": lambda: None,
		"empty policy": lambda: {},
		"a string for a policy": lambda: "policy",
		"rules and zones are not lists": lambda: mutated(
			lambda p: p.update(rules=None, zones="x", item_sets=5)
		),
		"entries that are not dicts": lambda: mutated(
			lambda p: p.update(rules=[None, 5, "x", []], zones=[None, 5])
		),
		"rule names a missing zone": lambda: mutated(lambda p: p["rules"][2].update(zone="Nowhere")),
		"rule names no zone": lambda: mutated(lambda p: p["rules"][2].update(zone=None)),
		"rule names a missing item set": lambda: mutated(lambda p: p["rules"][2].update(item_set="Pens")),
		"bad formula": lambda: mutated(lambda p: p["rules"][3].update(formula="carrier_price **")),
		"formula is not text": lambda: mutated(lambda p: p["rules"][3].update(formula=None)),
		"duplicate zone names": lambda: mutated(lambda p: p["zones"].append(copy.deepcopy(p["zones"][1]))),
		"duplicate countries": lambda: mutated(lambda p: p["zones"][0]["countries"].extend(["AE", "US"])),
		"two Everywhere-else zones": lambda: mutated(lambda p: p["zones"][0].update(everywhere_else=True)),
		"None limits": lambda: mutated(
			lambda p: p["rules"][2].update(min_order_value=None, max_weight_kg=None)
		),
		"text limits": lambda: mutated(
			lambda p: p["rules"][2].update(min_order_value="abc", max_volume_l="9")
		),
		"negative limits": lambda: mutated(
			lambda p: p["rules"][2].update(min_order_value=-5.0, max_weight_kg=-1.0)
		),
		"inverted band": lambda: mutated(
			lambda p: p["rules"][2].update(min_order_value=500.0, max_order_value=100.0)
		),
		"missing keys": lambda: mutated(lambda p: [r.clear() for r in p["rules"]]),
		"zones without keys": lambda: mutated(lambda p: [z.clear() for z in p["zones"]]),
		"fallback is not a dict": lambda: mutated(lambda p: p["zones"][1].update(fallback="fixed")),
		"default fallback is junk": lambda: mutated(lambda p: p.update(default_fallback=[1, 2])),
		"countries is a string": lambda: mutated(lambda p: p["zones"][1].update(countries="AE")),
		"zone name is a list": lambda: mutated(lambda p: p["zones"][0].update(name=["Bahrain"])),
		"rule zone is a list": lambda: mutated(lambda p: p["rules"][0].update(zone=["Bahrain"])),
		"rule item set is a dict": lambda: mutated(lambda p: p["rules"][1].update(item_set={"x": 1})),
		"item set name is a list": lambda: mutated(lambda p: p["item_sets"][0].update(name=["Watches"])),
		"item codes are numbers": lambda: mutated(lambda p: p["item_sets"][0].update(items=[7, None, 3.5])),
		"item set without lists": lambda: mutated(
			lambda p: p["item_sets"][0].update(item_groups=None, items="x")
		),
		"not_covered is junk": lambda: mutated(lambda p: p.update(not_covered="maybe")),
		"unknown price source": lambda: mutated(lambda p: p.update(price_source="maybe")),
		"price source is a list": lambda: mutated(lambda p: p.update(price_source=["rate_card"])),
		"rate card mode without a card": lambda: mutated(lambda p: p.update(price_source="rate_card")),
		"rate card mode with a junk card": lambda: mutated(
			lambda p: p.update(price_source="rate_card", rate_card=[1, 2])
		),
		"rate card mode with a card of nothing": lambda: mutated(
			lambda p: p.update(price_source="rate_card", rate_card={"profiles": [None, [], {"zones": 5}]})
		),
		"fallback to a card that is not there": lambda: mutated(
			lambda p: p["zones"][1].update(fallback={"method": "rate_card", "amount": 0.0})
		),
	}
	return cases


@pytest.mark.parametrize("name", list(broken_policies()))
def test_an_inconsistent_policy_is_priced_as_far_as_it_can_be_and_never_raises(name):
	policy = broken_policies()[name]()
	orders = [
		make_order("AE"),
		make_order("BH"),
		make_order("US"),
		make_order("RU"),
		make_order("", served=False),
		make_order("JP", quotes=[]),
		make_order("AE", items=[make_item("W", ["Watches"]), make_item()]),
		make_order("AE", items=[]),
	]
	for order in orders:
		result = resolve(policy, order)
		check_shape(result)
		for offer in result["offers"]:
			assert math.isfinite(offer["amount"]) and offer["amount"] >= 0
		previews = rule_previews(policy, order)
		rules = policy.get("rules") if isinstance(policy, dict) else None
		assert len(previews) == (len(rules) if isinstance(rules, list) else 0)
		for preview in previews:
			assert preview["amount"] is None or (math.isfinite(preview["amount"]) and preview["amount"] >= 0)
	problems = validate_policy(policy)
	assert isinstance(problems, list) and all(isinstance(p, str) and p for p in problems)


def test_a_broken_policy_fails_closed_not_free():
	"""Where the policy cannot be read the answer is 'no delivery' or the configured fallback, not zero."""
	policy = example()
	policy["rules"][2]["method"] = "surprise"
	policy["zones"][1]["fallback"] = {"method": "no_delivery", "amount": 0.0}
	result = resolve(policy, make_order("AE"))
	assert (result["offered"], result["offers"]) == (False, [])
	policy = example()
	policy["not_covered"] = "maybe"  # unrecognised: not the permissive choice
	policy["rules"] = []
	assert resolve(policy, make_order("AE"))["reason"] == "not_covered"
	# a policy with no "active" key is off, a zone with no "enabled" key is off, a rule likewise
	policy = example()
	del policy["zones"][1]["enabled"]
	assert resolve(policy, make_order("AE"))["zone"] == "Rest of world"
	policy = example()
	del policy["rules"][2]["enabled"]
	assert (
		resolve(policy, make_order("AE"))["reason"] is None
		and resolve(policy, make_order("AE"))["rule"] is None
	)
	del policy["active"]
	assert resolve(policy, make_order("AE"))["policy_active"] is False
	# a rule that names no zone does not apply to a zone that has no name either
	policy = example()
	policy["zones"] = [{"enabled": True, "everywhere_else": True, "countries": []}]
	policy["rules"] = [make_rule(None, method="free")]
	result = resolve(policy, make_order("AE"))
	assert (result["rule"], amount_of(result)) == (None, 38.2)


def test_a_missing_setting_reads_as_its_default():
	"""The first Select string of each field is its default, so a key that is missing means that choice."""
	assert [next(iter(LABELS[field].values())) for field in LABELS] == [
		"carrier_price",
		"policy_default",
		"no_delivery",
		"pass_through",
		"carrier",
	]
	policy = example()
	del policy["not_covered"]  # pass the carrier price through
	policy["rules"] = []
	assert amount_of(resolve(policy, make_order("AE"))) == 38.2
	policy = example()
	del policy["default_fallback"]  # don't offer delivery
	del policy["zones"][0]["fallback"]  # use the policy default
	assert resolve(policy, make_order("BH", quotes=[]))["reason"] == "not_offered"
	policy = example()
	del policy["rules"][2]["method"]  # the carrier price
	assert amount_of(resolve(policy, make_order("AE"))) == 39.0
	del policy["rules"][2]["round_up"]  # not rounded up
	assert amount_of(resolve(policy, make_order("AE"))) == 38.2


JUNK = [
	None,
	"",
	"  ",
	"abc",
	0,
	1,
	-1,
	2.5,
	-2.5,
	True,
	False,
	[],
	{},
	[None],
	[[]],
	{"a": 1},
	float("nan"),
	float("inf"),
	10**30,
	10**400,  # too big for a float
	10**5000,  # and more digits than str() will make of an integer
	"GCC",
	"Watches",
	"UAE",
	"AE",
	"formula",
	"fixed",
	"carrier_price * 2",
	"1 if",
]


def places_to_break(node):
	"""Every dict key and list position of a nested structure, as (container, key)."""
	found = []
	if isinstance(node, dict):
		for key, value in node.items():
			found.append((node, key))
			found.extend(places_to_break(value))
	elif isinstance(node, list):
		for position, value in enumerate(node):
			found.append((node, position))
			found.extend(places_to_break(value))
	return found


def test_no_mix_of_junk_in_the_policy_makes_the_engine_raise():
	"""A seeded fuzz: overwrite or delete a few values of the example, then price, preview and validate."""
	rng = random.Random(20261022)
	orders = [
		make_order("AE"),
		make_order("BH", quotes=[make_quote(12.5), make_quote(0.0, "b")]),
		make_order("US", quotes=[make_quote(60.0)]),
		make_order("RU"),
		make_order("JP", quotes=[]),
		make_order("AE", served=False),
		make_order("AE", items=[make_item("W", ["Watches"])]),
		make_order(" ae ", items=[], boxes=[], quotes=[make_quote(boxes_quoted=0)]),
	]
	for _ in range(400):
		policy = example()
		for _ in range(rng.randint(1, 6)):
			container, key = rng.choice(places_to_break(policy))
			if isinstance(container, dict) and rng.random() < 0.2:
				del container[key]
			else:
				container[key] = copy.deepcopy(rng.choice(JUNK))
		for order in orders:
			check_shape(resolve(policy, order))
			for preview in rule_previews(policy, order):
				assert set(preview) == {"index", "in_zone", "matches", "amount"}
		problems = validate_policy(policy)
		assert isinstance(problems, list) and all(isinstance(p, str) and p for p in problems)


# ---------------------------------------------------------------- validate_policy
def _repeat_a_country(policy):
	policy["zones"][0]["countries"].append("AE")  # Bahrain also lists a GCC country


def _second_everywhere_else(policy):
	policy["zones"][0].update(countries=[], everywhere_else=True)


def _rule_names_a_missing_zone(policy):
	policy["rules"][0]["zone"] = "Mars"


def _rule_names_no_zone(policy):
	policy["rules"][0]["zone"] = ""


def _rule_names_a_missing_item_set(policy):
	policy["rules"][1]["item_set"] = "Pens"


def _bad_formula(policy):
	policy["rules"][3]["formula"] = "carrier_price ** 2"


def _formula_that_fails_on_the_sample_order(policy):
	policy["rules"][3]["formula"] = "carrier_price / (order_value - 310)"


def _formula_rule_without_a_formula(policy):
	policy["rules"][3]["formula"] = "  "


def _formula_rule_with_no_text(policy):
	policy["rules"][3]["formula"] = None


def _inverted_order_value_band(policy):
	policy["rules"][2].update(min_order_value=1000.0, max_order_value=500.0)


def _empty_weight_band(policy):
	policy["rules"][2].update(min_weight_kg=5.0, max_weight_kg=5.0)


def _inverted_volume_band(policy):
	policy["rules"][2].update(min_volume_l=20.0, max_volume_l=2.5)


def _duplicate_zone_name(policy):
	policy["zones"].append({**copy.deepcopy(policy["zones"][0]), "countries": ["JO"]})


def _duplicate_item_set_name(policy):
	policy["item_sets"].append({"name": "Watches", "item_groups": ["Pens"], "items": []})


def _everywhere_else_lists_countries(policy):
	policy["zones"][2]["countries"] = ["JP"]


def _empty_item_set(policy):
	policy["item_sets"][0]["item_groups"] = []


def _item_set_of_blanks(policy):
	policy["item_sets"][0].update(item_groups=[" "], items=[""])


def _country_that_is_not_two_letters(policy):
	policy["zones"][1]["countries"].append("UAE")


def _negative_limit(policy):
	policy["rules"][2]["max_weight_kg"] = -1.0


def _limit_that_is_not_a_number(policy):
	policy["rules"][2]["min_order_value"] = "abc"


def _negative_zone_fallback(policy):
	policy["zones"][1]["fallback"]["amount"] = -35.0


def _negative_default_fallback(policy):
	policy["default_fallback"] = {"method": "fixed", "amount": -1.0}


def _fixed_fallback_without_an_amount(policy):
	policy["default_fallback"] = {"method": "fixed", "amount": None}


def _unknown_rule_method(policy):
	policy["rules"][0]["method"] = "discount"


def _unknown_not_covered(policy):
	policy["not_covered"] = "maybe"


def _unknown_default_fallback(policy):
	policy["default_fallback"] = {"method": "policy_default", "amount": 0.0}


def _unknown_zone_fallback(policy):
	policy["zones"][0]["fallback"] = {"method": "maybe", "amount": 0.0}


def _zone_without_a_name(policy):
	policy["zones"].append({**copy.deepcopy(policy["zones"][0]), "name": " ", "countries": []})


def _item_set_without_a_name(policy):
	policy["item_sets"].append({"name": "", "item_groups": ["Pens"], "items": []})


# name -> (change to the example, words the one sentence has to contain)
PROBLEMS = {
	"a country in two enabled zones": (_repeat_a_country, ["AE", "Bahrain", "GCC"]),
	"two Everywhere-else zones": (_second_everywhere_else, ["Everywhere else", "Bahrain", "Rest of world"]),
	"a rule naming a missing zone": (_rule_names_a_missing_zone, ["Rule 1", "Mars"]),
	"a rule naming no zone": (_rule_names_no_zone, ["Rule 1", "does not say which zone"]),
	"a rule naming a missing item set": (_rule_names_a_missing_item_set, ["Rule 2", "Pens"]),
	"a bad formula": (_bad_formula, ["Rule 4", "formula", "not allowed"]),
	"a formula that divides by zero on the sample": (
		_formula_that_fails_on_the_sample_order,
		["Rule 4", "divide by zero"],
	),
	"a formula rule with no formula": (_formula_rule_without_a_formula, ["Rule 4", "no formula"]),
	"a formula rule with no text at all": (_formula_rule_with_no_text, ["Rule 4", "no formula"]),
	"an inverted order value band": (_inverted_order_value_band, ["Rule 3", "order value"]),
	"an empty weight band": (_empty_weight_band, ["Rule 3", "weight"]),
	"an inverted volume band": (_inverted_volume_band, ["Rule 3", "volume"]),
	"duplicate zone names": (_duplicate_zone_name, ["Bahrain"]),
	"duplicate item set names": (_duplicate_item_set_name, ["Watches"]),
	"an Everywhere-else zone that lists countries": (
		_everywhere_else_lists_countries,
		["Rest of world", "JP"],
	),
	"an item set with no group and no item": (_empty_item_set, ["Watches"]),
	"an item set of blanks": (_item_set_of_blanks, ["Watches"]),
	"a zone country that is not two letters": (_country_that_is_not_two_letters, ["GCC", "UAE"]),
	"a negative limit": (_negative_limit, ["Rule 3", "negative", "weight"]),
	"a limit that is not a number": (_limit_that_is_not_a_number, ["Rule 3", "order value", "not a number"]),
	"a negative fixed amount for a zone": (_negative_zone_fallback, ["GCC", "below zero"]),
	"a negative fixed amount for the default": (_negative_default_fallback, ["default", "below zero"]),
	"a fixed fallback with no amount": (_fixed_fallback_without_an_amount, ["default", "not a number"]),
	"a rule that does not say how it charges": (_unknown_rule_method, ["Rule 1", "how"]),
	"an unknown setting for anything not covered": (_unknown_not_covered, ["not covered"]),
	"an unknown default fallback": (_unknown_default_fallback, ["default"]),
	"an unknown zone fallback": (_unknown_zone_fallback, ["Bahrain", "can't quote"]),
	"a zone without a name": (_zone_without_a_name, ["Zone 5", "name"]),
	"an item set without a name": (_item_set_without_a_name, ["Item set 2", "name"]),
}


def test_validate_policy_accepts_the_example():
	assert validate_policy(example()) == []


@pytest.mark.parametrize(("change", "words"), PROBLEMS.values(), ids=list(PROBLEMS))
def test_validate_policy_reports_each_problem(change, words):
	policy = example()
	change(policy)
	problems = validate_policy(policy)
	assert len(problems) == 1, problems
	for word in words:
		assert word in problems[0], (word, problems[0])
	# a plain sentence for an ERP user
	assert problems[0][0].isupper() and problems[0].endswith(".")
	assert not any(jargon in problems[0] for jargon in ("Traceback", "Error", "dict", "{"))


def test_validate_policy_numbers_rules_from_one_and_names_every_problem_it_finds():
	policy = example()
	_rule_names_a_missing_zone(policy)  # rule 1
	_negative_limit(policy)  # rule 3
	_bad_formula(policy)  # rule 4
	_repeat_a_country(policy)
	problems = validate_policy(policy)
	assert len(problems) == 4
	joined = "\n".join(problems)
	for word in ("Rule 1", "Rule 3", "Rule 4", "AE"):
		assert word in joined
	assert "Rule 0" not in joined and "Rule 5" not in joined


def test_validate_policy_allows_what_is_harmless():
	policy = example()
	# a disabled zone may repeat a country, and a disabled second Everywhere-else zone is fine
	policy["zones"].append({**copy.deepcopy(policy["zones"][1]), "name": "Old GCC", "enabled": False})
	policy["zones"].append(
		{**copy.deepcopy(policy["zones"][2]), "name": "Old rest", "enabled": False, "countries": ["JP"]}
	)
	# a country listed twice in one zone, and a stale formula on a rule that is not a formula rule
	policy["zones"][0]["countries"] = ["BH", "bh"]
	policy["rules"][2]["formula"] = "carrier_price ** 2"
	# a Not shipped zone may also be the Everywhere-else zone
	policy["zones"].append(
		{
			**copy.deepcopy(policy["zones"][3]),
			"name": "Nowhere",
			"everywhere_else": True,
			"enabled": False,
			"countries": [],
		}
	)
	# a rule for a disabled zone, a band with only one edge, and a lower edge of zero
	policy["rules"].append(make_rule("Old GCC", min_order_value=100.0))
	policy["rules"].append(make_rule("GCC", max_order_value=500.0, round_up=True))
	assert validate_policy(policy) == []


@pytest.mark.parametrize(
	"junk",
	[
		None,
		"policy",
		5,
		[],
		{},
		{"zones": "x", "rules": None, "item_sets": 3},
		{"rules": [None, 3, "r", {}], "zones": [[], 4]},
	],
)
def test_validate_policy_never_raises_on_odd_content(junk):
	problems = validate_policy(junk)
	assert isinstance(problems, list) and all(isinstance(problem, str) and problem for problem in problems)
	if not isinstance(junk, dict):
		assert problems, "a policy that is not a policy is a problem"


def test_validate_policy_checks_a_formula_the_way_saving_it_does():
	policy = example()
	for formula in ("carrier_price * 1.1", "max(carrier_price * 1.1, 45)", "weight_kg * 6 + 8"):
		policy["rules"][3]["formula"] = formula
		assert validate_policy(policy) == [], formula
	policy["rules"][3]["formula"] = "cost * 2"
	(problem,) = validate_policy(policy)
	assert "Unknown name 'cost'" in problem and "Rule 4" in problem


# ---------------------------------------------------------------- the rate card
# One product code of each kind, written the way the website keeps them (with braces).
DANGEROUS = "{F-CR-CSE-W-40}"  # a full-size fragrance: the Dangerous Goods profile
ALLOWED = "{P-AA-AT1-IG}"  # anything else: Rest of the World Allowed Items
REFILL = "{F-CR-OSA-M-100-RF}"  # the Refill Service profile
PETITE = "{F-CR-OSA-M-PT}"  # the Dangerous Goods Petite profile

EXPRESS = "International Express (4-7 days)"
SHIPPING = "International Shipping (10-15 days)"

# The reasons a refusal can carry, and the keys a guest-facing price may be built from.
REFUSALS = {"not_served", "not_shipped", "not_covered", "too_heavy", "not_offered"}


def sample_card():
	"""The trimmed Shopify export of Task 11, parsed afresh so a test can change it. Its four profiles are, in
	this order, Dangerous Goods Petite, Refill Service, Dangerous Goods and Rest of the World Allowed Items."""
	return parse_shopify_export(SAMPLE_CARD_CSV.read_bytes().decode("utf-8"))["card"]


def card_policy(**changes):
	"""The example policy with the rate card as its price source, holding the sample card."""
	return {**example(), "price_source": "rate_card", "rate_card": sample_card(), **changes}


def card_item(key=DANGEROUS, weight_kg=0.9, qty=1):
	"""An item as a rate card order carries it: its product code and its unit weight."""
	return {"item_code": f"ITEM-{key}", "qty": qty, "groups": [], "index_key": key, "weight_kg": weight_kg}


def card_order(country="US", *items, **changes):
	"""An order for the rate card: items, and no cartons and no carrier quotes (a call in rate card mode sends
	none). With no item given it holds one fragrance."""
	order = make_order(country, items=list(items) or [card_item()], boxes=[], quotes=[])
	return {**order, **changes}


def zone_of(card, profile, zone):
	"""The zone of a card by the names of its profile and zone."""
	(found,) = [z for p in card["profiles"] if p["name"] == profile for z in p["zones"] if z["name"] == zone]
	return found


def without_card_fields(offer):
	return {key: value for key, value in offer.items() if key != "trace"}


def test_rate_card_mode_prices_the_example_orders():
	policy = card_policy()
	# 0.9 kg is 1.984 lb: in the 0.7 to 2 lb tier of the USA zone
	result = resolve(policy, card_order("US", card_item(DANGEROUS, 0.9)))
	check_shape(result)
	assert (result["policy_active"], result["price_source"], result["zone"], result["rule"]) == (
		True,
		"rate_card",
		"Rest of world",
		None,
	)
	assert (result["offered"], result["reason"], result["message"]) == (True, None, "")
	offer = only_offer(result)
	assert without_card_fields(offer) == {
		"service_id": "rate-card",
		"name": EXPRESS,
		"amount": 60.0,
		"source": "rate_card",
		"reason": None,
		"estimated_days": 7,
	}
	assert type(offer["amount"]) is float and type(offer["estimated_days"]) is int
	assert offer["trace"] == [
		{"kind": "rate_card", "text": "Dangerous Goods, USA: 1.984 lb -> $60.00", "amount": 60.0},
		{"kind": "result", "text": "Customer pays 60.00.", "amount": 60.0},
	]
	# 1 kg is 2.205 lb: the next tier up
	one_kilo = only_offer(resolve(policy, card_order("US", card_item(DANGEROUS, 1.0))))
	assert one_kilo["amount"] == 80.0
	assert one_kilo["trace"][0]["text"] == "Dangerous Goods, USA: 2.205 lb -> $80.00"
	# the unit weight is multiplied by the quantity: two of 0.45 kg weigh what one of 0.9 kg does
	assert amount_of(resolve(policy, card_order("US", card_item(DANGEROUS, 0.45, qty=2)))) == 60.0

	# a cart of two kinds adds the prices: 60 for the fragrance and 40 for an allowed item of 1.001 lb
	cart = resolve(policy, card_order("US", card_item(DANGEROUS, 0.9), card_item(ALLOWED, 0.454)))
	check_shape(cart)
	offer = only_offer(cart)
	assert (offer["amount"], offer["name"], offer["estimated_days"]) == (100.0, EXPRESS, 7)
	assert [(step["kind"], step["amount"]) for step in offer["trace"]] == [
		("rate_card", 60.0),
		("rate_card", 40.0),
		("result", 100.0),
	]
	assert (
		offer["trace"][1]["text"]
		== "Rest of the World Allowed Items, Worldwide (Allowed Items): 1.001 lb -> $40.00"
	)
	assert offer["trace"][2]["text"] == "Customer pays 100.00."
	# the order of the items in the cart does not matter
	assert resolve(policy, card_order("US", card_item(ALLOWED, 0.454), card_item(DANGEROUS, 0.9))) == cart
	# methods that differ: the offer is named after the slower one and promises the longest time
	slow = only_offer(resolve(policy, card_order("US", card_item(DANGEROUS, 0.9), card_item(REFILL, 0.5))))
	assert (slow["amount"], slow["name"], slow["estimated_days"]) == (120.0, SHIPPING, 15)
	# the first profile that matches takes the item: a petite bottle is not a full-size fragrance
	petite = only_offer(resolve(policy, card_order("US", card_item(PETITE, 0.5))))
	assert (petite["amount"], petite["name"]) == (80.0, SHIPPING)
	assert petite["trace"][0]["text"].startswith("Dangerous Goods Petite, ")

	# Bahrain is free, and delivery is still offered, at 0.00
	free = resolve(policy, card_order("BH", card_item(DANGEROUS, 0.9)))
	check_shape(free)
	assert (free["zone"], free["offered"], free["reason"], free["message"]) == ("Bahrain", True, None, "")
	offer = only_offer(free)
	assert (offer["amount"], offer["name"], offer["estimated_days"]) == (0.0, "Free Shipping (1-2 days)", 2)
	assert type(offer["amount"]) is float and math.copysign(1.0, offer["amount"]) == 1.0
	assert offer["trace"][0]["text"].endswith(": 1.984 lb -> $0.00")
	assert offer["trace"][-1] == {"kind": "result", "text": "Customer pays 0.00.", "amount": 0.0}
	# a flat zone charges its one price at any weight
	gcc = only_offer(resolve(policy, card_order("AE", card_item(DANGEROUS, 25.0))))
	assert (gcc["amount"], gcc["name"]) == (35.0, EXPRESS)


def test_rate_card_mode_flags_a_weight_it_had_to_guess():
	policy = card_policy()
	# no weight at all counts as 0.35 kg (0.772 lb), and the receipt says so
	guessed = card_item(DANGEROUS)
	del guessed["weight_kg"]
	line = only_offer(resolve(policy, card_order("US", guessed)))["trace"][0]
	assert line == {
		"kind": "rate_card",
		"text": "Dangerous Goods, USA: 0.772 lb -> $60.00 (weight estimated)",
		"amount": 60.0,
	}
	# so does a weight that is not a number above zero
	for weight in (None, 0, -1, True, float("nan"), "heavy"):
		offer = only_offer(resolve(policy, card_order("US", card_item(DANGEROUS, weight))))
		assert offer["trace"][0]["text"].endswith("0.772 lb -> $60.00 (weight estimated)"), weight
	# a weight that is real is not flagged, even when another profile of the cart is guessed
	both = only_offer(resolve(policy, card_order("US", card_item(DANGEROUS, 0.9), card_item(ALLOWED, None))))
	assert "estimated" not in both["trace"][0]["text"] and both["trace"][1]["text"].endswith(
		"(weight estimated)"
	)


def test_rate_card_mode_reads_the_product_code_the_way_the_website_writes_it():
	policy = card_policy()
	for key in ("{F-CR-CSE-W-40}", "{f-cr-cse-w-40}", "F-CR-CSE-W-40", "  { F-CR-CSE-W-40 }  "):
		offer = only_offer(resolve(policy, card_order("US", card_item(key, 0.9))))
		assert offer["trace"][0]["text"].startswith("Dangerous Goods, USA"), key
	# an item with no product code (no Website Item) is not a fragrance: the profile that takes everything has it
	for item in (
		{"item_code": "X", "qty": 1, "weight_kg": 0.454},
		{"item_code": "X", "qty": 1, "weight_kg": 0.454, "index_key": None},
		{"item_code": "X", "qty": 1, "weight_kg": 0.454, "index_key": ""},
	):
		offer = only_offer(resolve(policy, card_order("US", item)))
		assert offer["trace"][0]["text"].startswith("Rest of the World Allowed Items, "), item
		assert offer["amount"] == 40.0


def test_rate_card_mode_ignores_carrier_quotes_and_served():
	policy = card_policy()
	plain = resolve(policy, card_order("US", card_item(DANGEROUS, 0.9)))
	assert amount_of(plain) == 60.0
	loud = make_quote(999.0)
	for changes in (
		{"served": False},
		{"quotes": [loud]},
		{"quotes": [make_quote(1.0, boxes_quoted=0)]},  # a partial quote is not even looked at
		{"quotes": "junk", "boxes": None},
		{"boxes": [{"weight_kg": 1e6, "volume_l": 1e6}], "order_value": 1e9},
		{"served": False, "quotes": [loud], "boxes": [{"weight_kg": 40.0, "volume_l": 99.0}]},
	):
		assert resolve(policy, {**card_order("US", card_item(DANGEROUS, 0.9)), **changes}) == plain, changes
	order = card_order("US", card_item(DANGEROUS, 0.9))
	del order["served"], order["quotes"], order["boxes"], order["order_value"]
	assert resolve(policy, order) == plain, "an order that says nothing of them is priced the same"
	# the rules, "Anything not covered" and the fallbacks have nothing to say either
	policy["not_covered"] = "no_delivery"
	policy["default_fallback"] = {"method": "free", "amount": 0.0}
	policy["rules"] = [make_rule("Rest of world", method="free"), *policy["rules"]]
	assert resolve(policy, card_order("US", card_item(DANGEROUS, 0.9))) == plain
	# the country need not be in a zone of the policy at all
	without_zone(policy, "Rest of world")
	nowhere = resolve(policy, card_order("US", card_item(DANGEROUS, 0.9)))
	assert (nowhere["zone"], amount_of(nowhere)) == (None, 60.0)
	assert nowhere["offers"][0]["trace"] == plain["offers"][0]["trace"]


def test_rate_card_mode_still_honours_not_shipped_zones():
	policy = card_policy()
	for code in ("RU", "BY"):
		result = resolve(policy, card_order(code, card_item(DANGEROUS, 0.9)))
		check_shape(result)
		assert (result["policy_active"], result["price_source"], result["zone"], result["rule"]) == (
			True,
			"rate_card",
			"Not shipped",
			None,
		)
		assert (result["offered"], result["reason"], result["offers"]) == (False, "not_shipped", [])
		assert result["message"] == "We don't deliver to this country."
	# the card has no say: a country the card lists is refused all the same
	policy["zones"][3]["countries"].append("US")
	for quotes in ([], [make_quote(1.0)]):
		result = resolve(policy, card_order("US", card_item(DANGEROUS, 0.9), quotes=quotes))
		assert (result["zone"], result["offered"], result["reason"]) == ("Not shipped", False, "not_shipped")
	# the zone is judged before the card, so a missing card does not change the reason
	policy["rate_card"] = None
	assert resolve(policy, card_order("US", card_item(DANGEROUS, 0.9)))["reason"] == "not_shipped"
	# a zone that is switched off refuses nobody
	policy = card_policy()
	policy["zones"][3].update(countries=["US"], enabled=False)
	assert amount_of(resolve(policy, card_order("US", card_item(DANGEROUS, 0.9)))) == 60.0
	# an Everywhere-else zone that is Not shipped refuses every country no other zone lists
	policy["zones"][2]["not_shipped"] = True
	assert resolve(policy, card_order("US", card_item(DANGEROUS, 0.9)))["reason"] == "not_shipped"
	assert amount_of(resolve(policy, card_order("AE", card_item(DANGEROUS, 0.9)))) == 35.0


def test_rate_card_mode_refusals():
	policy = card_policy()
	policy["zones"][1]["not_offered_message"] = "Please write to us for a delivery price."  # GCC
	policy["zones"][2]["not_offered_message"] = "Rest of world words."

	def refused(order, reason, message=None, zone=None, policy=policy):
		result = resolve(policy, order)
		check_shape(result)
		assert (result["policy_active"], result["price_source"], result["rule"]) == (True, "rate_card", None)
		assert (result["offered"], result["reason"], result["offers"]) == (False, reason, [])
		assert result["zone"] == zone
		assert result["message"] == (DEFAULT_MESSAGES[reason] if message is None else message)
		return result

	# heavier than the highest tier (20 lb): 10 kg is 22.046 lb. The zone's own words are not used for this.
	refused(card_order("US", card_item(DANGEROUS, 10.0)), "too_heavy", zone="Rest of world")
	assert DEFAULT_MESSAGES["too_heavy"] == "This order is heavier than our delivery rates cover."
	# the card does not deliver a refill to the Gulf: the zone's own words are used
	refused(
		card_order("AE", card_item(REFILL, 0.5)),
		"not_covered",
		"Please write to us for a delivery price.",
		"GCC",
	)
	refused(
		card_order("ZZ", card_item(DANGEROUS, 0.5)), "not_covered", "Rest of world words.", "Rest of world"
	)
	# words that are only white space are no words
	policy["zones"][1]["not_offered_message"] = " \n"
	refused(card_order("AE", card_item(REFILL, 0.5)), "not_covered", zone="GCC")
	# no item to price: not offered, and again not in the zone's words
	for items in (
		[],
		[None, 5, "x", []],
		[card_item(DANGEROUS, 0.9, qty=0)],
		[card_item(DANGEROUS, 0.9, qty=-2)],
	):
		refused(card_order("US", items=items), "not_offered", zone="Rest of world")
	# a card with no profile for the item
	only_dangerous = card_policy()
	only_dangerous["rate_card"]["profiles"] = [
		p for p in only_dangerous["rate_card"]["profiles"] if p["name"] == "Dangerous Goods"
	]
	refused(
		card_order("US", card_item(ALLOWED, 0.5)), "not_offered", zone="Rest of world", policy=only_dangerous
	)
	# a country that is not a country is not served; nothing else is looked at
	for code in ("UAE", "", " ", None, 7, "A-E"):
		refused(card_order(code, card_item(DANGEROUS, 0.9)), "not_served")

	# one kind of item that cannot be priced makes the whole order not offered, not a lower price
	refused(card_order("AE", card_item(DANGEROUS, 0.5), card_item(REFILL, 0.5)), "not_covered", zone="GCC")
	refused(
		card_order("US", card_item(ALLOWED, 0.5), card_item(DANGEROUS, 10.0)),
		"too_heavy",
		zone="Rest of world",
	)

	# no rate card at all: not covered, in the zone's words
	policy["zones"][1]["not_offered_message"] = "Please write to us for a delivery price."
	words = "Please write to us for a delivery price."
	for card in (None, {}, [], "card", 5, {"profiles": []}, {"profiles": None}, {"profiles": [None, 5, "x"]}):
		policy["rate_card"] = card
		refused(card_order("AE", card_item(DANGEROUS, 0.5)), "not_covered", words, "GCC")
	del policy["rate_card"]  # the key itself missing
	refused(card_order("AE", card_item(DANGEROUS, 0.5)), "not_covered", words, "GCC")


def test_carrier_fallback_can_use_the_rate_card():
	policy = example()
	policy["rate_card"] = sample_card()  # the price source stays the carrier
	policy["zones"][1]["fallback"] = {"method": "rate_card", "amount": 0.0}  # GCC
	fragrance = [card_item(DANGEROUS, 0.5)]  # 1.102 lb: the flat 35 of the Gulf zone

	# the carrier gave no quote at all
	result = resolve(policy, make_order("AE", items=fragrance, quotes=[]))
	check_shape(result)
	assert (result["policy_active"], result["price_source"], result["zone"], result["rule"]) == (
		True,
		"carrier",
		"GCC",
		None,
	)
	assert (result["offered"], result["reason"], result["message"]) == (True, "fallback_offer", "")
	offer = only_offer(result)
	assert without_card_fields(offer) == {
		"service_id": "rate-card",
		"name": EXPRESS,
		"amount": 35.0,
		"source": "rate_card",
		"reason": "fallback_offer",
		"estimated_days": 7,
	}
	assert [(step["kind"], step["amount"]) for step in offer["trace"]] == [
		("fallback", None),
		("rate_card", 35.0),
		("result", 35.0),
	]
	assert offer["trace"][0]["text"] == (
		"The carrier gave no price for the whole shipment. The fallback applies: the rate card."
	)
	assert (
		offer["trace"][1]["text"].startswith("Dangerous Goods, GCC")
		and "1.102 lb -> $35.00" in offer["trace"][1]["text"]
	)

	# every service quoted too few cartons
	boxes = [{"weight_kg": 1.2, "volume_l": 3.0}, {"weight_kg": 1.2, "volume_l": 3.0}]
	partial = resolve(
		policy, make_order("AE", items=fragrance, boxes=boxes, quotes=[make_quote(30.0, boxes_quoted=1)])
	)
	assert (partial["reason"], amount_of(partial), only_offer(partial)["source"]) == (
		"fallback_offer",
		35.0,
		"rate_card",
	)
	# the carrier does quote: the rules price it and the card is not used
	quoted = resolve(policy, make_order("AE", items=fragrance))
	assert (quoted["rule"], amount_of(quoted), only_offer(quoted)["source"]) == (2, 39.0, "rule")

	# the policy default is the card, and Bahrain says "use the policy default": a fragrance is free there
	policy["default_fallback"] = {"method": "rate_card", "amount": 0.0}
	bahrain = resolve(policy, make_order("BH", items=fragrance, quotes=[]))
	check_shape(bahrain)
	assert (bahrain["zone"], bahrain["offered"], bahrain["reason"]) == ("Bahrain", True, "fallback_offer")
	offer = only_offer(bahrain)
	assert (offer["amount"], offer["name"], offer["estimated_days"], offer["source"]) == (
		0.0,
		"Free Shipping (1-2 days)",
		2,
		"rate_card",
	)
	us = resolve(policy, make_order("US", items=[card_item(DANGEROUS, 0.9)], quotes=[]))
	assert (us["zone"], amount_of(us), only_offer(us)["reason"]) == ("Rest of world", 60.0, "fallback_offer")
	# a zone that decides for itself does not ask the default
	policy["zones"][1]["fallback"] = {"method": "fixed", "amount": 12.0}
	fixed = resolve(policy, make_order("AE", items=fragrance, quotes=[]))
	assert (only_offer(fixed)["source"], amount_of(fixed)) == ("fallback", 12.0)

	# the card cannot price the order: no delivery, for the card's reason (and the zone's words only for
	# "not covered")
	policy["zones"][1]["fallback"] = {"method": "rate_card", "amount": 0.0}
	policy["zones"][1]["not_offered_message"] = "Please write to us."
	heavy = resolve(policy, make_order("US", items=[card_item(DANGEROUS, 10.0)], quotes=[]))
	check_shape(heavy)
	assert (heavy["zone"], heavy["offered"], heavy["reason"], heavy["offers"]) == (
		"Rest of world",
		False,
		"too_heavy",
		[],
	)
	assert heavy["message"] == DEFAULT_MESSAGES["too_heavy"]
	uncovered = resolve(policy, make_order("AE", items=[card_item(REFILL, 0.5)], quotes=[]))
	assert (uncovered["zone"], uncovered["reason"], uncovered["message"]) == (
		"GCC",
		"not_covered",
		"Please write to us.",
	)
	nothing = resolve(policy, make_order("AE", items=[], quotes=[]))
	assert (nothing["reason"], nothing["message"]) == ("not_offered", DEFAULT_MESSAGES["not_offered"])
	# no card to use: the same, never a free delivery
	for card in (None, {}, "card"):
		policy["rate_card"] = card
		missing = resolve(policy, make_order("AE", items=fragrance, quotes=[]))
		check_shape(missing)
		assert (missing["offered"], missing["reason"], missing["offers"]) == (False, "not_covered", [])
	policy["rate_card"] = sample_card()

	# nobody delivers there: the card is no help, since a fallback never applies to a country that is not served
	unserved = resolve(policy, make_order("AE", items=fragrance, quotes=[], served=False))
	assert (unserved["offered"], unserved["reason"]) == (False, "not_served")
	# and a Not shipped zone is still refused
	assert resolve(policy, make_order("RU", items=fragrance, quotes=[]))["reason"] == "not_shipped"


def test_carrier_fallback_to_the_rate_card_after_a_formula_error():
	policy = example()
	policy["rate_card"] = sample_card()
	policy["zones"][1]["fallback"] = {"method": "rate_card", "amount": 0.0}
	policy["rules"][2] = make_rule(method="formula", formula="carrier_price / (order_value - 310)")
	fragrance = [card_item(DANGEROUS, 0.5)]
	result = resolve(policy, make_order("AE", items=fragrance, order_value=310.0))
	check_shape(result)
	assert (result["zone"], result["rule"], result["offered"], result["reason"], result["message"]) == (
		"GCC",
		2,  # the rule that held but could not be priced
		True,
		"formula_error",
		"",
	)
	offer = only_offer(result)
	assert (offer["service_id"], offer["amount"], offer["source"], offer["reason"]) == (
		"rate-card",
		35.0,
		"rate_card",
		"formula_error",
	)
	assert [step["kind"] for step in offer["trace"]] == ["fallback", "rate_card", "result"]
	assert offer["trace"][0]["text"].startswith("Rule 3 could not be priced.")
	assert offer["trace"][0]["text"].endswith("The fallback applies: the rate card.")
	# a card that cannot price the order leaves it not offered, still pointing at the rule
	nothing = resolve(policy, make_order("AE", items=[card_item(REFILL, 0.5)], order_value=310.0))
	assert (nothing["offered"], nothing["reason"], nothing["rule"], nothing["offers"]) == (
		False,
		"not_covered",
		2,
		[],
	)
	# the same formula prices when it can
	assert amount_of(resolve(policy, make_order("AE", items=fragrance, order_value=320.0))) == 3.82


def test_anything_not_covered_is_unchanged_by_the_rate_card():
	policy = without_zone(example(), "Rest of world")
	policy.update(rate_card=sample_card(), default_fallback={"method": "rate_card", "amount": 0.0})
	fragrance = [card_item(DANGEROUS, 0.9)]
	# "don't offer delivery" is still no delivery, although the card could price it
	policy["not_covered"] = "no_delivery"
	refused = resolve(policy, make_order("US", items=fragrance, quotes=[]))
	assert (refused["zone"], refused["offered"], refused["reason"]) == (None, False, "not_covered")
	assert resolve(policy, make_order("US", items=fragrance))["reason"] == "not_covered"
	# passing the carrier's price through has nothing to pass when there is no quote: the policy default decides
	policy["not_covered"] = "pass_through"
	through = resolve(policy, make_order("US", items=fragrance, quotes=[]))
	assert (through["zone"], through["reason"], amount_of(through)) == (None, "fallback_offer", 60.0)
	assert only_offer(through)["source"] == "rate_card"
	# and with a quote it passes the quote through
	passed = resolve(policy, make_order("US", items=fragrance, quotes=[make_quote(107.61)]))
	assert (amount_of(passed), only_offer(passed)["source"]) == (107.61, "carrier")


@pytest.mark.parametrize("source", ["carrier", "rate_card", "mystery", "", None, 5, ["rate_card"], "MISSING"])
def test_policy_off_returns_quotes_unchanged_in_every_price_source(source):
	quotes = [
		make_quote(38.2, "a", "Express"),
		make_quote(107.615, "b", "Economy", boxes_quoted=0),  # a partial quote is kept too
		make_quote(41, "d", "Whole dollars"),
	]
	order = make_order("AE", quotes=quotes)
	off = {**example(), "active": False}
	expected = resolve(off, order)
	policy = {**off, "rate_card": sample_card()}
	if source == "MISSING":
		del policy["price_source"]
	else:
		policy["price_source"] = source
	result = resolve(policy, order)
	check_shape(result)
	assert result == {**expected, "price_source": "rate_card" if source == "rate_card" else "carrier"}
	assert [
		(o["service_id"], o["name"], o["amount"], o["source"], o["reason"], o["estimated_days"])
		for o in result["offers"]
	] == [
		("a", "Express", 38.2, "carrier", None, None),
		("b", "Economy", 107.615, "carrier", None, None),
		("d", "Whole dollars", 41, "carrier", None, None),
	]
	assert type(result["offers"][2]["amount"]) is int  # not even the type changes
	assert (result["policy_active"], result["zone"], result["rule"], result["reason"]) == (
		False,
		None,
		None,
		None,
	)
	# off means off: the country need not be served, in a zone or even a country, and no card is read
	for country, served in (("RU", True), ("AE", False), ("", True), ("US", True)):
		other = resolve(policy, make_order(country, quotes=quotes, served=served))
		assert [o["amount"] for o in other["offers"]] == [38.2, 107.615, 41]
		assert other["policy_active"] is False
	nothing = resolve(policy, make_order("AE", quotes=[]))
	assert (nothing["offered"], nothing["reason"], nothing["offers"]) == (False, None, [])
	# an order of items for the card, as the website sends it in rate card mode, still gets the quotes it has
	assert (
		resolve(policy, card_order("US", card_item(), quotes=[make_quote(9.0)]))["offers"][0]["amount"] == 9.0
	)


def test_offers_carry_estimated_days_and_carrier_offers_carry_none():
	def days(result):
		check_shape(result)
		return [offer["estimated_days"] for offer in result["offers"]]

	policy = example()
	# everything the carrier's price makes: a rule, a pass-through, the quotes of a policy that is off, a fallback
	assert days(resolve(policy, make_order("AE"))) == [None]
	assert days(resolve(policy, make_order("AE", quotes=[make_quote(38.2, "a"), make_quote(60.0, "b")]))) == [
		None,
		None,
	]
	assert days(resolve(policy, make_order("AE", quotes=[]))) == [None]  # a fixed amount
	assert days(resolve(without_zone(example(), "Rest of world"), make_order("JP"))) == [
		None
	]  # passed through
	policy["zones"][1]["fallback"] = {"method": "free", "amount": 0.0}
	assert days(resolve(policy, make_order("AE", quotes=[]))) == [None]
	assert days(resolve({**example(), "active": False}, make_order("AE"))) == [None]
	# a rate card offer promises the longest time of its profiles
	cart = card_policy()
	assert days(resolve(cart, card_order("US", card_item(DANGEROUS, 0.9)))) == [7]
	assert days(resolve(cart, card_order("BH", card_item(DANGEROUS, 0.9)))) == [2]
	assert days(resolve(cart, card_order("US", card_item(DANGEROUS, 0.9), card_item(REFILL, 0.5)))) == [15]
	# and none when the card's zone does not say, or says something that is not a number of days
	for junk in (None, "7", True, -1, 61, 3.5, [7]):
		cart = card_policy()
		zone_of(cart["rate_card"], "Dangerous Goods", "USA")["days"] = junk
		assert days(resolve(cart, card_order("US", card_item(DANGEROUS, 0.9)))) == [None], junk
	# the number of days is a whole number, not a float that happens to be whole
	assert type(only_offer(resolve(card_policy(), card_order("US", card_item())))["estimated_days"]) is int


def test_an_unknown_price_source_prices_nothing_and_a_missing_one_means_the_carrier():
	for source in ("maybe", "Rate card", "RATE_CARD", " carrier", 5, 0, False, True, [], ["rate_card"], {}):
		policy = {**example(), "price_source": source, "rate_card": sample_card()}
		for order in (
			make_order("AE"),
			card_order("US", card_item()),
			make_order("AE", quotes=[]),
			make_order("RU"),
		):
			result = resolve(policy, order)
			check_shape(result)
			assert (result["price_source"], result["zone"], result["rule"]) == ("carrier", None, None), source
			assert (result["offered"], result["reason"], result["offers"]) == (False, "not_covered", []), (
				source
			)
			assert result["message"] == DEFAULT_MESSAGES["not_covered"]
		assert validate_policy(policy), f"validate_policy reports {source!r}"
	# a source that is missing, None or empty is the default, the carrier
	baseline = resolve(example(), make_order("AE"))
	for source in (None, ""):
		assert resolve({**example(), "price_source": source}, make_order("AE")) == baseline
	absent = example()
	del absent["price_source"]
	assert resolve(absent, make_order("AE")) == baseline
	assert (baseline["price_source"], amount_of(baseline)) == ("carrier", 39.0)
	assert resolve({}, make_order("AE"))["price_source"] == "carrier"


def test_every_result_names_the_price_source():
	orders = [
		make_order("AE"),
		make_order("AE", quotes=[]),
		make_order("RU"),
		make_order("AE", served=False),
		make_order("", served=False),
		make_order("JP"),
		card_order("US", card_item()),
		card_order("US", items=[]),
		card_order("BH", card_item(DANGEROUS, 40.0)),
	]
	for source in ("carrier", "rate_card"):
		policy = {**example(), "price_source": source, "rate_card": sample_card()}
		for active in (True, False):
			for order in orders:
				result = resolve({**policy, "active": active}, order)
				check_shape(result)
				assert result["price_source"] == source, (source, active, order["country_code"])
				assert result["policy_active"] is active


def test_labels_keep_their_order_and_gain_the_new_entries():
	# the tables keep the order they had, and the new table comes last
	assert list(LABELS) == ["method", "zone_fallback", "default_fallback", "not_covered", "price_source"]
	assert list(LABELS["price_source"].items()) == [("Carrier", "carrier"), ("Rate card", "rate_card")]
	# "Use the rate card" is the last choice of both fallback lists, after everything they offered before
	assert list(LABELS["zone_fallback"].items()) == [
		("Use the policy default", "policy_default"),
		("Don't offer delivery", "no_delivery"),
		("Charge a fixed amount", "fixed"),
		("Free", "free"),
		("Use the rate card", "rate_card"),
	]
	assert list(LABELS["default_fallback"].items()) == [
		("Don't offer delivery", "no_delivery"),
		("Charge a fixed amount", "fixed"),
		("Free", "free"),
		("Use the rate card", "rate_card"),
	]
	# the other two lists did not change, and the first label of each is still its default
	assert list(LABELS["method"].values()) == ["carrier_price", "free", "formula"]
	assert list(LABELS["not_covered"].values()) == ["pass_through", "no_delivery"]
	assert next(iter(LABELS["price_source"])) == "Carrier"
	for labels in LABELS.values():
		assert all(label.isascii() for label in labels), "a straight apostrophe, so a Select option matches"
		assert len(set(labels.values())) == len(labels), "two labels never share a code"


# ---------------------------------------------------------------- validate_policy and the rate card
NO_CARD_YET = "The price source is the rate card, but there is no rate card yet."


def card_with(mutate):
	"""The sample card after `mutate` changed it."""
	card = sample_card()
	mutate(card)
	return card


def test_validate_policy_rate_card_problems():
	card = sample_card()
	assert validate_card(card) == [], "the sample card is a good card"
	assert validate_policy(card_policy()) == []
	# a card nobody uses is no problem: the carrier prices and no fallback points at it
	assert validate_policy({**example(), "rate_card": card}) == []
	assert validate_policy({**example(), "price_source": "carrier", "rate_card": None}) == []

	def only_problem(policy):
		(problem,) = validate_policy(policy)
		assert problem[0].isupper() and problem.endswith("."), "a plain sentence for an ERP user"
		assert not any(jargon in problem for jargon in ("Traceback", "Error", "dict", "{", "None"))
		return problem

	# a price source that is not one
	for source in ("maybe", "Rate card", 5, ["rate_card"], False):
		problem = only_problem({**example(), "price_source": source})
		assert 'The setting for "Price source" is not valid.' in problem
		assert "carrier" in problem and "rate card" in problem
	# the rate card as the price source, with nothing to price from
	for nothing in (None, "card", 5, [card]):
		assert only_problem({**example(), "price_source": "rate_card", "rate_card": nothing}) == NO_CARD_YET
	assert only_problem({**example(), "price_source": "rate_card"}) == NO_CARD_YET
	# a fallback that is the rate card, with nothing to price from: each one says where it is
	policy = example()
	policy["zones"][1]["fallback"] = {"method": "rate_card", "amount": 0.0}
	assert only_problem(policy) == (
		'Zone "GCC" uses the rate card when the carrier can\'t quote, but there is no rate card yet.'
	)
	policy = example()
	policy["default_fallback"] = {"method": "rate_card", "amount": 0.0}
	assert only_problem(policy) == (
		"The policy default uses the rate card when the carrier can't quote, but there is no rate card yet."
	)
	policy = example()
	policy["zones"][0]["fallback"] = {"method": "rate_card", "amount": 0.0}
	policy["zones"].append(
		{**copy.deepcopy(policy["zones"][0]), "name": " ", "enabled": False, "countries": []}
	)
	policy["price_source"] = "rate_card"
	problems = validate_policy(policy)
	assert NO_CARD_YET in problems
	assert any(p.startswith('Zone "Bahrain" uses the rate card') for p in problems)
	assert any(p.startswith("Zone number 5 uses the rate card") for p in problems), problems
	# every problem of the card is reported with its own sentence, after "Rate card: "
	broken = card_with(lambda c: c["profiles"][2]["zones"][0]["tiers"][0].update(price=-5.0))
	problems = validate_policy({**example(), "rate_card": broken})
	assert problems == [f"Rate card: {problem}" for problem in validate_card(broken)] != []
	assert only_problem({**example(), "rate_card": broken}).startswith("Rate card: ")
	assert "Dangerous Goods" in problems[0] and "USA" in problems[0]
	many = card_with(lambda c: c["profiles"][2].update(match=[], zones=[]))
	many["profiles"][3]["zones"][0]["countries"].append("US")
	many["currency"] = "EUR"
	problems = validate_policy({**card_policy(), "rate_card": many})
	assert problems == [f"Rate card: {problem}" for problem in validate_card(many)]
	assert len(problems) >= 3 and all(p.startswith("Rate card: ") for p in problems)
	# the card's problems stand beside the policy's own
	both = {**card_policy(), "rate_card": broken, "not_covered": "maybe"}
	assert len(validate_policy(both)) == 2
	# a fallback is fine when the card is there, and a card that is there but not used is still checked
	policy = {**example(), "rate_card": card}
	policy["default_fallback"] = {"method": "rate_card", "amount": 0.0}
	policy["zones"][1]["fallback"] = {"method": "rate_card", "amount": 0.0}
	assert validate_policy(policy) == []
	assert validate_policy({**example(), "rate_card": {}}), "an empty card is a card with problems"


def test_validate_policy_never_raises_on_a_hostile_rate_card():
	for card in (
		None,
		"",
		[],
		{},
		5,
		{"profiles": "x"},
		{"profiles": [None, 5, "x", [], {}]},
		{
			"version": float("nan"),
			"currency": [],
			"unit": {},
			"profiles": [{"zones": [[], None, {"tiers": 5}]}],
		},
		card_with(
			lambda c: c["profiles"][2]["zones"][0].update(
				tiers=[{"from": float("nan"), "to": "x", "price": [1]}]
			)
		),
		card_with(lambda c: c["profiles"][2]["zones"][0].update(countries=["US"] * 100_000)),
		card_with(lambda c: c.update(profiles=c["profiles"] * 500)),
	):
		for source in ("rate_card", "carrier", "mystery"):
			problems = validate_policy({**example(), "price_source": source, "rate_card": card})
			assert isinstance(problems, list) and all(isinstance(p, str) and p for p in problems)


# ---------------------------------------------------------------- the card and the policy are left alone
def test_pricing_from_the_card_leaves_the_policy_and_the_order_alone():
	policy = card_policy()
	policy["default_fallback"] = {"method": "rate_card", "amount": 0.0}
	orders = [
		card_order("US", card_item(DANGEROUS, 0.9), card_item(ALLOWED, None)),
		card_order("BH", card_item(DANGEROUS, 0.9)),
		card_order("AE", card_item(REFILL, 0.5)),
		card_order("RU", card_item()),
		card_order("US", items=[]),
	]
	policy_before, orders_before = copy.deepcopy(policy), copy.deepcopy(orders)
	for order in orders:
		resolve(policy, order)
		rule_previews(policy, order)
		resolve({**policy, "price_source": "carrier"}, order)  # the card is also read as a fallback
		resolve({**policy, "active": False}, order)
	validate_policy(policy)
	assert policy == policy_before
	assert orders == orders_before


def test_rule_previews_do_not_depend_on_the_price_source():
	"""Rules are not used when the card prices, but the function keeps working on the draft as it is."""
	for order in (make_order("AE"), make_order("US", quotes=[make_quote(38.2)]), card_order("US")):
		assert rule_previews(card_policy(), order) == rule_previews(example(), order)
	assert [p["amount"] for p in rule_previews(card_policy(), make_order("AE"))][2] == 39.0


# ---------------------------------------------------------------- hostile cards and items
HOSTILE = [
	None,
	True,
	False,
	0,
	-1,
	1,
	2.5,
	float("nan"),
	float("inf"),
	float("-inf"),
	10**400,
	1e308,
	"",
	"5",
	"abc",
	"\ud800",
	[],
	{},
	[1],
	{"a": 1},
]
DELETED = object()


def paths_in(node, prefix=()):
	"""Every place in a nested structure (each dict key and list position, at any depth) as a path."""
	if isinstance(node, dict):
		for key, value in node.items():
			yield (*prefix, key)
			yield from paths_in(value, (*prefix, key))
	elif isinstance(node, list):
		for position, value in enumerate(node):
			yield (*prefix, position)
			yield from paths_in(value, (*prefix, position))


def follow(node, path):
	for step in path:
		node = node[step]
	return node


def junk_variants(card):
	"""The card with each of its places, in turn, holding each hostile value, and with each key, in turn, gone."""
	for path in list(paths_in(card)):
		for junk in HOSTILE:
			variant = copy.deepcopy(card)
			follow(variant, path[:-1])[path[-1]] = copy.deepcopy(junk)
			yield junk, variant
		if isinstance(follow(card, path[:-1]), dict):
			variant = copy.deepcopy(card)
			del follow(variant, path[:-1])[path[-1]]
			yield DELETED, variant


def small_card():
	"""A card with two profiles and every price above zero, small enough to be broken at every place."""
	return {
		"version": 1,
		"currency": "USD",
		"unit": "lb",
		"profiles": [
			{
				"name": "Fragrances",
				"match": ["F-*"],
				"zones": [
					{
						"name": "World",
						"countries": ["US", "BH"],
						"method": "Express (4-7 days)",
						"days": 7,
						"tiers": [
							{"from": 0.0, "to": 2.0, "price": 60.0},
							{"from": 2.01, "to": 5.0, "price": 80.0},
						],
					}
				],
			},
			{
				"name": "Everything else",
				"match": ["*"],
				"zones": [
					{
						"name": "Flat",
						"countries": ["US"],
						"method": "Standard",
						"days": None,
						"tiers": [{"from": None, "to": None, "price": 25.0}],
					}
				],
			},
		],
	}


def sweep_orders():
	return [
		card_order("US", card_item(DANGEROUS, 0.9)),
		card_order("US", card_item(DANGEROUS, 1.0)),
		card_order("US", card_item(ALLOWED, 0.454)),
		card_order("US", card_item(DANGEROUS, 0.9), card_item(ALLOWED, 0.454)),
		card_order("US", card_item(DANGEROUS, 0.9), card_item(REFILL, 0.5), card_item(PETITE, 0.2, qty=3)),
		card_order("BH", card_item(DANGEROUS, 0.9)),
		card_order("AE", card_item(REFILL, 0.5)),
		card_order("US", items=[]),
		card_order("ZZ", card_item()),
		card_order("US", {"item_code": "X", "qty": 1}),
	]


def card_policies(card):
	"""The same card in the two ways it can price: as the price source, and as every fallback of a carrier."""
	fallback = {"method": "rate_card", "amount": 0.0}
	carrier = {**example(), "rate_card": card, "default_fallback": fallback}
	for zone in carrier["zones"]:
		zone["fallback"] = dict(fallback)
	return {"rate card": card_policy(rate_card=card), "carrier with the card as its fallback": carrier}


def check_card_answers(policy, orders, *, free_is_a_fault=False):
	"""Whatever the card holds, an order gets one finite price from the card, or a refusal that offers nothing."""
	for order in orders:
		result = resolve(policy, order)
		check_shape(result)
		json.dumps(result, allow_nan=False)  # it goes over the wire, so there is no NaN or infinity in it
		for offer in result["offers"]:
			assert offer["service_id"] == RATE_CARD_SERVICE_ID and offer["source"] == "rate_card", offer
			assert type(offer["amount"]) is float and math.isfinite(offer["amount"]) and offer["amount"] >= 0
			assert math.copysign(1.0, offer["amount"]) == 1.0
			assert isinstance(offer["name"], str) and offer["name"]
			assert not free_is_a_fault or offer["amount"] > 0, (
				"a price of nothing out of a card that was broken"
			)
		if result["offers"]:
			assert result["offered"] and len(result["offers"]) == 1 and result["message"] == ""
		else:
			assert not result["offered"] and result["reason"] in REFUSALS and result["message"]
		for preview in rule_previews(policy, order):
			assert set(preview) == {"index", "in_zone", "matches", "amount"}
	problems = validate_policy(policy)
	assert isinstance(problems, list) and all(isinstance(p, str) and p for p in problems)


def test_a_rate_card_broken_at_any_one_place_is_answered_and_never_priced_at_nothing():
	card = small_card()
	assert validate_card(card) == []
	orders = [
		card_order("US", card_item(DANGEROUS, 0.9)),
		card_order("US", card_item(DANGEROUS, 1.0)),
		card_order("US", card_item(ALLOWED, 0.454)),
		card_order("US", card_item(DANGEROUS, 0.9), card_item(ALLOWED, 0.454)),
		card_order("BH", card_item(ALLOWED, 0.9)),
		card_order("US", items=[]),
	]
	variants = 0
	for junk, variant in junk_variants(card):
		# every price of the good card is above zero. A card may price at 0.00 only for a number that says so: a 0.
		# A price below zero is no price (the zone prices nothing), never a free delivery.
		says_nothing = isinstance(junk, (int, float)) and not isinstance(junk, bool) and junk == 0
		for policy in card_policies(variant).values():
			check_card_answers(policy, orders, free_is_a_fault=not says_nothing)
		variants += 1
	assert variants > 800, "every place was broken in every way"


def test_no_mix_of_junk_in_a_rate_card_policy_makes_the_engine_raise():
	"""A seeded fuzz over the policy and the sample card together: overwrite or delete a few values, then price,
	preview and validate."""
	rng = random.Random(20261023)
	orders = sweep_orders()
	for _ in range(150):
		for name, make in (
			("rate card", card_policy),
			("fallback", lambda: card_policies(sample_card())["carrier with the card as its fallback"]),
		):
			policy = make()
			for _ in range(rng.randint(1, 6)):
				container, key = rng.choice(places_to_break(policy))
				if isinstance(container, dict) and rng.random() < 0.2:
					del container[key]
				else:
					container[key] = copy.deepcopy(rng.choice([*JUNK, *HOSTILE]))
			for order in orders:
				result = resolve(policy, order)
				check_shape(result)
				assert all(math.isfinite(o["amount"]) and o["amount"] >= 0 for o in result["offers"]), name
				for preview in rule_previews(policy, order):
					assert set(preview) == {"index", "in_zone", "matches", "amount"}
			problems = validate_policy(policy)
			assert isinstance(problems, list) and all(isinstance(p, str) and p for p in problems)


def test_hostile_item_facts_never_raise_and_never_make_a_free_delivery():
	policy = card_policy()

	def priced(**changes):
		item = {**card_item(DANGEROUS, 0.9), **changes}
		result = resolve(policy, card_order("US", item))
		check_shape(result)
		return result

	# a weight that is not a number above zero counts as 0.35 kg, and the receipt says so
	for weight in (
		None,
		True,
		False,
		0,
		-1,
		-0.0,
		float("nan"),
		float("inf"),
		float("-inf"),
		10**400,
		-(10**400),
		"0.9",
		"heavy",
		[],
		{},
		[1],
		{"kg": 1},
	):
		result = priced(weight_kg=weight)
		assert (result["offered"], amount_of(result)) == (True, 60.0), weight
		assert only_offer(result)["trace"][0]["text"].endswith("(weight estimated)"), weight
	# a weight too big to be a number of pounds is no tier's: too heavy, never free
	for weight in (1e308, sys.float_info.max):
		result = priced(weight_kg=weight)
		assert (result["offered"], result["reason"], result["offers"]) == (False, "too_heavy", []), weight
	# a quantity that is not a number above zero leaves the item out; with nothing left, nothing is offered
	for qty in (
		None,
		True,
		False,
		0,
		-1,
		-0.0,
		float("nan"),
		float("inf"),
		float("-inf"),
		10**400,
		"2",
		[],
		{},
	):
		result = priced(qty=qty)
		assert (result["offered"], result["reason"], result["offers"]) == (False, "not_offered", []), qty
	assert priced(qty=1e308)["reason"] == "too_heavy"
	# the product code can be anything: what is not text has no code, and an item with no code is not a fragrance
	for key in (
		None,
		5,
		1.5,
		True,
		[],
		["F-X"],
		{},
		{"F-X": 1},
		b"F-CR",
		"",
		"   ",
		"{}",
		"*",
		"\x00",
		"\ud800",
	):
		result = priced(index_key=key)
		assert (result["offered"], amount_of(result)) == (True, 40.0), key
		assert only_offer(result)["trace"][0]["text"].startswith("Rest of the World Allowed Items"), key
	# things that are not items are left out and the real ones are priced; with only those, nothing is offered
	not_items = [None, 5, "x", [], ["F-X"], {}, {"index_key": DANGEROUS}, float("nan")]  # none has a quantity
	assert amount_of(resolve(policy, card_order("US", *not_items, card_item(DANGEROUS, 0.9)))) == 60.0
	only_junk = resolve(policy, card_order("US", items=not_items))
	assert (only_junk["offered"], only_junk["reason"]) == (False, "not_offered")
	for items in (None, "x", 5, True, {"qty": 1}):
		result = resolve(policy, card_order("US", items=items))
		assert (result["offered"], result["reason"]) == (False, "not_offered"), items
	# the same in the other place the card prices: a carrier's fallback
	fallback = card_policies(sample_card())["carrier with the card as its fallback"]
	for weight in (None, float("nan"), -1, "x"):
		result = resolve(fallback, card_order("US", {**card_item(DANGEROUS, 0.9), "weight_kg": weight}))
		assert (result["reason"], amount_of(result)) == ("fallback_offer", 60.0), weight


def test_oversized_rate_cards_are_priced_without_trouble():
	letters = string.ascii_uppercase
	codes = [a + b for a in letters for b in letters if a + b != "US"]
	order = card_order("US", card_item(DANGEROUS, 0.9))

	def price_with(change):
		card = sample_card()
		change(card)
		result = resolve(card_policy(rate_card=card), order)
		check_shape(result)
		return result

	def many_countries(card):  # 10,000 countries, the one that matters last
		zone_of(card, "Dangerous Goods", "USA")["countries"] = [*(codes * 15)[:10_000], "US"]

	def many_tiers(card):  # 10,000 tiers, from 0 to 1 lb, 1 to 2 lb and so on
		zone_of(card, "Dangerous Goods", "USA")["tiers"] = [
			{"from": float(i), "to": float(i) + 0.9, "price": float(i)} for i in range(10_000)
		]

	def many_zones(card):  # 10,000 zones in the profile, the one that matters last
		profile = card["profiles"][2]
		profile["zones"] = [
			{"name": f"Z{i}", "countries": [codes[i % len(codes)]], "method": "M", "days": 1, "tiers": []}
			for i in range(10_000)
		] + [zone_of(card, "Dangerous Goods", "USA")]

	def many_profiles(card):  # 10,000 profiles that take nothing, then the real ones
		card["profiles"] = [
			{"name": f"P{i}", "match": ["NOTHING-*"], "zones": []} for i in range(10_000)
		] + card["profiles"]

	def many_patterns(card):
		card["profiles"][2]["match"] = [f"NOTHING-{i}-*" for i in range(10_000)] + card["profiles"][2][
			"match"
		]

	def very_deep(card):  # nesting nobody reads
		deep = []
		for _ in range(900):
			deep = [deep]
		card["notes"] = deep

	assert amount_of(price_with(many_countries)) == 60.0
	assert amount_of(price_with(many_tiers)) == 2.0  # the first tier whose upper weight reaches 1.984 lb
	assert amount_of(price_with(many_zones)) == 60.0
	assert amount_of(price_with(many_profiles)) == 60.0
	assert amount_of(price_with(many_patterns)) == 60.0
	assert amount_of(price_with(very_deep)) == 60.0
	# and checking them costs about the same as pricing them
	for change in (many_countries, many_tiers, many_zones, many_profiles, many_patterns):
		card = sample_card()
		change(card)
		problems = validate_policy(card_policy(rate_card=card))
		assert all(isinstance(problem, str) for problem in problems)


def test_a_reason_the_card_does_not_know_is_read_as_not_covered(monkeypatch):
	"""The engine only ever speaks its own reasons, whatever the card module says."""
	for given in ("mystery", None, 5, ["too_heavy"]):
		monkeypatch.setattr(
			shipping_engine,
			"price_card",
			lambda card, order, given=given: {"priced": False, "reason": given, "total": 0.0, "lines": []},
		)
		policy = card_policy()
		policy["zones"][2]["not_offered_message"] = "Rest of world words."
		result = resolve(policy, card_order("US", card_item()))
		check_shape(result)
		assert (result["offered"], result["reason"], result["message"]) == (
			False,
			"not_covered",
			"Rest of world words.",
		)
		assert result["offers"] == []


def test_a_profile_that_is_not_a_profile_is_skipped_not_fatal():
	card = sample_card()
	card["profiles"].insert(0, ["Dangerous Goods", "F-*"])
	card["profiles"].insert(1, "Dangerous Goods")
	card["profiles"].insert(2, None)
	result = resolve(card_policy(rate_card=card), card_order("US", card_item(DANGEROUS, 0.9)))
	check_shape(result)
	assert amount_of(result) == 60.0
	card["profiles"] = [["Dangerous Goods"], "x", None, 5]
	refused = resolve(card_policy(rate_card=card), card_order("US", card_item(DANGEROUS, 0.9)))
	assert (refused["offered"], refused["reason"]) == (False, "not_covered")
	# a zone that is a list, a tier that is text, a prices that is not a number: the zone prices nothing
	card = sample_card()
	zone_of(card, "Dangerous Goods", "USA")["tiers"] = [[0.7, 2.0, 60.0], "tier", None]
	refused = resolve(card_policy(rate_card=card), card_order("US", card_item(DANGEROUS, 0.9)))
	assert (refused["offered"], refused["reason"]) == (False, "not_covered")
	for price in (float("nan"), float("inf"), "60", True, None, [60]):
		card = sample_card()
		zone_of(card, "Dangerous Goods", "USA")["tiers"][1]["price"] = price
		refused = resolve(card_policy(rate_card=card), card_order("US", card_item(DANGEROUS, 0.9)))
		assert (refused["offered"], refused["reason"], refused["offers"]) == (False, "not_covered", []), price
