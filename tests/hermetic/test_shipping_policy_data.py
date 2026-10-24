"""Pins the shipping policy's data shaping: cleaning what a guest posts, turning database rows into a policy
and back, what a customer may see of the policy, the policy's version, and the list of changes a draft makes.

Most cases run against the example policy in fixtures/shipping_policy_example.json: four zones (Bahrain, GCC,
Rest of world, Not shipped), a Watches item set and four rules, every key present on every object. The
module under test is pure Python; nothing here needs Frappe.
"""

import ast
import copy
import hashlib
import json
import math
import random
import re
from decimal import Decimal
from pathlib import Path

import pytest

from dsi_catalogue import shipping_policy_data
from dsi_catalogue.shipping_engine import LABELS, resolve, validate_policy
from dsi_catalogue.shipping_policy_data import (
	MAX_BOXES,
	MAX_ITEMS,
	MAX_QUOTES,
	InputError,
	clean_boxes,
	clean_items,
	clean_quotes,
	diff_policy,
	parse_json_arg,
	policy_from_rows,
	public_summary_from,
	read_json,
	response_from,
	rows_from_policy,
	snapshot_version,
)
from dsi_catalogue.shipping_rate_card import parse_shopify_export

FIXTURE = Path(__file__).parent / "fixtures" / "shipping_policy_example.json"
SAMPLE_CARD_CSV = Path(__file__).parent / "fixtures" / "shopify_shipping_config_sample.csv"
EN_DASH = chr(0x2013)  # in the names of some zones of the sample card, as the export has it

# The ERP's Country list for the example: the Country name for each ISO code, and the inverse.
COUNTRY_NAMES = {
	"BH": "Bahrain",
	"AE": "United Arab Emirates",
	"SA": "Saudi Arabia",
	"KW": "Kuwait",
	"OM": "Oman",
	"QA": "Qatar",
	"RU": "Russia",
	"BY": "Belarus",
}
COUNTRY_CODES = {name: code for code, name in COUNTRY_NAMES.items()}

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
LIMIT_KEYS = [key for key in sorted(RULE_KEYS) if key.startswith(("min_", "max_"))]

POLICY_ROW_KEYS = {
	"active",
	"price_source",
	"rate_card",
	"not_covered",
	"default_fallback_method",
	"default_fallback_amount",
	"rules",
}
ZONE_ROW_KEYS = {
	"zone_name",
	"enabled",
	"not_shipped",
	"everywhere_else",
	"countries",
	"customer_line",
	"not_offered_message",
	"fallback_method",
	"fallback_amount",
}
SET_ROW_KEYS = {"set_name", "item_groups", "items"}
RULE_ROW_KEYS = RULE_KEYS - {"name"}  # a rule row carries its `name` only when it has one

SUMMARY_KEYS = {"active", "zones"}
SUMMARY_ZONE_KEYS = {"name", "countries", "customer_line", "delivers"}
RESPONSE_KEYS = {"policy_active", "price_source", "zone", "offered", "reason", "message", "offers"}
OFFER_KEYS = {"service_id", "name", "amount", "source", "reason", "estimated_days"}

NOTHING_CHANGED = {
	"zones": {"added": [], "removed": [], "changed": []},
	"item_sets": {"added": [], "removed": [], "changed": []},
	"rules": {"added": [], "removed": [], "changed": [], "order_changed": False},
	"rate_card": {"added": [], "removed": [], "changed": []},
	"settings": [],
}

BIG = 10**400  # a JSON integer too big for a float
OVER_THE_CAP = math.nextafter(1e9, math.inf)  # the first float above 1e9


# ---------------------------------------------------------------- helpers
def example():
	"""A fresh copy of the example policy, so a test can change it freely."""
	return json.loads(FIXTURE.read_text(encoding="utf-8"))


def sample_card():
	"""The trimmed Shopify export of the rate card tests, parsed afresh so a test can change it. Its profiles are,
	in this order, Dangerous Goods Petite, Refill Service, Dangerous Goods and Rest of the World Allowed Items."""
	return parse_shopify_export(SAMPLE_CARD_CSV.read_bytes().decode("utf-8"))["card"]


def card_policy(**changes):
	"""The example policy with the rate card as its price source, holding the sample card."""
	return {**example(), "price_source": "rate_card", "rate_card": sample_card(), **changes}


def small_card():
	"""A card of two profiles, small enough to be broken at every place."""
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


def canonical(value):
	"""JSON text with sorted keys. Two values match here only when every number and flag has the same type
	too: 0 is not 0.0, and 1 is not true."""
	return json.dumps(value, sort_keys=True)


def named(policy):
	"""The policy with every rule given a name, as the rules read from the database have."""
	for position, rule in enumerate(policy["rules"]):
		rule["name"] = f"row-{position}"
	return policy


def make_zone(name, **changes):
	zone = {
		"name": name,
		"enabled": True,
		"countries": [],
		"not_shipped": False,
		"everywhere_else": False,
		"customer_line": "",
		"not_offered_message": "",
		"fallback": {"method": "policy_default", "amount": 0.0},
	}
	return {**zone, **changes}


def make_rule(zone="GCC", **changes):
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


def item(**changes):
	return {"item_code": "PERF-1", "qty": 1, **changes}


def box(**changes):
	return {"weight_kg": 1.0, "volume_l": 2.0, **changes}


def quote(**changes):
	return {"service_id": "PDX", "name": "Priority", "amount": 10.0, "boxes_quoted": 1, **changes}


def without(entry, key):
	return {k: v for k, v in entry.items() if k != key}


class Translator:
	"""A `translate` that marks what it is given and remembers it."""

	def __init__(self):
		self.asked = []

	def __call__(self, text):
		self.asked.append(text)
		return f"[ar] {text}"


def all_keys(value):
	"""Every dict key at any depth, so a test can look for a key that must not be there."""
	if isinstance(value, dict):
		for key, inner in value.items():
			yield key
			yield from all_keys(inner)
	elif isinstance(value, (list, tuple)):
		for inner in value:
			yield from all_keys(inner)


def reversed_keys(value):
	"""The same value with the keys of every dict in the opposite order."""
	if isinstance(value, dict):
		return {key: reversed_keys(value[key]) for key in reversed(list(value))}
	if isinstance(value, list):
		return [reversed_keys(inner) for inner in value]
	return value


def with_junk_at_every_place(value, junk):
	"""A copy of `value` for each place in it (each dict key, each list position, at any depth), with `junk`
	put in that place."""
	if isinstance(value, dict):
		for key, inner in value.items():
			yield copy.deepcopy({**value, key: junk})
			for deeper in with_junk_at_every_place(inner, junk):
				yield copy.deepcopy({**value, key: deeper})
	elif isinstance(value, list):
		for position, inner in enumerate(value):
			yield copy.deepcopy([*value[:position], junk, *value[position + 1 :]])
			for deeper in with_junk_at_every_place(inner, junk):
				yield copy.deepcopy([*value[:position], deeper, *value[position + 1 :]])


# Content that a database row or a guest's JSON could hold but no field should: wrong type, empty, hostile.
JUNK = [
	None,
	True,
	0,
	-1,
	BIG,
	1.5,
	float("nan"),
	float("inf"),
	"",
	"text",
	"  ",
	[],
	["x", None, 3],
	{},
	{"a": [1]},
]
JUNK_IDS = [repr(junk)[:24] for junk in JUNK]


def assert_number(value, *, above_zero=False):
	assert type(value) is float, value
	assert math.isfinite(value) and 0 <= value <= 1e9, value
	assert math.copysign(1.0, value) == 1.0, "no negative zero"
	assert not above_zero or value > 0


def assert_text(value):
	assert type(value) is str and value == value.strip() and 0 < len(value) <= 140, value
	assert value.isprintable(), "no control character, NUL or lone surrogate gets through"


def assert_clean(kind, cleaned):
	"""What every successful clean_* call promises, whatever it was given."""
	assert type(cleaned) is list
	if kind == "items":
		assert len(cleaned) <= MAX_ITEMS
		for entry in cleaned:
			assert set(entry) == {"item_code", "qty"}
			assert_text(entry["item_code"])
			assert_number(entry["qty"], above_zero=True)
	elif kind == "boxes":
		assert len(cleaned) <= MAX_BOXES
		for entry in cleaned:
			assert set(entry) == {"weight_kg", "volume_l"}
			assert_number(entry["weight_kg"])
			assert_number(entry["volume_l"])
	else:
		assert len(cleaned) <= MAX_QUOTES
		for entry in cleaned:
			assert set(entry) == {"service_id", "name", "amount", "boxes_quoted"}
			assert_text(entry["service_id"])
			assert_text(entry["name"])
			assert_number(entry["amount"])
			assert type(entry["boxes_quoted"]) is int and 0 <= entry["boxes_quoted"] <= MAX_BOXES


CLEANERS = {"items": clean_items, "boxes": clean_boxes, "quotes": clean_quotes}
VALID = {
	"items": [item(), item(item_code="WATCH-9", qty=0.5)],
	"boxes": [box(), box(weight_kg=0.35, volume_l=0)],
	"quotes": [quote(), quote(service_id="EPX", amount=12, boxes_quoted=2)],
}


# ---------------------------------------------------------------- the module's place in the design
def test_the_limits_are_those_of_the_brief():
	assert (MAX_ITEMS, MAX_BOXES, MAX_QUOTES) == (200, 50, 20)
	assert issubclass(InputError, ValueError)


def test_the_data_module_is_pure_python_that_takes_only_the_labels_from_the_engine():
	source = Path(shipping_policy_data.__file__).read_text(encoding="utf-8")
	imported, from_engine = set(), set()
	for node in ast.walk(ast.parse(source)):
		if isinstance(node, ast.Import):
			imported.update(alias.name for alias in node.names)
		elif isinstance(node, ast.ImportFrom):
			imported.add(node.module or "")
			if node.module == "dsi_catalogue.shipping_engine":
				from_engine.update(alias.name for alias in node.names)
	assert not {name for name in imported if name.split(".")[0] == "frappe"}
	assert from_engine == {"LABELS"}
	# nothing of the package comes in any other way, such as `import dsi_catalogue.shipping_engine`
	assert {name for name in imported if name.startswith("dsi_catalogue")} == {
		"dsi_catalogue.shipping_engine"
	}


# ---------------------------------------------------------------- reading a JSON argument
def test_parse_json_arg_reads_json_text_and_hands_back_anything_else():
	default = ["unused"]
	for empty in (None, "", "   ", "\n\t "):
		assert parse_json_arg(empty, default) is default
	assert parse_json_arg('[{"a": 1}]', default) == [{"a": 1}]
	assert parse_json_arg(' {"a": [1, 2]}\n', default) == {"a": [1, 2]}
	assert parse_json_arg("5", default) == 5
	assert parse_json_arg('"text"', default) == "text"
	assert parse_json_arg("null", default) is None
	# anything that is not text comes back as it is, falsy values too
	for value in ([], {}, 0, False, 2.5, ["x"], {"k": "v"}, ("t",), b"[]"):
		assert parse_json_arg(value, default) is value


@pytest.mark.parametrize(
	"text",
	[
		"[",
		'{"a": }',
		"not json",
		"[1, 2",
		"'single quotes'",
		'{"secret": SECRET-MARK}',
		"1" * 5000,  # an integer with more digits than Python will read
		"[" * 100000,  # nested deeper than Python will read
	],
	ids=["open", "gap", "words", "unclosed", "quotes", "secret", "5000 digits", "deep"],
)
def test_parse_json_arg_turns_every_failure_into_an_input_error(text):
	with pytest.raises(InputError) as caught:
		parse_json_arg(text, [])
	assert str(caught.value) == "The value is not valid JSON."
	assert "SECRET-MARK" not in str(caught.value) and text[:20] not in str(caught.value)
	assert isinstance(caught.value, ValueError)


# ---------------------------------------------------------------- cleaning what a guest posts
@pytest.mark.parametrize("kind", CLEANERS)
def test_clean_inputs_accept_json_strings_and_lists(kind):
	clean = CLEANERS[kind]
	given = VALID[kind]
	before = copy.deepcopy(given)
	cleaned = clean(given)
	assert_clean(kind, cleaned)
	assert len(cleaned) == 2
	assert given == before, "the caller's list is left alone"
	assert cleaned is not given
	# the same list as JSON text, with or without white space around it
	assert clean(json.dumps(given)) == cleaned
	assert clean(f"  \n{json.dumps(given)}\t ") == cleaned
	# cleaning twice changes nothing more
	assert clean(cleaned) == cleaned
	# nothing at all is an empty list
	for nothing in (None, "", "   ", [], "[]", "null"):
		assert clean(nothing) == []


def test_clean_items_give_the_code_and_a_float_quantity():
	assert clean_items([item(), item(item_code=" WATCH-9 ", qty=0.5), item(qty=3)]) == [
		{"item_code": "PERF-1", "qty": 1.0},
		{"item_code": "WATCH-9", "qty": 0.5},
		{"item_code": "PERF-1", "qty": 3.0},
	]


def test_clean_boxes_give_float_weights_and_volumes():
	assert clean_boxes([box(weight_kg=2, volume_l=0), box(weight_kg=0.35, volume_l=6.25)]) == [
		{"weight_kg": 2.0, "volume_l": 0.0},
		{"weight_kg": 0.35, "volume_l": 6.25},
	]


def test_clean_quotes_give_the_service_the_name_the_amount_and_whole_cartons_quoted():
	assert clean_quotes(
		[quote(amount=38.2, boxes_quoted=2), quote(service_id="EPX", name="Express", amount=0)]
	) == [
		{"service_id": "PDX", "name": "Priority", "amount": 38.2, "boxes_quoted": 2},
		{"service_id": "EPX", "name": "Express", "amount": 0.0, "boxes_quoted": 1},
	]


def test_unknown_extra_keys_are_dropped():
	(cleaned_item,) = clean_items([item(groups=["Watches"], price=99, note="x")])
	(cleaned_box,) = clean_boxes([box(items=3, packed_by="x")])
	(cleaned_quote,) = clean_quotes([quote(trace=[1], rule=2, source="rule", zone="GCC")])
	assert set(cleaned_item) == {"item_code", "qty"}
	assert set(cleaned_box) == {"weight_kg", "volume_l"}
	assert set(cleaned_quote) == {"service_id", "name", "amount", "boxes_quoted"}


def test_numbers_come_back_as_plain_floats_and_zero_is_never_negative():
	for number in (3, 2.5, 10**9, 0):
		(cleaned,) = clean_boxes([box(weight_kg=number, volume_l=number)])
		assert_number(cleaned["weight_kg"])
		assert cleaned["weight_kg"] == number
	(cleaned,) = clean_boxes([box(weight_kg=-0.0, volume_l=-0.0)])
	assert math.copysign(1.0, cleaned["weight_kg"]) == 1.0 and math.copysign(1.0, cleaned["volume_l"]) == 1.0
	(cleaned,) = clean_quotes([quote(amount=-0.0)])
	assert math.copysign(1.0, cleaned["amount"]) == 1.0


