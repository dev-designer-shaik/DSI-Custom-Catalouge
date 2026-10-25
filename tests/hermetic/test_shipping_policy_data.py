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
from dsi_catalogue.shipping_shopify_import import parse_shopify_export

FIXTURE = Path(__file__).parent / "fixtures" / "shipping_policy_example.json"

SAMPLE_CARD_CSV = Path(__file__).parent / "fixtures" / "shopify_shipping_config_sample.csv"

EN_DASH = chr(0x2013)  # in the names of some zones of the sample card, as the export has it

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


def test_current_policy_roundtrip_and_guest_redaction():
	p = json.loads(FIXTURE.read_text())
	rows = rows_from_policy(p, {"US": "United States"})
	rebuilt = policy_from_rows(rows["policy"], rows["zones"], rows["item_sets"], {"United States": "us"})
	assert rebuilt == p
	result = {
		"policy_active": True,
		"offered": True,
		"offers": [
			{
				"service_id": "A",
				"name": "A",
				"amount": 4,
				"provider": "Example",
				"carrier_code": "EXP",
				"scope": "International",
				"trace": ["private"],
				"formula": "private",
			}
		],
	}
	answer = response_from(result, lambda s: s)
	assert answer["offers"][0]["provider"] == "Example"
	assert "trace" not in answer["offers"][0] and "formula" not in answer["offers"][0]


def test_shipment_quote_limits_and_duplicates():
	from dsi_catalogue.shipping_policy_data import clean_shipment_quotes

	with pytest.raises(InputError):
		clean_shipment_quotes({str(i): {} for i in range(11)})
	with pytest.raises(InputError):
		clean_shipment_quotes(
			{"0": {"quotes": [{"service_id": "A", "amount": 1}, {"service_id": "A", "amount": 1}]}}
		)


def test_guest_plan_only_requests_carrier_shipments():
	result = {
		"needs_quotes": True,
		"shipments": [
			{"index": 0, "quote": False, "items": [{"item_code": "PEN", "qty": 1}]},
			{"index": 1, "quote": True, "items": [{"item_code": "GIFT", "qty": 1}], "services": None},
		],
	}
	assert [s["index"] for s in response_from(result, str)["shipments"]] == [1]
