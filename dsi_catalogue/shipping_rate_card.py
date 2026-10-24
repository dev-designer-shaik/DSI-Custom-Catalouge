"""The interim rate card: the Shopify store's shipping table, read, checked and priced from.

While the carrier account is blocked, delivery is charged from the table the Shopify store uses today: weights
in pounds, prices in US dollars, by kind of product and by country. This module is pure Python with no Frappe
import, so it is tested standalone. It reads the two CSV files exported from Shopify, checks a card, puts each
item of an order into a profile and works out what the order pays. `shipping_engine` imports it; it imports
nothing from the engine.

A card is plain data (it is kept as JSON text):

	{"version": 1, "currency": "USD", "unit": "lb",
	 "profiles": [{"name": "Dangerous Goods", "match": ["F-*"],
	               "zones": [{"name": "USA", "countries": ["US"], "method": "International Express (4-7 days)",
	                          "days": 7, "tiers": [{"from": 0.7, "to": 2.0, "price": 60.0}, ...]}]}]}

- A profile is a kind of product that Shopify rates on its own: Dangerous Goods (the full-size fragrances),
  Dangerous Goods Petite, Refill Service, and Rest of the World Allowed Items (everything else). Its `match`
  list holds product code patterns, where * stands for any run of characters, so F-*-PT is every code that
  starts with F- and ends with -PT. A pattern is read the way a shell reads a file name, so ? stands for any
  one character and [CD] for one of the characters listed ([!CD] for any other); `validate_card` accepts them
  and nothing else is special. The product code is the Website Item's index key without its braces, in
  capitals, so a pattern is written without braces (`validate_card` reports one that has them).
- A zone is a group of countries (two-letter codes) that a profile delivers to in the same way. It holds the
  delivery method as Shopify words it ("International Express (4-7 days)"), the days promised (the last number
  inside the method's brackets) and the prices. A country is in one zone of a profile, at most.
- A tier is a weight band, `from` to `to` in pounds, with its price in dollars. A zone with one flat price has a
  single tier whose `from` and `to` are both empty (None).

An order is priced in these steps:

1. Each item goes to the first profile, in the card's order, with a pattern that matches its product code. A
   profile with the pattern * matches everything, so it comes last and takes what the others leave. An item no
   profile takes is left out of the price, and the detail says so.
2. A profile's weight is its items' unit weights times their quantities, in kilograms, turned into pounds
   (2.20462262 pounds to the kilogram) and rounded to three decimals. An item with no usable weight counts as
   0.35 kg a unit, and its line is marked `defaulted_weight`. An item whose quantity is not a number above zero
   is ignored, and an order with nothing left to price is not offered (`not_offered`).
3. The profile's zone is the first one that lists the destination. If there is none, the card does not deliver
   that kind of product there, and the whole order cannot be priced (`not_covered`).
4. A flat zone charges its one price at any weight. Otherwise the tiers are read from the lightest up, and the
   first tier whose upper weight is at least the profile's weight is charged. So a weight that falls in a gap
   between two tiers (Shopify's 0.6 to 0.7 lb, say) pays the next tier up, and a weight under the first tier
   pays the first. A weight over the top tier cannot be priced (`too_heavy`).
5. The order pays the profiles' prices added together, in dollars and cents. One profile that cannot be priced
   (no zone for the country, a weight over the top tier, or a zone whose tiers hold something unreadable, such as
   a price below zero) means the whole order cannot: it is never charged as free.
6. The delivery method is named after the profiles' method text, or, if they differ, after the one that takes
   the most days, and it promises the largest number of days.

Nothing here raises on a half-finished card or order (the wizard prices a draft as it is typed). Where
something cannot be read, the order is not priced, with a `reason` and a plain sentence in `detail` for the
staff. `parse_shopify_export` raises `RateCardError` for a file that cannot be used at all.
"""

import csv
import io
import math
from decimal import ROUND_HALF_UP, Context, Decimal, InvalidOperation
from fnmatch import fnmatchcase

LB_PER_KG = 2.20462262
DEFAULT_ITEM_WEIGHT_KG = 0.35  # what an item with no usable weight counts as, for each unit

# The profiles of the Shopify export, in the order they take an item, with their product code patterns. The one
# with the pattern * takes whatever is left, so it comes last.
KNOWN_PROFILES = {
	"Dangerous Goods Petite": ["F-*-PT", "F-CR-PC-*"],
	"Refill Service": ["F-*-RF"],
	"Dangerous Goods": ["F-*"],
	"Rest of the World Allowed Items": ["*"],
}

# The most a card may hold, which `validate_card` reports.
MAX_PROFILES = 12
MAX_ZONES = 40  # in a profile
MAX_TIERS = 60  # in a zone
MAX_COUNTRIES = 260  # in a zone
MAX_PATTERNS = 12  # in a profile
MAX_PATTERN_LENGTH = 80
MAX_TEXT = 140  # the longest profile name, zone name or delivery method
MAX_PRICE = 1_000_000
MAX_WEIGHT_LB = 10_000
MAX_DAYS = 60
# `validate_card` lists at most this many problems, and past it one sentence more says how many there are
# besides: a card can be wrong in over a hundred thousand places, and the list goes into every save, Review
# and import. The wizard's import lists its sentences within the same number.
MAX_LISTED = 50

_BOM = chr(0xFEFF)  # a UTF-8 byte order mark, as a character
# Pounds. Shopify's tiers run 2 to 2.01 and 4 to 4.01, so a hundredth of a pound between two tiers is not a gap.
_GAP = Decimal("0.011")
_BIGGEST = math.nextafter(math.inf, 0.0)  # the largest float, for a weight too big to be a number


