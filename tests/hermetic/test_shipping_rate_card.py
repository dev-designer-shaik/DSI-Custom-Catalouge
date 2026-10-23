"""Pins the interim rate card: reading the Shopify export, checking a card, putting an item into a profile and
pricing an order from the card.

Most cases run against the trimmed sample in fixtures/shopify_shipping_config_sample.csv (20 rows of the real
export, real prices, the country lists cut to a few countries) and
fixtures/shopify_countries_not_delivered_sample.csv. Once parsed, the sample card has four profiles in this order:

	0  Dangerous Goods Petite         F-*-PT, F-CR-PC-*   GCC (flat 20), International (2.41-4 lb 80, 4.1-5 lb 100)
	1  Refill Service                 F-*-RF              USA (0.01-2 lb 60, 2.01-4 lb 80)
	2  Dangerous Goods                F-*                 USA, Europe, Tier 2, Bahrain (flat 0), GCC (flat 35)
	3  Rest of the World Allowed ...  *                   Worldwide (3 tiers), GCC (flat 0)

In 9 rows of the sample (file lines 7-10, 13-15 and 20-21) the Country count cell was recounted to the cut
list, so it says 4 or 5 where the real export says Europe 43, Tier 2 28, Worldwide 226 and Petite
International 72. Every other count cell is the real one.

One local-only test reads the real export from ~/shipping-interim-rates (or $RATES_DIR), which is outside this
public repository, and skips when the files are not there. The module under test is pure Python; nothing here
needs Frappe.
"""

import ast
import copy
import csv
import io
import json
import math
import os
import random
import runpy
from decimal import Decimal
from pathlib import Path

import pytest

from dsi_catalogue import shipping_rate_card
from dsi_catalogue.shipping_rate_card import (
	DEFAULT_ITEM_WEIGHT_KG,
	KNOWN_PROFILES,
	LB_PER_KG,
	RateCardError,
	card_summary,
	classify_item,
	lb_text,
	normalize_index_key,
	parse_shopify_export,
	price_card,
	validate_card,
)

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_CONFIG = FIXTURES / "shopify_shipping_config_sample.csv"
SAMPLE_NOT_DELIVERED = FIXTURES / "shopify_countries_not_delivered_sample.csv"
# The real export stays outside this public repository, and `real_text` looks for it only when a test asks.

HEADER = [
	"Profile",
	"Zone (recommended)",
	"Country count",
	"Countries (ISO-2 - Name)",
	"Shipping method",
	"Weight from (lb)",
	"Weight to (lb)",
	"Price (USD)",
]
EXPRESS = "International Express (4-7 days)"
SHIPPING = "International Shipping (10-15 days)"
GCC_FREE = "Free Shipping (1-3 days)"
BAHRAIN_FREE = "Free Shipping (1-2 days)"

# One product code of each kind, written the way the website keeps them.
DANGEROUS = "{F-CR-CSE-W-40}"
PETITE = "{F-CR-OSA-M-PT}"
REFILL = "{F-CR-OSA-M-100-RF}"
ALLOWED = "{P-AA-AT1-IG}"

RESULT_KEYS = {"priced", "reason", "total", "name", "days", "lines", "detail"}
LINE_KEYS = {"profile", "zone", "weight_lb", "price", "tier", "items", "defaulted_weight"}
REASONS = {None, "not_covered", "too_heavy", "not_offered"}
SUMMARY_KEYS = {"profiles", "zones", "tiers", "countries"}

MISSING = object()  # an item with no weight_kg key at all


# ---------------------------------------------------------------- helpers
def canonical(value):
	"""JSON text with sorted keys. Two values match here only when every number has the same type too: 35 is
	not 35.0."""
	return json.dumps(value, sort_keys=True, ensure_ascii=False)


def sample_config():
	"""The sample file as a plain UTF-8 decode gives it: the byte order mark and the CRLF line ends are still
	there, as in the Shopify export."""
	return SAMPLE_CONFIG.read_bytes().decode("utf-8")


def sample_not_delivered():
	return SAMPLE_NOT_DELIVERED.read_bytes().decode("utf-8")


def sample():
	return parse_shopify_export(sample_config(), sample_not_delivered())


def sample_card():
	"""A fresh copy of the parsed sample card, so a test can change it freely."""
	return sample()["card"]


def tier(low, high, price):
	return {"from": low, "to": high, "price": price}


def flat(price):
	return {"from": None, "to": None, "price": price}


def rate_row(
	profile="Dangerous Goods",
	zone="USA",
	countries="US - United States",
	method=EXPRESS,
	low="0.01",
	high="2",
	price="35",
	count=None,
):
	"""One row of the export as eight cells. The country count is worked out from the list unless given."""
	if count is None:
		count = str(len([token for token in countries.split(";") if token.strip()]))
	return [profile, zone, count, countries, method, low, high, price]


def csv_of(*rows, header=HEADER, eol="\r\n"):
	buffer = io.StringIO(newline="")
	csv.writer(buffer, lineterminator=eol).writerows([header, *rows])
	return buffer.getvalue()


def parse_rows(*rows, **kwargs):
	return parse_shopify_export(csv_of(*rows, **kwargs))


def profile_of(card, name):
	return next(profile for profile in card["profiles"] if profile["name"] == name)


def zone_of(card, profile_name, zone_name):
	return next(
		zone for zone in profile_of(card, profile_name)["zones"] if zone["name"].startswith(zone_name)
	)


def notes_for(parsed, start):
	return [note for note in parsed["notes"] if note.startswith(start)]


def problems_with(problems, fragment):
	return [problem for problem in problems if fragment in problem]


def pounds(lb):
	"""The unit weight, in kilograms, of something that weighs `lb` pounds."""
	return lb / LB_PER_KG


def item(key, weight_kg=MISSING, qty=1):
	entry = {"index_key": key, "qty": qty}
	if weight_kg is not MISSING:
		entry["weight_kg"] = weight_kg
	return entry


def heavy(key, lb, qty=1):
	"""An item of `qty` units that together weigh `lb` pounds."""
	return item(key, pounds(lb) / qty, qty)


def order(country, *items):
	return {"country_code": country, "items": list(items)}


def price(card, country, *items):
	return price_card(card, order(country, *items))


def check_shape(result):
	"""Every result has the same keys and types, whether or not the order could be priced."""
	assert set(result) == RESULT_KEYS
	assert type(result["priced"]) is bool
	assert result["reason"] in REASONS
	assert (result["reason"] is None) == result["priced"]
	assert type(result["total"]) is float and math.isfinite(result["total"]) and result["total"] >= 0
	assert math.copysign(1.0, result["total"]) == 1.0, "never -0.0"
	assert isinstance(result["name"], str)
	assert result["days"] is None or type(result["days"]) is int
	assert isinstance(result["detail"], str) and result["detail"].endswith(".")
	assert isinstance(result["lines"], list)
	if not result["priced"]:
		assert (result["total"], result["name"], result["days"]) == (0.0, "", None)
	cents = Decimal(0)
	for line in result["lines"]:
		assert set(line) == LINE_KEYS
		assert isinstance(line["profile"], str) and isinstance(line["zone"], str)
		assert (
			type(line["weight_lb"]) is float and math.isfinite(line["weight_lb"]) and line["weight_lb"] >= 0
		)
		assert type(line["items"]) is int and line["items"] >= 1
		assert type(line["defaulted_weight"]) is bool
		if line["price"] is None:
			assert line["tier"] is None
			assert not result["priced"]
		else:
			assert type(line["price"]) is float and line["price"] >= 0
			assert type(line["tier"]) is int and line["tier"] >= 1
			cents += Decimal(str(line["price"]))
	if result["priced"]:
		assert result["lines"], "a priced order has at least one line"
		assert Decimal(str(result["total"])) == cents, "the total is the sum of the lines"
	return result


def priced(result, total):
	check_shape(result)
	assert result["priced"] is True, result["detail"]
	assert result["reason"] is None
	assert result["total"] == total
	return result


def refused(result, reason):
	check_shape(result)
	assert result["priced"] is False
	assert result["reason"] == reason
	return result


# ---------------------------------------------------------------- the constants and the module
def test_the_constants_and_the_known_profiles_are_the_ones_the_plan_fixes():
	assert LB_PER_KG == 2.20462262
	assert DEFAULT_ITEM_WEIGHT_KG == 0.35
	assert issubclass(RateCardError, ValueError)
	assert list(KNOWN_PROFILES.items()) == [
		("Dangerous Goods Petite", ["F-*-PT", "F-CR-PC-*"]),
		("Refill Service", ["F-*-RF"]),
		("Dangerous Goods", ["F-*"]),
		("Rest of the World Allowed Items", ["*"]),
	]


def test_the_module_is_pure_python_that_imports_only_what_it_needs():
	"""No frappe, and no engine: the engine imports this module, never the other way round."""
	source = Path(shipping_rate_card.__file__).read_text(encoding="utf-8")
	imported = set()
	for node in ast.walk(ast.parse(source)):
		if isinstance(node, ast.Import):
			imported.update(alias.name.split(".")[0] for alias in node.names)
		elif isinstance(node, ast.ImportFrom):
			imported.add((node.module or "").split(".")[0])
	assert imported == {"csv", "decimal", "fnmatch", "io", "math"}


@pytest.mark.parametrize(
	("pounds", "text"),
	[
		(0.7, "0.7"),
		(2.0, "2"),
		(2.01, "2.01"),
		(18.01, "18.01"),
		(1.984, "1.984"),
		(0.0, "0"),
		(10000.0, "10000"),
		(0.1 + 0.2, "0.3"),  # not 0.30000000000000004
		(3.327, "3.327"),
		(1e19, "1e+19"),
	],
)
def test_lb_text_writes_a_weight_the_way_a_sentence_does(pounds, text):
	assert lb_text(pounds) == text
	assert not hasattr(shipping_rate_card, "_lb"), "one public function, not a private copy"


# ---------------------------------------------------------------- reading the export
SAMPLE_ZONES = {
	"Dangerous Goods Petite": [
		(
			"GCC \u2013 KW, OM, QA, SA, AE",
			["KW", "OM", "QA", "SA", "AE"],
			"International Express (1-3 days)",
			3,
			[flat(20.0)],
		),
		(
			"International (Allowed Countries)",
			["US", "AL", "AD", "AT"],
			SHIPPING,
			15,
			[tier(2.41, 4.0, 80.0), tier(4.1, 5.0, 100.0)],
		),
	],
	"Refill Service": [("USA", ["US"], SHIPPING, 15, [tier(0.01, 2.0, 60.0), tier(2.01, 4.0, 80.0)])],
	"Dangerous Goods": [
		(
			"USA",
			["US"],
			EXPRESS,
			7,
			[
				tier(0.1, 0.6, 35.0),
				tier(0.7, 2.0, 60.0),
				tier(2.01, 4.0, 80.0),
				tier(4.01, 5.0, 100.0),
				tier(18.01, 20.0, 400.0),
			],
		),
		(
			"Europe, UK & Select Asia",
			["GB", "AL", "AD", "AT"],
			EXPRESS,
			7,
			[tier(0.01, 2.0, 75.0), tier(2.01, 4.0, 95.0)],
		),
		(
			"Asia, Africa & Americas (Tier 2)",
			["BJ", "BF", "KH", "CI"],
			EXPRESS,
			7,
			[tier(0.01, 2.0, 90.0), tier(2.01, 4.0, 110.0)],
		),
		("Bahrain (Local)", ["BH"], BAHRAIN_FREE, 2, [flat(0.0)]),
		("GCC \u2013 KW, SA, AE, OM", ["KW", "SA", "AE", "OM"], EXPRESS, 7, [flat(35.0)]),
	],
	"Rest of the World Allowed Items": [
		(
			"Worldwide (Allowed Items)",
			["AX", "AL", "AU", "CN", "US"],
			EXPRESS,
			7,
			[tier(0.01, 2.0, 40.0), tier(2.01, 4.0, 80.0), tier(4.01, 5.0, 100.0)],
		),
		(
			"GCC \u2013 all 6 (incl. BH & QA)",
			["BH", "KW", "OM", "SA", "AE", "QA"],
			GCC_FREE,
			3,
			[flat(0.0)],
		),
	],
}


def test_parse_reads_profiles_zones_tiers_and_flat_rows():
	card = sample()["card"]
	assert set(card) == {"version", "currency", "unit", "profiles"}
	assert (card["version"], card["currency"], card["unit"]) == (1, "USD", "lb")
	assert all(set(profile) == {"name", "match", "zones"} for profile in card["profiles"])
	assert all(
		set(zone) == {"name", "countries", "method", "days", "tiers"}
		for p in card["profiles"]
		for zone in p["zones"]
	)
	actual = {
		profile["name"]: [
			(zone["name"], zone["countries"], zone["method"], zone["days"], zone["tiers"])
			for zone in profile["zones"]
		]
		for profile in card["profiles"]
	}
	# compared as JSON text, so a price of 35 would not pass for 35.0
	assert canonical(actual) == canonical(SAMPLE_ZONES)
	assert json.loads(json.dumps(card, allow_nan=False)) == card, "the card is plain JSON"
	# the accented names are gone: only the two-letter codes are kept
	assert zone_of(card, "Dangerous Goods", "Asia")["countries"][-1] == "CI"
	assert zone_of(card, "Rest of the World Allowed Items", "Worldwide")["countries"][0] == "AX"


def test_parse_the_sample_has_the_counts_the_wizard_will_show():
	parsed = sample()
	assert card_summary(parsed["card"]) == {"profiles": 4, "zones": 10, "tiers": 20, "countries": 18}
	assert parsed["not_delivered"] == ["AF", "KP", "RU"]