# What is refused. The first nine are the cases the brief names.
REFUSED = [
	pytest.param(clean_items, [item()] * (MAX_ITEMS + 1), id="201 items"),
	pytest.param(clean_boxes, [box()] * (MAX_BOXES + 1), id="51 boxes"),
	pytest.param(clean_quotes, [quote()] * (MAX_QUOTES + 1), id="21 quotes"),
	pytest.param(clean_items, [item(qty=0)], id="qty 0"),
	pytest.param(clean_quotes, [quote(amount=-1)], id="amount -1"),
	pytest.param(clean_quotes, [quote(amount=float("nan"))], id="amount nan"),
	pytest.param(clean_quotes, [quote(amount=float("inf"))], id="amount inf"),
	pytest.param(clean_quotes, [without(quote(), "service_id")], id="missing service_id"),
	pytest.param(clean_quotes, [quote(boxes_quoted=-1)], id="boxes_quoted -1"),
]


@pytest.mark.parametrize(("clean", "given"), REFUSED)
def test_clean_inputs_refuse_oversize_negative_and_non_finite_values(clean, given):
	with pytest.raises(InputError) as caught:
		clean(given)
	message = str(caught.value)
	assert message[0].isupper() and message.endswith("."), "a plain sentence"
	# the same input as JSON text is refused just the same (the numbers survive json.dumps)
	with pytest.raises(InputError):
		clean(json.dumps(given))


def test_the_boundary_sizes_are_accepted():
	assert len(clean_items([item()] * MAX_ITEMS)) == MAX_ITEMS
	assert len(clean_boxes([box()] * MAX_BOXES)) == MAX_BOXES
	assert len(clean_quotes([quote()] * MAX_QUOTES)) == MAX_QUOTES


@pytest.mark.parametrize(
	("clean", "limit"), [(clean_items, MAX_ITEMS), (clean_boxes, MAX_BOXES), (clean_quotes, MAX_QUOTES)]
)
def test_an_oversize_list_is_refused_before_its_entries_are_read(clean, limit):
	with pytest.raises(InputError) as caught:
		clean([None] * (limit + 1))
	assert f"at most {limit}" in str(caught.value)


NUMBER_FIELDS = [
	pytest.param(clean_items, item, "qty", id="qty"),
	pytest.param(clean_boxes, box, "weight_kg", id="weight_kg"),
	pytest.param(clean_boxes, box, "volume_l", id="volume_l"),
	pytest.param(clean_quotes, quote, "amount", id="amount"),
]
BAD_NUMBERS = {
	"negative": -1,
	"slightly negative": -0.1,
	"minus infinity": float("-inf"),
	"nan": float("nan"),
	"infinity": float("inf"),
	"text": "5",
	"nothing": None,
	"true": True,
	"false": False,
	"list": [1],
	"object": {"a": 1},
	"just over the cap": OVER_THE_CAP,
	"over the cap": 1e9 + 1,
	"a huge float": 1e300,
	"a huge int": BIG,
	"a huge negative int": -BIG,
}


@pytest.mark.parametrize(("clean", "build", "field"), NUMBER_FIELDS)
@pytest.mark.parametrize("bad", BAD_NUMBERS.values(), ids=BAD_NUMBERS.keys())
def test_a_number_has_to_be_a_real_finite_number_from_zero_to_one_billion(clean, build, field, bad):
	with pytest.raises(InputError):
		clean([build(**{field: bad})])


@pytest.mark.parametrize(("clean", "build", "field"), NUMBER_FIELDS)
def test_a_missing_number_is_refused(clean, build, field):
	with pytest.raises(InputError):
		clean([without(build(), field)])


@pytest.mark.parametrize(("clean", "build", "field"), NUMBER_FIELDS)
@pytest.mark.parametrize("edge", [1e9, 10**9, 1e9 - 1, 1e-300, 0.000001, 0.5, 1], ids=repr)
def test_numbers_up_to_one_billion_are_accepted(clean, build, field, edge):
	(cleaned,) = clean([build(**{field: edge})])
	assert_number(cleaned[field])
	assert cleaned[field] == edge


@pytest.mark.parametrize(("clean", "build", "field"), NUMBER_FIELDS[1:])
@pytest.mark.parametrize("zero", [0, 0.0, -0.0])
def test_zero_is_fine_for_a_weight_a_volume_and_an_amount(clean, build, field, zero):
	(cleaned,) = clean([build(**{field: zero})])
	assert_number(cleaned[field])
	assert cleaned[field] == 0


@pytest.mark.parametrize("zero", [0, 0.0, -0.0])
def test_a_quantity_has_to_be_above_zero(zero):
	with pytest.raises(InputError):
		clean_items([item(qty=zero)])


def test_a_json_integer_too_big_for_a_float_is_refused_as_input_not_as_an_overflow():
	big = "1" + "0" * 400
	for clean, text in (
		(clean_items, f'[{{"item_code": "A", "qty": {big}}}]'),
		(clean_items, f'[{{"item_code": "A", "qty": -{big}}}]'),
		(clean_boxes, f'[{{"weight_kg": {big}, "volume_l": 1}}]'),
		(clean_boxes, f'[{{"weight_kg": 1, "volume_l": {big}}}]'),
		(clean_quotes, f'[{{"service_id": "A", "amount": {big}}}]'),
		(clean_quotes, f'[{{"service_id": "A", "amount": 1, "boxes_quoted": {big}}}]'),
		(clean_quotes, '[{"service_id": "A", "amount": 1e999}]'),  # JSON that Python reads as infinity
		(clean_items, '[{"item_code": "A", "qty": NaN}]'),
		(clean_items, '[{"item_code": "A", "qty": Infinity}]'),
	):
		with pytest.raises(InputError):
			clean(text)
	# as a Python int too, and as text with more digits than Python reads
	with pytest.raises(InputError):
		clean_items([item(qty=10**400)])
	with pytest.raises(InputError):
		clean_items(f'[{{"item_code": "A", "qty": {"1" * 5000}}}]')


@pytest.mark.parametrize(
	("given", "expected"), [(0, 0), (1, 1), (1.0, 1), (2.0, 2), (50, 50), (50.0, 50), (None, 0)], ids=repr
)
def test_boxes_quoted_is_a_whole_number_of_cartons(given, expected):
	(cleaned,) = clean_quotes([quote(boxes_quoted=given)])
	assert cleaned["boxes_quoted"] == expected and type(cleaned["boxes_quoted"]) is int


def test_boxes_quoted_missing_means_none_were_quoted():
	(cleaned,) = clean_quotes([without(quote(), "boxes_quoted")])
	assert cleaned["boxes_quoted"] == 0 and type(cleaned["boxes_quoted"]) is int
	assert clean_quotes('[{"service_id": "A", "amount": 3}]') == [
		{"service_id": "A", "name": "A", "amount": 3.0, "boxes_quoted": 0}
	]


@pytest.mark.parametrize(
	"bad",
	[
		1.5,
		0.5,
		50.5,
		51,
		51.0,
		-1,
		-0.5,
		True,
		False,
		"1",
		[],
		{},
		float("nan"),
		float("inf"),
		BIG,
		-BIG,
		1e300,
	],
	ids=repr,
)
def test_boxes_quoted_that_is_not_a_whole_number_of_cartons_is_refused(bad):
	with pytest.raises(InputError):
		clean_quotes([quote(boxes_quoted=bad)])


TEXT_FIELDS = [
	pytest.param(clean_items, item, "item_code", id="item_code"),
	pytest.param(clean_quotes, quote, "service_id", id="service_id"),
]


@pytest.mark.parametrize(("clean", "build", "field"), TEXT_FIELDS)
def test_an_id_has_to_be_non_empty_text_of_at_most_140_characters(clean, build, field):
	for bad in ("", "   ", "\t\n", None, 5, 1.5, True, [], ["A"], {}):
		with pytest.raises(InputError):
			clean([build(**{field: bad})])
	with pytest.raises(InputError):
		clean([without(build(), field)])
	with pytest.raises(InputError):
		clean([build(**{field: "x" * 141})])
	(cleaned,) = clean([build(**{field: "x" * 140})])
	assert cleaned[field] == "x" * 140


@pytest.mark.parametrize(("clean", "build", "field"), TEXT_FIELDS)
def test_an_id_is_stripped_and_the_limit_counts_the_stripped_text(clean, build, field):
	(cleaned,) = clean([build(**{field: "  A-1\t\n"})])
	assert cleaned[field] == "A-1"
	(cleaned,) = clean([build(**{field: f" {'x' * 140} "})])
	assert cleaned[field] == "x" * 140


def test_a_quote_name_defaults_to_its_service_id_and_is_stripped():
	assert clean_quotes([without(quote(service_id="EPX"), "name")])[0]["name"] == "EPX"
	for empty in (None, "", "   ", "\n"):
		assert clean_quotes([quote(service_id=" EPX ", name=empty)])[0]["name"] == "EPX"
	assert clean_quotes([quote(name="  Priority express \n")])[0]["name"] == "Priority express"


def test_a_quote_name_has_to_be_text_of_at_most_140_characters():
	assert clean_quotes([quote(name="n" * 140)])[0]["name"] == "n" * 140
	for bad in ("n" * 141, 5, 1.5, True, False, [], ["Priority"], {}):
		with pytest.raises(InputError):
			clean_quotes([quote(name=bad)])


# Text that JSON can carry and Python holds, but that no id or name should be: a lone surrogate cannot be written
# as UTF-8 (the database driver and Frappe's JSON encoder both raise on it), and a control character or NUL has no
# place in a code a person types.
NOT_PLAIN = {
	"a lone low surrogate": "A\ud800B",
	"a lone surrogate alone": "\ud800",
	"a lone high surrogate": "N\ud83d",
	"a NUL": "A\x00B",
	"a tab": "A\tB",
	"a new line": "A\nB",
	"an escape sequence": "A\x1b[0mB",
	"a delete character": "A\x7fB",
	"a line separator": "A\u2028B",
}
# What stays: any script, a space inside, punctuation, an emoji.
PLAIN = {
	"letters, digits and a space": "AB 12",
	"an Arabic name": "شركة الشحن السريع",
	"accents and Japanese": "Café 日本",
	"punctuation": "PERF-1/#2 (50 ml) & more",
	"an emoji": "\U0001f4e6 box",
}
PLAIN_TEXT_FIELDS = [*TEXT_FIELDS, pytest.param(clean_quotes, quote, "name", id="name")]


def who_of(clean):
	return "Item 1" if clean is clean_items else "Quote 1"


@pytest.mark.parametrize(("clean", "build", "field"), PLAIN_TEXT_FIELDS)
@pytest.mark.parametrize("bad", NOT_PLAIN.values(), ids=NOT_PLAIN.keys())
def test_an_id_or_a_name_that_is_not_plain_text_is_refused_with_a_sentence_that_does_not_repeat_it(
	clean, build, field, bad
):
	with pytest.raises(InputError) as caught:
		clean([build(**{field: bad})])
	assert str(caught.value) == f"{who_of(clean)}: {field} has to be plain text."
	with pytest.raises(InputError, match="has to be plain text"):
		clean(json.dumps([build(**{field: bad})]))  # and as the JSON text a form post sends


@pytest.mark.parametrize(("clean", "build", "field"), PLAIN_TEXT_FIELDS)
@pytest.mark.parametrize("good", PLAIN.values(), ids=PLAIN.keys())
def test_plain_text_in_any_script_is_kept_as_it_is(clean, build, field, good):
	(cleaned,) = clean([build(**{field: good})])
	assert cleaned[field] == good
	(cleaned,) = clean([build(**{field: f"  {good}\t\n"})])
	assert cleaned[field] == good, "white space around it is still stripped, and not what is checked"


def test_json_text_with_a_lone_surrogate_really_gives_python_one_and_is_refused():
	# This is why the refusal exists: json.loads accepts the escape and returns text nothing can write.
	assert json.loads('"A\\ud800B"') == "A\ud800B"
	with pytest.raises(UnicodeEncodeError):
		"A\ud800B".encode("utf-8")
	with pytest.raises(InputError, match=re.escape("Item 1: item_code has to be plain text.")):
		clean_items('[{"item_code": "A\\ud800B", "qty": 1}]')
	with pytest.raises(InputError, match=re.escape("Quote 1: service_id has to be plain text.")):
		clean_quotes('[{"service_id": "A\\ud800B", "amount": 1}]')
	with pytest.raises(InputError, match=re.escape("Quote 1: name has to be plain text.")):
		clean_quotes('[{"service_id": "A", "name": "N\\ud83d", "amount": 1}]')


def test_a_name_that_is_empty_once_stripped_still_defaults_to_the_service_id():
	for empty in ("", "  ", "\t\n"):
		assert clean_quotes([quote(service_id="EPX", name=empty)])[0]["name"] == "EPX"


NOT_LISTS = {
	"a number": 5,
	"a float": 1.5,
	"true": True,
	"false": False,
	"zero": 0,
	"an object": {"item_code": "A"},
	"an empty object": {},
	"bytes": b"[]",
	"JSON of an object": '{"a": 1}',
	"JSON of text": '"text"',
	"JSON of a number": "5",
	"JSON of true": "true",
	"words": "not json",
	"open bracket": "[",
}


@pytest.mark.parametrize("clean", CLEANERS.values(), ids=CLEANERS.keys())
@pytest.mark.parametrize("value", NOT_LISTS.values(), ids=NOT_LISTS.keys())
def test_a_value_that_is_not_a_list_of_entries_is_refused(clean, value):
	with pytest.raises(InputError):
		clean(value)


@pytest.mark.parametrize("kind", CLEANERS)
def test_a_tuple_is_not_a_list_even_when_every_entry_is_fine(kind):
	with pytest.raises(InputError, match="have to be a list"):
		CLEANERS[kind](tuple(VALID[kind]))


@pytest.mark.parametrize("kind", CLEANERS)
@pytest.mark.parametrize("entry", ["text", 5, 1.5, None, True, [], ["a"], ("a",)], ids=repr)
def test_an_entry_that_is_not_an_object_is_refused_and_so_is_the_whole_list(kind, entry):
	clean = CLEANERS[kind]
	with pytest.raises(InputError):
		clean([entry])
	with pytest.raises(InputError):
		clean([*VALID[kind], entry])  # a good list with one bad entry gives nothing back
	with pytest.raises(InputError):
		clean(json.dumps([entry]))


MARK = "MARK<b>x</b>"