class RateCardError(ValueError):
	"""A rates file that cannot be used. The message is a plain sentence for the person importing it."""


# ---------------------------------------------------------------- reading loose data
def _as_dict(value):
	return value if isinstance(value, dict) else {}


def _as_list(value):
	return value if isinstance(value, (list, tuple)) else []


def _text(value):
	"""Text as it is; anything that is not text is empty."""
	return value if isinstance(value, str) else ""


def _words(value):
	"""A name as it is kept: single spaces between the words, none at the ends."""
	return " ".join(_text(value).split())


def _number(value):
	"""A finite float for an int, float or Decimal, otherwise None. A bool, some text, a NaN, an infinity and an
	integer too big for a float are no numbers."""
	if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
		return None
	try:
		number = float(value)
	except (OverflowError, ValueError):  # an integer of 400 digits, a signalling NaN
		return None
	return number if math.isfinite(number) else None


def _country(value):
	"""A country code as two upper-case letters, or "" when it is not one. Never raises."""
	if not isinstance(value, str):
		return ""
	code = value.strip()
	# Checked before upper-casing: a dotless i or a sharp s turns into plain letters when upper-cased.
	return code.upper() if len(code) == 2 and code.isascii() and code.isalpha() else ""


def _is_plain(text):
	"""Text a person could have typed: printable (so no control character, no NUL and no lone surrogate) and
	writable as UTF-8."""
	try:
		text.encode("utf-8")
	except UnicodeEncodeError:
		return False
	return text.isprintable()


def _printable(text):
	"""`text` with anything unprintable shown as ?. It reads the text character by character, which is slow for
	a long one (0.4 s for 10 MB), so it is given only the part a sentence shows."""
	return "".join(char if char.isprintable() else "?" for char in text)


def _quote(text, limit=60):
	"""`text` in double quotes for a sentence: cut short, with anything unprintable shown as ?. Only its first
	`limit` + 1 characters are read (one more than fits says whether it has to be cut), so a text of megabytes
	costs what a short one does."""
	text = _printable(text[: limit + 1])
	return '"' + (text if len(text) <= limit else text[: limit - 3] + "...") + '"'


def lb_text(pounds):
	"""A weight or a tier edge in pounds as a sentence writes it: 0.7, 2, 18.01, 1.984. The card's own sentences
	and the engine's receipt lines both use this one, so they cannot drift apart."""
	return f"{pounds:.10g}"


def _listing(items, limit=5):
	"""Quoted texts for a sentence, the first few of them."""
	shown = ", ".join(_quote(item) for item in items[:limit])
	return shown if len(items) <= limit else f"{shown} and {len(items) - limit} more"


# ---------------------------------------------------------------- money
_CENTS = Decimal("0.01")
# A float has at most 309 digits before the point, and the cents add two more. Decimal's own default of 28
# digits would make `quantize` raise for any amount above 1e26.
_ROOM = Context(prec=400)


def _round_cents(value):
	"""A Decimal in cents, rounded half up. A price below zero stays below zero."""
	return value.quantize(_CENTS, rounding=ROUND_HALF_UP, context=_ROOM)


def _cents(amount):
	"""`amount` (a finite float) in cents, rounded half up from the decimal text of the number, never below
	zero: 1.005 is 1.005, not the binary number nearest to it."""
	value = Decimal(str(amount))
	return _round_cents(value) if value > 0 else Decimal(0)


def _decimal(text):
	"""The number written in `text`: digits with an optional sign and point, and nothing else (no exponent,
	no thousands separator, no currency sign, no other script's digits). None when it is not one."""
	text = text.strip()
	whole, _, fraction = (text[1:] if text[:1] in ("+", "-") else text).partition(".")
	digits = whole + fraction
	if not digits or not digits.isascii() or not digits.isdigit():
		return None
	try:
		value = Decimal(text)
		finite = math.isfinite(float(value))
	except (InvalidOperation, ValueError, OverflowError):
		return None
	return value if finite else None


# ---------------------------------------------------------------- reading the Shopify export
# The columns of the config file: the key used below, and the title in the export.
_COLUMNS = {
	"profile": "Profile",
	"zone": "Zone (recommended)",
	"count": "Country count",
	"countries": "Countries (ISO-2 - Name)",
	"method": "Shipping method",
	"low": "Weight from (lb)",
	"high": "Weight to (lb)",
	"price": "Price (USD)",
}
_OPTIONAL_COLUMNS = ("count",)  # only used to say when the list of countries does not add up

_KNOWN_BY_NAME = {" ".join(name.split()).casefold(): name for name in KNOWN_PROFILES}


def _records(text):
	"""[(row number, cells)] for the rows of CSV text that are not blank. Row 1 is the first line; blank lines
	are counted but not listed. Raises csv.Error for CSV that cannot be read."""
	if text.startswith(_BOM):
		text = text[1:]
	# strict: an unclosed quote is an error, not the rest of the file swallowed into one cell
	rows = csv.reader(io.StringIO(text, newline=""), strict=True)
	return [(number, cells) for number, cells in enumerate(rows, start=1) if any(c.strip() for c in cells)]


def _find_columns(header, wanted):
	"""{key: position} of the wanted columns in a header row. Case and runs of spaces do not matter, and the
	first of two columns with the same name is the one used."""
	positions = {}
	for position, cell in enumerate(header):
		positions.setdefault(" ".join(cell.split()).casefold(), position)
	found = {}
	for key, title in wanted.items():
		wanted_name = " ".join(title.split()).casefold()
		if wanted_name in positions:
			found[key] = positions[wanted_name]
	return found


def _cell(cells, columns, key):
	position = columns.get(key)
	return cells[position].strip() if position is not None and position < len(cells) else ""