def test_parse_puts_the_catch_all_last_and_gives_known_profiles_their_patterns():
	# in the file: Dangerous Goods, Rest of the World Allowed Items, Refill Service, Dangerous Goods Petite
	raw = list(csv.reader(io.StringIO(sample_config().lstrip("\ufeff"), newline="")))
	assert list(dict.fromkeys(row[0] for row in raw[1:])) == [
		"Dangerous Goods",
		"Rest of the World Allowed Items",
		"Refill Service",
		"Dangerous Goods Petite",
	]
	card = sample_card()
	assert [profile["name"] for profile in card["profiles"]] == list(KNOWN_PROFILES)
	for profile in card["profiles"]:
		assert profile["match"] == KNOWN_PROFILES[profile["name"]]
		assert profile["match"] is not KNOWN_PROFILES[profile["name"]], "a copy, not the module's own list"
	card["profiles"][0]["match"].append("X")
	assert KNOWN_PROFILES["Dangerous Goods Petite"] == ["F-*-PT", "F-CR-PC-*"]


def test_parse_orders_unknown_profiles_between_the_known_ones_and_the_catch_all():
	parsed = parse_rows(
		rate_row("Rest of the World Allowed Items", "Everywhere"),
		rate_row("Gadgets", "Anywhere"),
		rate_row("Dangerous Goods", "USA"),
		rate_row("Accessories", "Anywhere"),
		rate_row("Refill Service", "USA"),
	)
	card = parsed["card"]
	assert [profile["name"] for profile in card["profiles"]] == [
		"Refill Service",
		"Dangerous Goods",
		"Gadgets",
		"Accessories",
		"Rest of the World Allowed Items",
	]
	assert [profile["match"] for profile in card["profiles"]] == [["F-*-RF"], ["F-*"], [], [], ["*"]]


def test_parse_matches_profile_names_ignoring_case_and_extra_spaces():
	parsed = parse_rows(
		rate_row("  dangerous   GOODS ", "USA", low="0.1", high="1", price="30"),
		rate_row("Dangerous Goods", "usa", low="1.01", high="2", price="50"),
		rate_row("REFILL service", "USA"),
	)
	card = parsed["card"]
	assert [profile["name"] for profile in card["profiles"]] == ["Refill Service", "Dangerous Goods"]
	assert profile_of(card, "Dangerous Goods")["match"] == ["F-*"]
	# one profile, one zone, both tiers: the spelling of the profile and the zone does not split them
	(zone,) = profile_of(card, "Dangerous Goods")["zones"]
	assert zone["name"] == "USA"
	assert zone["tiers"] == [tier(0.1, 1.0, 30.0), tier(1.01, 2.0, 50.0)]


def test_parse_groups_rows_by_profile_and_zone_wherever_they_are_and_sorts_the_tiers():
	parsed = parse_rows(
		rate_row(zone="USA", low="2.01", high="4", price="80"),
		rate_row(zone="Europe", countries="GB - United Kingdom", low="0.01", high="2", price="75"),
		rate_row(zone="USA", low="0.01", high="2", price="60"),
		rate_row(zone="USA", low="4.01", high="5", price="100"),
	)
	profile = profile_of(parsed["card"], "Dangerous Goods")
	assert [zone["name"] for zone in profile["zones"]] == ["USA", "Europe"]
	assert profile["zones"][0]["tiers"] == [
		tier(0.01, 2.0, 60.0),
		tier(2.01, 4.0, 80.0),
		tier(4.01, 5.0, 100.0),
	]


@pytest.mark.parametrize(
	("tokens", "codes"),
	[
		("US - United States; GB - United Kingdom", ["US", "GB"]),
		("us - lower case ;  gb  - spaced", ["US", "GB"]),
		("CD - Congo - Kinshasa (DRC); CG - Congo - Brazzaville", ["CD", "CG"]),
		("AX - \u00c5land Islands; CI - C\u00f4te d'Ivoire", ["AX", "CI"]),
		("US; GB", ["US", "GB"]),
		("US - United States;", ["US"]),
		("; US - United States ;; GB - UK", ["US", "GB"]),
	],
)
def test_parse_reads_country_tokens_by_the_code_before_the_first_dash(tokens, codes):
	parsed = parse_rows(rate_row(countries=tokens, count=str(len(codes))))
	assert zone_of(parsed["card"], "Dangerous Goods", "USA")["countries"] == codes
	assert notes_for(parsed, "Row") == []


def test_parse_wants_the_spaced_dash_between_a_country_code_and_its_name():
	parsed = parse_rows(
		rate_row(countries="US - United States; GB-United Kingdom; FR -France; DE- Germany", count="1")
	)
	assert zone_of(parsed["card"], "Dangerous Goods", "USA")["countries"] == ["US"]
	(note,) = notes_for(parsed, "Row 2")
	for token in ('"GB-United Kingdom"', '"FR -France"', '"DE- Germany"'):
		assert token in note


def test_parse_copes_with_numbers_of_any_length():
	long_digits = "9" * 5000  # int() refuses a run of more than 4300 digits
	parsed = parse_rows(
		rate_row(count=long_digits, method=f"Express ({long_digits} days)", price="9" * 300),
		rate_row(low="1", high=long_digits, price="1"),
		rate_row(low="1", high="2", price=long_digits),
		rate_row(low="3", high="4", price="9" * 400),
	)
	zone = zone_of(parsed["card"], "Dangerous Goods", "USA")
	assert zone["days"] is None
	assert [t["price"] for t in zone["tiers"]] == [float("9" * 300)], (
		"a number too big for a float is no number"
	)
	skipped = [note.split(":")[0] for note in parsed["notes"] if note.startswith("Row")]
	assert skipped == ["Row 3 was skipped", "Row 4 was skipped", "Row 5 was skipped"]
	assert not [n for n in parsed["notes"] if "countries," in n], "a count of 5000 digits is not a count"
	assert validate_card(parsed["card"]), "and a price of 1e300 is for validate_card to report"


@pytest.mark.parametrize(
	("method", "days"),
	[
		("International Express (4-7 days)", 7),
		("Free Shipping (1-2 days)", 2),
		("International Shipping (10-15 days)", 15),
		("Express (1 day)", 1),
		("Express (4\u20137 days)", 7),
		("Express (priority) (3-5 days)", 5),
		("Express (3-5 days) (note)", None),
		("Express (3-5 days", None),
		("Express 3-5 days", None),
		("Express (next day)", None),
		("Express ()", None),
		("Express (" + "9" * 40 + " days)", None),
		("", None),
	],
)
def test_parse_takes_the_days_from_the_last_brackets(method, days):
	parsed = parse_rows(rate_row(method=method))
	assert zone_of(parsed["card"], "Dangerous Goods", "USA")["days"] == days
	assert zone_of(parsed["card"], "Dangerous Goods", "USA")["method"] == method


@pytest.mark.parametrize(
	("text", "number"),
	[
		("35", 35.0),
		(" 35 ", 35.0),
		("35.00", 35.0),
		("+5", 5.0),
		(".5", 0.5),
		("5.", 5.0),
		("0.125", 0.13),  # cents are rounded half up from the decimal as written
		("2.675", 2.68),  # the float 2.675 is 2.67499..., so rounding the float would give 2.67
		("0.134", 0.13),
		("0.135", 0.14),
		("-5", -5.0),  # kept: validate_card reports a price below zero
	],
)
def test_parse_reads_prices_as_decimals_rounded_to_cents(text, number):
	parsed = parse_rows(rate_row(price=text))
	assert zone_of(parsed["card"], "Dangerous Goods", "USA")["tiers"][0]["price"] == number
	assert type(zone_of(parsed["card"], "Dangerous Goods", "USA")["tiers"][0]["price"]) is float


@pytest.mark.parametrize(
	"text",
	[
		"1_0",
		"\u0661\u0662",
		"nan",
		"inf",
		"-inf",
		"1e3",
		"$5",
		"5,0",
		"1,000",
		"abc",
		"5 0",
		"--5",
		"0x10",
		"\u00b2",
	],
)
def test_parse_refuses_prices_and_weights_that_are_not_plain_decimal_numbers(text):
	for bad in (rate_row(price=text), rate_row(low=text), rate_row(high=text)):
		parsed = parse_rows(rate_row(zone="Good"), bad)
		assert notes_for(parsed, "Row 3 was skipped"), text
		(zone,) = profile_of(parsed["card"], "Dangerous Goods")["zones"]
		assert zone["name"] == "Good" and len(zone["tiers"]) == 1, "the bad row left nothing behind"


def test_parse_notes_what_it_cannot_use():
	parsed = parse_rows(
		rate_row(
			countries="US - United States; Narnia; XX1 - Bad; ZZ - Zedland", count="2", low="0.1", high="0.6"
		),
		rate_row(low="0.7", high="", price="60"),
		rate_row("Gadgets", "Anywhere", method="Express (3 days)", price="12"),
		rate_row(low="2.01", high="4", price="abc"),
	)
	card = parsed["card"]
	# the row with only one weight and the row with a bad price are not in the card
	assert zone_of(card, "Dangerous Goods", "USA")["tiers"] == [tier(0.1, 0.6, 35.0)]
	assert zone_of(card, "Dangerous Goods", "USA")["countries"] == ["US", "ZZ"]
	(bad_tokens,) = notes_for(parsed, "Row 2")  # one note for the row, naming both tokens
	assert '"Narnia"' in bad_tokens and '"XX1 - Bad"' in bad_tokens and '"USA"' in bad_tokens
	assert "two-letter country code" in bad_tokens and "ZZ" not in bad_tokens
	(one_weight,) = notes_for(parsed, "Row 3")
	assert "skipped" in one_weight and "only one of the two weights" in one_weight
	(bad_price,) = notes_for(parsed, "Row 5")
	assert "skipped" in bad_price and '"abc"' in bad_price and "not a number" in bad_price
	# an unknown profile is kept, gets no patterns, and says so
	assert profile_of(card, "Gadgets")["match"] == []
	(unknown,) = [note for note in parsed["notes"] if '"Gadgets"' in note and "patterns" in note]
	assert "not one of the known profiles" in unknown
	assert not notes_for(parsed, "Row 4"), "the Gadgets row itself was fine"


@pytest.mark.parametrize(
	("cells", "what"),
	[
		(rate_row(profile=""), "no profile"),
		(rate_row(profile="   "), "no profile"),
		(rate_row(zone=""), "no zone name"),
		(rate_row(countries="", count="0"), "no country list"),
		(rate_row(countries="  ; ; ", count="0"), "no country list"),
		(rate_row(price=""), "no price"),
		(rate_row(price="  "), "no price"),
		(rate_row(low="", high="2"), "only one of the two weights"),
		(rate_row(low="1", high=" "), "only one of the two weights"),
		(rate_row(low="abc", high="2"), "weight"),
		(rate_row(low="1", high="2.5.1"), "weight"),
		(["Dangerous Goods", "USA"], "no country list"),  # a short row
	],
)
def test_parse_skips_a_row_that_misses_something_and_says_which_row(cells, what):
	parsed = parse_rows(rate_row(zone="Good", low="0.1", high="2"), cells)
	(note,) = notes_for(parsed, "Row 3")
	assert note.startswith("Row 3 was skipped") and what in note
	assert [zone["name"] for zone in profile_of(parsed["card"], "Dangerous Goods")["zones"]] == ["Good"]


def test_parse_row_numbers_count_the_header_as_row_1_and_the_blank_lines_as_rows():
	text = csv_of(rate_row(zone="Good"), rate_row(price=""))
	assert notes_for(parse_shopify_export(text), "Row 3 was skipped")
	spaced = text.replace("\r\n", "\r\n\r\n")  # a blank line after every line
	assert notes_for(parse_shopify_export(spaced), "Row 5 was skipped")
	assert parse_shopify_export(spaced)["card"] == parse_shopify_export(text)["card"]


def test_parse_keeps_the_first_row_of_a_zone_and_notes_what_the_later_rows_disagree_about():
	parsed = parse_rows(
		rate_row(countries="US - United States; CA - Canada", low="0.01", high="2", price="35"),
		rate_row(countries="US - United States; MX - Mexico", low="2.01", high="4", price="60"),
		rate_row(
			countries="US - United States; CA - Canada",
			method="Express (2-3 days)",
			low="4.01",
			high="6",
			price="80",
		),
		rate_row(countries="CA - Canada; US - United States", low="6.01", high="8", price="95"),
	)
	zone = zone_of(parsed["card"], "Dangerous Goods", "USA")
	assert zone["countries"] == ["US", "CA"]
	assert zone["method"] == EXPRESS
	assert [t["price"] for t in zone["tiers"]] == [35.0, 60.0, 80.0, 95.0], "every row still gives a tier"
	(countries,) = notes_for(parsed, "Row 3")
	assert "different countries" in countries and "row 2" in countries
	(method,) = notes_for(parsed, "Row 4")
	assert "different shipping method" in method and "row 2" in method
	assert not notes_for(parsed, "Row 5"), "the same countries in another order are not a disagreement"


def test_parse_notes_a_country_count_that_does_not_match_the_list_once_per_zone():
	parsed = parse_rows(
		rate_row(countries="US - United States; CA - Canada", count="5", low="0.01", high="2"),
		rate_row(countries="US - United States; CA - Canada", count="5", low="2.01", high="4"),
	)
	(note,) = [n for n in parsed["notes"] if "5 countries" in n]
	assert note.startswith("Row 2") and "2 could be read" in note
	# a count that is right, or not a number, or missing, says nothing
	assert not [n for n in parse_rows(rate_row(count="1"))["notes"] if "count" in n]
	assert not [n for n in parse_rows(rate_row(count="many"))["notes"] if "count" in n]
	assert not [n for n in parse_rows(rate_row(count=""))["notes"] if "count" in n]


def test_parse_notes_a_country_listed_twice_and_keeps_it_once():
	parsed = parse_rows(rate_row(countries="US - United States; us - again; CA - Canada", count="3"))
	assert zone_of(parsed["card"], "Dangerous Goods", "USA")["countries"] == ["US", "CA"]
	(note,) = [n for n in parsed["notes"] if "more than once" in n]
	assert note.startswith("Row 2") and "US" in note