@pytest.mark.parametrize(
	("clean", "bad"),
	[
		(clean_items, item(qty=MARK)),
		(clean_items, item(item_code=MARK * 30)),
		(clean_items, MARK),
		(clean_boxes, box(weight_kg=MARK)),
		(clean_boxes, box(volume_l=MARK)),
		(clean_boxes, MARK),
		(clean_quotes, quote(amount=MARK)),
		(clean_quotes, quote(service_id=MARK * 30)),
		(clean_quotes, quote(name=MARK * 30)),
		(clean_quotes, quote(boxes_quoted=MARK)),
		(clean_quotes, MARK),
	],
	ids=lambda value: value.__name__ if callable(value) else None,
)
def test_refusals_are_plain_sentences_that_do_not_repeat_what_was_sent(clean, bad):
	with pytest.raises(InputError) as caught:
		clean([bad])
	message = str(caught.value)
	assert "MARK" not in message and "<" not in message
	assert message[0].isupper() and message.endswith(".")
	assert "Traceback" not in message and "0x" not in message and "Error" not in message


def test_a_refusal_says_which_entry_it_is_about():
	with pytest.raises(InputError, match="Item 3"):
		clean_items([item(), item(), item(qty=0)])
	with pytest.raises(InputError, match="Box 2"):
		clean_boxes([box(), box(weight_kg=-1)])
	with pytest.raises(InputError, match="Quote 2"):
		clean_quotes([quote(), quote(amount=-1)])


@pytest.mark.parametrize(
	("clean", "given", "message"),
	[
		(clean_items, "[", "The items are not valid JSON."),
		(clean_boxes, "[", "The boxes are not valid JSON."),
		(clean_quotes, "[", "The quotes are not valid JSON."),
		(clean_items, {"a": 1}, "The items have to be a list."),
		(clean_boxes, 5, "The boxes have to be a list."),
		(clean_quotes, "5", "The quotes have to be a list."),
		(clean_items, [item()] * 201, "There are too many items: at most 200 are allowed."),
		(clean_boxes, [box()] * 51, "There are too many boxes: at most 50 are allowed."),
		(clean_quotes, [quote()] * 21, "There are too many quotes: at most 20 are allowed."),
		(clean_items, ["x"], "Item 1 has to be an object."),
		(clean_boxes, [box(), None], "Box 2 has to be an object."),
		(clean_quotes, [5], "Quote 1 has to be an object."),
		(clean_items, [item(qty=0)], "Item 1: qty has to be a number above 0 and at most 1000000000."),
		(clean_boxes, [box(weight_kg=-1)], "Box 1: weight_kg has to be a number from 0 to 1000000000."),
		(clean_boxes, [box(volume_l="x")], "Box 1: volume_l has to be a number from 0 to 1000000000."),
		(clean_quotes, [quote(amount=None)], "Quote 1: amount has to be a number from 0 to 1000000000."),
		(
			clean_quotes,
			[quote(boxes_quoted=1.5)],
			"Quote 1: boxes_quoted has to be a whole number from 0 to 50.",
		),
		(clean_items, [item(item_code=" ")], "Item 1: item_code has to be text that is not empty."),
		(clean_quotes, [quote(service_id=None)], "Quote 1: service_id has to be text that is not empty."),
		(clean_items, [item(item_code="x" * 141)], "Item 1: item_code is longer than 140 characters."),
		(clean_quotes, [quote(service_id="x" * 141)], "Quote 1: service_id is longer than 140 characters."),
		(clean_quotes, [quote(name=5)], "Quote 1: name has to be text."),
		(clean_quotes, [quote(name="n" * 141)], "Quote 1: name is longer than 140 characters."),
		(clean_items, [item(item_code="A\x00B")], "Item 1: item_code has to be plain text."),
		(clean_quotes, [quote(service_id="A\ud800B")], "Quote 1: service_id has to be plain text."),
		(clean_quotes, [quote(name="N\tB")], "Quote 1: name has to be plain text."),
	],
	ids=lambda value: value if isinstance(value, str) and value.endswith(".") else None,
)
def test_each_refusal_says_what_is_wrong_in_a_plain_sentence(clean, given, message):
	with pytest.raises(InputError) as caught:
		clean(given)
	assert str(caught.value) == message


@pytest.mark.parametrize("kind", CLEANERS)
@pytest.mark.parametrize("junk", JUNK, ids=JUNK_IDS)
def test_cleaners_raise_input_error_or_return_clean_data_whatever_they_are_given(kind, junk):
	clean = CLEANERS[kind]
	for variant in with_junk_at_every_place(VALID[kind], junk):
		for given in (variant, json.dumps(variant)):
			try:
				cleaned = clean(given)
			except InputError:
				continue
			assert_clean(kind, cleaned)


@pytest.mark.parametrize("kind", CLEANERS)
@pytest.mark.parametrize("hostile", NOT_PLAIN.values(), ids=NOT_PLAIN.keys())
def test_unwritable_text_is_refused_or_cleaned_whatever_place_it_is_put(kind, hostile):
	clean = CLEANERS[kind]
	for variant in with_junk_at_every_place(VALID[kind], hostile):
		for given in (variant, json.dumps(variant)):
			try:
				cleaned = clean(given)
			except InputError:
				continue
			assert_clean(kind, cleaned)  # assert_text: an id or a name that got through is plain


# ---------------------------------------------------------------- database rows <-> the policy
def test_policy_round_trips_through_rows():
	policy = example()
	rows = rows_from_policy(policy, COUNTRY_NAMES)
	assert set(rows) == {"policy", "zones", "item_sets"}
	back = policy_from_rows(rows["policy"], rows["zones"], rows["item_sets"], COUNTRY_CODES)
	assert back == policy
	assert canonical(back) == canonical(policy), "the same types too: floats stay floats, flags stay flags"
	# Select labels and Country names went into the rows, not the engine's codes
	assert rows["policy"]["not_covered"] == "Pass the carrier price through"
	assert rows["policy"]["default_fallback_method"] == "Don't offer delivery"
	assert [row["fallback_method"] for row in rows["zones"]] == [
		"Use the policy default",
		"Charge a fixed amount",
		"Use the policy default",
		"Use the policy default",
	]
	assert [row["method"] for row in rows["policy"]["rules"]] == [
		"Free",
		"Free",
		"Carrier price",
		"Custom formula",
	]
	assert [row["country"] for row in rows["zones"][1]["countries"]] == [
		"United Arab Emirates",
		"Saudi Arabia",
		"Kuwait",
		"Oman",
		"Qatar",
	]
	assert rows["zones"][2]["countries"] == []


def busy_policy():
	"""A policy with a value of its own, different from every default, in every field."""
	return {
		"active": False,
		"price_source": "rate_card",
		"rate_card": {
			"version": 1,
			"currency": "USD",
			"unit": "lb",
			"profiles": [
				{
					"name": "Profil d'\u00e9t\u00e9",
					"match": ["A-*"],
					"zones": [
						{
							"name": "Zone A",
							"countries": ["AE", "QA"],
							"method": "Express (4-7 days)",
							"days": 7,
							"tiers": [
								{"from": 0.5, "to": 2.5, "price": 12.25},
								{"from": 2.51, "to": 5.0, "price": 40.0},
							],
						},
						{
							"name": "Flat",
							"countries": ["BH"],
							"method": "Free",
							"days": None,
							"tiers": [{"from": None, "to": None, "price": 0.0}],
						},
					],
				}
			],
		},
		"not_covered": "no_delivery",
		"default_fallback": {"method": "fixed", "amount": 12.5},
		"zones": [
			{
				"name": "Zone A",
				"enabled": False,
				"countries": ["AE", "QA"],
				"not_shipped": True,
				"everywhere_else": False,
				"customer_line": "Line A",
				"not_offered_message": "Message A",
				"fallback": {"method": "free", "amount": 1.25},
			},
			{
				"name": "Zone B",
				"enabled": True,
				"countries": [],
				"not_shipped": False,
				"everywhere_else": True,
				"customer_line": "Line B",
				"not_offered_message": "Message B",
				"fallback": {"method": "no_delivery", "amount": 2.5},
			},
		],
		"item_sets": [
			{"name": "Set A", "item_groups": ["Group 1", "Group 2"], "items": ["ITEM-1", "ITEM-2"]},
			{"name": "Set B", "item_groups": [], "items": ["ITEM-3"]},
		],
		"rules": [
			{
				"name": "row-a",
				"enabled": True,
				"label": "Rule A",
				"zone": "Zone A",
				"item_set": "Set A",
				"min_order_value": 1.5,
				"max_order_value": 2.5,
				"min_weight_kg": 3.5,
				"max_weight_kg": 4.5,
				"min_volume_l": 5.5,
				"max_volume_l": 6.5,
				"method": "formula",
				"formula": "carrier_price * 2",
				"round_up": True,
			},
			{
				"name": "row-b",
				"enabled": False,
				"label": "Rule B",
				"zone": "Zone B",
				"item_set": None,
				"min_order_value": 7.5,
				"max_order_value": 8.5,
				"min_weight_kg": 9.5,
				"max_weight_kg": 10.5,
				"min_volume_l": 11.5,
				"max_volume_l": 12.5,
				"method": "free",
				"formula": "",
				"round_up": False,
			},
		],
	}


def test_every_field_survives_the_round_trip_with_a_value_of_its_own():
	"""A field that is dropped, renamed or swapped with another in either direction cannot hide here."""
	policy = busy_policy()
	rows = rows_from_policy(policy, COUNTRY_NAMES)
	back = policy_from_rows(rows["policy"], rows["zones"], rows["item_sets"], COUNTRY_CODES)
	assert back == policy
	assert canonical(back) == canonical(policy)
	# and in the rows themselves
	assert rows["policy"]["price_source"] == "Rate card"
	assert json.loads(rows["policy"]["rate_card"]) == policy["rate_card"]
	assert rows["policy"]["default_fallback_amount"] == 12.5
	assert rows["policy"]["default_fallback_method"] == "Charge a fixed amount"
	assert [row["fallback_amount"] for row in rows["zones"]] == [1.25, 2.5]
	assert rows["item_sets"][0] == {
		"set_name": "Set A",
		"item_groups": [{"item_group": "Group 1"}, {"item_group": "Group 2"}],
		"items": [{"item": "ITEM-1"}, {"item": "ITEM-2"}],
	}
	assert rows["policy"]["rules"][0]["name"] == "row-a"
	assert rows["policy"]["rules"][0]["item_set"] == "Set A"
	assert {key: rows["policy"]["rules"][0][key] for key in LIMIT_KEYS} == {
		"min_order_value": 1.5,
		"max_order_value": 2.5,
		"min_weight_kg": 3.5,
		"max_weight_kg": 4.5,
		"min_volume_l": 5.5,
		"max_volume_l": 6.5,
	}
	flags = (
		rows["zones"][0]["enabled"],
		rows["zones"][0]["not_shipped"],
		rows["zones"][1]["everywhere_else"],
	)
	assert flags == (0, 1, 1)


def test_a_number_field_that_holds_no_number_is_left_for_validate_policy_to_report():
	rows = rows_from_policy(example(), COUNTRY_NAMES)
	rows["policy"]["default_fallback_method"] = "Charge a fixed amount"
	rows["policy"]["default_fallback_amount"] = "ten"
	rows["zones"][1]["fallback_amount"] = BIG  # too big for a float
	rows["policy"]["rules"][2]["max_order_value"] = "lots"
	rows["policy"]["rules"][3]["min_weight_kg"] = True
	policy = policy_from_rows(rows["policy"], rows["zones"], rows["item_sets"], COUNTRY_CODES)
	assert policy["default_fallback"]["amount"] == "ten"
	assert policy["zones"][1]["fallback"]["amount"] == BIG
	assert policy["rules"][2]["max_order_value"] == "lots"
	assert policy["rules"][3]["min_weight_kg"] is True
	problems = validate_policy(policy)
	assert sum("not a number" in problem for problem in problems) == 4, problems
	# and written back as they are
	written = rows_from_policy(policy, COUNTRY_NAMES)
	assert written["policy"]["default_fallback_amount"] == "ten"
	assert written["zones"][1]["fallback_amount"] == BIG
	assert written["policy"]["rules"][2]["max_order_value"] == "lots"
	# a database that returns a Decimal gets a float
	rows["policy"]["default_fallback_amount"] = Decimal("12.50")
	rows["zones"][1]["fallback_amount"] = Decimal("0.1")
	policy = policy_from_rows(rows["policy"], rows["zones"], rows["item_sets"], COUNTRY_CODES)
	assert (
		policy["default_fallback"]["amount"] == 12.5 and type(policy["default_fallback"]["amount"]) is float
	)
	assert policy["zones"][1]["fallback"]["amount"] == 0.1


def test_a_policy_with_rule_names_round_trips_with_them():
	policy = named(example())
	rows = rows_from_policy(policy, COUNTRY_NAMES)
	assert [row["name"] for row in rows["policy"]["rules"]] == ["row-0", "row-1", "row-2", "row-3"]
	assert policy_from_rows(rows["policy"], rows["zones"], rows["item_sets"], COUNTRY_CODES) == policy


def test_rows_use_the_doctype_field_names():
	rows = rows_from_policy(example(), COUNTRY_NAMES)
	assert set(rows["policy"]) == POLICY_ROW_KEYS
	assert all(set(row) == RULE_ROW_KEYS for row in rows["policy"]["rules"])
	assert all(set(row) == ZONE_ROW_KEYS for row in rows["zones"])
	assert all(set(row) == SET_ROW_KEYS for row in rows["item_sets"])
	assert rows["zones"][1] == {
		"zone_name": "GCC",
		"enabled": 1,
		"not_shipped": 0,
		"everywhere_else": 0,
		"countries": [
			{"country": "United Arab Emirates"},
			{"country": "Saudi Arabia"},
			{"country": "Kuwait"},
			{"country": "Oman"},
			{"country": "Qatar"},
		],
		"customer_line": "Gulf delivery, priced by the carrier. Watches ship free.",
		"not_offered_message": "",
		"fallback_method": "Charge a fixed amount",
		"fallback_amount": 35.0,
	}
	assert rows["item_sets"] == [
		{"set_name": "Watches", "item_groups": [{"item_group": "Watches"}], "items": []}
	]
	assert rows["policy"]["rules"][3] == {
		"enabled": 1,
		"label": "International with a minimum",
		"zone": "Rest of world",
		"item_set": None,
		"min_order_value": 0.0,
		"max_order_value": 0.0,
		"min_weight_kg": 0.0,
		"max_weight_kg": 0.0,
		"min_volume_l": 0.0,
		"max_volume_l": 0.0,
		"method": "Custom formula",
		"formula": "max(carrier_price * 1.1, 45)",
		"round_up": 1,
	}
	assert {key: rows["policy"][key] for key in POLICY_ROW_KEYS - {"rules"}} == {
		"active": 1,
		"price_source": "Carrier",
		"rate_card": "",
		"not_covered": "Pass the carrier price through",
		"default_fallback_method": "Don't offer delivery",
		"default_fallback_amount": 0.0,
	}
	# the Check fields are the 0 and 1 a database holds
	assert type(rows["policy"]["active"]) is int and type(rows["zones"][0]["enabled"]) is int