def _skip(notes, number, why):
	notes.append(f"Row {number} was skipped: {why}.")


def _read_row(number, cells, columns, notes):
	"""One row of the config file as a dict, or None (with a note saying why) when it cannot be used."""
	profile, zone = _words(_cell(cells, columns, "profile")), _words(_cell(cells, columns, "zone"))
	countries, price_text = _cell(cells, columns, "countries"), _cell(cells, columns, "price")
	low_text, high_text = _cell(cells, columns, "low"), _cell(cells, columns, "high")
	if not profile:
		return _skip(notes, number, "it has no profile")
	if not zone:
		return _skip(notes, number, "it has no zone name")
	if not any(token.strip() for token in countries.split(";")):
		return _skip(notes, number, "it has no country list")
	if not price_text:
		return _skip(notes, number, "it has no price")
	price = _decimal(price_text)
	if price is None:
		return _skip(notes, number, f"the price {_quote(price_text)} is not a number")
	low = high = None  # both empty is a flat price
	if low_text or high_text:
		if not (low_text and high_text):
			return _skip(notes, number, "it has only one of the two weights (weight from, weight to)")
		low, high = _decimal(low_text), _decimal(high_text)
		if low is None or high is None:
			return _skip(
				notes, number, f"the weight {_quote(low_text if low is None else high_text)} is not a number"
			)
		low, high = float(low) + 0.0, float(high) + 0.0
	return {
		"number": number,
		"profile": profile,
		"zone": zone,
		"count": _cell(cells, columns, "count"),
		"countries": countries,
		"method": _words(_cell(cells, columns, "method")),
		"low": low,
		"high": high,
		"price": float(_round_cents(price)) + 0.0,
	}


def _read_config(text, notes):
	"""The usable rows of the config file. Raises RateCardError when there are none."""
	if not isinstance(text, str):
		raise RateCardError("The rates file has to be text.")
	try:
		records = _records(text)
	except csv.Error as error:
		raise RateCardError(f"The rates file could not be read as CSV ({error}).") from None
	if not records:
		raise RateCardError("The rates file is empty.")
	(_, header), *body = records
	columns = _find_columns(header, _COLUMNS)
	missing = [
		title for key, title in _COLUMNS.items() if key not in columns and key not in _OPTIONAL_COLUMNS
	]
	if missing:
		raise RateCardError(
			f"The rates file is missing these columns: {', '.join(missing)}. "
			"The first row has to be the column names, separated by commas."
		)
	rows = [row for number, cells in body if (row := _read_row(number, cells, columns, notes)) is not None]
	if not rows:
		raise RateCardError("No row of the rates file could be used." + (f" {notes[0]}" if notes else ""))
	return rows


def _parse_countries(cell):
	"""(codes, bad tokens, codes repeated) of a country list. The tokens are split on ';', and the code of a
	token is the text before its first ' - ' (or the whole token when it has none)."""
	codes, bad, repeated = [], [], []
	for token in cell.split(";"):
		token = token.strip()
		if not token:  # a trailing ';' is not a country
			continue
		code = _country(token.partition(" - ")[0])
		if not code:
			bad.append(token)
		elif code in codes:
			repeated.append(code)
		else:
			codes.append(code)
	return codes, bad, repeated


def _days(method):
	"""The days a method promises: the last whole number inside its last pair of brackets, else None."""
	start = method.rfind("(")
	end = method.find(")", start) if start >= 0 else -1
	if end < 0:
		return None
	last = ""  # the last run of digits
	run = []
	for char in method[start + 1 : end] + " ":
		if "0" <= char <= "9":
			run.append(char)
		elif run:
			last, run = "".join(run), []
	return int(last) if last and len(last) <= 6 else None


def _zone_countries(first, name, notes):
	codes, bad, repeated = _parse_countries(first["countries"])
	where = f"Row {first['number']}"
	if bad:
		notes.append(
			f"{where}: left out of zone {_quote(name)}, because they do not start with a two-letter country "
			f"code: {_listing(bad)}."
		)
	if repeated:
		notes.append(
			f"{where}: {', '.join(dict.fromkeys(repeated))} appear more than once in zone {_quote(name)}. "
			"Each is kept once."
		)
	count = first["count"]
	# a count of ten digits or more is not a count (and int() refuses a very long run of digits)
	if count.isascii() and count.isdigit() and len(count) < 10 and int(count) != len(codes):
		notes.append(f"{where} says the zone has {count} countries, but {len(codes)} could be read.")
	return codes


def _zone_method(rows, name, notes):
	method_row = next((row for row in rows if row["method"]), None)
	if method_row is None:
		notes.append(
			f"Row {rows[0]['number']} has no shipping method, so zone {_quote(name)} has no delivery name yet."
		)
		return ""
	for row in rows:
		if row["method"] and row["method"] != method_row["method"]:
			notes.append(
				f"Row {row['number']} has a different shipping method from row {method_row['number']} of zone "
				f"{_quote(name)}. The method of row {method_row['number']} is used."
			)
	return method_row["method"]