def test_parse_skips_a_header_row_repeated_in_the_middle_of_the_file_and_says_so():
	"""Two exports pasted together leave a second header row. It has no price, so it is skipped like any row
	without one, and the rows around it are all kept."""
	europe = rate_row(zone="Europe", countries="GB - United Kingdom", price="75")
	parsed = parse_rows(rate_row(), HEADER, europe)
	assert parsed["notes"] == ['Row 3 was skipped: the price "Price (USD)" is not a number.']
	assert [z["name"] for z in profile_of(parsed["card"], "Dangerous Goods")["zones"]] == ["USA", "Europe"]
	assert zone_of(parsed["card"], "Dangerous Goods", "USA")["tiers"] == [tier(0.01, 2.0, 35.0)]
	assert zone_of(parsed["card"], "Dangerous Goods", "Europe")["tiers"] == [tier(0.01, 2.0, 75.0)]


def test_parse_uses_the_first_of_two_columns_with_the_same_title():
	header = [*HEADER, "Price (USD)"]
	parsed = parse_shopify_export(csv_of([*rate_row(price="35"), "999"], header=header))
	assert zone_of(parsed["card"], "Dangerous Goods", "USA")["tiers"] == [tier(0.01, 2.0, 35.0)]
	assert parsed["notes"] == []
	# the first one, wherever it is: the copy that comes second is the one ignored
	header = ["price (usd) ", *HEADER]
	parsed = parse_shopify_export(csv_of(["999", *rate_row(price="35")], header=header))
	assert zone_of(parsed["card"], "Dangerous Goods", "USA")["tiers"] == [tier(0.01, 2.0, 999.0)]


def test_parse_keeps_a_zone_whose_countries_are_all_unreadable_and_the_card_says_it_has_none():
	"""Not one token starts with a two-letter code: they are left out, with a note, and the zone has no
	countries, which validate_card reports and which no order can ever be delivered to."""
	parsed = parse_rows(rate_row(countries="Atlantis; Narnia - Fantasy; 12 - Number"))
	zone = zone_of(parsed["card"], "Dangerous Goods", "USA")
	assert zone["countries"] == []
	(left_out,) = notes_for(parsed, "Row 2: left out")
	assert '"Atlantis", "Narnia - Fantasy", "12 - Number"' in left_out
	assert problems_with(
		validate_card(parsed["card"]), 'Zone 1 ("USA") of profile 1 ("Dangerous Goods") has no countries.'
	)
	refused(price(parsed["card"], "US", item(DANGEROUS, 1.0)), "not_covered")


def test_parse_flat_rows_and_the_ways_they_can_clash_with_tiers():
	flat_row = rate_row(zone="Flat", low="", high="", price="35")
	# a flat zone: both weights empty, one row
	card = parse_rows(flat_row)["card"]
	assert zone_of(card, "Dangerous Goods", "Flat")["tiers"] == [flat(35.0)]
	# a flat row and tier rows in one zone: the tiers win and the flat row is noted
	parsed = parse_rows(
		rate_row(zone="Mixed", low="", high="", price="9"),
		rate_row(zone="Mixed", low="0.01", high="2", price="35"),
	)
	assert zone_of(parsed["card"], "Dangerous Goods", "Mixed")["tiers"] == [tier(0.01, 2.0, 35.0)]
	(note,) = [n for n in parsed["notes"] if "flat price" in n]
	assert "weight tiers are kept" in note and "row 2" in note
	# two flat rows: the first is kept
	parsed = parse_rows(flat_row, rate_row(zone="Flat", low="", high="", price="50"))
	assert zone_of(parsed["card"], "Dangerous Goods", "Flat")["tiers"] == [flat(35.0)]
	(note,) = [n for n in parsed["notes"] if "two flat prices" in n]
	assert "row 2" in note and "row 3" in note


def test_parse_notes_a_zone_without_a_delivery_method():
	parsed = parse_rows(rate_row(method=""), rate_row(method="", low="2.01", high="4"))
	assert zone_of(parsed["card"], "Dangerous Goods", "USA")["method"] == ""
	assert len([n for n in parsed["notes"] if "no shipping method" in n]) == 1
	# a later row that has the method gives it to the zone
	parsed = parse_rows(rate_row(method=""), rate_row(method=EXPRESS, low="2.01", high="4"))
	assert zone_of(parsed["card"], "Dangerous Goods", "USA")["method"] == EXPRESS
	assert zone_of(parsed["card"], "Dangerous Goods", "USA")["days"] == 7
	assert not [n for n in parsed["notes"] if "method" in n]


def test_parse_notes_gaps_between_tiers_and_a_late_first_tier_but_does_not_fix_them():
	parsed = sample()
	card = parsed["card"]
	# the real quirks are still in the card
	assert zone_of(card, "Dangerous Goods", "USA")["tiers"][:2] == [
		tier(0.1, 0.6, 35.0),
		tier(0.7, 2.0, 60.0),
	]
	assert zone_of(card, "Dangerous Goods Petite", "International")["tiers"][1]["from"] == 4.1
	gaps = [n for n in parsed["notes"] if "gap" in n]
	assert sorted(gaps) == sorted(
		[
			'Zone "USA" of profile "Dangerous Goods": there is a gap between 0.6 lb and 0.7 lb. Weights in the gap pay the next tier up.',
			'Zone "USA" of profile "Dangerous Goods": there is a gap between 5 lb and 18.01 lb. Weights in the gap pay the next tier up.',
			'Zone "International (Allowed Countries)" of profile "Dangerous Goods Petite": there is a gap between 4 lb and 4.1 lb. Weights in the gap pay the next tier up.',
		]
	)
	late = [n for n in parsed["notes"] if "first tier starts" in n]
	assert sorted(late) == sorted(
		[
			'Zone "USA" of profile "Dangerous Goods": the first tier starts at 0.1 lb. Lighter weights pay the first tier.',
			# the sample keeps only the last two tiers of the petite zone, so it starts at 2.41 lb
			'Zone "International (Allowed Countries)" of profile "Dangerous Goods Petite": the first tier starts at 2.41 lb. Lighter weights pay the first tier.',
		]
	)
	assert len(parsed["notes"]) == 5, "nothing else to say about the sample"


def test_parse_does_not_note_the_usual_one_hundredth_of_a_pound_between_tiers():
	parsed = parse_rows(
		rate_row(low="0.01", high="2", price="1"),
		rate_row(low="2.01", high="4", price="2"),
		rate_row(low="4.01", high="6", price="3"),
		rate_row(low="6.011", high="8", price="4"),  # 0.011 is still not "more than 0.011"
		rate_row(low="8.012", high="9", price="5"),
	)
	(gap,) = parsed["notes"]
	assert "gap between 8 lb and 8.012 lb" in gap
	# and a first tier that starts at 0.011 lb is not late, but one at 0.012 lb is
	assert parse_rows(rate_row(low="0.011", high="2"))["notes"] == []
	(late,) = parse_rows(rate_row(low="0.012", high="2"))["notes"]
	assert "first tier starts at 0.012 lb" in late


def test_parse_accepts_any_column_order_case_and_spacing_and_ignores_extra_columns():
	header = [
		"PRICE (usd)",
		"extra",
		" profile ",
		"Weight   to (lb)",
		"weight from (LB)",
		"Zone (Recommended)",
		"Shipping  method",
		"Countries (iso-2 - name)",
		"Country count",
		"More",
	]
	cells = ["35", "x", "Dangerous Goods", "2", "0.01", "USA", EXPRESS, "US - United States", "1", "y"]
	shuffled = parse_shopify_export(csv_of(cells, header=header))
	assert shuffled["card"] == parse_rows(rate_row())["card"]
	assert shuffled["notes"] == []
	# the Country count column is optional
	short_header = [h for h in HEADER if h != "Country count"]
	short = [c for i, c in enumerate(rate_row()) if HEADER[i] != "Country count"]
	assert parse_shopify_export(csv_of(short, header=short_header))["card"] == parse_rows(rate_row())["card"]


@pytest.mark.parametrize("eol", ["\r\n", "\n", "\r"])
@pytest.mark.parametrize("bom", ["", "\ufeff"])
def test_parse_accepts_a_byte_order_mark_and_any_line_ending(bom, eol):
	text = bom + csv_of(rate_row(), rate_row(low="2.01", high="4", price="60"), eol=eol)
	assert (
		parse_shopify_export(text)["card"]
		== parse_rows(rate_row(), rate_row(low="2.01", high="4", price="60"))["card"]
	)


def test_parse_the_sample_files_keep_the_quirks_of_the_export():
	raw = SAMPLE_CONFIG.read_bytes()
	assert raw.startswith(b"\xef\xbb\xbf") and b"\r\n" in raw
	assert SAMPLE_NOT_DELIVERED.read_bytes().startswith(b"\xef\xbb\xbf")
	# decoded without utf-8-sig the mark is still in the text, and the parse copes
	assert sample_config().startswith("\ufeff")
	assert (
		parse_shopify_export(sample_config())["card"]
		== parse_shopify_export(sample_config().lstrip("\ufeff"))["card"]
	)


def test_parse_not_delivered_countries():
	assert sample()["not_delivered"] == ["AF", "KP", "RU"]
	assert sample()["notes"] == parse_shopify_export(sample_config())["notes"], "that file adds no notes"
	messy = (
		"ISO-2,Country\r\n"
		"ru,Russia\r\n"  # row 2
		"AF,Afghanistan\r\n"  # row 3
		"RU,Russia again\r\n"  # row 4: a repeat
		"\r\n"  # row 5: blank
		",No code\r\n"  # row 6
		"XYZ,Three letters\r\n"  # row 7
		"A1,A digit\r\n"  # row 8
		"\u00c0B,An accent\r\n"  # row 9
		"  cu  ,Cuba\r\n"  # row 10
	)
	parsed = parse_shopify_export(sample_config(), messy)
	assert parsed["not_delivered"] == ["RU", "AF", "CU"]
	skipped = [n for n in parsed["notes"] if n.startswith("Not-delivered row")]
	assert [n.split(" was skipped")[0] for n in skipped] == [f"Not-delivered row {r}" for r in (6, 7, 8, 9)]
	assert "XYZ" in skipped[1]
	# the columns can come in either order, and the second file never changes the card
	assert parse_shopify_export(sample_config(), "Country,ISO-2\r\nRussia,ru\r\n")["not_delivered"] == ["RU"]
	assert parsed["card"] == sample()["card"]


@pytest.mark.parametrize(
	("text", "fragment"),
	[
		("", "no countries"),
		("   \r\n\r\n", "no countries"),
		("ISO-2,Country\r\n", "no countries"),
		("Country\r\nRussia\r\n", '"ISO-2" column'),
		("ISO-2,Country\r\nXYZ,Bad\r\n", None),  # only a bad row: the row note says it
		('ISO-2,Country\r\n"RU,Russia\r\n', "could not be read as CSV"),
		(5, "not text"),
		(b"ISO-2\r\nRU\r\n", "not text"),
		(["RU"], "not text"),
	],
)
def test_parse_not_delivered_problems_are_notes_never_errors(text, fragment):
	parsed = parse_shopify_export(sample_config(), text)
	assert parsed["not_delivered"] == []
	extra = [n for n in parsed["notes"] if "not-delivered" in n.lower()]
	assert extra, "something is said"
	if fragment:
		assert any(fragment in n for n in extra)
	assert parsed["card"] == sample()["card"]


def test_parse_without_a_not_delivered_file_says_nothing_about_it():
	for parsed in (parse_shopify_export(sample_config()), parse_shopify_export(sample_config(), None)):
		assert parsed["not_delivered"] == []
		assert not [n for n in parsed["notes"] if "not-delivered" in n.lower()]


@pytest.mark.parametrize(
	("text", "fragment"),
	[
		("", "is empty"),
		("\ufeff", "is empty"),
		("   \r\n\t\r\n  ", "is empty"),
		("\r\n\r\n\r\n", "is empty"),
		(",,,,,,,\r\n,,,,,,,\r\n", "is empty"),
		(None, "has to be text"),
		(5, "has to be text"),
		(b"Profile,Zone (recommended)\r\n", "has to be text"),
		(["Profile"], "has to be text"),
		("Name,Price\r\nA,1\r\n", "missing these columns: Profile, Zone (recommended)"),
		("Profile;Zone (recommended);Price (USD)\r\nA;B;1\r\n", "missing these columns"),
		(
			csv_of(rate_row(), header=[h for h in HEADER if h != "Price (USD)"]),
			"missing these columns: Price (USD)",
		),
		(
			csv_of(rate_row(), header=[h for h in HEADER if h != "Countries (ISO-2 - Name)"]),
			"Countries (ISO-2 - Name)",
		),
		(",".join(HEADER) + "\r\n", "No row of the rates file could be used"),
		(
			csv_of(rate_row(price=""), rate_row(profile="")),
			"No row of the rates file could be used. Row 2 was skipped",
		),
		('Profile,Zone\r\n"unterminated', "could not be read as CSV"),
		(
			",".join(HEADER) + "\r\n" + "Dangerous Goods,USA," + "x" * 200000 + "\r\n",
			"could not be read as CSV",
		),
	],
)
def test_parse_refuses_a_file_without_the_expected_columns_or_rows(text, fragment):
	with pytest.raises(RateCardError) as raised:
		parse_shopify_export(text)
	message = str(raised.value)
	assert fragment in message
	assert message[0].isupper() and message.endswith(".") and "\n" not in message