def test_a_rule_row_carries_its_name_only_when_it_has_one():
	policy = example()
	policy["rules"][1]["name"] = "abc123"
	rows = rows_from_policy(policy, COUNTRY_NAMES)["policy"]["rules"]
	assert [("name" in row) for row in rows] == [False, True, False, False]
	assert rows[1]["name"] == "abc123"
	policy["rules"][1]["name"] = ""
	assert "name" not in rows_from_policy(policy, COUNTRY_NAMES)["policy"]["rules"][1]


def policy_with_code(kind, code):
	"""The example with `code` in the one field that holds a choice of this kind."""
	policy = example()
	if kind == "method":
		policy["rules"][0]["method"] = code
	elif kind == "zone_fallback":
		policy["zones"][0]["fallback"]["method"] = code
	elif kind == "default_fallback":
		policy["default_fallback"]["method"] = code
	elif kind == "price_source":
		policy["price_source"] = code
	else:
		policy["not_covered"] = code
	return policy


def label_in_rows(kind, rows):
	return {
		"method": rows["policy"]["rules"][0]["method"],
		"zone_fallback": rows["zones"][0]["fallback_method"],
		"default_fallback": rows["policy"]["default_fallback_method"],
		"not_covered": rows["policy"]["not_covered"],
		"price_source": rows["policy"]["price_source"],
	}[kind]


@pytest.mark.parametrize(
	("kind", "label", "code"),
	[(kind, label, code) for kind, labels in LABELS.items() for label, code in labels.items()],
	ids=lambda value: str(value),
)
def test_every_select_label_maps_to_its_code_and_back(kind, label, code):
	policy = policy_with_code(kind, code)
	rows = rows_from_policy(policy, COUNTRY_NAMES)
	assert label_in_rows(kind, rows) == label
	assert policy_from_rows(rows["policy"], rows["zones"], rows["item_sets"], COUNTRY_CODES) == policy


def test_an_unknown_select_label_passes_through_unchanged_and_validate_policy_reports_it():
	for kind in LABELS:
		policy = policy_with_code(kind, "mystery")
		rows = rows_from_policy(policy, COUNTRY_NAMES)
		assert label_in_rows(kind, rows) == "mystery", "an unknown code is written back as it is"
		back = policy_from_rows(rows["policy"], rows["zones"], rows["item_sets"], COUNTRY_CODES)
		assert back == policy, "and read back as it is"
		assert validate_policy(back), f"the engine's checker reports an unknown {kind}"
	# a label the doctype never offered is read as its own code, in every field
	rows = rows_from_policy(example(), COUNTRY_NAMES)
	for kind in LABELS:
		put_label_in_rows(kind, rows, "Surprise")
	read = policy_from_rows(rows["policy"], rows["zones"], rows["item_sets"], COUNTRY_CODES)
	assert [code_in_policy(kind, read) for kind in LABELS] == ["Surprise"] * len(LABELS) == ["Surprise"] * 5
	assert len(validate_policy(read)) >= 5


def put_label_in_rows(kind, rows, label):
	"""Put `label` in the one row field that holds a choice of this kind."""
	if kind == "method":
		rows["policy"]["rules"][0]["method"] = label
	elif kind == "zone_fallback":
		rows["zones"][0]["fallback_method"] = label
	elif kind == "default_fallback":
		rows["policy"]["default_fallback_method"] = label
	elif kind == "price_source":
		rows["policy"]["price_source"] = label
	else:
		rows["policy"]["not_covered"] = label


def code_in_policy(kind, policy):
	return {
		"method": policy["rules"][0]["method"],
		"zone_fallback": policy["zones"][0]["fallback"]["method"],
		"default_fallback": policy["default_fallback"]["method"],
		"not_covered": policy["not_covered"],
		"price_source": policy["price_source"],
	}[kind]


def test_each_field_knows_only_its_own_options():
	"""A label that belongs to another field's list is not one of this field's, so it passes through."""
	crossed = 0
	for kind in LABELS:
		for labels in LABELS.values():
			for label, code in labels.items():
				if label in LABELS[kind]:
					continue
				rows = rows_from_policy(example(), COUNTRY_NAMES)
				put_label_in_rows(kind, rows, label)
				policy = policy_from_rows(rows["policy"], rows["zones"], rows["item_sets"], COUNTRY_CODES)
				assert code_in_policy(kind, policy) == label, (kind, label)
				# and the same going the other way: a code from another list has no label here
				policy = policy_with_code(kind, code)
				if code not in LABELS[kind].values():
					assert label_in_rows(kind, rows_from_policy(policy, COUNTRY_NAMES)) == code, (kind, code)
				crossed += 1
	assert crossed > 10, "the loop really did cross the lists"


def test_an_empty_item_set_is_none_in_both_directions():
	policy = example()
	policy["rules"][0]["item_set"] = ""
	rows = rows_from_policy(policy, COUNTRY_NAMES)
	assert rows["policy"]["rules"][0]["item_set"] is None
	rows["policy"]["rules"][0]["item_set"] = ""
	back = policy_from_rows(rows["policy"], rows["zones"], rows["item_sets"], COUNTRY_CODES)
	assert back["rules"][0]["item_set"] is None
	rows["policy"]["rules"][0]["item_set"] = "Watches"
	back = policy_from_rows(rows["policy"], rows["zones"], rows["item_sets"], COUNTRY_CODES)
	assert back["rules"][0]["item_set"] == "Watches"


def test_rows_from_the_database_are_made_into_clean_values():
	"""The shapes a database gives: 0 and 1 for flags, None for an empty number or text, extra columns."""
	policy_row = {
		"name": "Shipping Policy",
		"modified": "2026-10-22 10:00:00",
		"active": 1,
		"not_covered": "Don't offer delivery",
		"default_fallback_method": "Charge a fixed amount",
		"default_fallback_amount": None,
		"rules": [
			{
				"name": "ab12cd34",
				"idx": 1,
				"enabled": 1,
				"label": None,
				"zone": "GCC",
				"item_set": "",
				"min_order_value": None,
				"max_order_value": 500,
				"min_weight_kg": None,
				"max_weight_kg": None,
				"min_volume_l": 2,
				"max_volume_l": None,
				"method": "Custom formula",
				"formula": None,
				"round_up": 0,
			},
			{"name": None, "enabled": 0, "zone": None, "item_set": None, "method": "Free", "round_up": 1},
		],
	}
	zone_rows = [
		{
			"name": "GCC",
			"creation": "2026-10-01",
			"zone_name": "GCC",
			"enabled": 1,
			"not_shipped": 0,
			"everywhere_else": 0,
			"countries": [{"country": "Qatar", "idx": 1}, {"country": "Atlantis"}, {"country": "Oman"}],
			"customer_line": None,
			"not_offered_message": None,
			"fallback_method": "Use the policy default",
			"fallback_amount": None,
		}
	]
	set_rows = [
		{
			"set_name": "Watches",
			"item_groups": [{"item_group": "Watches"}, {"item_group": None}, {"item_group": ""}],
			"items": [{"item": "W-1"}, {"item": None}],
		}
	]
	policy = policy_from_rows(policy_row, zone_rows, set_rows, {"Qatar": "qa", "Oman": "om "})
	assert policy == {
		"active": True,
		"price_source": "",  # a row that says nothing of it
		"rate_card": None,
		"not_covered": "no_delivery",
		"default_fallback": {"method": "fixed", "amount": 0.0},
		"zones": [
			{
				"name": "GCC",
				"enabled": True,
				"countries": ["QA", "OM"],  # upper case, in the order given, Atlantis has no code
				"not_shipped": False,
				"everywhere_else": False,
				"customer_line": "",
				"not_offered_message": "",
				"fallback": {"method": "policy_default", "amount": 0.0},
			}
		],
		"item_sets": [{"name": "Watches", "item_groups": ["Watches"], "items": ["W-1"]}],
		"rules": [
			{
				"name": "ab12cd34",
				"enabled": True,
				"label": "",
				"zone": "GCC",
				"item_set": None,
				"min_order_value": 0.0,
				"max_order_value": 500.0,
				"min_weight_kg": 0.0,
				"max_weight_kg": 0.0,
				"min_volume_l": 2.0,
				"max_volume_l": 0.0,
				"method": "formula",
				"formula": "",
				"round_up": False,
			},
			make_rule(zone="", enabled=False, method="free", round_up=True),
		],
	}
	assert json.loads(json.dumps(policy)) == policy, "plain data, as the wizard sends it to the browser"
	for rule in policy["rules"]:
		assert all(type(rule[key]) is float for key in LIMIT_KEYS)
		assert all(type(rule[key]) is bool for key in ("enabled", "round_up"))
	assert type(policy["active"]) is bool and type(policy["zones"][0]["enabled"]) is bool


def test_a_row_with_no_values_reads_as_empty_not_as_an_error():
	assert policy_from_rows({}, [], [], {}) == {
		"active": False,
		"price_source": "",
		"rate_card": None,
		"not_covered": "",
		"default_fallback": {"method": "", "amount": 0.0},
		"zones": [],
		"item_sets": [],
		"rules": [],
	}
	assert policy_from_rows(None, None, None, None) == policy_from_rows({}, [], [], {})
	assert rows_from_policy({}, {}) == {
		"policy": {
			"active": 0,
			"price_source": "",
			"rate_card": "",
			"not_covered": "",
			"default_fallback_method": "",
			"default_fallback_amount": 0.0,
			"rules": [],
		},
		"zones": [],
		"item_sets": [],
	}


def test_flags_are_read_the_way_frappe_reads_a_check_field():
	"""A number other than 0, or text holding one, is on. Anything else is off, so doubt never switches
	something on."""
	given = [1, 0, True, False, None, "1", "0", "", "2", " 1 ", 2, 0.0, 1.0]
	given += ["true", "yes", "no", "abc", float("nan"), float("inf"), BIG, [], {}, [1]]
	rows = [{"zone_name": str(position), "enabled": value} for position, value in enumerate(given)]
	zones = policy_from_rows({}, rows, [], {})["zones"]
	assert [zone["enabled"] for zone in zones] == [
		*[True, False, True, False, False, True, False, False, True, True, True, False, True],
		*[False] * 10,
	]
	assert all(type(zone["enabled"]) is bool for zone in zones)


FLAG_VALUES = [1, 0, True, False, None, "1", "0", "", "2", " 1 ", 2, -1, 0.0, 0.5, 1.0, "true", "yes", "no"]
FLAG_VALUES += ["abc", float("nan"), float("inf"), BIG, [], {}, [1], (0,), {"a": 1}, object()]
FLAGS = {
	"policy": ("active",),
	"zone": ("enabled", "not_shipped", "everywhere_else"),
	"rule": ("enabled", "round_up"),
}


def test_flags_are_written_the_way_the_engine_reads_them():
	"""The engine reads a flag as Python truth ("yes", "0", 0.5 and 2 are on; 0, "" and [] are off), so that is
	what is written: a draft that previews live must not save off. Reading a row back is the other direction,
	and `cint` is the rule there (see the test above)."""
	for value in FLAG_VALUES:
		policy = example()
		policy["active"] = value
		policy["zones"][0].update({key: value for key in FLAGS["zone"]})
		policy["rules"][0].update({key: value for key in FLAGS["rule"]})
		rows = rows_from_policy(policy, COUNTRY_NAMES)
		written = [
			*(rows["policy"][key] for key in FLAGS["policy"]),
			*(rows["zones"][0][key] for key in FLAGS["zone"]),
			*(rows["policy"]["rules"][0][key] for key in FLAGS["rule"]),
		]
		assert written == [1 if value else 0] * 6, value
		assert all(type(flag) is int for flag in written), value
	# and it is the engine's own reading, not a copy of it: on for `resolve` is on in the row
	order = {"country_code": "AE", "items": [], "order_value": 1.0, "boxes": [], "quotes": [], "served": True}
	for value in FLAG_VALUES:
		policy = {**example(), "active": value}
		assert resolve(policy, order)["policy_active"] is bool(value), value
		assert rows_from_policy(policy, COUNTRY_NAMES)["policy"]["active"] == int(bool(value)), value
	# a bool is a bool again once it has been through the rows
	for flag in (True, False):
		policy = example()
		policy["active"] = flag
		policy["zones"][0].update({key: flag for key in FLAGS["zone"]})
		policy["rules"][0].update({key: flag for key in FLAGS["rule"]})
		rows = rows_from_policy(policy, COUNTRY_NAMES)
		back = policy_from_rows(rows["policy"], rows["zones"], rows["item_sets"], COUNTRY_CODES)
		assert back["active"] is flag
		assert [back["zones"][0][key] for key in FLAGS["zone"]] == [flag] * 3
		assert [back["rules"][0][key] for key in FLAGS["rule"]] == [flag] * 2
		assert all(type(back["zones"][0][key]) is bool for key in FLAGS["zone"])


def test_a_country_name_the_map_does_not_know_is_dropped_from_its_zone():
	zone_rows = [
		{
			"zone_name": "Z",
			"countries": [
				{"country": "Bahrain"},
				{"country": "Atlantis"},
				{"country": ""},
				{"country": None},
				{},
				"Oman",
				{"country": "Qatar"},
				{"country": "Kuwait"},
			],
		}
	]
	codes = {"Bahrain": "bh", "Qatar": "", "Kuwait": None, "Oman": "OM"}
	assert policy_from_rows({}, zone_rows, [], codes)["zones"][0]["countries"] == ["BH"]


def test_countries_in_the_policy_are_written_as_country_names():
	policy = example()
	policy["zones"][0]["countries"] = ["BH", "xx", " ae ", "", None, "  "]
	rows = rows_from_policy(policy, COUNTRY_NAMES)
	# a code with no Country name is written as it is, so saving fails loudly instead of quietly losing it
	assert rows["zones"][0]["countries"] == [
		{"country": "Bahrain"},
		{"country": "XX"},
		{"country": "United Arab Emirates"},
	]
	# the names may be keyed in either case: the ERP stores country codes in lower case
	lower = {code.lower(): name for code, name in COUNTRY_NAMES.items()}
	assert rows_from_policy(example(), lower) == rows_from_policy(example(), COUNTRY_NAMES)