def _zone_tiers(rows, where, notes):
	"""The tiers of a zone from its rows, lightest first: the weight rows, or else the first flat price."""
	flat_rows = [row for row in rows if row["low"] is None]
	tier_rows = [row for row in rows if row["low"] is not None]
	if tier_rows:
		for row in flat_rows:
			notes.append(
				f"{where} has both a flat price and weight tiers. The weight tiers are kept and the flat price "
				f"(row {row['number']}) is ignored."
			)
		tiers = [{"from": row["low"], "to": row["high"], "price": row["price"]} for row in tier_rows]
		tiers.sort(key=lambda tier: (tier["from"], tier["to"]))
		if Decimal(str(tiers[0]["from"])) > _GAP:
			notes.append(
				f"{where}: the first tier starts at {lb_text(tiers[0]['from'])} lb. Lighter weights pay the first tier."
			)
		for index in range(1, len(tiers)):
			before, after = tiers[index - 1], tiers[index]
			if Decimal(str(after["from"])) - Decimal(str(before["to"])) > _GAP:  # exact: 6.011 - 6 is 0.011
				notes.append(
					f"{where}: there is a gap between {lb_text(before['to'])} lb and {lb_text(after['from'])} lb. "
					"Weights in the gap pay the next tier up."
				)
		return tiers
	for row in flat_rows[1:]:
		notes.append(
			f"{where} has two flat prices. The first (row {flat_rows[0]['number']}) is kept and "
			f"row {row['number']} is ignored."
		)
	return [{"from": None, "to": None, "price": flat_rows[0]["price"]}]


def _build_zone(profile_name, group, notes):
	rows, name = group["rows"], group["name"]
	first = rows[0]
	where = f"Zone {_quote(name)} of profile {_quote(profile_name)}"
	countries = _zone_countries(first, name, notes)
	for row in rows[1:]:
		if set(_parse_countries(row["countries"])[0]) != set(countries):
			notes.append(
				f"Row {row['number']} lists different countries from row {first['number']} of zone "
				f"{_quote(name)}. The list of row {first['number']} is used."
			)
	method = _zone_method(rows, name, notes)
	return {
		"name": name,
		"countries": countries,
		"method": method,
		"days": _days(method),
		"tiers": _zone_tiers(rows, where, notes),
	}


def _profile_order(keys):
	"""The order a card keeps its profiles in: the known ones in the order they take an item, then any the file
	adds, and the catch-all (the profile with the pattern *) last."""
	known = [key for key in _KNOWN_BY_NAME if key in keys]
	catch_all = [key for key in known if "*" in KNOWN_PROFILES[_KNOWN_BY_NAME[key]]]
	unknown = [key for key in keys if key not in _KNOWN_BY_NAME]
	return [key for key in known if key not in catch_all] + unknown + catch_all


def _build_card(rows, notes):
	groups = {}  # profile key -> {"name", "zones": {zone key -> {"name", "rows"}}}, in order of appearance
	for row in rows:
		zones = groups.setdefault(row["profile"].casefold(), {"name": row["profile"], "zones": {}})["zones"]
		zones.setdefault(row["zone"].casefold(), {"name": row["zone"], "rows": []})["rows"].append(row)
	profiles = []
	for key in _profile_order(list(groups)):
		known_name = _KNOWN_BY_NAME.get(key)
		name = known_name or groups[key]["name"]
		if known_name is None:
			notes.append(
				f"Profile {_quote(name)} is not one of the known profiles, so it has no product code patterns "
				"yet. It will not be used until patterns are added."
			)
		profiles.append(
			{
				"name": name,
				"match": list(KNOWN_PROFILES.get(known_name, [])),
				"zones": [_build_zone(name, group, notes) for group in groups[key]["zones"].values()],
			}
		)
	return {"version": 1, "currency": "USD", "unit": "lb", "profiles": profiles}


def _read_not_delivered(text, notes):
	"""The country codes of the not-delivered file. A file that is empty or cannot be read is a note."""
	if text is None:
		return []
	none_marked = "so no country was marked as not delivered."
	if not isinstance(text, str):
		notes.append(f"The not-delivered file is not text, {none_marked}")
		return []
	try:
		records = _records(text)
	except csv.Error as error:
		notes.append(f"The not-delivered file could not be read as CSV ({error}), {none_marked}")
		return []
	if not records:
		notes.append(f"The not-delivered file has no countries, {none_marked}")
		return []
	(_, header), *body = records
	columns = _find_columns(header, {"code": "ISO-2"})
	if "code" not in columns:
		notes.append(f'The not-delivered file has no "ISO-2" column, {none_marked}')
		return []
	if not body:
		notes.append(f"The not-delivered file has no countries, {none_marked}")
		return []
	codes = []
	for number, cells in body:
		raw = _cell(cells, columns, "code")
		code = _country(raw)
		if not code:
			what = f"{_quote(raw)} is not a two-letter country code" if raw else "it has no country code"
			notes.append(f"Not-delivered row {number} was skipped: {what}.")
		elif code not in codes:
			codes.append(code)
	return codes


def parse_shopify_export(config_csv: str, not_delivered_csv: str | None = None) -> dict:
	"""Read the two files exported from Shopify. Returns {"card", "not_delivered": [ISO-2], "notes": [str]}.

	`config_csv` has the columns Profile, Zone (recommended), Country count, Countries (ISO-2 - Name), Shipping
	method, Weight from (lb), Weight to (lb) and Price (USD), in any order, with any case or spacing; other
	columns are ignored and Country count is optional. A byte order mark, CRLF line ends and blank lines are
	fine. `not_delivered_csv`, if given, has an ISO-2 column.

	The card has the known profiles with their product code patterns (an unknown profile gets none and a note
	says so), the catch-all last, and each zone's tiers lightest first; prices are rounded to cents, half up.
	Rows and country tokens that cannot be used are left out, each with a note naming the row. The quirks of
	the export are noted and kept: a gap between tiers, and a first tier that does not start near zero. The
	not-delivered file only adds notes when it is empty or unreadable.

	Raises RateCardError, with a plain sentence, when the config text is empty, is not text, has no recognisable
	header, cannot be read as CSV or has no row that can be used.
	"""
	notes = []
	rows = _read_config(config_csv, notes)
	card = _build_card(rows, notes)
	return {"card": card, "not_delivered": _read_not_delivered(not_delivered_csv, notes), "notes": notes}