def test_parse_never_raises_anything_but_a_rate_card_error_for_text():
	rng = random.Random(20261023)
	alphabet = [
		*',"\r\n;- ()0123456789.AaZz\u2013\u00c5\x00\ud800',
		"Profile",
		"Price (USD)",
		"US - X",
		"\ufeff",
		"\t",
	]
	texts = ["", '"', '"""', ",", "\x00", "\ud800", "a\r\n" * 5, "Profile\r\n", "\r", "\ufeff\ufeff"]
	for _ in range(300):
		texts.append("".join(rng.choice(alphabet) for _ in range(rng.randint(0, 80))))
	for _ in range(100):  # a plausible file with a few values knocked out or replaced
		rows = [rate_row(low=str(i), high=str(i + 1), price=str(i)) for i in range(5)]
		for row in rows:
			row[rng.randrange(8)] = rng.choice(["", "x", " ", "-1", "1e9", "\x00", "A" * 300, "nan"])
		texts.append(csv_of(*rows))
	for text in texts:
		try:
			parsed = parse_shopify_export(text, text)
		except RateCardError as error:
			assert str(error).endswith(".")
			continue
		assert set(parsed) == {"card", "not_delivered", "notes"}
		assert all(isinstance(note, str) and note for note in parsed["notes"])
		assert all(len(code) == 2 and code.isascii() and code.isupper() for code in parsed["not_delivered"])
		assert isinstance(validate_card(parsed["card"]), list)
		check_shape(price_card(parsed["card"], order("US", item(DANGEROUS, 1.0))))


# ---------------------------------------------------------------- checking a card
def test_validate_card_accepts_the_parsed_sample():
	assert validate_card(sample_card()) == []
	assert validate_card(json.loads(json.dumps(sample_card()))) == [], "and after a trip through JSON"
	# keys it does not know are ignored, wherever they are
	card = sample_card()
	card["note"] = "from the wizard"
	profile = card["profiles"][0]
	profile["colour"], profile["zones"][0]["comment"], profile["zones"][0]["tiers"][0]["id"] = "red", 5, [1]
	assert validate_card(card) == []
	assert card_summary(card) == card_summary(sample_card())


def zones(card, profile_name):
	return profile_of(card, profile_name)["zones"]


def set_in(card, profile_name, zone_name, **changes):
	zone_of(card, profile_name, zone_name).update(changes)


def a_tier_of_the_usa_zone(card, index, **changes):
	zone_of(card, "Dangerous Goods", "USA")["tiers"][index].update(changes)


def first_zone_tier(card, **changes):
	zones(card, "Dangerous Goods")[0]["tiers"][0].update(changes)


def make_card(profiles):
	return {"version": 1, "currency": "USD", "unit": "lb", "profiles": profiles}


def tidy_profile(name="P", match=("X-*",), countries=("US",), zone_name="Z"):
	return {
		"name": name,
		"match": list(match),
		"zones": [
			{
				"name": zone_name,
				"countries": list(countries),
				"method": "Express (4 days)",
				"days": 4,
				"tiers": [flat(1.0)],
			}
		],
	}


def many_countries(count):
	letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
	return [a + b for a in letters for b in letters][:count]


def _duplicate_country_in_a_profile(card):
	zone_of(card, "Dangerous Goods", "Europe")["countries"].append("US")


def _duplicate_country_in_a_zone(card):
	set_in(card, "Dangerous Goods", "USA", countries=["US", "US"])


def _overlapping_tiers(card):
	a_tier_of_the_usa_zone(card, 1, **{"from": 0.5})


def _touching_tiers(card):
	a_tier_of_the_usa_zone(card, 1, **{"from": 0.6})


def _tiers_out_of_order_that_overlap(card):
	zone_of(card, "Dangerous Goods", "USA")["tiers"].reverse()
	a_tier_of_the_usa_zone(card, 3, **{"from": 0.5})  # now the second tier by weight


def _from_above_to(card):
	a_tier_of_the_usa_zone(card, 4, **{"from": 21.0})


def _flat_zone_with_two_tiers(card):
	set_in(card, "Dangerous Goods", "Bahrain", tiers=[flat(0.0), tier(0.0, 2.0, 5.0)])


def _a_flat_tier_among_tiers(card):
	zone_of(card, "Dangerous Goods", "USA")["tiers"].append(flat(1.0))


def _one_sided_tier(card):
	a_tier_of_the_usa_zone(card, 0, to=None)


def _weights_beyond_the_limit(card):
	a_tier_of_the_usa_zone(card, 4, to=10001.0)


def _price_above_the_limit(card):
	first_zone_tier(card, price=1000000.01)


def _negative_price(card):
	first_zone_tier(card, price=-0.01)


def _non_finite_price(card):
	first_zone_tier(card, price=float("nan"))


def _unknown_country_code(card):
	zone_of(card, "Dangerous Goods", "USA")["countries"].append("USA")


def _zone_without_countries(card):
	set_in(card, "Dangerous Goods", "USA", countries=[])


def _zone_without_a_name(card):
	set_in(card, "Dangerous Goods", "USA", name="  ")


def _zone_without_a_method(card):
	set_in(card, "Dangerous Goods", "USA", method="")


def _zone_without_tiers(card):
	set_in(card, "Dangerous Goods", "USA", tiers=[])


def _profile_without_a_name(card):
	profile_of(card, "Refill Service")["name"] = ""


def _duplicate_profile_name(card):
	profile_of(card, "Refill Service")["name"] = " dangerous  GOODS"


def _catch_all_not_last(card):
	card["profiles"].insert(0, card["profiles"].pop())


def _two_catch_alls(card):
	profile_of(card, "Dangerous Goods")["match"] = ["*"]


def _no_catch_all(card):
	profile_of(card, "Rest of the World Allowed Items")["match"] = ["P-*"]


def _no_patterns(card):
	profile_of(card, "Refill Service")["match"] = []


def _pattern_with_braces(card):
	profile_of(card, "Dangerous Goods")["match"] = ["{F-*}"]


def _version(card):
	card["version"] = 2


def _currency(card):
	card["currency"] = "usd"


def _unit(card):
	card["unit"] = "kg"


def _too_many_profiles(card):
	card["profiles"] = [tidy_profile(f"P{n}", match=[f"X{n}-*"]) for n in range(13)]


def _too_many_zones(card):
	profile = tidy_profile()
	profile["zones"] = [
		{**profile["zones"][0], "name": f"Z{n}", "countries": [many_countries(41)[n]]} for n in range(41)
	]
	card["profiles"] = [profile]


def _too_many_tiers(card):
	zone_of(card, "Dangerous Goods", "USA")["tiers"] = [tier(float(n), n + 0.5, 1.0) for n in range(61)]


def _too_many_countries(card):
	set_in(card, "Dangerous Goods", "USA", countries=many_countries(261))


def _too_many_patterns(card):
	profile_of(card, "Dangerous Goods")["match"] = [f"F-{n}-*" for n in range(13)]


def _name_too_long(card):
	profile_of(card, "Dangerous Goods")["name"] = "N" * 141


def _zone_name_too_long(card):
	set_in(card, "Dangerous Goods", "USA", name="Z" * 141)


def _method_too_long(card):
	set_in(card, "Dangerous Goods", "USA", method="M" * 141)


@pytest.mark.parametrize(
	("change", "fragment", "count"),
	[
		pytest.param(
			_duplicate_country_in_a_profile, "already in zone 1", 1, id="duplicate country in a profile"
		),
		pytest.param(_duplicate_country_in_a_zone, "more than once", 1, id="duplicate country in a zone"),
		pytest.param(_overlapping_tiers, "overlaps", 1, id="overlapping tiers"),
		pytest.param(_touching_tiers, "overlaps", 1, id="a tier that starts where the last one ends"),
		pytest.param(_tiers_out_of_order_that_overlap, "overlaps", 1, id="overlap found whatever the order"),
		pytest.param(_from_above_to, "wrong way round", 1, id="from above to"),
		pytest.param(_flat_zone_with_two_tiers, "only one tier", 1, id="flat zone with two tiers"),
		pytest.param(_a_flat_tier_among_tiers, "only one tier", 1, id="a flat tier among weight tiers"),
		pytest.param(_one_sided_tier, "only one of the two weights", 1, id="a tier with one weight"),
		pytest.param(_weights_beyond_the_limit, "10000 lb", 1, id="weights beyond the limit"),
		pytest.param(_price_above_the_limit, "from 0 to 1000000", 1, id="price above the limit"),
		pytest.param(_negative_price, "price that has to be a number", 1, id="negative price"),
		pytest.param(_non_finite_price, "price that has to be a number", 1, id="non-finite price"),
		pytest.param(_unknown_country_code, "not a country code", 1, id="unknown country code"),
		pytest.param(_zone_without_countries, "has no countries", 1, id="zone with no countries"),
		pytest.param(_zone_without_a_name, "has no name", 1, id="zone without a name"),
		pytest.param(_zone_without_a_method, "no delivery method", 1, id="zone without a method"),
		pytest.param(_zone_without_tiers, "has no tiers", 1, id="zone without tiers"),
		pytest.param(_profile_without_a_name, "has no name", 1, id="empty profile name"),
		pytest.param(_duplicate_profile_name, "same name as profile", 1, id="duplicate profile name"),
		pytest.param(_catch_all_not_last, "not the last profile", 1, id="catch-all that is not last"),
		pytest.param(_two_catch_alls, "also matches every item", 2, id="two catch-all profiles"),
		pytest.param(_no_catch_all, "No profile matches every item", 1, id="no catch-all at all"),
		pytest.param(_no_patterns, "will never be used", 1, id="profile with no patterns"),
		pytest.param(_pattern_with_braces, "brace", 1, id="a pattern with braces"),
		pytest.param(_version, "version has to be 1", 1, id="version"),
		pytest.param(_currency, 'currency has to be "USD"', 1, id="currency"),
		pytest.param(_unit, 'unit has to be "lb"', 1, id="unit"),
		pytest.param(_too_many_profiles, "13 profiles; at most 12", None, id="too many profiles"),
		pytest.param(_too_many_zones, "41 zones; at most 40", None, id="too many zones"),
		pytest.param(_too_many_tiers, "61 tiers; at most 60", None, id="too many tiers"),
		pytest.param(_too_many_countries, "261 countries; at most 260", None, id="too many countries"),
		pytest.param(
			_too_many_patterns, "13 product code patterns; at most 12", None, id="too many patterns"
		),
		pytest.param(_name_too_long, "longer than 140", 1, id="profile name too long"),
		pytest.param(_zone_name_too_long, "longer than 140", 1, id="zone name too long"),
		pytest.param(_method_too_long, "longer than 140", 1, id="method too long"),
	],
)
def test_validate_card_reports_each_problem(change, fragment, count):
	card = sample_card()
	change(card)
	problems = validate_card(card)
	assert all(isinstance(p, str) and p[0].isupper() and p.endswith(".") for p in problems)
	assert problems_with(problems, fragment), problems
	if count is not None:  # an oversized list also leaves the card without a catch-all, so it is not counted
		assert len(problems) == count, problems


def test_validate_card_pattern_length_limit_is_80_characters():
	card = sample_card()
	profile_of(card, "Dangerous Goods")["match"] = ["F-" + "A" * 78]  # 80 characters
	assert validate_card(card) == []
	profile_of(card, "Dangerous Goods")["match"] = ["F-" + "A" * 79]  # 81 characters
	assert len(problems_with(validate_card(card), "longer than 80 characters")) == 1


@pytest.mark.parametrize("pattern", ["{F-*}", "F-{CR}-*", "F-*}", "{", "}", "{}"])
def test_validate_card_reports_a_brace_in_a_pattern_and_does_not_change_it(pattern):
	"""A pattern is matched against the product code without its braces, so one typed with braces could never
	match. It is reported in plain words, once, and left as typed."""
	card = sample_card()
	profile_of(card, "Dangerous Goods")["match"] = ["F-*", pattern]
	assert validate_card(card) == [
		'Profile 3 ("Dangerous Goods"): pattern 2 contains a brace. '
		"Write the product code without braces, such as F-CR-XYZ-*."
	]
	assert profile_of(card, "Dangerous Goods")["match"] == ["F-*", pattern]


def test_validate_card_allows_the_other_shell_wildcards_and_they_work():
	"""The patterns are read like a shell's: * is any run of characters, ? any one character and [CD] one of the
	characters listed, so the card accepts them (the module docstring says so)."""
	card = sample_card()
	profile_of(card, "Dangerous Goods")["match"] = ["F-?R-*", "F-[CD]X-*"]
	assert validate_card(card) == []
	assert [classify_item(card, key) for key in ("{F-CR-1}", "{F-ZR-1}", "{F-CRR-1}")] == [2, 2, 3]
	assert [classify_item(card, key) for key in ("{F-CX-1}", "{F-DX-1}", "{F-EX-1}")] == [2, 2, 3]


def test_validate_card_limits_are_inclusive():
	card = make_card(
		[tidy_profile(f"P{n}", match=[f"X{n}-*"]) for n in range(11)] + [tidy_profile("All", match=["*"])]
	)
	assert validate_card(card) == [], "12 profiles"
	profile = card["profiles"][0]
	profile["match"] = [f"A{n}-*" for n in range(12)]
	profile["zones"][0]["countries"] = many_countries(260)
	profile["zones"][0]["name"] = "N" * 140
	profile["zones"][0]["method"] = "M" * 140
	profile["zones"][0]["tiers"] = [tier(float(n), n + 0.5, 1.0) for n in range(60)]
	profile["name"] = "P" * 140
	assert validate_card(card) == [], "12 patterns, 260 countries, 60 tiers, names of 140 characters"
	profile["zones"] += [
		{**profile["zones"][0], "name": f"Z{n}", "countries": [many_countries(300)[n + 260]]}
		for n in range(39)
	]
	assert validate_card(card) == [], "40 zones"


@pytest.mark.parametrize(
	"value", [-0.01, float("nan"), float("inf"), float("-inf"), "35", True, None, [1], 10**400]
)
def test_validate_card_refuses_a_price_that_is_not_a_number_from_zero_to_a_million(value):
	card = sample_card()
	first_zone_tier(card, price=value)
	problems = validate_card(card)
	assert len(problems) == 1 and "price that has to be a number from 0 to 1000000" in problems[0], problems
	assert problems[0].startswith('Tier 1 of zone 1 ("USA") of profile 3 ("Dangerous Goods")')