def test_rows_from_policy_and_policy_from_rows_leave_what_they_are_given_alone():
	policy = named(example())
	snapshot = copy.deepcopy(policy)
	rows = rows_from_policy(policy, COUNTRY_NAMES)
	assert policy == snapshot
	rows_snapshot = copy.deepcopy(rows)
	policy_from_rows(rows["policy"], rows["zones"], rows["item_sets"], COUNTRY_CODES)
	assert rows == rows_snapshot
	assert rows["policy"]["rules"] is not policy["rules"]


# ---------------------------------------------------------------- what a customer may see
def test_public_summary_hides_everything_but_zones_while_active():
	policy = example()
	translate = Translator()
	summary = public_summary_from(policy, COUNTRY_NAMES, translate)
	assert summary == {
		"active": True,
		"zones": [
			{
				"name": "Bahrain",
				"countries": [{"code": "BH", "name": "Bahrain"}],
				"customer_line": "[ar] Free delivery in Bahrain.",
				"delivers": True,
			},
			{
				"name": "GCC",
				"countries": [
					{"code": "AE", "name": "United Arab Emirates"},
					{"code": "SA", "name": "Saudi Arabia"},
					{"code": "KW", "name": "Kuwait"},
					{"code": "OM", "name": "Oman"},
					{"code": "QA", "name": "Qatar"},
				],
				"customer_line": "[ar] Gulf delivery, priced by the carrier. Watches ship free.",
				"delivers": True,
			},
			{
				"name": "Rest of world",
				"countries": [],
				"customer_line": "[ar] International delivery, priced at checkout.",
				"delivers": True,
			},
			{
				"name": "Not shipped",
				"countries": [{"code": "RU", "name": "Russia"}, {"code": "BY", "name": "Belarus"}],
				"customer_line": "[ar] We do not deliver to these countries.",
				"delivers": False,
			},
		],
	}
	assert set(summary) == SUMMARY_KEYS
	for zone in summary["zones"]:
		assert set(zone) == SUMMARY_ZONE_KEYS
		assert all(set(country) == {"code", "name"} for country in zone["countries"])
	# only the customer lines were handed to translate, never a name or a message
	assert translate.asked == [zone["customer_line"] for zone in policy["zones"]]
	# nothing of the rules, formulas, fallbacks or amounts is in what a customer gets
	text = json.dumps(summary)
	for hidden in (
		"max(carrier_price",
		"carrier_price",
		"1.1",
		"35.0",
		"45",
		"Free in Bahrain",
		"Free watches in the Gulf",
		"Gulf at the carrier price",
		"International with a minimum",
		"We don't deliver to this country.",  # a zone's checkout message is not part of the summary
		"policy_default",
		"no_delivery",
		"pass_through",
		"fixed",
		"amount",
		"fallback",
		"formula",
		"rules",
		"round",
		"item_sets",
		'Watches"',  # the item set (as a name, not inside a customer line)
	):
		assert hidden not in text, hidden


def test_public_summary_depends_only_on_the_public_fields():
	"""Whatever the private settings say, the summary does not change; nothing can leak through it."""
	plain = public_summary_from(example(), COUNTRY_NAMES, Translator())
	loud = example()
	loud["not_covered"] = "XQZ-not-covered"
	loud["default_fallback"] = {"method": "XQZ-fixed", "amount": 98765.25}
	loud["item_sets"] = [{"name": "XQZ-set", "item_groups": ["XQZ-group"], "items": ["XQZ-item"]}]
	loud["rules"] = [
		make_rule(zone, label="XQZ-label", formula="XQZ-formula(1)", item_set="XQZ-set") for zone in "ABCD"
	]
	for zone in loud["zones"]:
		zone["not_offered_message"] = "XQZ-message"
		zone["fallback"] = {"method": "XQZ-fixed", "amount": 98765.25}
	summary = public_summary_from(loud, COUNTRY_NAMES, Translator())
	assert summary == plain
	assert "XQZ" not in json.dumps(summary) and "98765" not in json.dumps(summary)
	for key in ("rules", "item_sets", "default_fallback", "not_covered"):
		stripped = example()
		del stripped[key]
		assert public_summary_from(stripped, COUNTRY_NAMES, Translator()) == plain


def test_public_summary_is_empty_when_off():
	policy = example()
	policy["active"] = False
	assert public_summary_from(policy, COUNTRY_NAMES, Translator()) == {"active": False, "zones": []}
	for off in (False, 0, None):
		policy["active"] = off
		assert public_summary_from(policy, COUNTRY_NAMES, Translator()) == {"active": False, "zones": []}
	del policy["active"]
	assert public_summary_from(policy, COUNTRY_NAMES, Translator()) == {"active": False, "zones": []}
	translate = Translator()
	public_summary_from(policy, COUNTRY_NAMES, translate)
	assert translate.asked == [], "an off policy has nothing to translate"


def test_public_summary_lists_enabled_zones_in_order_and_leaves_out_the_rest():
	policy = example()
	policy["zones"][1]["enabled"] = False  # GCC
	policy["zones"].insert(0, make_zone("Last-minute", enabled=False, countries=["FR"], customer_line="x"))
	summary = public_summary_from(policy, COUNTRY_NAMES, Translator())
	assert [zone["name"] for zone in summary["zones"]] == ["Bahrain", "Rest of world", "Not shipped"]
	assert "GCC" not in json.dumps(summary) and "Last-minute" not in json.dumps(summary)
	assert "Gulf delivery" not in json.dumps(summary), "a disabled zone's line stays private"


def test_an_everywhere_else_zone_has_no_countries_in_the_summary_and_a_not_shipped_zone_does_not_deliver():
	policy = {
		"active": True,
		"zones": [
			make_zone("Rest", everywhere_else=True, countries=["FR", "DE"]),
			make_zone("Nope", not_shipped=True, countries=["RU"]),
			make_zone("Both", everywhere_else=True, not_shipped=True),
		],
	}
	rest, nope, both = public_summary_from(policy, COUNTRY_NAMES, Translator())["zones"]
	assert rest["countries"] == [] and rest["delivers"] is True
	assert nope["countries"] == [{"code": "RU", "name": "Russia"}] and nope["delivers"] is False
	assert both["countries"] == [] and both["delivers"] is False


def test_a_blank_customer_line_is_empty_and_is_not_sent_to_translate():
	policy = {
		"active": True,
		"zones": [
			make_zone("A", customer_line=""),
			make_zone("B", customer_line="  \n"),
			make_zone("C", customer_line=None),
		],
	}
	translate = Translator()
	summary = public_summary_from(policy, COUNTRY_NAMES, translate)
	assert [zone["customer_line"] for zone in summary["zones"]] == ["", "", ""]
	assert translate.asked == []


def test_the_customer_line_and_the_message_are_translated_as_they_are_stored():
	"""Translations are found by the exact source text, so nothing is trimmed on the way to translate."""
	policy = {"active": True, "zones": [make_zone("A", customer_line="  Free delivery.\n")]}
	translate = Translator()
	(zone,) = public_summary_from(policy, COUNTRY_NAMES, translate)["zones"]
	assert translate.asked == ["  Free delivery.\n"]
	assert zone["customer_line"] == "[ar]   Free delivery.\n"
	translate = Translator()
	assert response_from({"message": " Sorry. "}, translate)["message"] == "[ar]  Sorry. "
	assert translate.asked == [" Sorry. "]


def test_a_country_without_a_name_in_the_list_shows_its_code_and_codes_are_upper_case():
	policy = {"active": True, "zones": [make_zone("A", countries=["bh", " ae ", "ZZ", "", None, "   "])]}
	zone = public_summary_from(policy, COUNTRY_NAMES, Translator())["zones"][0]
	assert zone["countries"] == [
		{"code": "BH", "name": "Bahrain"},
		{"code": "AE", "name": "United Arab Emirates"},
		{"code": "ZZ", "name": "ZZ"},
	]
	lower = {code.lower(): name for code, name in COUNTRY_NAMES.items()}
	assert public_summary_from(example(), lower, Translator()) == public_summary_from(
		example(), COUNTRY_NAMES, Translator()
	)


def test_public_summary_leaves_the_policy_alone():
	policy = example()
	snapshot = copy.deepcopy(policy)
	public_summary_from(policy, COUNTRY_NAMES, Translator())
	assert policy == snapshot


# ---------------------------------------------------------------- what a guest is told
def make_order(country="AE", *, quotes=None, served=True, boxes=None):
	boxes = [{"weight_kg": 1.0, "volume_l": 2.0}] if boxes is None else boxes
	quotes = (
		[{"service_id": "PDX", "name": "Priority", "amount": 38.2, "boxes_quoted": len(boxes)}]
		if quotes is None
		else quotes
	)
	return {
		"country_code": country,
		"items": [{"item_code": "PERF-1", "qty": 1, "groups": ["Fragrances"]}],
		"order_value": 310.0,
		"boxes": boxes,
		"quotes": quotes,
		"served": served,
	}


def card_order(country="US", weight_kg=0.9, key="{F-CR-CSE-W-40}"):
	"""An order as a call in rate card mode sends it: items with their product code and unit weight, and no
	cartons and no carrier quotes."""
	items = [{"item_code": "PERF-1", "qty": 1, "groups": [], "index_key": key, "weight_kg": weight_kg}]
	return {**make_order(country, boxes=[], quotes=[]), "items": items}


def results():
	"""Real results of the engine on the example policy, each with its trace and rule position."""
	policy = example()
	fallback = {
		**card_policy(price_source="carrier"),
		"default_fallback": {"method": "rate_card", "amount": 0.0},
	}
	broken = example()
	broken["rules"][3]["formula"] = "carrier_price / (weight_kg - 1)"
	broken["zones"][2]["fallback"] = {"method": "fixed", "amount": 12.0}
	off = {**example(), "active": False}
	return {
		"rule price": resolve(policy, make_order("AE")),
		"formula price": resolve(
			policy,
			make_order("FR", quotes=[{"service_id": "S", "name": "N", "amount": 20.0, "boxes_quoted": 1}]),
		),
		"not shipped": resolve(policy, make_order("RU")),
		"not served": resolve(policy, make_order("AE", served=False)),
		"fallback offer": resolve(policy, make_order("AE", quotes=[])),
		"formula error with a fallback": resolve(broken, make_order("FR")),
		"formula error without one": resolve(example() | {"rules": broken["rules"]}, make_order("FR")),
		"policy off": resolve(off, make_order("AE")),
		"rate card price": resolve(card_policy(), card_order("US")),
		"rate card price for nothing": resolve(card_policy(), card_order("BH")),
		"rate card too heavy": resolve(card_policy(), card_order("US", weight_kg=10.0)),
		"rate card not shipped": resolve(card_policy(), card_order("RU")),
		"rate card fallback": resolve(fallback, card_order("US")),
	}


@pytest.mark.parametrize("scenario", list(results()))
def test_guest_response_has_no_trace_or_formula_text(scenario):
	result = results()[scenario]
	# the engine's result does carry a rule position and a trace for each offer, so there is something to hide
	assert "rule" in result and all(offer["trace"] for offer in result["offers"])
	translate = Translator()
	response = response_from(result, translate)
	assert set(response) == RESPONSE_KEYS
	assert all(set(offer) == OFFER_KEYS for offer in response["offers"])
	# neither the trace nor the rule position appears anywhere in the response, at any depth
	keys = set(all_keys(response))
	assert "trace" not in keys and "rule" not in keys
	# and no formula or step of the working: "formula_error" is a public reason code, so it is set aside
	text = json.dumps(response).replace("formula_error", "")
	for offer in result["offers"]:
		for step in offer["trace"]:
			assert step["text"] not in text
	for private in (
		"trace",
		"max(carrier_price",
		"carrier_price",
		"weight_kg",
		"formula",
		"Formula",
		"Rule ",
		"quoted",
	):
		assert private not in text, private
	# what it does carry is the result's public part, as it is
	assert response["policy_active"] is result["policy_active"]
	assert response["zone"] == result["zone"]
	assert response["offered"] is result["offered"]
	assert response["reason"] == result["reason"]
	assert response["offers"] == [{key: offer[key] for key in OFFER_KEYS} for offer in result["offers"]]
	assert response["message"] == (f"[ar] {result['message']}" if result["message"] else "")
	assert translate.asked == ([result["message"]] if result["message"] else [])


def test_guest_response_for_a_priced_order():
	response = response_from(resolve(example(), make_order("AE")), Translator())
	assert response == {
		"policy_active": True,
		"price_source": "carrier",
		"zone": "GCC",
		"offered": True,
		"reason": None,
		"message": "",
		"offers": [
			{
				"service_id": "PDX",
				"name": "Priority",
				"amount": 39.0,
				"source": "rule",
				"reason": None,
				"estimated_days": None,
			}
		],
	}


def test_guest_response_for_a_refusal_carries_the_translated_message_and_the_reason_code():
	response = response_from(resolve(example(), make_order("RU")), Translator())
	assert response == {
		"policy_active": True,
		"price_source": "carrier",
		"zone": "Not shipped",
		"offered": False,
		"reason": "not_shipped",
		"message": "[ar] We don't deliver to this country.",
		"offers": [],
	}


def test_guest_response_for_the_fallback_offer():
	response = response_from(resolve(example(), make_order("AE", quotes=[])), Translator())
	assert response["offers"] == [
		{
			"service_id": "policy-fallback",
			"name": "Standard delivery",
			"amount": 35.0,
			"source": "fallback",
			"reason": "fallback_offer",
			"estimated_days": None,
		}
	]
	assert (response["offered"], response["reason"], response["message"]) == (True, "fallback_offer", "")


def test_guest_response_with_the_policy_off_echoes_the_quotes():
	off = {**example(), "active": False}
	response = response_from(resolve(off, make_order("AE")), Translator())
	assert response == {
		"policy_active": False,
		"price_source": "carrier",
		"zone": None,
		"offered": True,
		"reason": None,
		"message": "",
		"offers": [
			{
				"service_id": "PDX",
				"name": "Priority",
				"amount": 38.2,
				"source": "carrier",
				"reason": None,
				"estimated_days": None,
			}
		],
	}


def test_guest_response_depends_only_on_the_public_part_of_the_result():
	"""Whatever the trace and the rule position say, the response does not change."""
	for result in results().values():
		plain = response_from(result, Translator())
		changed = copy.deepcopy(result)
		changed["rule"] = 999
		for offer in changed["offers"]:
			offer["trace"] = [{"kind": "rule", "text": "SECRET formula 1 / 0", "amount": 98765.25}]
		without_private = copy.deepcopy(result)
		del without_private["rule"]
		for offer in without_private["offers"]:
			del offer["trace"]
		assert response_from(changed, Translator()) == plain
		assert response_from(without_private, Translator()) == plain
		assert "SECRET" not in json.dumps(response_from(changed, Translator()))