# ---------------------------------------------------------------- checking a card
def _cap(text):
	return text[:1].upper() + text[1:]


def _named(entry):
	return _text(_as_dict(entry).get("name")).strip()


def _ref(label, position, name):
	"""How a sentence points at a profile or a zone: 'profile 2 ("Refill Service")', or 'profile 2' if it has no
	name. Positions count from 1."""
	return f"{label} {position}" + (f" ({_quote(name)})" if name else "")


def _check_name(subject, name, problems):
	"""Report a name that is empty, too long or not plain text. `subject` is 'profile 2' or the like. Whether a
	name is plain text is read from its first MAX_TEXT + 1 characters: a longer one is reported as too long, and
	what else is wrong further on is found once it is cut to size."""
	if not name:
		problems.append(f"{_cap(subject)} has no name.")
		return
	if len(name) > MAX_TEXT:
		problems.append(f"{_cap(subject)} has a name longer than {MAX_TEXT} characters.")
	if not _is_plain(name[: MAX_TEXT + 1]):
		problems.append(f"{_cap(subject)} has a name that is not plain text.")


def _check_patterns(subject, profile, problems):
	"""Report the profile's patterns that cannot be used. Returns True if one of them is only asterisks. As for
	a name, whether a pattern is plain text is read from its first MAX_PATTERN_LENGTH + 1 characters."""
	patterns = _as_list(profile.get("match"))
	if len(patterns) > MAX_PATTERNS:
		problems.append(
			f"{_cap(subject)} has {len(patterns)} product code patterns; at most {MAX_PATTERNS} are allowed."
		)
	usable, catch_all = 0, False
	for number, pattern in enumerate(patterns[:MAX_PATTERNS], start=1):
		text = _text(pattern).strip()
		if not text:
			problems.append(f"{_cap(subject)}: pattern {number} has to be text that is not empty.")
			continue
		usable += 1
		if len(text) > MAX_PATTERN_LENGTH:
			problems.append(
				f"{_cap(subject)}: pattern {number} is longer than {MAX_PATTERN_LENGTH} characters."
			)
		if not _is_plain(text[: MAX_PATTERN_LENGTH + 1]):
			problems.append(f"{_cap(subject)}: pattern {number} has to be plain text.")
		if "{" in text or "}" in text:  # an item's code is matched without its braces, so this never matches
			problems.append(
				f"{_cap(subject)}: pattern {number} contains a brace. "
				"Write the product code without braces, such as F-CR-XYZ-*."
			)
		catch_all = catch_all or not text.strip("*")  # nothing but asterisks (it is not empty)
	if not usable:
		problems.append(f"{_cap(subject)} has no product code patterns, so it will never be used.")
	return catch_all


def _weight_problem(value):
	"""True when `value` is not a weight in pounds from 0 to MAX_WEIGHT_LB."""
	number = _number(value)
	return number is None or not 0 <= number <= MAX_WEIGHT_LB


def _check_tier(where, tier, problems):
	"""Report what is wrong with one tier. Returns (from, to) when it is a tier with usable weights, else None;
	a flat tier is None as well."""
	if not isinstance(tier, dict):
		problems.append(f"{_cap(where)} is not a valid tier.")
		return None
	price = _number(tier.get("price"))
	if price is None or not 0 <= price <= MAX_PRICE:
		problems.append(f"{_cap(where)} has a price that has to be a number from 0 to {MAX_PRICE}.")
	low, high = tier.get("from"), tier.get("to")
	if low is None and high is None:  # a flat price
		return None
	if low is None or high is None:
		problems.append(f"{_cap(where)} has only one of the two weights; a flat price has neither.")
		return None
	if _weight_problem(low) or _weight_problem(high):
		problems.append(f"{_cap(where)} has weights that have to be numbers from 0 to {MAX_WEIGHT_LB} lb.")
		return None
	low, high = _number(low), _number(high)
	if low > high:
		problems.append(
			f"{_cap(where)} has its weights the wrong way round: "
			f"it starts at {lb_text(low)} lb but ends at {lb_text(high)} lb."
		)
		return None
	return low, high


def _check_tiers(zone_subject, tiers, problems):
	if not tiers:
		problems.append(f"{_cap(zone_subject)} has no tiers.")
		return
	if len(tiers) > MAX_TIERS:
		problems.append(f"{_cap(zone_subject)} has {len(tiers)} tiers; at most {MAX_TIERS} are allowed.")
	weighted = []  # (from, to, position) of the tiers that have usable weights
	for position, tier in enumerate(tiers[:MAX_TIERS], start=1):
		edges = _check_tier(f"tier {position} of {zone_subject}", tier, problems)
		if edges is not None:
			weighted.append((*edges, position))
	flat = any(
		isinstance(t, dict) and t.get("from") is None and t.get("to") is None for t in tiers[:MAX_TIERS]
	)
	if flat and len(tiers) > 1:
		problems.append(f"{_cap(zone_subject)} has a flat price, so it can have only one tier.")
	weighted.sort()
	for index in range(1, len(weighted)):
		(_, before_to, before), (low, _, position) = weighted[index - 1], weighted[index]
		if low <= before_to:
			problems.append(
				f"{_cap(f'tier {position} of {zone_subject}')} overlaps the tier before it: it starts at "
				f"{lb_text(low)} lb, but tier {before} ends at {lb_text(before_to)} lb."
			)