def test_validate_card_accepts_a_price_of_zero_and_of_a_million():
	card = sample_card()
	first_zone_tier(card, price=0.0)
	assert validate_card(card) == []
	first_zone_tier(card, price=1000000)
	assert validate_card(card) == []


def test_validate_card_accepts_weights_of_exactly_zero_and_ten_thousand_pounds():
	"""The limit is inclusive on both sides: a tier can run from 0 to 10,000 lb, and a first float beyond it is
	refused."""
	card = sample_card()
	for edges in ((0.0, 10000.0), (10000.0, 10000.0), (0.0, 0.0)):
		set_in(card, "Dangerous Goods", "USA", tiers=[tier(*edges, 5.0)])
		assert validate_card(card) == [], edges
	beyond = math.nextafter(10000.0, math.inf)
	for edges in ((0.0, beyond), (beyond, beyond + 1)):
		set_in(card, "Dangerous Goods", "USA", tiers=[tier(*edges, 5.0)])
		problems = validate_card(card)
		assert len(problems) == 1 and "weights that have to be numbers from 0 to 10000 lb" in problems[0], (
			edges
		)


@pytest.mark.parametrize("value", [-1.0, float("nan"), float("inf"), "2", True, [2], 10**400, 10000.5])
def test_validate_card_refuses_weights_that_are_not_numbers_from_zero_to_ten_thousand(value):
	for edge in ("from", "to"):
		card = sample_card()
		a_tier_of_the_usa_zone(card, 4, **{edge: value})
		problems = validate_card(card)
		assert problems_with(problems, "weights that have to be numbers from 0 to 10000 lb"), (edge, problems)


@pytest.mark.parametrize(
	"code", ["USA", "us", "U1", "U", "", " US", "\u00c9S", "I\u0307", 5, None, ["US"], 1.5, True]
)
def test_validate_card_refuses_a_country_that_is_not_two_capital_letters(code):
	card = sample_card()
	zone_of(card, "Dangerous Goods", "USA")["countries"].append(code)
	problems = validate_card(card)
	assert len(problems) == 1 and "not a country code" in problems[0], problems


@pytest.mark.parametrize("name", ["", "   ", None, 5, ["x"], "bad\x00name", "tab\there", "lone\ud800"])
def test_validate_card_refuses_names_that_are_not_plain_text(name):
	for place in ("profile", "zone", "method"):
		card = sample_card()
		target = {
			"profile": profile_of(card, "Dangerous Goods"),
			"zone": zone_of(card, "Dangerous Goods", "USA"),
			"method": zone_of(card, "Dangerous Goods", "USA"),
		}[place]
		target["method" if place == "method" else "name"] = name
		assert validate_card(card), (place, name)


@pytest.mark.parametrize("days", [61, -1, True, 4.5, "4", [4], float("nan"), 10**400])
def test_validate_card_refuses_days_that_are_not_a_whole_number_from_0_to_60(days):
	card = sample_card()
	set_in(card, "Dangerous Goods", "USA", days=days)
	problems = validate_card(card)
	assert len(problems) == 1 and "days" in problems[0] and "0 to 60" in problems[0], problems


@pytest.mark.parametrize("days", [None, 0, 1, 60])
def test_validate_card_accepts_days_that_are_empty_or_from_0_to_60(days):
	card = sample_card()
	set_in(card, "Dangerous Goods", "USA", days=days)
	assert validate_card(card) == []


@pytest.mark.parametrize("version", [1.0, True, "1", None])
def test_validate_card_wants_the_version_to_be_the_whole_number_1(version):
	card = sample_card()
	card["version"] = version
	assert len(problems_with(validate_card(card), "version has to be 1")) == 1


def test_validate_card_says_there_is_no_card_for_something_that_is_not_an_object():
	for junk in (None, 5, "card", [], [sample_card()], True):
		assert validate_card(junk) == ["There is no rate card."]


@pytest.mark.parametrize("profiles", [None, [], "profiles", {"a": 1}, 5])
def test_validate_card_wants_at_least_one_profile(profiles):
	card = sample_card()
	card["profiles"] = profiles
	assert validate_card(card) == ["The rate card has no profiles."]
	del card["profiles"]
	assert validate_card(card) == ["The rate card has no profiles."]


def test_validate_card_names_the_profile_zone_and_tier_with_1_based_positions():
	card = sample_card()
	a_tier_of_the_usa_zone(card, 2, price=-1)
	(problem,) = validate_card(card)
	assert problem.startswith('Tier 3 of zone 1 ("USA") of profile 3 ("Dangerous Goods")')
	card = sample_card()
	zone_of(card, "Refill Service", "USA")["countries"] = []
	(problem,) = validate_card(card)
	assert problem.startswith('Zone 1 ("USA") of profile 2 ("Refill Service")')
	card = sample_card()
	refill = profile_of(card, "Refill Service")
	refill["name"], refill["match"] = "", []
	problems = validate_card(card)
	assert len(problems) == 2
	assert problems[0] == "Profile 2 has no name." and "Profile 2 has no product code patterns" in problems[1]


def test_validate_card_gives_unnamed_things_a_position_only_and_keeps_going():
	card = sample_card()
	refill = profile_of(card, "Refill Service")
	refill["name"] = None
	refill["zones"][0]["name"] = 7
	refill["zones"][0]["tiers"][1]["price"] = "x"
	problems = validate_card(card)
	assert len(problems) == 3, problems
	assert any(p.startswith("Tier 2 of zone 1 of profile 2 ") for p in problems)


def test_validate_card_reports_everything_it_finds_in_one_pass():
	card = sample_card()
	_duplicate_country_in_a_profile(card)
	_overlapping_tiers(card)
	_zone_without_a_method(card)
	_no_patterns(card)
	assert len(validate_card(card)) == 4


def put(card, path, value):
	"""Set the part of the card at `path` (keys and list positions) to `value`."""
	node = card
	for step in path[:-1]:
		node = node[step]
	node[path[-1]] = value


@pytest.mark.parametrize(
	("path", "value"),
	[
		pytest.param(("profiles", 0), [], id="profile is a list"),
		pytest.param(("profiles", 0), "profile", id="profile is text"),
		pytest.param(("profiles", 2, "zones", 0), "zone", id="zone is text"),
		pytest.param(("profiles", 2, "zones", 0), None, id="zone is None"),
		pytest.param(("profiles", 2, "zones", 0, "tiers", 0), "tier", id="tier is text"),
		pytest.param(("profiles", 2, "zones", 0, "tiers", 0), [1, 2], id="tier is a list"),
		pytest.param(("profiles", 2, "zones"), "zones", id="zones is text"),
		pytest.param(("profiles", 2, "match"), "F-*", id="match is text"),
		pytest.param(
			("profiles", 2, "match"), [5, None, "", "  ", ["F-*"], "bad\x00"], id="patterns that are not text"
		),
		pytest.param(("profiles", 2, "zones", 0, "countries"), "US", id="countries is text"),
		pytest.param(("profiles", 2, "zones", 0, "tiers"), {"from": 1}, id="tiers is a dict"),
	],
)
def test_validate_card_calls_a_wrongly_shaped_part_a_problem_and_never_raises(path, value):
	card = sample_card()
	put(card, path, value)
	problems = validate_card(card)
	assert problems and all(isinstance(p, str) and p.endswith(".") for p in problems)


def test_validate_card_leaves_the_card_alone():
	card = sample_card()
	_overlapping_tiers(card)
	_duplicate_country_in_a_profile(card)
	before = copy.deepcopy(card)
	validate_card(card)
	card_summary(card)
	assert card == before


def test_validate_card_asks_a_profile_to_have_patterns_and_there_to_be_one_catch_all_at_the_end():
	card = make_card([tidy_profile("All", match=["*"]), tidy_profile("Mine", match=["X-*"])])
	assert len(problems_with(validate_card(card), "not the last profile")) == 1
	card = make_card([tidy_profile("Mine", match=["X-*"]), tidy_profile("All", match=["**"])])
	assert validate_card(card) == [], "a run of asterisks is a catch-all too"
	card = make_card([tidy_profile("Mine", match=["X-*", " * "])])
	assert validate_card(card) == [], "spaces round a pattern do not matter"
	card = make_card([tidy_profile("Mine", match=["*-X"])])
	assert len(problems_with(validate_card(card), "No profile matches every item")) == 1


def test_card_summary_counts_what_the_card_holds():
	assert card_summary(sample_card()) == {"profiles": 4, "zones": 10, "tiers": 20, "countries": 18}
	assert card_summary(make_card([])) == {"profiles": 0, "zones": 0, "tiers": 0, "countries": 0}
	card = make_card([tidy_profile("A", countries=["US", "GB"]), tidy_profile("B", countries=["US", "AE"])])
	assert card_summary(card) == {"profiles": 2, "zones": 2, "tiers": 2, "countries": 3}, (
		"countries are counted once"
	)
	for junk in (
		None,
		5,
		"x",
		[],
		{},
		{"profiles": 5},
		{
			"profiles": [
				None,
				5,
				{"zones": "x"},
				{"zones": [None, {"tiers": 5, "countries": [5, "usa", "us "]}]},
			]
		},
	):
		summary = card_summary(junk)
		assert set(summary) == SUMMARY_KEYS and all(type(n) is int and n >= 0 for n in summary.values())


# ---------------------------------------------------------------- putting an item into a profile
@pytest.mark.parametrize(
	("key", "position"),
	[
		("{F-CR-OSA-M-PT}", 0),
		("{F-CR-PC-W}", 0),
		("{F-CR-OSA-M-100-RF}", 1),
		("{F-CR-CSE-W-40}", 2),
		("{P-AA-AT1-IG}", 3),
		("", 3),
		# braces, case and spaces do not matter
		("F-CR-OSA-M-PT", 0),
		("f-cr-osa-m-pt", 0),
		("  { f-cr-osa-m-pt }  ", 0),
		("{F-CR-OSA-M-PT}\n", 0),
		("{f-cr-osa-m-100-rf}", 1),
		("{ p-aa-at1-ig }", 3),
		("{}", 3),
		# the first profile that matches wins: a petite code also matches F-*, and a petite refill is both
		("{F-CR-PC-W-100-RF}", 0),
		("{F-CR-PC-}", 0),
		("{F-}", 2),
		("{F}", 3),
		# not text is no key
		(None, 3),
		(5, 3),
		(["F-CR-PT"], 3),
		({"F-CR-PT": 1}, 3),
		(b"F-CR-PT", 3),
	],
)
def test_classify_item_by_product_code(key, position):
	assert classify_item(sample_card(), key) == position


def test_classify_item_returns_the_position_in_the_cards_profile_list_and_the_order_is_the_precedence():
	card = sample_card()
	card["profiles"].insert(0, card["profiles"].pop(2))  # Dangerous Goods (F-*) now comes before Petite
	assert [p["name"] for p in card["profiles"]][:3] == [
		"Dangerous Goods",
		"Dangerous Goods Petite",
		"Refill Service",
	]
	assert classify_item(card, "{F-CR-OSA-M-PT}") == 0, "F-* takes it first"
	assert classify_item(card, "{F-CR-OSA-M-100-RF}") == 0
	assert classify_item(card, "{P-AA-AT1-IG}") == 3
	card["profiles"].reverse()  # the catch-all first: it takes everything
	assert {classify_item(card, key) for key in (PETITE, REFILL, DANGEROUS, ALLOWED, "")} == {0}


def test_classify_item_gives_none_when_no_profile_has_a_matching_pattern():
	card = sample_card()
	profile_of(card, "Rest of the World Allowed Items")["match"] = ["P-*"]
	assert classify_item(card, "{P-AA-AT1-IG}") == 3
	assert classify_item(card, "{W-AA-1}") is None
	assert classify_item(card, "") is None
	for junk in (
		None,
		5,
		"x",
		[],
		{},
		{"profiles": None},
		{"profiles": [None, 5, "x", [], {}, {"match": "F-*"}, {"match": [5, None, ["F-*"]]}]},
	):
		assert classify_item(junk, "{F-CR-PT}") is None


def test_classify_item_ignores_blank_patterns_so_a_keyless_item_goes_to_the_catch_all():
	card = sample_card()
	profile_of(card, "Dangerous Goods")["match"] = ["", "  ", "F-*"]
	assert classify_item(card, "") == 3
	assert classify_item(card, "{F-CR-CSE-W-40}") == 2


def test_classify_item_upper_cases_and_strips_the_patterns_too():
	card = make_card([tidy_profile("Mine", match=["  f-*-pt  "]), tidy_profile("All", match=["*"])])
	assert classify_item(card, "{F-CR-OSA-M-PT}") == 0
	assert classify_item(card, "{F-CR-OSA-M-RF}") == 1


def test_classify_item_leaves_the_card_alone():
	card = sample_card()
	before = copy.deepcopy(card)
	classify_item(card, "{F-CR-OSA-M-PT}")
	assert card == before


@pytest.mark.parametrize(
	("raw", "expected"),
	[
		("{F-CR-OSA-M-PT}", "F-CR-OSA-M-PT"),
		("f-cr-osa-m-pt", "F-CR-OSA-M-PT"),
		("  {f-cr-osa-m-pt}\t", "F-CR-OSA-M-PT"),
		("{ F-CR }", "F-CR"),
		("{{F-CR}}", "F-CR"),
		("F-{CR}-PT", "F-CR-PT"),
		("", ""),
		("{}", ""),
		(None, ""),
		(5, ""),
		(["F"], ""),
		(b"F", ""),
		(object(), ""),
	],
)
def test_normalize_index_key(raw, expected):
	assert normalize_index_key(raw) == expected