def test_guest_response_leaves_the_result_alone():
	result = resolve(example(), make_order("AE"))
	snapshot = copy.deepcopy(result)
	response_from(result, Translator())
	assert result == snapshot


# ---------------------------------------------------------------- the policy's version
def test_snapshot_version_is_stable_and_sensitive():
	policy = example()
	version = snapshot_version(policy)
	assert re.fullmatch(r"[0-9a-f]{16}", version)
	assert snapshot_version(example()) == version, "the same policy gives the same version every time"
	assert snapshot_version(reversed_keys(policy)) == version, "the order of the keys does not matter"
	assert snapshot_version(json.loads(json.dumps(policy))) == version
	# one changed amount, and every other kind of change, give a different version
	changes = {}

	def change(label, mutate):
		changed = example()
		mutate(changed)
		changes[label] = snapshot_version(changed)

	change("an amount", lambda p: p["zones"][1]["fallback"].update(amount=35.5))
	change("a default amount", lambda p: p["default_fallback"].update(amount=1.0))
	change("active", lambda p: p.update(active=False))
	change("not covered", lambda p: p.update(not_covered="no_delivery"))
	change("a country", lambda p: p["zones"][1]["countries"].append("EG"))
	change("a zone name", lambda p: p["zones"][0].update(name="Bahrain "))
	change("a rule limit", lambda p: p["rules"][2].update(max_order_value=500.0))
	change("a formula", lambda p: p["rules"][3].update(formula="max(carrier_price * 1.2, 45)"))
	change("a flag", lambda p: p["rules"][2].update(round_up=False))
	change("an item set", lambda p: p["item_sets"][0]["items"].append("W-1"))
	change("a rule added", lambda p: p["rules"].append(make_rule("GCC")))
	change("rule order", lambda p: p["rules"].reverse())
	change("a rule name", lambda p: p["rules"][0].update(name="row-0"))
	assert version not in changes.values()
	assert len(set(changes.values())) == len(changes), "each change gives its own version"


def test_snapshot_version_is_the_first_16_hex_characters_of_the_sha256_of_the_sorted_compact_json():
	assert snapshot_version({}) == "44136fa355b3678a"  # sha256 of {}
	policy = {"b": [1, 2], "a": "é", "c": {"y": 1.5, "x": None}}
	# sorted keys, no spaces, non-ASCII escaped; worked out here with hashlib alone
	assert snapshot_version(policy) == "c54c914cf686ed46"
	text = '{"a":"\\u00e9","b":[1,2],"c":{"x":null,"y":1.5}}'
	assert snapshot_version(policy) == hashlib.sha256(text.encode("ascii")).hexdigest()[:16]
	assert snapshot_version([1, 2]) != snapshot_version([2, 1]), "the order of a list matters"


def test_snapshot_version_copes_with_values_json_cannot_hold():
	assert re.fullmatch(r"[0-9a-f]{16}", snapshot_version({"when": {1, 2}, "n": float("nan"), "z": object}))


# ---------------------------------------------------------------- what a draft would change
def test_diff_reports_added_removed_changed_zones_and_rule_order():
	current = named(example())
	draft = copy.deepcopy(current)
	# zones: Not shipped removed, Africa added, GCC changed, Bahrain renamed (so one removed and one added)
	draft["zones"] = [zone for zone in draft["zones"] if zone["name"] != "Not shipped"]
	draft["zones"][1]["customer_line"] = "Gulf delivery."
	draft["zones"][0]["name"] = "Bahrain (home)"
	draft["zones"].append(make_zone("Africa", countries=["EG", "KE"]))
	# item sets: Watches gets an item, Pens is new
	draft["item_sets"][0]["items"].append("W-1")
	draft["item_sets"].append({"name": "Pens", "item_groups": ["Pens"], "items": []})
	# rules: row-0 deleted, row-1 and row-2 swapped, row-3's formula edited, a new rule at the end
	rules = draft["rules"]
	rules[3]["formula"] = "max(carrier_price * 1.2, 45)"
	draft["rules"] = [rules[2], rules[1], rules[3], make_rule("Africa", label="Africa at the carrier price")]
	# settings
	draft["active"] = False
	draft["not_covered"] = "no_delivery"
	diff = diff_policy(current, draft)
	assert diff == {
		"zones": {
			"added": ["Africa", "Bahrain (home)"],
			"removed": ["Bahrain", "Not shipped"],
			"changed": ["GCC"],
		},
		"item_sets": {"added": ["Pens"], "removed": [], "changed": ["Watches"]},
		"rules": {
			"added": [{"index": 3, "name": None, "label": "Africa at the carrier price", "zone": "Africa"}],
			"removed": [{"index": 0, "name": "row-0", "label": "Free in Bahrain", "zone": "Bahrain"}],
			"changed": [
				{
					"index": 2,
					"name": "row-3",
					"label": "International with a minimum",
					"zone": "Rest of world",
				}
			],
			"order_changed": True,
		},
		"rate_card": {"added": [], "removed": [], "changed": []},
		"settings": ["active", "not_covered"],
	}


def test_diff_of_identical_policies_is_empty():
	assert diff_policy(named(example()), named(example())) == NOTHING_CHANGED
	# rules with no name are matched by what they say, so an untouched policy shows no change either
	assert diff_policy(example(), example()) == NOTHING_CHANGED
	assert diff_policy({}, {}) == NOTHING_CHANGED
	assert diff_policy(example(), reversed_keys(example())) == NOTHING_CHANGED


def test_diff_lists_are_sorted_by_name_and_reported_once():
	current = {"zones": [make_zone("m")], "item_sets": [{"name": "m", "item_groups": [], "items": []}]}
	draft = {
		"zones": [make_zone("z"), make_zone("B"), make_zone("a"), make_zone("a")],
		"item_sets": [
			{"name": "y", "item_groups": ["g"], "items": []},
			{"name": "x", "item_groups": ["g"], "items": []},
		],
	}
	diff = diff_policy(current, draft)
	assert diff["zones"] == {"added": ["B", "a", "z"], "removed": ["m"], "changed": []}
	assert diff["item_sets"] == {"added": ["x", "y"], "removed": ["m"], "changed": []}


def test_the_name_lists_are_sorted_however_the_sets_behind_them_are_ordered():
	rng = random.Random(7)
	names = [f"n{number:02d}" for number in range(40)]

	def shuffled(chosen):
		return rng.sample(chosen, len(chosen))

	def every_third(name):
		return int(name[1:]) % 3 == 0

	current = {
		"zones": [make_zone(name) for name in shuffled(names[:25])],
		"item_sets": [{"name": name, "item_groups": ["g"], "items": []} for name in shuffled(names[:25])],
	}
	draft = {
		"zones": [make_zone(name, customer_line="new" * every_third(name)) for name in shuffled(names[10:])],
		"item_sets": [
			{"name": name, "item_groups": ["h" if every_third(name) else "g"], "items": []}
			for name in shuffled(names[10:])
		],
	}
	diff = diff_policy(current, draft)
	changed = [name for name in names[10:25] if every_third(name)]
	assert changed == ["n12", "n15", "n18", "n21", "n24"]
	for kind in ("zones", "item_sets"):
		assert diff[kind] == {"added": names[25:], "removed": names[:10], "changed": changed}


def test_a_renamed_zone_or_item_set_is_one_removed_and_one_added():
	current = example()
	draft = copy.deepcopy(current)
	draft["zones"][1]["name"] = "Gulf"
	draft["item_sets"][0]["name"] = "Timepieces"
	diff = diff_policy(current, draft)
	assert diff["zones"] == {"added": ["Gulf"], "removed": ["GCC"], "changed": []}
	assert diff["item_sets"] == {"added": ["Timepieces"], "removed": ["Watches"], "changed": []}


def test_a_zone_or_item_set_is_changed_when_anything_but_its_name_differs():
	current = example()
	for mutate, where in (
		(lambda p: p["zones"][0].update(enabled=False), "zones"),
		(lambda p: p["zones"][1]["countries"].remove("QA"), "zones"),
		(lambda p: p["zones"][1]["fallback"].update(amount=36.0), "zones"),
		(lambda p: p["zones"][3].update(not_offered_message="Sorry."), "zones"),
		(lambda p: p["zones"][2].update(everywhere_else=False), "zones"),
		(lambda p: p["item_sets"][0]["item_groups"].append("Straps"), "item_sets"),
		(lambda p: p["item_sets"][0]["items"].append("W-1"), "item_sets"),
	):
		draft = copy.deepcopy(current)
		mutate(draft)
		diff = diff_policy(current, draft)
		found = diff[where]
		assert (len(found["changed"]), found["added"], found["removed"]) == (1, [], []), where
		assert diff["rules"] == NOTHING_CHANGED["rules"] and diff["settings"] == []


def test_diff_settings_name_the_top_level_settings_that_differ_sorted():
	current = example()
	for mutate, expected in (
		(lambda p: p.update(active=False), ["active"]),
		(lambda p: p.update(not_covered="no_delivery"), ["not_covered"]),
		(lambda p: p["default_fallback"].update(amount=5.0), ["default_fallback"]),
		(lambda p: p["default_fallback"].update(method="free"), ["default_fallback"]),
		(
			lambda p: p.update(
				not_covered="no_delivery", active=False, default_fallback={"method": "free", "amount": 1.0}
			),
			["active", "default_fallback", "not_covered"],
		),
	):
		draft = copy.deepcopy(current)
		mutate(draft)
		diff = diff_policy(current, draft)
		assert diff["settings"] == expected
		assert diff["zones"] == NOTHING_CHANGED["zones"] and diff["rules"] == NOTHING_CHANGED["rules"]
	# a flag is compared as a flag: on is on if written true, 1 or "yes", and off if written false or null
	assert diff_policy(current, {**current, "active": 1})["settings"] == []
	assert diff_policy(current, {**current, "active": "yes"})["settings"] == []
	off = {**current, "active": False}
	assert diff_policy(off, {**off, "active": None})["settings"] == []
	assert diff_policy(off, current)["settings"] == ["active"]


def test_rule_order_changed_only_when_rules_in_both_trade_places():
	base = named(example())

	def order_changed(mutate):
		draft = copy.deepcopy(base)
		mutate(draft["rules"])
		return diff_policy(base, draft)["rules"]["order_changed"]

	assert order_changed(lambda rules: rules.reverse()) is True
	assert order_changed(lambda rules: rules.insert(0, rules.pop())) is True, "moved to the top"
	assert order_changed(lambda rules: rules.append(rules.pop(0))) is True, "moved to the bottom"
	assert order_changed(lambda rules: rules.insert(2, rules.pop(1))) is True, "moved down one"
	assert order_changed(lambda rules: rules.pop(1)) is False, "a deleted rule leaves the others in order"
	assert order_changed(lambda rules: rules.pop(0)) is False
	assert order_changed(lambda rules: rules.insert(2, make_rule("GCC"))) is False, "a new rule in the middle"
	assert order_changed(lambda rules: rules.insert(0, make_rule("GCC"))) is False
	assert order_changed(lambda rules: rules[1].update(round_up=True)) is False, "an edit alone"
	assert order_changed(lambda rules: None) is False


def test_a_moved_and_edited_rule_is_both_changed_and_reordered():
	base = named(example())
	draft = copy.deepcopy(base)
	draft["rules"].insert(0, draft["rules"].pop(3))
	draft["rules"][0]["round_up"] = False
	diff = diff_policy(base, draft)["rules"]
	assert diff["order_changed"] is True
	assert diff["changed"] == [
		{"index": 0, "name": "row-3", "label": "International with a minimum", "zone": "Rest of world"}
	]
	assert diff["added"] == [] and diff["removed"] == []


def test_a_rule_that_only_moved_is_not_changed():
	base = named(example())
	draft = copy.deepcopy(base)
	draft["rules"].reverse()
	diff = diff_policy(base, draft)["rules"]
	assert diff["changed"] == [] and diff["added"] == [] and diff["removed"] == []
	assert diff["order_changed"] is True


def test_rules_are_identified_by_name_and_a_rule_without_a_name_counts_as_added():
	base = named(example())
	draft = copy.deepcopy(base)
	draft["rules"].append({**draft["rules"][0], "name": None})  # a copy of row-0 with no name of its own
	draft["rules"].append(make_rule("GCC", name="not-in-the-policy-yet", label="Ghost"))
	diff = diff_policy(base, draft)["rules"]
	assert diff["added"] == [
		{"index": 4, "name": None, "label": "Free in Bahrain", "zone": "Bahrain"},
		{"index": 5, "name": "not-in-the-policy-yet", "label": "Ghost", "zone": "GCC"},
	]
	assert diff["removed"] == [] and diff["changed"] == [] and diff["order_changed"] is False
	# the name is matched exactly: ROW-0 is not row-0
	draft = copy.deepcopy(base)
	draft["rules"][0]["name"] = "ROW-0"
	diff = diff_policy(base, draft)["rules"]
	assert [entry["name"] for entry in diff["added"]] == ["ROW-0"]
	assert [entry["name"] for entry in diff["removed"]] == ["row-0"]


def test_a_rule_whose_name_is_gone_from_the_draft_is_removed_with_its_place_in_the_current_policy():
	base = named(example())
	draft = copy.deepcopy(base)
	del draft["rules"][1], draft["rules"][2]  # row-1 and row-3 go
	diff = diff_policy(base, draft)["rules"]
	assert [(entry["index"], entry["name"]) for entry in diff["removed"]] == [(1, "row-1"), (3, "row-3")]
	assert diff["removed"][0] == {
		"index": 1,
		"name": "row-1",
		"label": "Free watches in the Gulf",
		"zone": "GCC",
	}
	assert diff["added"] == [] and diff["changed"] == []


def test_rules_without_names_have_no_identity_so_an_edited_one_is_removed_and_added():
	"""Rules read from the database always have names; these have none, as in the example file."""
	current = example()
	draft = copy.deepcopy(current)
	draft["rules"][2]["round_up"] = False
	diff = diff_policy(current, draft)["rules"]
	assert diff["added"] == [{"index": 2, "name": None, "label": "Gulf at the carrier price", "zone": "GCC"}]
	assert diff["removed"] == [
		{"index": 2, "name": None, "label": "Gulf at the carrier price", "zone": "GCC"}
	]
	assert diff["changed"] == [] and diff["order_changed"] is False


def test_identical_rules_without_names_are_paired_one_for_one():
	rule = make_rule("GCC", label="No name")
	one, two = {"rules": [rule]}, {"rules": [rule, rule]}
	grown = diff_policy(one, two)["rules"]
	assert grown["added"] == [{"index": 1, "name": None, "label": "No name", "zone": "GCC"}]
	assert grown["removed"] == [] and grown["changed"] == [] and grown["order_changed"] is False
	shrunk = diff_policy(two, one)["rules"]
	assert shrunk["removed"] == [{"index": 1, "name": None, "label": "No name", "zone": "GCC"}]
	assert shrunk["added"] == [] and shrunk["changed"] == [] and shrunk["order_changed"] is False