def _check_countries(zone_subject, countries, owners, this_zone, problems):
	"""Report the zone's country codes that cannot be used or are listed twice. `owners` maps a code to the
	zone of the same profile that listed it first, and `this_zone` is how a sentence points at this zone."""
	if not countries:
		problems.append(f"{_cap(zone_subject)} has no countries.")
		return
	if len(countries) > MAX_COUNTRIES:
		problems.append(
			f"{_cap(zone_subject)} has {len(countries)} countries; at most {MAX_COUNTRIES} are allowed."
		)
	here = set()
	for code in countries[:MAX_COUNTRIES]:
		if not (isinstance(code, str) and code == _country(code) and code):
			what = f"{_quote(code)}, which is" if isinstance(code, str) else "something that is"
			problems.append(
				f"{_cap(zone_subject)} lists {what} not a country code (two capital letters, such as US)."
			)
		elif code in here:
			problems.append(f"{_cap(zone_subject)} lists {code} more than once.")
		elif code in owners:
			problems.append(
				f"{_cap(zone_subject)} lists {code}, which is already in {owners[code]} of this profile."
			)
		else:
			here.add(code)
			owners[code] = this_zone


def _check_zone(profile_subject, position, zone, owners, problems):
	if not isinstance(zone, dict):
		problems.append(f"{_cap(_ref('zone', position, ''))} of {profile_subject} is not a valid zone.")
		return
	name = _named(zone)
	subject = f"{_ref('zone', position, name)} of {profile_subject}"
	_check_name(subject, name, problems)
	_check_countries(subject, _as_list(zone.get("countries")), owners, _ref("zone", position, name), problems)
	method = _text(zone.get("method")).strip()
	if not method:
		problems.append(f"{_cap(subject)} has no delivery method text.")
	elif len(method) > MAX_TEXT:
		problems.append(f"{_cap(subject)} has a delivery method longer than {MAX_TEXT} characters.")
	elif not _is_plain(method):
		problems.append(f"{_cap(subject)} has a delivery method that is not plain text.")
	days = zone.get("days")
	if days is not None and (
		isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= MAX_DAYS
	):
		problems.append(
			f"{_cap(subject)} has days that have to be a whole number from 0 to {MAX_DAYS}, or empty."
		)
	_check_tiers(subject, _as_list(zone.get("tiers")), problems)


def _check_profile(position, profile, seen, problems):
	"""Report what is wrong with one profile. Returns (its subject, whether it has a catch-all pattern)."""
	name = _named(profile)
	subject = _ref("profile", position, name)
	if not isinstance(profile, dict):
		problems.append(f"Profile {position} is not a valid profile.")
		return subject, False
	_check_name(subject, name, problems)
	# Only a name within its limit has a key: a longer one is refused for its length, compared by its first
	# characters it would be "the same name" as another that is not, and its key would cost what its length
	# does (5 MB of short words took 136 ms and 100 MB). A name with no key is neither kept nor compared.
	key = " ".join(name.split()).casefold() if len(name) <= MAX_TEXT else ""
	if key and key in seen:
		problems.append(f"{_cap(subject)} has the same name as {seen[key]}.")
	elif key:
		seen[key] = subject
	catch_all = _check_patterns(subject, profile, problems)
	zones = _as_list(profile.get("zones"))
	if not zones:
		problems.append(f"{_cap(subject)} has no zones.")
	elif len(zones) > MAX_ZONES:
		problems.append(f"{_cap(subject)} has {len(zones)} zones; at most {MAX_ZONES} are allowed.")
	owners = {}
	for number, zone in enumerate(zones[:MAX_ZONES], start=1):
		_check_zone(subject, number, zone, owners, problems)
	return subject, catch_all


def validate_card(card) -> list[str]:
	"""What is wrong with a card, as plain sentences that name the profile, zone or tier (positions count from
	1). An empty list means the card can be used. Never raises.

	Besides the shape (version 1, currency "USD", unit "lb"), it reports: a missing or repeated name; a profile
	with no patterns, which would never be used; a pattern with a brace in it, which could never match (an
	item's code is matched without its braces); a card where no profile matches everything (a pattern of *),
	where more than one does, or where the one that does is not last; a country that is not two capital
	letters or is in two zones of a profile; a zone with no countries, no tiers or no delivery method; days
	outside 0 to 60; a flat price beside other tiers; a tier with one weight, weights the wrong way round, or
	one that starts at or below where the tier before it ends; a price outside 0 to 1,000,000; and a list
	longer than the limits (12 profiles, 40 zones in a profile, 60 tiers and 260 countries in a zone, 12
	patterns, 80 characters in a pattern, 140 in a name or method, 10,000 lb in a weight). Only the first
	entries of a list over its limit are looked at.

	At most 50 sentences are listed (MAX_LISTED). A card with more problems gets its first 50, in the order
	they were found, and then one that says how many more there are ("There are 7 more problems with the
	card.").
	"""
	if not isinstance(card, dict):
		return ["There is no rate card."]
	problems = []
	for key, expected, how in (("version", 1, "1"), ("currency", "USD", '"USD"'), ("unit", "lb", '"lb"')):
		value = card.get(key)
		if type(value) is not type(expected) or value != expected:
			problems.append(f"The rate card's {key} has to be {how}.")
	profiles = _as_list(card.get("profiles"))
	if not profiles:
		problems.append("The rate card has no profiles.")
		return problems
	if len(profiles) > MAX_PROFILES:
		problems.append(f"The rate card has {len(profiles)} profiles; at most {MAX_PROFILES} are allowed.")
	seen, catch_alls = {}, []
	checked = profiles[:MAX_PROFILES]
	for position, profile in enumerate(checked, start=1):
		subject, catch_all = _check_profile(position, profile, seen, problems)
		if catch_all:
			catch_alls.append((position, subject))
	for number, (position, subject) in enumerate(catch_alls):
		if number:
			problems.append(
				f"{_cap(subject)} also matches every item (a pattern of *), like {catch_alls[0][1]}; "
				"only the first would ever be used."
			)
		if position != len(checked):
			problems.append(
				f"{_cap(subject)} matches every item (a pattern of *) but is not the last profile, so the "
				"profiles after it would never be used."
			)
	if not catch_alls:
		problems.append(
			"No profile matches every item (a pattern of *), so items that match no profile would be left "
			"out of the price."
		)
	if len(problems) > MAX_LISTED:
		return [*problems[:MAX_LISTED], _more_problems(len(problems) - MAX_LISTED)]
	return problems