# ---------------------------------------------------------------- pricing an order
@pytest.mark.parametrize(
	("weight", "total", "tier_number"),
	[
		(0.5, 35.0, 1),
		(0.6, 35.0, 1),
		(0.65, 60.0, 2),  # in the gap between 0.6 and 0.7: the next tier up
		(0.7, 60.0, 2),
		(1.0, 60.0, 2),
		(2.0, 60.0, 2),  # exactly the top of a tier
		(2.004, 80.0, 3),  # in the gap between 2 and 2.01
		(2.01, 80.0, 3),
		(4.0, 80.0, 3),
		(5.0, 100.0, 4),
		(19.0, 400.0, 5),
		(20.0, 400.0, 5),
	],
)
def test_price_card_usa_dangerous_goods_tier_by_tier(weight, total, tier_number):
	result = priced(price(sample_card(), "US", heavy(DANGEROUS, weight)), total)
	assert (result["name"], result["days"]) == (EXPRESS, 7)
	assert result["lines"] == [
		{
			"profile": "Dangerous Goods",
			"zone": "USA",
			"weight_lb": weight,
			"price": total,
			"tier": tier_number,
			"items": 1,
			"defaulted_weight": False,
		}
	]


def test_price_card_examples_from_the_export():
	"""The examples the plan lists, worked from the sample's real tiers and prices."""
	card = sample_card()
	# USA dangerous goods: 0.5 lb is 35, 0.65 lb (between two tiers) is 60, exactly 2 lb is 60, 2.004 lb is 80
	for lb, total in ((0.5, 35.0), (0.65, 60.0), (2.0, 60.0), (2.004, 80.0)):
		result = priced(price(card, "US", heavy(DANGEROUS, lb)), total)
		assert (result["name"], result["days"]) == (EXPRESS, 7)
	# 1 kg is 2.205 lb, which is in the 2.01-4 lb tier: 80, not 60 (the 60 tier carries up to 2 lb, or 0.907 kg)
	for kilos, lb, total in (
		(1.0, 2.205, 80.0),
		(0.9, 1.984, 60.0),
		(0.9072, 2.0, 60.0),
		(0.9075, 2.001, 80.0),
	):
		line = priced(price(card, "US", item(DANGEROUS, kilos)), total)["lines"][0]
		assert line["weight_lb"] == lb, kilos
	# Bahrain: dangerous goods are free, and still priced
	bahrain = priced(price(card, "BH", heavy(DANGEROUS, 3.0)), 0.0)
	assert (bahrain["name"], bahrain["days"]) == (BAHRAIN_FREE, 2)
	assert bahrain["lines"][0]["zone"] == "Bahrain (Local)" and bahrain["lines"][0]["price"] == 0.0
	# the four-country GCC zone: 35 at any weight, even one that no tier would carry
	for weight_kg in (0.001, DEFAULT_ITEM_WEIGHT_KG, 1.0, 50.0, 1e6):
		for country in ("KW", "SA", "AE", "OM"):
			result = priced(price(card, country, item(DANGEROUS, weight_kg)), 35.0)
			assert (result["name"], result["days"], result["lines"][0]["tier"]) == (EXPRESS, 7, 1)
	# petite to the GCC: flat 20; allowed items to the GCC: free
	petite = priced(price(card, "AE", item(PETITE, 0.2)), 20.0)
	assert (petite["name"], petite["days"]) == ("International Express (1-3 days)", 3)
	allowed = priced(price(card, "AE", item(ALLOWED, 2.0)), 0.0)
	assert (allowed["name"], allowed["days"]) == (GCC_FREE, 3)
	assert priced(price(card, "QA", item(ALLOWED, 80.0)), 0.0)["lines"][0]["zone"].startswith("GCC")


def test_price_card_other_zones_and_profiles_of_the_sample():
	card = sample_card()
	assert (
		priced(price(card, "GB", heavy(DANGEROUS, 1.0)), 75.0)["lines"][0]["zone"]
		== "Europe, UK & Select Asia"
	)
	assert (
		priced(price(card, "CI", heavy(DANGEROUS, 3.0)), 110.0)["lines"][0]["zone"]
		== "Asia, Africa & Americas (Tier 2)"
	)
	assert priced(price(card, "US", heavy(PETITE, 3.0)), 80.0)["name"] == SHIPPING
	assert priced(price(card, "US", heavy(PETITE, 4.05)), 100.0)["lines"][0]["tier"] == 2, "4 to 4.1 is a gap"
	assert priced(price(card, "US", heavy(REFILL, 1.0)), 60.0)["days"] == 15
	assert (
		priced(price(card, "AX", heavy(ALLOWED, 3.0)), 80.0)["lines"][0]["zone"]
		== "Worldwide (Allowed Items)"
	)
	assert priced(price(card, "US", heavy(ALLOWED, 1.0)), 40.0)["days"] == 7


def test_price_card_reads_the_country_loosely():
	card = sample_card()
	for form in ("US", "us", " us ", "\tUs\n"):
		assert priced(price(card, form, heavy(DANGEROUS, 1.0)), 60.0)
	# the zone's own codes are read loosely too
	zone_of(card, "Dangerous Goods", "USA")["countries"] = [" us "]
	assert priced(price(card, "US", heavy(DANGEROUS, 1.0)), 60.0)


def test_price_card_adds_the_profiles_in_a_cart():
	card = sample_card()
	cart = [heavy(DANGEROUS, 1.5), heavy(ALLOWED, 1.0)]
	result = priced(price(card, "US", *cart), 100.0)  # 60 + 40
	assert [(line["profile"], line["weight_lb"], line["price"]) for line in result["lines"]] == [
		("Dangerous Goods", 1.5, 60.0),
		("Rest of the World Allowed Items", 1.0, 40.0),
	]
	assert (result["name"], result["days"]) == (EXPRESS, 7), "both take 4-7 days, so the text is the same"
	assert price(card, "US", *reversed(cart)) == result, "the order of the cart does not matter"
	# lines follow the card's profile order: Refill comes before Dangerous Goods
	slower = priced(price(card, "US", heavy(DANGEROUS, 1.5), heavy(REFILL, 1.0)), 120.0)
	assert [line["profile"] for line in slower["lines"]] == ["Refill Service", "Dangerous Goods"]
	assert (slower["name"], slower["days"]) == (SHIPPING, 15), "named after the slower profile"
	# every profile in one cart: petite 80 (3 lb) + refill 60 + dangerous goods 60 + allowed 40
	everything = [heavy(PETITE, 3.0), heavy(REFILL, 1.0), heavy(DANGEROUS, 1.5), heavy(ALLOWED, 1.0)]
	assert len(priced(price(card, "US", *everything), 240.0)["lines"]) == 4


def test_price_card_names_a_mixed_cart_after_the_profile_that_takes_the_most_days():
	card = sample_card()
	# Bahrain: dangerous goods are free in 1-2 days, allowed items free in 1-3 days
	result = priced(price(card, "BH", heavy(DANGEROUS, 1.0), heavy(ALLOWED, 1.0)), 0.0)
	assert (result["name"], result["days"]) == (GCC_FREE, 3)
	# the same days under different texts: the first line in profile order names the offer
	card = make_card(
		[
			tidy_profile("First", match=["A-*"]),
			tidy_profile("Second", match=["B-*"]),
			tidy_profile("All", match=["*"]),
		]
	)
	card["profiles"][0]["zones"][0]["method"] = "Courier (3 days)"
	card["profiles"][1]["zones"][0]["method"] = "Post (3 days)"
	card["profiles"][1]["zones"][0]["days"] = 4
	card["profiles"][0]["zones"][0]["days"] = 4
	result = priced(price(card, "US", item("B-1", 1.0), item("A-1", 1.0)), 2.0)
	assert (result["name"], result["days"]) == ("Courier (3 days)", 4)
	# no days anywhere: the first line names it, and days stay empty
	for profile in card["profiles"]:
		profile["zones"][0]["days"] = None
	result = priced(price(card, "US", item("B-1", 1.0), item("A-1", 1.0)), 2.0)
	assert (result["name"], result["days"]) == ("Courier (3 days)", None)
	# one profile has days and the other does not: the one with days is the slower
	card["profiles"][1]["zones"][0]["days"] = 2
	assert price(card, "US", item("B-1", 1.0), item("A-1", 1.0))["name"] == "Post (3 days)"


def test_price_card_sums_prices_exactly_in_cents():
	card = make_card([tidy_profile("A", match=["A-*"]), tidy_profile("All", match=["*"])])
	card["profiles"][0]["zones"][0]["tiers"] = [flat(0.1)]
	card["profiles"][1]["zones"][0]["tiers"] = [flat(0.2)]
	assert (
		priced(price(card, "US", item("A-1", 1.0), item("B-1", 1.0)), 0.3)["total"] == 0.3
	)  # not 0.30000000000000004
	# half a cent goes up, read from the decimal a person wrote: the binary numbers nearest to 1.005 and 2.675 are
	# just below the half, so a price read from the binary value (Decimal(x) rather than Decimal(str(x))) would
	# round them down (10.005 would not tell the two readings apart: its binary value is above the half)
	card["profiles"][0]["zones"][0]["tiers"] = [flat(1.005)]
	card["profiles"][1]["zones"][0]["tiers"] = [flat(0.004)]
	result = priced(price(card, "US", item("A-1", 1.0), item("B-1", 1.0)), 1.01)
	assert [line["price"] for line in result["lines"]] == [1.01, 0.0], (
		"each line to cents, half up, then added"
	)
	card["profiles"][0]["zones"][0]["tiers"] = [flat(2.675)]
	card["profiles"][1]["zones"][0]["tiers"] = [flat(1.005)]
	result = priced(price(card, "US", item("A-1", 1.0), item("B-1", 1.0)), 3.69)
	assert [line["price"] for line in result["lines"]] == [2.68, 1.01]
	# a price below zero is no price at all: the zone prices nothing and the order is not priced, never free
	card["profiles"][0]["zones"][0]["tiers"] = [flat(-5.0)]
	card["profiles"][1]["zones"][0]["tiers"] = [flat(3.0)]
	result = refused(price(card, "US", item("A-1", 1.0), item("B-1", 1.0)), "not_covered")
	assert "no usable prices" in result["detail"] and result["lines"][0]["price"] is None
	card["profiles"][1]["zones"][0]["tiers"] = [flat(-3.0)]
	refused(price(card, "US", item("A-1", 1.0), item("B-1", 1.0)), "not_covered")
	# a price of exactly nothing is a free delivery, as Bahrain's is
	card["profiles"][0]["zones"][0]["tiers"] = [flat(0.0)]
	card["profiles"][1]["zones"][0]["tiers"] = [flat(0.0)]
	assert priced(price(card, "US", item("A-1", 1.0), item("B-1", 1.0)), 0.0)["total"] == 0.0


def test_price_card_refuses_what_it_cannot_price():
	card = sample_card()
	# heavier than the top tier
	heavy_order = refused(price(card, "GB", heavy(DANGEROUS, 8.0)), "too_heavy")
	assert heavy_order["lines"] == [
		{
			"profile": "Dangerous Goods",
			"zone": "Europe, UK & Select Asia",
			"weight_lb": 8.0,
			"price": None,
			"tier": None,
			"items": 1,
			"defaulted_weight": False,
		}
	]
	assert "8 lb" in heavy_order["detail"] and "4 lb" in heavy_order["detail"]
	# a profile with no zone for the country
	not_served = refused(price(card, "AE", item(REFILL, 0.5)), "not_covered")
	assert not_served["lines"] == [
		{
			"profile": "Refill Service",
			"zone": "",
			"weight_lb": 1.102,
			"price": None,
			"tier": None,
			"items": 1,
			"defaulted_weight": False,
		}
	]
	assert "Refill Service" in not_served["detail"] and "AE" in not_served["detail"]
	# a country in no zone at all
	for country in ("AU", "KP", "RU", "ZZ"):
		assert refused(price(card, country, item(DANGEROUS, 0.5)), "not_covered")
	# an empty cart
	for items in ([], None, "items", {"index_key": "x"}, [None, 5, "x", []]):
		assert refused(price_card(card, {"country_code": "US", "items": items}), "not_offered")
	assert refused(price_card(card, {"country_code": "US"}), "not_offered")
	assert refused(price_card(card, {}), "not_offered")
	# one profile that cannot be priced means the whole order is not priced
	cart = [
		heavy(PETITE, 3.0),
		heavy(DANGEROUS, 25.0),
	]  # petite is fine (80), dangerous goods are over the top tier
	result = refused(price(card, "US", *cart), "too_heavy")
	assert (result["total"], result["name"], result["days"]) == (0.0, "", None)
	assert [(line["profile"], line["price"]) for line in result["lines"]] == [
		("Dangerous Goods Petite", 80.0),
		("Dangerous Goods", None),
	], "the lines computed so far, then the one that failed"
	assert result["lines"][0]["tier"] == 1
	# the first profile in the card's order is the one that gives the reason
	both = [heavy(REFILL, 1.0), heavy(DANGEROUS, 8.0)]
	assert refused(price(card, "GB", *both), "not_covered")["lines"][0]["profile"] == "Refill Service"
	assert refused(price(card, "GB", heavy(DANGEROUS, 8.0), heavy(ALLOWED, 1.0)), "too_heavy")
	# nothing after the failure is computed
	last = refused(price(card, "AE", heavy(REFILL, 1.0), heavy(ALLOWED, 1.0)), "not_covered")
	assert [line["profile"] for line in last["lines"]] == ["Refill Service"]


@pytest.mark.parametrize(
	"card",
	[
		None,
		5,
		"card",
		[],
		{},
		{"profiles": []},
		{"profiles": None},
		{"profiles": "x"},
		{"profiles": [None, 5]},
	],
)
def test_price_card_without_a_card_is_not_covered(card):
	result = refused(price_card(card, order("US", item(DANGEROUS, 1.0))), "not_covered")
	assert result["detail"] == "There is no rate card."
	assert result["lines"] == []
	assert price_card(card, None)["detail"] == "There is no rate card.", "the card is judged first"


@pytest.mark.parametrize(
	"country", [None, "", " ", "U", "USA", "U1", "\u00dcS", 5, ["US"], {"US": 1}, True, b"US"]
)
def test_price_card_needs_a_two_letter_country(country):
	result = refused(price(sample_card(), country, item(DANGEROUS, 1.0)), "not_covered")
	assert "country" in result["detail"]
	assert refused(price_card(sample_card(), {"items": [item(DANGEROUS, 1.0)]}), "not_covered")