def test_duplicate_names_do_not_confuse_the_diff():
	base = named(example())
	draft = copy.deepcopy(base)
	draft["rules"].append({**draft["rules"][0]})  # a second row called row-0
	diff = diff_policy(base, draft)["rules"]
	assert diff["added"] == [{"index": 4, "name": "row-0", "label": "Free in Bahrain", "zone": "Bahrain"}]
	assert diff["removed"] == [] and diff["changed"] == []
	# and a draft that repeats a zone name does not break it either
	twice = copy.deepcopy(base)
	twice["zones"].append(make_zone("GCC"))
	assert diff_policy(base, twice)["zones"]["added"] == []


def test_diff_leaves_both_policies_alone():
	current, draft = named(example()), named(example())
	draft["rules"].reverse()
	draft["zones"].pop()
	before = copy.deepcopy((current, draft))
	diff_policy(current, draft)
	assert (current, draft) == before


# ---------------------------------------------------------------- odd content never raises
def policy_variants(junk):
	return with_junk_at_every_place(named(example()), junk)


@pytest.mark.parametrize("junk", JUNK, ids=JUNK_IDS)
def test_a_policy_with_odd_content_never_raises_in_any_function(junk):
	reference = named(example())
	for variant in policy_variants(junk):
		rows = rows_from_policy(variant, COUNTRY_NAMES)
		assert set(rows) == {"policy", "zones", "item_sets"}
		public_summary_from(variant, COUNTRY_NAMES, Translator())
		assert re.fullmatch(r"[0-9a-f]{16}", snapshot_version(variant))
		for diff in (
			diff_policy(reference, variant),
			diff_policy(variant, reference),
			diff_policy(variant, variant),
		):
			assert set(diff) == set(NOTHING_CHANGED)
			assert isinstance(diff["rules"]["order_changed"], bool)
			for kind in ("zones", "item_sets"):
				assert all(isinstance(name, str) for names in diff[kind].values() for name in names)
		# and what comes back from the rows is still the policy shape
		back = policy_from_rows(rows["policy"], rows["zones"], rows["item_sets"], COUNTRY_CODES)
		assert set(back) == POLICY_KEYS


@pytest.mark.parametrize("junk", JUNK, ids=JUNK_IDS)
def test_rows_with_odd_content_are_read_into_the_policy_shape_without_raising(junk):
	rows = rows_from_policy(named(example()), COUNTRY_NAMES)
	packed = [rows["policy"], rows["zones"], rows["item_sets"]]
	for variant in with_junk_at_every_place(packed, junk):
		policy = policy_from_rows(*variant, COUNTRY_CODES)
		assert set(policy) == POLICY_KEYS
		assert isinstance(policy["active"], bool) and isinstance(policy["not_covered"], str)
		assert set(policy["default_fallback"]) == {"method", "amount"}
		for zone in policy["zones"]:
			assert set(zone) == ZONE_KEYS
			assert all(isinstance(zone[key], bool) for key in ("enabled", "not_shipped", "everywhere_else"))
			assert all(isinstance(zone[key], str) for key in ("name", "customer_line", "not_offered_message"))
			assert all(isinstance(code, str) and code == code.upper() and code for code in zone["countries"])
		for item_set in policy["item_sets"]:
			assert set(item_set) == {"name", "item_groups", "items"}
			assert all(
				isinstance(entry, str) and entry for entry in item_set["item_groups"] + item_set["items"]
			)
		for rule in policy["rules"]:
			assert set(rule) == RULE_KEYS
			assert all(isinstance(rule[key], bool) for key in ("enabled", "round_up"))
			assert all(isinstance(rule[key], str) for key in ("label", "zone", "method", "formula"))
			assert rule["item_set"] is None or (isinstance(rule["item_set"], str) and rule["item_set"])
		# the version of whatever came out can always be worked out
		snapshot_version(policy)


@pytest.mark.parametrize("junk", JUNK, ids=JUNK_IDS)
def test_odd_country_lists_and_results_never_raise(junk):
	rows = rows_from_policy(named(example()), COUNTRY_NAMES)
	policy_from_rows(rows["policy"], rows["zones"], rows["item_sets"], junk)
	policy_from_rows(rows["policy"], rows["zones"], rows["item_sets"], {"Bahrain": junk})
	if isinstance(junk, str):
		policy_from_rows(rows["policy"], rows["zones"], rows["item_sets"], {junk: "bh"})
	rows_from_policy(example(), junk)
	rows_from_policy(example(), {"BH": junk})
	public_summary_from(example(), junk, Translator())
	public_summary_from(example(), {"BH": junk}, Translator())
	for result in results().values():
		for variant in with_junk_at_every_place(result, junk):
			response = response_from(variant, Translator())
			assert set(response) == RESPONSE_KEYS
			assert all(set(offer) == OFFER_KEYS for offer in response["offers"])
	for odd in (junk, [junk], {"offers": junk}, {"offers": [junk]}, {"message": junk}):
		assert set(response_from(odd, Translator())) == RESPONSE_KEYS


# ---------------------------------------------------------------- the rate card
def test_rate_card_round_trips_through_rows_and_the_fixture_still_matches():
	# the example file says what a policy with no card holds
	policy = example()
	assert (policy["price_source"], policy["rate_card"]) == ("carrier", None)
	rows = rows_from_policy(policy, COUNTRY_NAMES)
	assert (rows["policy"]["price_source"], rows["policy"]["rate_card"]) == ("Carrier", "")
	back = policy_from_rows(rows["policy"], rows["zones"], rows["item_sets"], COUNTRY_CODES)
	assert back == policy
	assert canonical(back) == canonical(policy), "the same types too: no card stays None"

	# a policy with a card: the label of the source, and the card as JSON text a person can read in the form
	policy = card_policy()
	rows = rows_from_policy(policy, COUNTRY_NAMES)
	assert rows["policy"]["price_source"] == "Rate card"
	text = rows["policy"]["rate_card"]
	assert text == json.dumps(policy["rate_card"], ensure_ascii=False, sort_keys=True, indent=1)
	assert json.loads(text) == policy["rate_card"]
	assert text.startswith("{\n ") and EN_DASH in text and "\\u2013" not in text, "indented, and not escaped"
	back = policy_from_rows(rows["policy"], rows["zones"], rows["item_sets"], COUNTRY_CODES)
	assert back == policy
	assert canonical(back) == canonical(policy), (
		"prices stay floats, days stay whole numbers, no days stay None"
	)
	# the order of the card's keys does not change what is written
	reordered = {**policy, "rate_card": reversed_keys(policy["rate_card"])}
	assert rows_from_policy(reordered, COUNTRY_NAMES)["policy"]["rate_card"] == text
	# a row that already holds the card as a dict is read as it is, and not held on to
	row = {**rows["policy"], "rate_card": copy.deepcopy(policy["rate_card"])}
	read = policy_from_rows(row, rows["zones"], rows["item_sets"], COUNTRY_CODES)
	assert read["rate_card"] == policy["rate_card"] and read["rate_card"] is not row["rate_card"]
	# neither direction changes what it is given
	snapshot = copy.deepcopy(policy)
	rows_from_policy(policy, COUNTRY_NAMES)
	assert policy == snapshot
	rows_snapshot = copy.deepcopy(rows)
	policy_from_rows(rows["policy"], rows["zones"], rows["item_sets"], COUNTRY_CODES)
	assert rows == rows_snapshot

	# a policy that has neither field writes empty ones; it does not raise
	bare = example()
	del bare["price_source"], bare["rate_card"]
	written = rows_from_policy(bare, COUNTRY_NAMES)["policy"]
	assert (written["price_source"], written["rate_card"]) == ("", "")
	# only a dict is a card: anything else writes nothing
	for junk in (None, [], ["profiles"], "text", "{}", 5, 1.5, True):
		assert rows_from_policy({"rate_card": junk}, {})["policy"]["rate_card"] == "", junk
	# a card that holds what JSON cannot is written as well as it can be, and never makes the call raise
	odd = rows_from_policy({"rate_card": {"note": {1, 2}}}, {})["policy"]["rate_card"]
	assert json.loads(odd) == {"note": "{1, 2}"}
	loop = {}
	loop["me"] = loop
	deep = []
	for _ in range(100_000):  # built, not parsed: JSON this deep cannot be read, but a caller can hold it
		deep = [deep]
	for card in (loop, {1: "a", "b": 2}, {"deep": deep}):
		assert rows_from_policy({"rate_card": card}, {})["policy"]["rate_card"] == ""


def _put_in_a_tier(card, number):
	card["profiles"][0]["zones"][0]["tiers"][0]["price"] = number


def _put_in_a_key_of_its_own(card, number):
	card["note"] = number


def _put_in_a_list(card, number):
	card["notes"] = [1, number, "x"]


def _put_deep_inside(card, number):
	card["profiles"][1]["zones"][0]["extra"] = {"deeper": [{"value": number}]}