def _more_problems(count):
	if count == 1:
		return "There is 1 more problem with the card."
	return f"There are {count} more problems with the card."


def card_summary(card) -> dict:
	"""How much the card holds, for the import preview: {"profiles", "zones", "tiers", "countries"}. The
	countries are counted once however many zones list them. Never raises."""
	profiles = [profile for profile in _as_list(_as_dict(card).get("profiles")) if isinstance(profile, dict)]
	zones = [
		zone for profile in profiles for zone in _as_list(profile.get("zones")) if isinstance(zone, dict)
	]
	tiers = sum(1 for zone in zones for tier in _as_list(zone.get("tiers")) if isinstance(tier, dict))
	countries = {_country(code) for zone in zones for code in _as_list(zone.get("countries"))} - {""}
	return {"profiles": len(profiles), "zones": len(zones), "tiers": tiers, "countries": len(countries)}


# ---------------------------------------------------------------- putting an item into a profile
def normalize_index_key(value) -> str:
	"""The product code of an index key: braces removed, outer spaces stripped, upper case. What is not text
	has no key, so it is empty."""
	if not isinstance(value, str):
		return ""
	return value.replace("{", "").replace("}", "").strip().upper()


def classify_item(card, index_key) -> int | None:
	"""The position, in the card's list of profiles, of the first profile with a pattern that matches the
	item's product code; None when no profile does. Patterns use * for any run of characters (and, as a shell
	does, ? for any one character and [CD] for one of the characters listed) and are read in capitals; a blank
	pattern matches nothing. An empty key matches only a pattern made of asterisks."""
	key = normalize_index_key(index_key)
	for position, profile in enumerate(_as_list(_as_dict(card).get("profiles"))):
		for pattern in _as_list(_as_dict(profile).get("match")):
			pattern = _text(pattern).strip().upper()
			if pattern and fnmatchcase(key, pattern):
				return position
	return None


# ---------------------------------------------------------------- pricing an order
def _refused(reason, detail, lines=()):
	return {
		"priced": False,
		"reason": reason,
		"total": 0.0,
		"name": "",
		"days": None,
		"lines": list(lines),
		"detail": detail,
	}


def _order_items(order):
	"""The order's items that can be priced, as {"key", "weight_kg", "qty"}: a quantity above zero, and a unit
	weight that is None when it is missing or not a number above zero."""
	items = []
	for entry in _as_list(order.get("items")):
		entry = _as_dict(entry)
		qty = _number(entry.get("qty"))
		if qty is None or qty <= 0:
			continue
		weight = _number(entry.get("weight_kg"))
		items.append(
			{
				"key": normalize_index_key(entry.get("index_key")),
				"weight_kg": weight if weight is not None and weight > 0 else None,
				"qty": qty,
			}
		)
	return items


def _group_weight(entries):
	"""(kilograms, how many of the items had no usable weight) for the items of one profile."""
	kilos, guessed = 0.0, 0
	for entry in entries:
		unit = entry["weight_kg"]
		if unit is None:
			unit, guessed = DEFAULT_ITEM_WEIGHT_KG, guessed + 1
		kilos += unit * entry["qty"]
	return kilos, guessed


def _zone_for(profile, country):
	"""The first zone of the profile that lists the country."""
	for zone in _as_list(profile.get("zones")):
		if isinstance(zone, dict) and any(
			_country(code) == country for code in _as_list(zone.get("countries"))
		):
			return zone
	return None


def _usable_tiers(zone):
	"""[(position, from, to, price)] for the zone's tiers, lightest first (a flat price has from and to None).
	None when the zone cannot price an order: no tiers, a tier that cannot be read (a price that is not a
	number from 0 to MAX_PRICE, weights that are not usable), or a flat price beside other tiers. A price below
	zero is never read as a free delivery. A position counts from 1 in the zone's own list."""
	tiers = []
	for position, tier in enumerate(_as_list(zone.get("tiers")), start=1):
		if not isinstance(tier, dict):
			return None
		price = _number(tier.get("price"))
		if price is None or price < 0 or price > MAX_PRICE:
			return None
		low, high = tier.get("from"), tier.get("to")
		if low is None and high is None:
			tiers.append((position, None, None, price))
			continue
		low, high = _number(low), _number(high)
		if low is None or high is None or low > high:
			return None
		tiers.append((position, low, high, price))
	if any(low is None for _, low, _, _ in tiers):
		return tiers if len(tiers) == 1 else None
	return sorted(tiers, key=lambda tier: (tier[1], tier[2], tier[0])) or None


def _choose_tier(tiers, pounds):
	"""The first tier whose upper weight is at least `pounds` (a flat price takes any weight), else None."""
	for tier in tiers:
		if tier[1] is None or pounds <= tier[2]:
			return tier
	return None