def test_price_card_leaves_out_an_item_no_profile_takes_and_says_so():
	card = sample_card()
	profile_of(card, "Rest of the World Allowed Items")["match"] = ["P-*"]
	result = priced(price(card, "US", heavy(DANGEROUS, 1.5), heavy("{W-AA-1}", 2.0)), 60.0)
	assert len(result["lines"]) == 1
	assert "1 item matches no profile" in result["detail"]
	assert (
		"2 items match no profile"
		in price(card, "US", heavy(DANGEROUS, 1.5), item("W-1", 1.0), item("W-2", 1.0))["detail"]
	)
	# when nothing is taken, there is nothing to price: it is not free
	nothing = refused(price(card, "US", item("{W-AA-1}", 2.0)), "not_offered")
	assert nothing["lines"] == [] and "match" in nothing["detail"]


def test_price_card_detail_tells_the_staff_how_each_price_was_found():
	card = sample_card()
	detail = priced(price(card, "US", heavy(DANGEROUS, 1.5), heavy(ALLOWED, 1.0)), 100.0)["detail"]
	assert 'Dangerous Goods to US: 1.5 lb is in tier 2 (0.7 to 2 lb) of zone "USA", $60.00.' in detail
	assert "Rest of the World Allowed Items to US: 1 lb is in tier 1 (0.01 to 2 lb)" in detail
	assert detail.endswith("The delivery price is $100.00.")
	assert (
		'zone "Bahrain (Local)" has one flat price, $0.00.'
		in price(card, "BH", item(DANGEROUS, 1.0))["detail"]
	)
	assert (
		'in the gap before tier 2 (0.7 to 2 lb) of zone "USA", so it pays tier 2'
		in price(card, "US", heavy(DANGEROUS, 0.65))["detail"]
	)
	assert (
		'below the first tier (0.1 to 0.6 lb) of zone "USA", so it pays the first tier'
		in price(card, "US", heavy(DANGEROUS, 0.05))["detail"]
	)
	assert "estimate" in price(card, "US", item(DANGEROUS))["detail"]


# ---------------------------------------------------------------- weights
def test_price_card_weights():
	card = sample_card()
	three = price(card, "US", item(DANGEROUS, 0.5, qty=3))["lines"][0]
	assert (three["weight_lb"], three["items"]) == (3.307, 1)  # 1.5 kg
	two = price(card, "US", item(DANGEROUS, 0.4), item(DANGEROUS, 0.6, qty=2))["lines"][0]
	assert (two["weight_lb"], two["items"]) == (3.527, 2)  # 1.6 kg
	# another profile is a line of its own
	lines = price(card, "US", item(DANGEROUS, 0.4), item(ALLOWED, 0.6, qty=2), item(DANGEROUS, 0.1))["lines"]
	assert [(line["profile"], line["weight_lb"], line["items"]) for line in lines] == [
		("Dangerous Goods", 1.102, 2),
		("Rest of the World Allowed Items", 2.646, 1),
	]
	# a quantity can be a fraction, and an int or a float or a Decimal are all numbers
	assert price(card, "US", item(DANGEROUS, 1.0, qty=0.5))["lines"][0]["weight_lb"] == 1.102
	assert price(card, "US", item(DANGEROUS, 1, qty=2))["lines"][0]["weight_lb"] == 4.409
	assert (
		price(card, "US", item(DANGEROUS, Decimal("0.5"), qty=Decimal("3")))["lines"][0]["weight_lb"] == 3.307
	)
	# an item with no usable weight counts as 0.35 kg a unit, and only its own line says so
	lines = price(card, "US", item(DANGEROUS, 1.0), item(DANGEROUS), item(ALLOWED, 1.0))["lines"]
	assert [(line["profile"], line["weight_lb"], line["defaulted_weight"]) for line in lines] == [
		("Dangerous Goods", 2.976, True),  # 1 kg + 0.35 kg
		("Rest of the World Allowed Items", 2.205, False),
	]
	assert price(card, "US", item(DANGEROUS, 1.0, qty=3))["lines"][0]["defaulted_weight"] is False
	for unusable in (MISSING, None, 0, -1, float("nan"), True):
		line = price(card, "US", item(DANGEROUS, unusable, qty=2))["lines"][0]
		assert (line["weight_lb"], line["defaulted_weight"]) == (1.543, True)  # 2 x 0.35 kg
	# a quantity of 0 is ignored: it adds no weight and no default
	only = price(card, "US", heavy(DANGEROUS, 1.5), item(DANGEROUS, qty=0))["lines"][0]
	assert (only["items"], only["weight_lb"], only["defaulted_weight"]) == (1, 1.5, False)
	assert refused(price(card, "US", item(DANGEROUS, 1.0, qty=0)), "not_offered")


@pytest.mark.parametrize(
	"weight",
	[
		MISSING,
		None,
		0,
		0.0,
		-0.0,
		-1,
		-0.5,
		float("nan"),
		float("inf"),
		float("-inf"),
		True,
		False,
		"1.5",
		"",
		[1.0],
		{"kg": 1},
		10**400,
		b"1",
	],
)
def test_price_card_counts_an_unusable_weight_as_035_kg_a_unit_and_says_so(weight):
	line = priced(price(sample_card(), "US", item(DANGEROUS, weight, qty=2)), 60.0)["lines"][0]
	assert line["weight_lb"] == 1.543  # 2 x 0.35 kg
	assert line["defaulted_weight"] is True


@pytest.mark.parametrize(
	"qty", [0, 0.0, -1, -0.5, float("nan"), float("inf"), True, False, None, "2", "", [2], 10**400, MISSING]
)
def test_price_card_ignores_an_item_whose_quantity_is_not_a_number_above_zero(qty):
	card = sample_card()
	broken = item(DANGEROUS, 1.0)
	if qty is MISSING:
		del broken["qty"]
	else:
		broken["qty"] = qty
	assert refused(price(card, "US", broken), "not_offered")["lines"] == []
	# beside a good item it adds nothing: neither weight nor a default
	result = price(card, "US", heavy(DANGEROUS, 1.5), broken, item(DANGEROUS))
	assert result["lines"][0]["items"] == 2 and result["lines"][0]["defaulted_weight"] is True
	only = price(card, "US", heavy(DANGEROUS, 1.5), broken)
	assert (
		only["lines"][0]["items"],
		only["lines"][0]["weight_lb"],
		only["lines"][0]["defaulted_weight"],
	) == (1, 1.5, False)


def test_price_card_ignores_things_in_the_cart_that_are_not_items():
	card = sample_card()
	cart = [None, 5, "item", [], {}, {"index_key": DANGEROUS}, heavy(DANGEROUS, 1.5)]
	result = priced(price_card(card, {"country_code": "US", "items": cart}), 60.0)
	assert result["lines"][0]["items"] == 1


def test_price_card_an_item_with_no_key_goes_to_the_catch_all():
	card = sample_card()
	for key in (MISSING, None, "", "  ", 5, ["F-CR-PT"]):
		entry = {"qty": 1, "weight_kg": pounds(1.0)}
		if key is not MISSING:
			entry["index_key"] = key
		line = priced(price(card, "US", entry), 40.0)["lines"][0]
		assert line["profile"] == "Rest of the World Allowed Items"


def test_price_card_a_huge_weight_is_too_heavy_never_an_error():
	card = sample_card()
	assert refused(price(card, "US", item(DANGEROUS, 1e300, qty=1e300)), "too_heavy")
	assert refused(price(card, "US", item(DANGEROUS, 1e308, qty=10)), "too_heavy")
	huge = price(card, "US", item(DANGEROUS, 1e10, qty=1e10))
	assert refused(huge, "too_heavy")["lines"][0]["weight_lb"] > 1e19
	assert priced(price(card, "KW", item(DANGEROUS, 1e300, qty=1e300)), 35.0), "a flat zone does not mind"


# ---------------------------------------------------------------- tier edges
def test_price_card_tier_edges_and_float_noise():
	card = sample_card()
	# 0.6000000001 lb is 0.600 lb after rounding to three decimals, so it is still in the first tier
	first = priced(price(card, "US", heavy(DANGEROUS, 0.6000000001)), 35.0)
	assert first["lines"][0]["weight_lb"] == 0.6 and first["lines"][0]["tier"] == 1
	assert priced(price(card, "US", heavy(DANGEROUS, 0.6004)), 35.0)["lines"][0]["weight_lb"] == 0.6
	# 0.6006 lb is 0.601 lb: in the gap, so the next tier up
	assert priced(price(card, "US", heavy(DANGEROUS, 0.6006)), 60.0)["lines"][0]["weight_lb"] == 0.601
	# the top of the last tier, and just over it
	assert priced(price(card, "US", heavy(DANGEROUS, 20.0004)), 400.0)["lines"][0]["weight_lb"] == 20.0
	assert (
		refused(price(card, "US", heavy(DANGEROUS, 20.001)), "too_heavy")["lines"][0]["weight_lb"] == 20.001
	)
	assert refused(price(card, "US", heavy(DANGEROUS, 20.0006)), "too_heavy")
	# 2.0000000000000004 lb is exactly 2 lb for pricing, as one item or as the sum of two
	assert (pounds(2.0000000000000004) * LB_PER_KG) > 2.0, "the noise really is above the edge"
	assert priced(price(card, "US", item(DANGEROUS, pounds(2.0000000000000004))), 60.0)
	assert priced(price(card, "US", item(DANGEROUS, pounds(1.9999999999999998))), 60.0)
	rest = pounds(2.0000000000000004) - 0.4  # 0.4 + rest is 2.0000000000000004 lb once converted
	assert (0.4 + rest) * LB_PER_KG > 2.0, "and so is the sum"
	assert (
		priced(price(card, "US", item(DANGEROUS, 0.4), item(DANGEROUS, rest)), 60.0)["lines"][0]["weight_lb"]
		== 2.0
	)
	# three items of 0.2 lb, and a hundred of 0.01 lb
	assert (
		priced(price(card, "US", *[heavy(DANGEROUS, 0.2) for _ in range(3)]), 35.0)["lines"][0]["weight_lb"]
		== 0.6
	)
	assert price(card, "US", item(DANGEROUS, pounds(0.01), qty=100))["lines"][0]["weight_lb"] == 1.0
	# a weight below the first tier pays the first tier
	for lb in (0.0001, 0.001, 0.05, 0.0999):
		assert priced(price(card, "US", heavy(DANGEROUS, lb)), 35.0)["lines"][0]["tier"] == 1
	# the first tier of the petite zone starts at 2.41 lb, and anything lighter pays it
	assert priced(price(card, "US", heavy(PETITE, 0.5)), 80.0)["lines"][0]["tier"] == 1


def test_price_card_uses_tiers_in_weight_order_whatever_order_the_card_keeps_them():
	card = sample_card()
	zone_of(card, "Dangerous Goods", "USA")["tiers"].reverse()  # now 18.01-20, 4.01-5, 2.01-4, 0.7-2, 0.1-0.6
	# the price is the same, and `tier` is the tier's place in the card's own list
	assert priced(price(card, "US", heavy(DANGEROUS, 0.65)), 60.0)["lines"][0]["tier"] == 4
	assert priced(price(card, "US", heavy(DANGEROUS, 3.0)), 80.0)["lines"][0]["tier"] == 3
	assert priced(price(card, "US", heavy(DANGEROUS, 0.5)), 35.0)["lines"][0]["tier"] == 5


def test_price_card_the_first_zone_that_lists_the_country_is_the_one_used():
	card = sample_card()
	profile_of(card, "Dangerous Goods")["zones"].append(
		{"name": "Again", "countries": ["US"], "method": "Other (1 days)", "days": 1, "tiers": [flat(1.0)]}
	)
	assert priced(price(card, "US", heavy(DANGEROUS, 1.0)), 60.0)["lines"][0]["zone"] == "USA"


@pytest.mark.parametrize(
	"damage",
	[
		pytest.param(lambda t: t.update(price=float("nan")), id="NaN price"),
		pytest.param(lambda t: t.update(price=float("inf")), id="infinite price"),
		pytest.param(lambda t: t.update(price="60"), id="text price"),
		pytest.param(lambda t: t.update(price=True), id="bool price"),
		pytest.param(lambda t: t.update(price=2e6), id="price above the limit"),
		pytest.param(lambda t: t.update(price=-0.01), id="price below zero"),
		pytest.param(lambda t: t.update(price=-1e308), id="price far below zero"),
		pytest.param(lambda t: t.update(price=None), id="no price"),
		pytest.param(lambda t: t.update({"from": "0.7"}), id="text weight"),
		pytest.param(lambda t: t.update(to=None), id="one weight only"),
		pytest.param(lambda t: t.update(to=float("nan")), id="NaN weight"),
		pytest.param(lambda t: t.update({"from": 9.0}), id="from above to"),
	],
)
def test_price_card_a_zone_with_a_tier_it_cannot_read_prices_nothing_rather_than_guess(damage):
	card = sample_card()
	damage(zone_of(card, "Dangerous Goods", "USA")["tiers"][1])
	result = refused(price(card, "US", heavy(DANGEROUS, 1.0)), "not_covered")
	assert "no usable prices" in result["detail"] and '"USA"' in result["detail"]
	# the damage is only in that zone
	assert priced(price(card, "GB", heavy(DANGEROUS, 1.0)), 75.0)


def test_price_card_a_zone_with_no_tiers_or_a_flat_tier_among_others_prices_nothing():
	card = sample_card()
	zone_of(card, "Dangerous Goods", "USA")["tiers"] = []
	assert refused(price(card, "US", heavy(DANGEROUS, 1.0)), "not_covered")
	zone_of(card, "Dangerous Goods", "Bahrain")["tiers"].append(tier(0.0, 2.0, 5.0))
	assert refused(price(card, "BH", heavy(DANGEROUS, 1.0)), "not_covered")