@pytest.mark.parametrize("number", [math.nan, math.inf, -math.inf], ids=["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize(
	"put",
	[_put_in_a_tier, _put_in_a_key_of_its_own, _put_in_a_list, _put_deep_inside],
	ids=lambda put: put.__name__,
)
def test_a_card_holding_a_number_json_has_no_words_for_is_written_as_no_text(number, put):
	"""JSON has no NaN and no Infinity. Python writes them all the same, as text a browser cannot read
	(JSON.parse refuses `"note": NaN`), so a card that holds one anywhere is written as "", the text of no
	card, which the wizard refuses to save before it writes anything."""
	card = sample_card()
	put(card, number)
	text = rows_from_policy({"rate_card": card}, {})["policy"]["rate_card"]
	assert text == ""


@pytest.mark.parametrize("number", [math.nan, math.inf, -math.inf], ids=["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize(
	"put",
	[_put_in_a_tier, _put_in_a_key_of_its_own, _put_in_a_list, _put_deep_inside],
	ids=lambda put: put.__name__,
)
def test_stored_card_text_holding_a_number_json_has_no_words_for_reads_as_no_card(number, put):
	"""The other way round: Python reads NaN, Infinity and -Infinity in JSON text, but they are not JSON.
	Stored card text that holds one anywhere (a form save while the policy is off keeps what was typed) reads
	as no card, as any text that is not JSON does: the wizard's state would carry the number to a browser
	that refuses it, and the card could never be written back."""
	card = sample_card()
	put(card, number)
	text = json.dumps(card)  # written as Python writes it, with the words
	assert policy_from_rows({"rate_card": text}, [], [], {})["rate_card"] is None
	assert (
		policy_from_rows({"rate_card": json.dumps(sample_card())}, [], [], {})["rate_card"] == sample_card()
	)


@pytest.mark.parametrize("literal", ["1e999", "-1e999"])
@pytest.mark.parametrize(
	"put",
	[_put_in_a_tier, _put_in_a_key_of_its_own, _put_in_a_list, _put_deep_inside],
	ids=lambda put: put.__name__,
)
def test_stored_card_text_holding_a_number_too_big_for_a_float_reads_as_no_card(literal, put):
	"""1e999 is a number by JSON's grammar, but too big for a float: Python reads it as an infinity without
	a word for it, so it is no more a number a browser can be sent than Infinity is. Text holding one reads
	as no card too."""
	card = sample_card()
	put(card, 0.125)  # a mark to put the literal in its place
	text = json.dumps(card).replace("0.125", literal)
	assert literal in text and "Infinity" not in text
	assert policy_from_rows({"rate_card": text}, [], [], {})["rate_card"] is None


def test_read_json_reads_every_number_a_float_can_hold():
	"""Only what a float cannot hold is refused: the numbers of a card, the smallest and the biggest float,
	an integer of any size up to Python's own limit, and a negative zero are read as they always were."""
	numbers = read_json("[0.5, 1e2, -0.0, 10, 2.675, 1.7976931348623157e308, 5e-324, 123456789012345678901]")
	assert numbers == [0.5, 100.0, 0.0, 10, 2.675, 1.7976931348623157e308, 5e-324, 123456789012345678901]
	assert math.copysign(1.0, numbers[2]) == -1.0, "a negative zero stays one"
	assert [type(number) for number in numbers[2:4]] == [float, int]
	for text in ("1e999", "-1e999", "[1E309]", '{"a": {"b": [-2e400]}}', "NaN", "-Infinity"):
		with pytest.raises(ValueError):
			read_json(text)


def test_a_number_frappe_would_round_to_zero_without_a_word_is_not_finite():
	"""Frappe rounds a Float field with `flt`, which reads NaN, an infinity or an integer too big for a float
	as 0, whether it comes as a number or as text (commas aside). Those are what `is_not_finite` finds, so
	that the doctypes refuse them before they round. A finite number, and what Frappe does not read as a
	number at all, are not."""
	not_finite = shipping_policy_data.is_not_finite
	for value in (math.nan, math.inf, -math.inf, "NaN", " nan ", "-Infinity", "1e400", "1,000e400"):
		assert not_finite(value), value
	for value in (Decimal("NaN"), Decimal("-Infinity"), 10**400, -(10**400)):
		assert not_finite(value), value
	for value in (0, 0.0, -1.5, 1e308, 10**300, Decimal("1.5"), "12.5", "1,234.5", True):
		assert not not_finite(value), value
	for value in (None, "", "abc", [], {}, object()):
		assert not not_finite(value), value


def test_a_card_of_numbers_json_can_hold_is_written_as_it_always_was():
	"""Only NaN and the infinities changed: any other card is written byte for byte as before, odd values kept
	as text (a set, a Decimal), the smallest and the biggest floats as they are."""
	odd = {
		**sample_card(),
		"note": {1, 2},
		"rate": Decimal("1.5"),
		"big": 10**20,
		"tiny": 5e-324,
		"huge": 1e308,
	}
	for card in (sample_card(), odd):
		text = rows_from_policy({"rate_card": card}, {})["policy"]["rate_card"]
		assert text == json.dumps(card, ensure_ascii=False, sort_keys=True, indent=1, default=str)
		assert "NaN" not in text and "Infinity" not in text


def test_broken_card_text_reads_as_no_card():
	for text in (
		"",
		" \n\t",
		"not json",
		"{",
		"[",
		'{"a": }',
		'{"a": 1',
		"{'a': 1}",
		"[1, 2]",
		"[]",
		"5",
		"1.5",
		"null",
		"true",
		'"x"',
		"1" * 5000,  # an integer with more digits than Python will read
		"[" * 100_000,  # nested deeper than Python will read
		"{" * 100_000,
		"\ud800",
	):
		policy = policy_from_rows({"active": 1, "rate_card": text}, [], [], {})
		assert policy["rate_card"] is None, text[:20]
		assert policy["active"] is True, "the rest of the row is read as usual"
		assert set(policy) == POLICY_KEYS
	# anything that is not text and not a dict is no card either, and a missing field is none
	for value in (None, 5, 1.5, True, [], ["x"], b'{"a": 1}', Decimal("1"), object()):
		assert policy_from_rows({"rate_card": value}, [], [], {})["rate_card"] is None, value
	assert policy_from_rows({}, [], [], {})["rate_card"] is None
	# a JSON object is a card, however little is in it; the card is the object as it was written
	for text, expected in (
		("{}", {}),
		(' {"profiles": []}\n', {"profiles": []}),
		('{"version": 1, "x": [1, 2.5, null, true, "a"]}', {"version": 1, "x": [1, 2.5, None, True, "a"]}),
	):
		assert policy_from_rows({"rate_card": text}, [], [], {})["rate_card"] == expected
	# NaN and the infinities are words Python reads but JSON has none of: text holding one is no card either
	for constant in ("NaN", "Infinity", "-Infinity"):
		assert policy_from_rows({"rate_card": f'{{"a": {constant}}}'}, [], [], {})["rate_card"] is None
	# a dict that is too deep to copy is no card
	deep = []
	for _ in range(5000):
		deep = [deep]
	assert policy_from_rows({"rate_card": {"deep": deep}}, [], [], {})["rate_card"] is None
	# the source reads as a Select does: the label becomes the code, and a label of nobody's passes through
	for label, code in (
		("Carrier", "carrier"),
		("Rate card", "rate_card"),
		("Surprise", "Surprise"),
		(None, ""),
		("", ""),
	):
		assert policy_from_rows({"price_source": label}, [], [], {})["price_source"] == code


def profile_names(card):
	return [profile["name"] for profile in card["profiles"]]


def test_diff_reports_rate_card_profile_changes_and_price_source():
	current = card_policy(price_source="carrier")
	assert profile_names(current["rate_card"]) == [
		"Dangerous Goods Petite",
		"Refill Service",
		"Dangerous Goods",
		"Rest of the World Allowed Items",
	]
	assert diff_policy(current, copy.deepcopy(current)) == NOTHING_CHANGED

	# one profile removed, one changed (a price), one added, and the price source switched
	draft = copy.deepcopy(current)
	draft["price_source"] = "rate_card"
	profiles = draft["rate_card"]["profiles"]
	del profiles[1]  # Refill Service
	profiles[1]["zones"][0]["tiers"][0]["price"] = 61.0  # Dangerous Goods
	profiles.append({"name": "Gift cards", "match": ["G-*"], "zones": []})
	diff = diff_policy(current, draft)
	assert diff["rate_card"] == {
		"added": ["Gift cards"],
		"removed": ["Refill Service"],
		"changed": ["Dangerous Goods"],
	}
	assert diff["settings"] == ["price_source", "rate_card"]
	# the rest of the policy is untouched, and the card changes nothing there
	assert {key: diff[key] for key in ("zones", "item_sets", "rules")} == {
		key: NOTHING_CHANGED[key] for key in ("zones", "item_sets", "rules")
	}
	assert set(diff) == set(NOTHING_CHANGED) and set(diff["rate_card"]) == {"added", "removed", "changed"}

	# only the source differs
	switched = {**current, "price_source": "rate_card"}
	assert diff_policy(current, switched) == {**NOTHING_CHANGED, "settings": ["price_source"]}
	# only the card differs: the source is not named
	assert diff_policy(current, draft | {"price_source": "carrier"})["settings"] == ["rate_card"]
	# the names are sorted, each once, and a rename is one removed and one added
	renamed = copy.deepcopy(current)
	renamed["rate_card"]["profiles"][0]["name"] = "Small bottles"
	renamed["rate_card"]["profiles"][1]["name"] = "Another"
	assert diff_policy(current, renamed)["rate_card"] == {
		"added": ["Another", "Small bottles"],
		"removed": ["Dangerous Goods Petite", "Refill Service"],
		"changed": [],
	}
	# a profile is changed when anything about it differs: a pattern, a country, the method, the days, a tier
	for mutate in (
		lambda p: p["match"].append("X-*"),
		lambda p: p["zones"][0]["countries"].append("EG"),
		lambda p: p["zones"][0].update(method="Other (1-2 days)"),
		lambda p: p["zones"][0].update(days=9),
		lambda p: p["zones"][0]["tiers"].pop(),
		lambda p: p["zones"].reverse(),
	):
		changed = copy.deepcopy(current)
		mutate(changed["rate_card"]["profiles"][2])
		assert diff_policy(current, changed)["rate_card"] == {
			"added": [],
			"removed": [],
			"changed": ["Dangerous Goods"],
		}
	# the order of the profiles decides which one takes an item, so a card with its profiles in another order
	# is another card, even though no profile changed
	shuffled = copy.deepcopy(current)
	shuffled["rate_card"]["profiles"].reverse()
	found = diff_policy(current, shuffled)
	assert found["rate_card"] == NOTHING_CHANGED["rate_card"]
	assert found["settings"] == ["rate_card"]

	# a card appearing, and disappearing
	none = {**current, "rate_card": None}
	added = diff_policy(none, current)
	assert added["rate_card"] == {
		"added": [
			"Dangerous Goods",
			"Dangerous Goods Petite",
			"Refill Service",
			"Rest of the World Allowed Items",
		],
		"removed": [],
		"changed": [],
	}
	assert added["settings"] == ["rate_card"]
	removed = diff_policy(current, none)
	assert removed["rate_card"] == {"added": [], "removed": added["rate_card"]["added"], "changed": []}
	assert removed["settings"] == ["rate_card"]
	# a policy that does not say is a policy with the carrier and no card
	bare = example()
	del bare["price_source"], bare["rate_card"]
	assert diff_policy(bare, example()) == NOTHING_CHANGED
	assert diff_policy(example(), bare) == NOTHING_CHANGED
	assert diff_policy(bare, {**bare, "price_source": ""}) == NOTHING_CHANGED
	assert diff_policy({}, {}) == NOTHING_CHANGED
	# neither policy is altered
	before = copy.deepcopy((current, draft))
	diff_policy(current, draft)
	assert (current, draft) == before


def test_diff_copes_with_a_card_that_is_not_a_card():
	current = card_policy()
	for junk in (
		None,
		[],
		"card",
		5,
		True,
		{},
		{"profiles": None},
		{"profiles": "x"},
		{"profiles": [None, 5, "x", []]},
	):
		draft = {**current, "rate_card": junk}
		for diff in (diff_policy(current, draft), diff_policy(draft, current), diff_policy(draft, draft)):
			assert set(diff) == set(NOTHING_CHANGED)
			assert set(diff["rate_card"]) == {"added", "removed", "changed"}
			assert all(isinstance(name, str) for names in diff["rate_card"].values() for name in names)
		assert diff_policy(draft, draft) == NOTHING_CHANGED
	# profiles with no name are one profile, named "": the first of them
	nameless = {**current, "rate_card": {"profiles": [{"zones": []}, {"name": None}, {"name": ""}]}}
	assert diff_policy(nameless, nameless)["rate_card"] == NOTHING_CHANGED["rate_card"]


def marked_card():
	"""A card whose names, patterns and weights are unmistakable, so that a leak cannot hide."""
	return {
		"version": 1,
		"currency": "USD",
		"unit": "lb",
		"profiles": [
			{
				"name": "XQZ-profile",
				"match": ["XQZ-KEY-*"],
				"zones": [
					{
						"name": "XQZ-zone",
						"countries": ["BH"],
						"method": "Express (4-7 days)",
						"days": 7,
						"tiers": [{"from": 0.0, "to": 987.654, "price": 123.45}],
					}
				],
			},
			{
				"name": "XQZ-everything",
				"match": ["*"],
				"zones": [
					{
						"name": "XQZ-other-zone",
						"countries": ["BH"],
						"method": "Express (4-7 days)",
						"days": 3,
						"tiers": [{"from": 0.0, "to": 654.321, "price": 11.0}],
					}
				],
			},
		],
	}


def test_guest_response_has_estimated_days_and_price_source_but_no_card_contents():
	order = card_order("BH", key="{XQZ-KEY-1}")
	order["items"].append(
		{"item_code": "OTHER", "qty": 2, "groups": [], "index_key": "{OTHER-1}", "weight_kg": 0.1}
	)
	result = resolve(card_policy(rate_card=marked_card()), order)
	assert result["offered"] is True and result["price_source"] == "rate_card"
	# the engine's result does hold the card's workings, so there is something to hide
	assert any("XQZ" in step["text"] for step in result["offers"][0]["trace"])
	translate = Translator()
	response = response_from(result, translate)
	assert response == {
		"policy_active": True,
		"price_source": "rate_card",
		"zone": "Bahrain",
		"offered": True,
		"reason": None,
		"message": "",
		"offers": [
			{
				"service_id": "rate-card",
				"name": "Express (4-7 days)",
				"amount": 134.45,
				"source": "rate_card",
				"reason": None,
				"estimated_days": 7,
			}
		],
	}
	assert translate.asked == [], "there is no message to translate"
	# walked from the top: no name, pattern, weight or key of the card, and no trace, anywhere in it
	text = json.dumps(response)
	for hidden in ("XQZ", "987.654", "654.321", "KEY", "OTHER"):
		assert hidden not in text, hidden
	for step in result["offers"][0]["trace"]:
		assert step["text"] not in text
	assert set(all_keys(response)) == RESPONSE_KEYS | OFFER_KEYS
	for key in (
		"profiles",
		"zones",
		"tiers",
		"lines",
		"match",
		"countries",
		"trace",
		"rule",
		"weight_lb",
		"days",
	):
		assert key not in set(all_keys(response)), key

	# a refusal says why, in the customer's language, and carries nothing of the card either
	too_heavy = response_from(resolve(card_policy(), card_order("US", weight_kg=10.0)), Translator())
	assert too_heavy == {
		"policy_active": True,
		"price_source": "rate_card",
		"zone": "Rest of world",
		"offered": False,
		"reason": "too_heavy",
		"message": "[ar] This order is heavier than our delivery rates cover.",
		"offers": [],
	}
	# whatever the engine's result says of the source, a guest is told one of the two
	for given, expected in (
		("rate_card", "rate_card"),
		("carrier", "carrier"),
		("mystery", "carrier"),
		(None, "carrier"),
		(5, "carrier"),
		(["rate_card"], "carrier"),
	):
		assert response_from({"price_source": given}, Translator())["price_source"] == expected
	assert response_from({}, Translator())["price_source"] == "carrier"
	assert response_from({}, Translator())["offers"] == []
	# an offer is picked by name: the days are passed on, a card's inner key is not, a missing one is None
	picked = response_from(
		{
			"offers": [
				{"service_id": "s", "estimated_days": 4, "weight_lb": 1.5, "tiers": [1]},
				{"service_id": "t"},
			]
		},
		Translator(),
	)["offers"]
	assert picked == [
		{
			"service_id": "s",
			"name": None,
			"amount": None,
			"source": None,
			"reason": None,
			"estimated_days": 4,
		},
		{
			"service_id": "t",
			"name": None,
			"amount": None,
			"source": None,
			"reason": None,
			"estimated_days": None,
		},
	]


def test_snapshot_version_changes_with_the_card():
	policy = card_policy()
	version = snapshot_version(policy)
	assert re.fullmatch(r"[0-9a-f]{16}", version)
	assert snapshot_version(card_policy()) == version, "the same card gives the same version every time"
	assert snapshot_version(reversed_keys(policy)) == version, "the order of the keys does not matter"
	assert snapshot_version(json.loads(json.dumps(policy))) == version
	changes = {}

	def change(label, mutate):
		changed = card_policy()
		mutate(changed)
		changes[label] = snapshot_version(changed)

	def dangerous(policy):
		return policy["rate_card"]["profiles"][2]

	change("a price", lambda p: dangerous(p)["zones"][0]["tiers"][0].update(price=61.0))
	change("a tier edge", lambda p: dangerous(p)["zones"][0]["tiers"][0].update(to=0.61))
	change("a pattern", lambda p: dangerous(p)["match"].append("X-*"))
	change("a country", lambda p: dangerous(p)["zones"][0]["countries"].append("EG"))
	change("a method", lambda p: dangerous(p)["zones"][0].update(method="Other (1-2 days)"))
	change("the days", lambda p: dangerous(p)["zones"][0].update(days=8))
	change("a profile name", lambda p: dangerous(p).update(name="Fragrances"))
	change("a profile removed", lambda p: p["rate_card"]["profiles"].pop(1))
	change("the order of the profiles", lambda p: p["rate_card"]["profiles"].reverse())
	change("the card removed", lambda p: p.update(rate_card=None))
	change("the source", lambda p: p.update(price_source="carrier"))
	assert version not in changes.values()
	assert len(set(changes.values())) == len(changes), "each change gives its own version"
	# a card written to its row and read back is the same card, so the wizard's version matches the saved one
	rows = rows_from_policy(policy, COUNTRY_NAMES)
	back = policy_from_rows(rows["policy"], rows["zones"], rows["item_sets"], COUNTRY_CODES)
	assert snapshot_version(back) == version
	# a policy with no card does not share a version with one that has a card
	assert snapshot_version({**policy, "rate_card": None}) != version


@pytest.mark.parametrize("junk", JUNK, ids=JUNK_IDS)
def test_a_policy_with_a_rate_card_and_odd_content_never_raises_in_any_function(junk):
	reference = named(card_policy(rate_card=small_card()))
	for variant in with_junk_at_every_place(reference, junk):
		rows = rows_from_policy(variant, COUNTRY_NAMES)
		assert set(rows["policy"]) == POLICY_ROW_KEYS
		assert type(rows["policy"]["price_source"]) is str and type(rows["policy"]["rate_card"]) is str
		back = policy_from_rows(rows["policy"], rows["zones"], rows["item_sets"], COUNTRY_CODES)
		assert set(back) == POLICY_KEYS
		assert isinstance(back["price_source"], str)
		assert back["rate_card"] is None or isinstance(back["rate_card"], dict)
		assert re.fullmatch(r"[0-9a-f]{16}", snapshot_version(variant))
		public_summary_from(variant, COUNTRY_NAMES, Translator())
		for diff in (
			diff_policy(reference, variant),
			diff_policy(variant, reference),
			diff_policy(variant, variant),
		):
			assert set(diff) == set(NOTHING_CHANGED)
			assert set(diff["rate_card"]) == {"added", "removed", "changed"}
			assert all(isinstance(name, str) for names in diff["rate_card"].values() for name in names)
		same = diff_policy(variant, variant)
		assert same["rate_card"] == NOTHING_CHANGED["rate_card"] and "rate_card" not in same["settings"]