def _tier_sentence(who, country, zone_name, pounds, tiers, chosen, guessed, count):
	position, low, high, price = chosen
	where = f'zone "{zone_name}"'
	if low is None:
		sentence = f"{who} to {country}: {where} has one flat price, ${_cents(price):.2f}."
	else:
		band = f"tier {position} ({lb_text(low)} to {lb_text(high)} lb)"
		if pounds >= low:
			how = f"is in {band} of {where}"
		elif chosen is tiers[0]:
			how = f"is below the first tier ({lb_text(low)} to {lb_text(high)} lb) of {where}, so it pays the first tier"
		else:
			how = f"is in the gap before {band} of {where}, so it pays tier {position}"
		sentence = f"{who} to {country}: {lb_text(pounds)} lb {how}, ${_cents(price):.2f}."
	if guessed:
		sentence += (
			f" The weight is an estimate: {guessed} of {count} items have no weight on file "
			f"({DEFAULT_ITEM_WEIGHT_KG} kg is used for each unit)."
		)
	return sentence


def _price_profile(profile, entries, country):
	"""Price one profile's items. Returns (line, zone, reason, sentence); `reason` is None when it is priced."""
	who = _words(profile.get("name")) or "A profile with no name"
	kilos, guessed = _group_weight(entries)
	pounds = round(min(kilos * LB_PER_KG, _BIGGEST), 3)
	line = {
		"profile": _words(profile.get("name")),
		"zone": "",
		"weight_lb": pounds,
		"price": None,
		"tier": None,
		"items": len(entries),
		"defaulted_weight": guessed > 0,
	}
	zone = _zone_for(profile, country)
	if zone is None:
		return (
			line,
			None,
			"not_covered",
			f"{who} has no zone for {country} in the rate card, so those items cannot be delivered there.",
		)
	line["zone"] = _words(zone.get("name"))
	tiers = _usable_tiers(zone)
	if tiers is None:
		return (
			line,
			zone,
			"not_covered",
			f'Zone "{line["zone"]}" of {who} has no usable prices, so those items cannot be priced.',
		)
	chosen = _choose_tier(tiers, pounds)
	if chosen is None:
		return (
			line,
			zone,
			"too_heavy",
			f'{who} to {country}: {lb_text(pounds)} lb is heavier than the highest tier of zone "{line["zone"]}" '
			f"({lb_text(tiers[-1][2])} lb), so the order cannot be priced.",
		)
	line["price"], line["tier"] = float(_cents(chosen[3])) + 0.0, chosen[0]
	return (
		line,
		zone,
		None,
		_tier_sentence(who, country, line["zone"], pounds, tiers, chosen, guessed, len(entries)),
	)


def _zone_days(zone):
	days = zone.get("days")
	return days if isinstance(days, int) and not isinstance(days, bool) and 0 <= days <= MAX_DAYS else None


def _offer_name(promises):
	"""(name, days) of the delivery method for [(method, days)] in profile order: the method they share, or
	else the method of the slowest profile (the first when they tie); the days are the largest."""
	longest = max((days for _, days in promises if days is not None), default=None)
	if len({method for method, _ in promises}) == 1:
		return promises[0][0], longest
	return next((method, longest) for method, days in promises if days == longest)


def _left_out(count):
	if count == 1:
		return "1 item matches no profile and was left out of the price."
	return f"{count} items match no profile and were left out of the price."


def price_card(card, order) -> dict:
	"""What an order pays for delivery, from the card. `order` is {"country_code", "items": [{"index_key",
	"weight_kg" (a unit weight, may be missing), "qty"}]}. The result is

		{"priced": bool, "reason": None | "not_covered" | "too_heavy" | "not_offered",
		 "total": float, "name": str, "days": int | None, "lines": [line], "detail": str}
		line = {"profile", "zone", "weight_lb", "price", "tier", "items", "defaulted_weight"}

	`lines` has one line for each profile with items, in the card's order. `tier` is the chosen tier's place
	(from 1) in the zone's own list and `items` the number of items in the line. When the order cannot be
	priced, `priced` is False, `total` is 0.0, `name` is "" and `days` is None; `reason` comes from the first
	profile that failed, `lines` holds the lines before it and then that one with `price` and `tier` None, and
	`detail` says why. `detail` is for the staff, not the customer. Never raises; the steps are in the module
	docstring. Nothing passed in is changed.
	"""
	profiles = _as_list(_as_dict(card).get("profiles"))
	if not any(isinstance(profile, dict) for profile in profiles):
		return _refused("not_covered", "There is no rate card.")
	order = _as_dict(order)
	items = _order_items(order)
	if not items:
		return _refused(
			"not_offered", "The order has no item with a quantity above zero, so there is nothing to price."
		)
	country = _country(order.get("country_code"))
	if not country:
		return _refused("not_covered", "The destination country is not a two-letter code.")
	groups, left_out = {}, 0
	for entry in items:
		position = classify_item(card, entry["key"])
		if position is None:
			left_out += 1
		else:
			groups.setdefault(position, []).append(entry)
	if not groups:
		return _refused(
			"not_offered",
			"No item of the order matches a profile of the rate card, so there is nothing to price.",
		)
	lines, sentences, promises, total = [], [], [], Decimal(0)
	for position in sorted(groups):
		line, zone, reason, sentence = _price_profile(profiles[position], groups[position], country)
		lines.append(line)
		sentences.append(sentence)
		if reason is not None:
			return _refused(reason, " ".join(sentences), lines)
		promises.append((_words(zone.get("method")), _zone_days(zone)))
		total += Decimal(str(line["price"]))
	if left_out:
		sentences.append(_left_out(left_out))
	name, days = _offer_name(promises)
	amount = float(_cents(total)) + 0.0
	sentences.append(f"The delivery price is ${amount:.2f}.")
	return {
		"priced": True,
		"reason": None,
		"total": amount,
		"name": name,
		"days": days,
		"lines": lines,
		"detail": " ".join(sentences),
	}
