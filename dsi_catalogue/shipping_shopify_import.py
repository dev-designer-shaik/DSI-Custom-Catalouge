"""Shopify CSV reader and conversion to ordinary product groups and country rows."""

import csv
import io
import math
import re
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


def policy_from_export(config_csv, not_delivered_csv=None):
	parsed = parse_shopify_export(config_csv, not_delivered_csv)
	zones, sets, rules, held = [], [], [], {}
	for profile in parsed["card"]["profiles"]:
		name = "" if "*" in profile.get("match", []) else profile["name"]
		if name:
			sets.append({"name": name, "items": [], "item_groups": [], "patterns": profile.get("match", [])})
		for zone in profile["zones"]:
			key = (zone["name"], tuple(sorted(zone["countries"])))
			if key not in held:
				zname = zone["name"]
				if any(z["name"] == zname for z in zones):
					zname += " (" + profile["name"] + ")"
				held[key] = zname
				zones.append(
					{
						"name": zname,
						"enabled": True,
						"countries": zone["countries"],
						"not_shipped": False,
						"customer_line": "",
						"not_offered_message": "",
					}
				)
			rules.append(
				{
					"name": None,
					"zone": held[key],
					"item_set": name or None,
					"services": [],
					"method": "table",
					"price_table": [{"up_to_lb": t["to"], "price": t["price"]} for t in zone["tiers"]],
					"label": re.sub(r"\s*\([^)]*days?\)\s*", "", zone.get("method", ""), flags=re.I).strip(),
					"days": zone.get("days"),
				}
			)
	if parsed["not_delivered"]:
		zones.append(
			{
				"name": "Not delivered",
				"enabled": True,
				"countries": parsed["not_delivered"],
				"not_shipped": True,
				"customer_line": "",
				"not_offered_message": "We do not deliver to this country.",
			}
		)
	return {
		"active": False,
		"fallback": {"method": "no_delivery", "amount": 0},
		"zones": zones,
		"item_sets": sets,
		"rules": rules,
		"notes": parsed["notes"],
		"summary": {
			"sets": len(sets) + int(any(not r["item_set"] for r in rules)),
			"rows": len(rules),
			"zones": len(zones),
			"tiers": sum(len(r["price_table"]) for r in rules),
		},
	}


def normalize_index_key(value):
	return value.strip().strip("{}").upper() if isinstance(value, str) else ""
