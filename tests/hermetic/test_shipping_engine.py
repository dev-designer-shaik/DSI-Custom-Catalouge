"""Pins the shipping pricing engine: which zone and rule an order meets, what it is charged, and what
happens when the carrier cannot quote.

Most cases run against the example policy in fixtures/shipping_policy_example.json: four zones
(Bahrain, GCC, Rest of world, Not shipped), a Watches item set, and four rules in this order:

	0  Bahrain, every item, free
	1  GCC, the Watches set, free
	2  GCC, every item, the carrier price, rounded up
	3  Rest of world, every item, max(carrier_price * 1.1, 45), rounded up
"""

import ast
import copy
import json
import math
import random
import sys
from pathlib import Path

import pytest

from dsi_catalogue import shipping_engine
from dsi_catalogue.shipping_engine import (
	DEFAULT_MESSAGES,
	FALLBACK_SERVICE_ID,
	FALLBACK_SERVICE_NAME,
	LABELS,
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

FIXTURE = Path(__file__).parent / "fixtures" / "shipping_policy_example.json"

POLICY_KEYS = {"active", "not_covered", "default_fallback", "zones", "item_sets", "rules"}
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
RESULT_KEYS = {"policy_active", "zone", "rule", "offered", "reason", "message", "offers"}
OFFER_KEYS = {"service_id", "name", "amount", "source", "reason", "trace"}
STEP_KEYS = {"kind", "text", "amount"}
STEP_KINDS = {"carrier", "rule", "formula", "round", "fallback", "result"}


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
	assert isinstance(result["offered"], bool)
	assert result["offered"] == bool(result["offers"])
	assert result["reason"] is None or result["reason"] in REASONS
	assert isinstance(result["message"], str)
	assert result["rule"] is None or (isinstance(result["rule"], int) and result["rule"] >= 0)
	for offer in result["offers"]:
		assert set(offer) == OFFER_KEYS, offer
		assert offer["source"] in ("carrier", "rule", "fallback")
		assert offer["reason"] is None or offer["reason"] in REASONS
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
	assert list(LABELS) == ["method", "zone_fallback", "default_fallback", "not_covered"]
	assert list(LABELS["method"].items()) == [
		("Carrier price", "carrier_price"),
		("Free", "free"),
		("Custom formula", "formula"),
	]
	assert list(LABELS["zone_fallback"].items()) == [
		("Use the policy default", "policy_default"),
		("Don't offer delivery", "no_delivery"),
		("Charge a fixed amount", "fixed"),
		("Free", "free"),
	]
	assert list(LABELS["default_fallback"].items()) == [
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
	)
	assert (FALLBACK_SERVICE_ID, FALLBACK_SERVICE_NAME) == ("policy-fallback", "Standard delivery")
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


@pytest.mark.parametrize("empty", [0.0, 0, None, "absent"])
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


@pytest.mark.parametrize("junk", [float("nan"), float("inf"), float("-inf"), None, "38.2", True, [1]])
def test_rounding_refuses_what_is_not_a_finite_number(junk):
	with pytest.raises(ValueError):
		to_cents(junk)
	with pytest.raises(ValueError):
		round_up_dollar(junk)


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