def test_price_card_days_outside_0_to_60_or_not_whole_are_ignored():
	card = sample_card()
	for days in (61, -1, True, 4.5, "4", None, 10**400, float("nan")):
		set_in(card, "Dangerous Goods", "USA", days=days)
		result = priced(price(card, "US", heavy(DANGEROUS, 1.0)), 60.0)
		assert result["days"] is None


def test_price_card_leaves_the_card_and_the_order_alone():
	card, cart = (
		sample_card(),
		order("US", heavy(DANGEROUS, 1.5), heavy(ALLOWED, 1.0), item(PETITE), item(REFILL, 0.5, qty=2)),
	)
	card_before, cart_before = copy.deepcopy(card), copy.deepcopy(cart)
	price_card(card, cart)
	price_card(card, {**cart, "country_code": "AE"})
	price_card(card, {**cart, "country_code": "ZZ"})
	assert card == card_before and cart == cart_before
	first = price_card(card, cart)
	first["lines"].append("changed")
	assert price_card(card, cart)["lines"] != first["lines"], "every call gives new lines"


# ---------------------------------------------------------------- junk
JUNK = [
	None,
	"",
	"  ",
	"abc",
	"US",
	"{F-CR-PT}",
	0,
	1,
	-1,
	2.5,
	-2.5,
	True,
	False,
	10**30,
	10**400,
	[],
	{},
	[None],
	[[]],
	{"a": 1},
	(1, 2),
	float("nan"),
	float("inf"),
	float("-inf"),
	Decimal("1.5"),
	Decimal("NaN"),
	b"US",
	"\x00",
	"\ud800",
	"\u00e9" * 300,
	object(),
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


def check_everything(card, orders, keys=(PETITE, DANGEROUS, "", "{F-CR-PC-W}")):
	problems = validate_card(card)
	assert isinstance(problems, list) and all(isinstance(p, str) and p for p in problems)
	assert set(card_summary(card)) == SUMMARY_KEYS
	for key in keys:
		found = classify_item(card, key)
		assert found is None or (type(found) is int and found >= 0)
	for cart in orders:
		check_shape(price_card(card, cart))


def test_nothing_raises_on_odd_input():
	"""Any value where a card, an order, an item or a country is expected gives a plain answer."""
	good_card, good_order = sample_card(), order("US", item(DANGEROUS, 1.0))
	for junk in JUNK:
		check_everything(junk, [good_order, junk])
		assert classify_item(junk, junk) is None or type(classify_item(junk, junk)) is int
		assert classify_item(good_card, junk) in (0, 1, 2, 3, None)
		assert isinstance(normalize_index_key(junk), str)
		check_shape(price_card(good_card, junk))
		check_shape(price_card(good_card, {"country_code": junk, "items": [item(DANGEROUS, 1.0)]}))
		check_shape(price_card(good_card, {"country_code": "US", "items": junk}))
		check_shape(price_card(good_card, order("US", junk)))
		check_shape(price_card(good_card, order("US", {"index_key": junk, "weight_kg": 1.0, "qty": 1})))
		check_shape(price_card(good_card, order("US", {"index_key": DANGEROUS, "weight_kg": junk, "qty": 1})))
		check_shape(
			price_card(good_card, order("US", {"index_key": DANGEROUS, "weight_kg": 1.0, "qty": junk}))
		)
		for other in JUNK:
			check_shape(price_card(junk, other))


def test_no_mix_of_junk_in_the_card_or_the_order_makes_anything_raise():
	"""A seeded fuzz: overwrite or delete a few values of the sample card, then check, summarise and price."""
	rng = random.Random(20261023)
	base = sample_card()
	orders = [
		order("US", heavy(DANGEROUS, 1.5), heavy(ALLOWED, 1.0)),
		order("GB", heavy(DANGEROUS, 8.0)),
		order("AE", item(PETITE), item(REFILL, 0.5, qty=2), item(DANGEROUS, 2.0)),
		order("BH", item(DANGEROUS, 1.0), item(ALLOWED, 1.0)),
		order("CI", heavy(DANGEROUS, 3.0), item("", 1.0)),
		order("US", heavy(PETITE, 4.05), heavy(REFILL, 3.0)),
		order(" us ", item("{W-1}", 0.2)),
		order("ZZ", item(DANGEROUS)),
		order("US"),
	]
	for _ in range(600):
		card = copy.deepcopy(base)
		for _ in range(rng.randint(1, 6)):
			container, key = rng.choice(places_to_break(card))
			if isinstance(container, dict) and rng.random() < 0.2:
				del container[key]
			else:
				container[key] = copy.deepcopy(rng.choice(JUNK))
		check_everything(card, orders)
	for _ in range(400):  # and the order
		cart = copy.deepcopy(orders[rng.randrange(len(orders))])
		for _ in range(rng.randint(1, 4)):
			container, key = rng.choice(places_to_break(cart))
			container[key] = copy.deepcopy(rng.choice(JUNK))
		check_shape(price_card(base, cart))


def test_oversized_junk_is_handled_without_going_through_all_of_it():
	huge = {"profiles": [tidy_profile(f"P{n}", match=["*"]) for n in range(2000)]}
	problems = validate_card(huge)
	assert problems_with(problems, "2000 profiles; at most 12")
	assert len(problems) < 100
	card = make_card([tidy_profile(countries=many_countries(676))])
	card["profiles"][0]["zones"][0]["tiers"] = [tier(float(n), n + 0.5, 1.0) for n in range(5000)]
	assert problems_with(validate_card(card), "5000 tiers; at most 60")
	check_shape(price_card(card, order("AB", item("X-1", 1.0))))


def _full_zone_list():
	card = make_card([tidy_profile("All", match=["*"])])
	profile = card["profiles"][0]
	zone = profile["zones"][0]
	profile["zones"] = [{**zone, "name": f"Z{n}", "countries": [many_countries(40)[n]]} for n in range(40)]
	return card, profile["zones"]


def _full_tier_list():
	card = make_card([tidy_profile("All", match=["*"])])
	tiers = card["profiles"][0]["zones"][0]["tiers"] = [tier(float(n), n + 0.5, 1.0) for n in range(60)]
	return card, tiers


def _full_country_list():
	card = make_card([tidy_profile("All", match=["*"], countries=many_countries(260))])
	return card, card["profiles"][0]["zones"][0]["countries"]


def _full_pattern_list():
	card = make_card([tidy_profile("All", match=["*", *(f"A{n}-*" for n in range(11))])])
	return card, card["profiles"][0]["match"]


# A list kept full to its limit: how to make it, the limit, how a sentence counts its entries, an entry that is
# bad, and how a bad entry inside the limit is reported.
FULL_LISTS = {
	"zones": (_full_zone_list, 40, "zones", None, "is not a valid zone"),
	"tiers": (_full_tier_list, 60, "tiers", None, "is not a valid tier"),
	"countries": (_full_country_list, 260, "countries", "bad", "not a country code"),
	"patterns": (_full_pattern_list, 12, "product code patterns", "", "has to be text that is not empty"),
}


@pytest.mark.parametrize("kind", FULL_LISTS)
def test_only_the_first_entries_of_an_oversized_list_are_inspected(kind):
	"""Past its limit a list is not looked at: bad entries there add nothing to the one sentence that says the
	list is too long (and the limit itself is inclusive, so a full list is checked entry by entry)."""
	build, limit, noun, bad, reported = FULL_LISTS[kind]
	card, entries = build()
	assert len(entries) == limit and validate_card(card) == [], "full to its limit, and still valid"
	entries[-1] = bad  # a bad entry inside the limit is reported, by itself
	problems = validate_card(card)
	assert len(problems) == 1 and reported in problems[0] and "at most" not in problems[0], problems
	for extra in (1, 5, 500):  # bad entries past the limit are not even looked at
		card, entries = build()
		entries.extend([bad] * extra)
		problems = validate_card(card)
		assert len(problems) == 1, (extra, problems)
		assert f"has {limit + extra} {noun}; at most {limit} are allowed." in problems[0], problems
		check_shape(price_card(card, order("AB", item("X-1", 1.0))))  # and pricing from it copes too


# ---------------------------------------------------------------- the real export, on this machine only
def real_text(name):
	"""The text of one file of the real export, or a skip when it cannot be found. The folder is looked up here,
	not when the module is imported: a machine with no home folder (a container run as a user that has no
	entry) must still collect the tests in this file and skip this one."""
	folder = os.environ.get("RATES_DIR")
	try:
		path = (Path(folder) if folder else Path.home() / "shipping-interim-rates") / name
	except (RuntimeError, KeyError, OSError):  # there is no home folder to look in
		pytest.skip(
			"the real export is not here (no home folder to look in); it is kept outside this repository"
		)
	if not path.is_file():
		pytest.skip(f"the real export is not here ({path}); it stays outside this public repository")
	return path.read_bytes().decode("utf-8")


def _no_home_folder(cls):
	raise RuntimeError("Could not determine home directory.")


def test_the_real_export_is_looked_up_late_so_a_machine_with_no_home_folder_still_collects_and_skips(
	monkeypatch,
):
	monkeypatch.delenv("RATES_DIR", raising=False)
	monkeypatch.setattr(Path, "home", classmethod(_no_home_folder))
	# importing this module does not look for a home folder (it would fail every test in the file) ...
	runpy.run_path(__file__, run_name="a_copy_of_the_tests")
	# ... and the one test that needs the real export skips
	with pytest.raises(pytest.skip.Exception, match="no home folder"):
		real_text("shopify_shipping_config.csv")
	# a folder named in RATES_DIR does not need a home folder at all
	monkeypatch.setenv("RATES_DIR", str(FIXTURES))
	assert real_text("shopify_shipping_config_sample.csv").startswith("﻿Profile")
	monkeypatch.setenv("RATES_DIR", str(FIXTURES / "nowhere"))
	with pytest.raises(pytest.skip.Exception, match="nowhere"):
		real_text("shopify_shipping_config.csv")


def test_real_export_prices_the_known_examples():
	"""Pins today's real figures (4 profiles, 14 zones, 49 tiers, 232 countries, 12 countries that are not
	delivered to, from AC to TA, and 3 notes), so it has to be updated whenever the owner refreshes the
	export. It only runs on a machine that holds the export, and skips everywhere else."""
	parsed = parse_shopify_export(
		real_text("shopify_shipping_config.csv"), real_text("shopify_countries_not_delivered.csv")
	)
	card = parsed["card"]
	assert validate_card(card) == []
	summary = card_summary(card)
	# 4 profiles and 49 tiers: every flat zone counts as the one tier it has
	assert (summary["profiles"], summary["tiers"]) == (4, 49)
	assert summary == {"profiles": 4, "zones": 14, "tiers": 49, "countries": 232}
	assert [profile["name"] for profile in card["profiles"]] == list(KNOWN_PROFILES)
	assert len(parsed["not_delivered"]) == 12
	assert parsed["not_delivered"][0] == "AC" and parsed["not_delivered"][-1] == "TA"
	assert len(parsed["notes"]) == 3, parsed["notes"]  # the 0.1 lb start and the two 0.1 lb gaps
	assert not set(parsed["not_delivered"]) & {
		c for p in card["profiles"] for z in p["zones"] for c in z["countries"]
	}
	# the examples of the brief, from the real tiers
	for weight, total in ((0.5, 35.0), (0.65, 60.0), (2.0, 60.0), (2.004, 80.0), (20.0, 400.0)):
		assert priced(price(card, "US", heavy(DANGEROUS, weight)), total)
	assert priced(price(card, "US", item(DANGEROUS, 1.0)), 80.0), "1 kg is 2.205 lb"
	assert refused(price(card, "US", heavy(DANGEROUS, 20.001)), "too_heavy")
	assert priced(price(card, "GB", heavy(DANGEROUS, 7.0)), 135.0)
	assert refused(price(card, "GB", heavy(DANGEROUS, 8.0)), "too_heavy")
	assert priced(price(card, "BH", heavy(DANGEROUS, 3.0)), 0.0)["name"] == BAHRAIN_FREE
	qatar = priced(price(card, "QA", heavy(DANGEROUS, 3.0)), 35.0)
	assert (qatar["name"], qatar["days"]) == (SHIPPING, 15)
	assert priced(price(card, "KW", heavy(DANGEROUS, 30.0)), 35.0)
	assert priced(price(card, "AE", item(PETITE, 0.2)), 20.0)
	assert priced(price(card, "BH", item(PETITE, 0.2)), 0.0)
	assert priced(price(card, "AE", item(ALLOWED, 2.0)), 0.0)["name"] == GCC_FREE
	assert priced(price(card, "AU", heavy(ALLOWED, 3.0)), 80.0)
	assert priced(price(card, "US", heavy(PETITE, 4.05)), 100.0)
	assert priced(price(card, "GB", heavy(REFILL, 1.0)), 75.0)
	assert priced(price(card, "US", heavy(DANGEROUS, 1.5), heavy(ALLOWED, 1.0)), 100.0)
	assert refused(price(card, "AE", item(REFILL, 0.5)), "not_covered")
	assert refused(price(card, "AU", item(DANGEROUS, 0.5)), "not_covered")
	for code in parsed["not_delivered"]:
		assert refused(price(card, code, item(ALLOWED, 0.5)), "not_covered")


def test_real_export_contains_every_row_of_the_trimmed_sample():
	"""The sample in this repository is cut from the real export: same methods, tiers and prices, and its
	countries are a subset of the real zone's."""
	real = parse_shopify_export(real_text("shopify_shipping_config.csv"))["card"]
	for profile in sample_card()["profiles"]:
		for zone in profile["zones"]:
			counterpart = next(
				z for z in profile_of(real, profile["name"])["zones"] if z["name"] == zone["name"]
			)
			assert (zone["method"], zone["days"]) == (counterpart["method"], counterpart["days"])
			assert all(t in counterpart["tiers"] for t in zone["tiers"]), zone["name"]
			assert set(zone["countries"]) <= set(counterpart["countries"]), zone["name"]
