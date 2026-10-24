"""Shaping the data around the shipping policy: what a guest posts, what the database holds, what a
customer may see, and what a draft would change.

Pure Python with no Frappe import, so it is tested standalone. The ERP layer reads rows and request
arguments and hands them to these functions; `shipping_engine` prices an order from the plain dicts
that come out. The only thing taken from the engine is `LABELS`, the Select strings the doctypes store.
The policy shape is the one `shipping_engine` documents. The rows are the doctypes' own fields:

	policy row = {"active", "price_source", "rate_card", "not_covered", "default_fallback_method",
	              "default_fallback_amount",
	              "rules": [{"name"?, "enabled", "label", "zone", "item_set", "min_order_value",
	                         "max_order_value", "min_weight_kg", "max_weight_kg", "min_volume_l",
	                         "max_volume_l", "method", "formula", "round_up"}]}
	zone row   = {"zone_name", "enabled", "not_shipped", "everywhere_else", "countries": [{"country"}],
	              "customer_line", "not_offered_message", "fallback_method", "fallback_amount"}
	set row    = {"set_name", "item_groups": [{"item_group"}], "items": [{"item"}]}

Four jobs:

1. `clean_items`, `clean_boxes` and `clean_quotes` check what a guest posts. They return plain data with
   plain floats, or raise `InputError` with a sentence that is safe to show in a customer's browser (it
   names the field and never repeats what was sent). Nothing else is allowed through: unknown keys are
   dropped, a number that is negative, not finite or above a billion is refused, and so is an id or a name
   that is not plain text (a control character, NUL or a lone surrogate: JSON can carry them, but nothing
   downstream can write them).
2. `policy_from_rows` and `rows_from_policy` turn database rows into the policy and back. Select labels
   become the engine's codes (an unknown label passes through unchanged, for the engine to fail closed
   on and `validate_policy` to report), Country names become upper-case ISO codes, and the rate card is
   kept as JSON text in its row and is a dict in the policy. Neither raises on odd content: it is read as
   empty (a card that is not JSON is no card), or left for `validate_policy` to report.
3. `public_summary_from` and `response_from` shape what a customer is told: the zones, and the answer to
   one order. Both build their output field by field, so nothing else can reach a customer, and nothing
   of the rate card does: only the price it gave.
4. `snapshot_version` names a policy's content, and `diff_policy` lists what a draft would change.
"""

import copy
import hashlib
import json
import math
from decimal import Decimal

from dsi_catalogue.shipping_engine import LABELS

MAX_ITEMS = 200
MAX_BOXES = 50
MAX_QUOTES = 20
MAX_NUMBER = 10**9  # the most a guest may post for a quantity, a weight, a volume or an amount
MAX_TEXT = 140  # the longest item code, service id or service name

_LIMITS = (
	"min_order_value",
	"max_order_value",
	"min_weight_kg",
	"max_weight_kg",
	"min_volume_l",
	"max_volume_l",
)
_OFFER_KEYS = ("service_id", "name", "amount", "source", "reason", "estimated_days")
# in alphabetical order, as they are reported
_SETTINGS = ("active", "default_fallback", "not_covered", "price_source", "rate_card")

# The code behind each Select label, the other way round.
_LABEL_OF_CODE = {kind: {code: label for label, code in labels.items()} for kind, labels in LABELS.items()}


class InputError(ValueError):
	"""Input that may not be accepted. The message is a plain sentence, safe to show in a browser."""


# ---------------------------------------------------------------- what a guest posts
def parse_json_arg(value, default):
	"""A request argument as a Python value: empty (None, "" or white space) is `default`, text is read as
	JSON, and anything else is already a value and comes back as it is.

	Raises InputError when the text is not JSON that Python can read.
	"""
	if value is None:
		return default
	if not isinstance(value, str):
		return value
	if not value.strip():
		return default
	try:
		return json.loads(value)
	except (ValueError, RecursionError):  # bad JSON, an integer of over 4300 digits, nesting too deep
		raise InputError("The value is not valid JSON.") from None


def _entries(value, plural, limit):
	"""The list a guest posted (as a list or as JSON text), after checking its size. None is an empty list."""
	try:
		entries = parse_json_arg(value, None)
	except InputError:
		raise InputError(f"The {plural} are not valid JSON.") from None
	if entries is None:
		return []
	if not isinstance(entries, list):
		raise InputError(f"The {plural} have to be a list.")
	if len(entries) > limit:
		raise InputError(f"There are too many {plural}: at most {limit} are allowed.")
	return entries


def _clean(value, plural, singular, limit, clean_entry):
	cleaned = []
	for position, entry in enumerate(_entries(value, plural, limit), start=1):
		who = f"{singular} {position}"
		if not isinstance(entry, dict):
			raise InputError(f"{who} has to be an object.")
		cleaned.append(clean_entry(entry, who))
	return cleaned


def _is_amount(value, above_zero):
	"""A real number from 0 (or above 0) up to MAX_NUMBER. A bool is no number here. A NaN and the infinities
	fail the range check, whichever way it is written. An int is compared as it is: `float(10**400)` would
	overflow."""
	if isinstance(value, bool) or not isinstance(value, (int, float)):
		return False
	return 0 < value <= MAX_NUMBER if above_zero else 0 <= value <= MAX_NUMBER


def _number(entry, field, who, *, above_zero=False):
	value = entry.get(field)
	if not _is_amount(value, above_zero):
		bounds = f"above 0 and at most {MAX_NUMBER}" if above_zero else f"from 0 to {MAX_NUMBER}"
		raise InputError(f"{who}: {field} has to be a number {bounds}.")
	return float(value) + 0.0  # the + 0.0 turns -0.0 into 0.0


def _boxes_quoted(entry, who):
	"""How many cartons the service quoted: a whole number from 0 to MAX_BOXES, 0 when it is not given."""
	value = entry.get("boxes_quoted")
	if value is None:
		return 0
	# 1.0 is a whole number; 1.5, NaN and infinity are not
	if isinstance(value, float) and value.is_integer():
		value = int(value)
	if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_BOXES:
		raise InputError(f"{who}: boxes_quoted has to be a whole number from 0 to {MAX_BOXES}.")
	return value


def _is_plain(text):
	"""Text a person could have typed: printable (so no control character, no NUL and no lone surrogate) and
	writable as UTF-8. A space inside is fine, and so is any script: an Arabic name is plain text.

	JSON can carry a lone surrogate ("\\ud800"), a text Python holds but nothing can write: the database
	driver and Frappe's JSON encoder both raise on it, and an id or a name comes back in the response.
	(`isprintable` already refuses every lone surrogate; the encoding is tried as well, so that the point of
	the check does not rest on how `isprintable` treats one.)
	"""
	try:
		text.encode("utf-8")
	except UnicodeEncodeError:
		return False
	return text.isprintable()


def _identifier(entry, field, who):
	value = entry.get(field)
	text = value.strip() if isinstance(value, str) else ""
	if not text:
		raise InputError(f"{who}: {field} has to be text that is not empty.")
	if len(text) > MAX_TEXT:
		raise InputError(f"{who}: {field} is longer than {MAX_TEXT} characters.")
	if not _is_plain(text):
		raise InputError(f"{who}: {field} has to be plain text.")
	return text


def _service_name(entry, service_id, who):
	value = entry.get("name")
	if value is None:
		return service_id
	if not isinstance(value, str):
		raise InputError(f"{who}: name has to be text.")
	name = value.strip()
	if len(name) > MAX_TEXT:
		raise InputError(f"{who}: name is longer than {MAX_TEXT} characters.")
	if not _is_plain(name):
		raise InputError(f"{who}: name has to be plain text.")
	return name or service_id


def _item(entry, who):
	return {
		"item_code": _identifier(entry, "item_code", who),
		"qty": _number(entry, "qty", who, above_zero=True),
	}


def _box(entry, who):
	return {
		"weight_kg": _number(entry, "weight_kg", who),
		"volume_l": _number(entry, "volume_l", who),
	}


def _quote(entry, who):
	service_id = _identifier(entry, "service_id", who)
	return {
		"service_id": service_id,
		"name": _service_name(entry, service_id, who),
		"amount": _number(entry, "amount", who),
		"boxes_quoted": _boxes_quoted(entry, who),
	}


def clean_items(items) -> list[dict]:
	"""The order's items as [{"item_code", "qty"}]: a list or JSON text of one, at most MAX_ITEMS long.

	The item code is stripped plain text of 1 to 140 characters (see `_is_plain`), the quantity a number above
	0 and at most a billion (0.5 is allowed). Other keys are dropped. Raises InputError for anything else.
	"""
	return _clean(items, "items", "Item", MAX_ITEMS, _item)


def clean_boxes(boxes) -> list[dict]:
	"""The packed cartons as [{"weight_kg", "volume_l"}]: a list or JSON text of one, at most MAX_BOXES
	long. Both measures are numbers from 0 to a billion. Other keys are dropped. Raises InputError otherwise.
	"""
	return _clean(boxes, "boxes", "Box", MAX_BOXES, _box)


def clean_quotes(quotes) -> list[dict]:
	"""The carrier's quotes as [{"service_id", "name", "amount", "boxes_quoted"}]: a list or JSON text of
	one, at most MAX_QUOTES long.

	The service id is stripped plain text of 1 to 140 characters. The name is stripped plain text of at most
	140 characters and is the service id when empty or missing. The amount is a number from 0 to a billion.
	`boxes_quoted` is a whole number of cartons from 0 to MAX_BOXES (1.0 is read as 1) and is 0 when
	missing. Other keys are dropped. Raises InputError for anything else.
	"""
	return _clean(quotes, "quotes", "Quote", MAX_QUOTES, _quote)


# ---------------------------------------------------------------- reading loose data
def _dict(value):
	return value if isinstance(value, dict) else {}


def _list(value):
	return value if isinstance(value, (list, tuple)) else []


def _text(value):
	"""A text field as text: None is empty."""
	if value is None:
		return ""
	return value if isinstance(value, str) else str(value)


def _flag(value):
	"""A Check field of a database row as a bool, read the way Frappe's `cint` reads it: a number other than 0,
	or text holding one, is on. None, other text, a NaN and anything unreadable are off, which is the safe
	side. This is for rows read from the database; a policy's flags are read by `_check`."""
	try:
		return int(float(value)) != 0
	except (TypeError, ValueError, OverflowError):
		return False


def _check(value):
	"""A flag of a policy as the 1 or 0 a Check field holds, read the way the engine reads it (`not
	policy.get("active")`): anything Python counts as true is 1, so "yes", "0" and 0.5 are 1, and "", None and
	[] are 0. A draft therefore saves with the flags it previewed with. (A database row is read the other way
	round, by `_flag`.)"""
	return 1 if value else 0


def _float(value):
	"""A Float field as a float: None is 0.0. What is not a number is left as it is, for `validate_policy` to
	report."""
	if value is None:
		return 0.0
	if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
		return value
	try:
		return float(value)
	except (OverflowError, ValueError):
		return value


def is_not_finite(value) -> bool:
	"""Is `value` what Frappe reads as a number (a number, or text holding one, commas aside) that is NaN, an
	infinity or too big for a float? Rounded to the decimals of its field such a number becomes 0 without a
	word (Frappe's `flt` reads a number it cannot round as 0), so the doctypes refuse it before they round.
	What Frappe does not read as a number at all (None, other text) is not one of these."""
	if isinstance(value, str):
		value = value.replace(",", "")
	try:
		return not math.isfinite(float(value))
	except OverflowError:  # an integer too big for a float
		return True
	except (TypeError, ValueError):
		return False


def _to_code(kind, label):
	"""The engine's code for a Select label. A label that is not one of the doctype's passes through."""
	label = _text(label)
	return LABELS[kind].get(label, label)


def _to_label(kind, code):
	"""The Select label for an engine code. A code that has no label passes through."""
	code = _text(code)
	return _LABEL_OF_CODE[kind].get(code, code)


def _texts(values):
	"""The values that are not empty, as text."""
	return [text for text in (_text(value) for value in values) if text]


def _column(rows, field):
	"""One field of the rows of a child table, as text, leaving out the empty ones."""
	return _texts(row.get(field) for row in _list(rows) if isinstance(row, dict))


def _codes(countries):
	"""Country codes written loosely as upper-case codes, leaving out the empty ones."""
	return [text.upper() for text in (code.strip() for code in _texts(_list(countries))) if text]


def _no_json_word(word):
	raise ValueError(f"{word} is not JSON")


def _finite_float(text):
	number = float(text)
	if not math.isfinite(number):
		raise ValueError(f"{text} is too big for a number")
	return number


def read_json(text):
	"""JSON text as Python data, read the way a browser reads it: NaN, Infinity and -Infinity, which Python's
	own reader takes, raise ValueError like anything else that is not JSON, and so does a number too big for a
	float (1e999, which Python would read as an infinity), an integer of over 4,300 digits too; nesting too
	deep raises RecursionError. The stored rate card is read with it, by the loader and by the policy's own
	check, so a card holding one is no card, never a number a browser cannot read."""
	return json.loads(text, parse_constant=_no_json_word, parse_float=_finite_float)


def _card_from(value):
	"""The rate card of a row: the dict that a Code field's JSON text holds, or a dict already. Text that is
	not JSON (`read_json`: NaN and the infinities are not), JSON that is not an object, and anything else are
	no card (None)."""
	try:
		if isinstance(value, dict):
			return copy.deepcopy(value)
		if not isinstance(value, str) or not value.strip():
			return None
		card = read_json(value)
	except (ValueError, RecursionError, TypeError):
		# not JSON (NaN included), an integer of over 4300 digits, nesting too deep, or a dict that cannot be
		# copied
		return None
	return card if isinstance(card, dict) else None


def _upper_keys(names):
	"""A map of code to Country name, keyed by upper-case code: the ERP stores country codes in lower case."""
	return {_text(code).upper(): name for code, name in _dict(names).items()}


# ---------------------------------------------------------------- database rows -> policy
def _code_of(country, codes):
	"""The upper-case ISO code of a Country name, or "" when the map does not give one."""
	code = codes.get(country)
	return _text(code).strip().upper() if code else ""


def _zone_from_row(row, codes):
	countries = (_code_of(country, codes) for country in _column(row.get("countries"), "country"))
	return {
		"name": _text(row.get("zone_name")),
		"enabled": _flag(row.get("enabled")),
		"countries": [code for code in countries if code],
		"not_shipped": _flag(row.get("not_shipped")),
		"everywhere_else": _flag(row.get("everywhere_else")),
		"customer_line": _text(row.get("customer_line")),
		"not_offered_message": _text(row.get("not_offered_message")),
		"fallback": {
			"method": _to_code("zone_fallback", row.get("fallback_method")),
			"amount": _float(row.get("fallback_amount")),
		},
	}


def _set_from_row(row):
	return {
		"name": _text(row.get("set_name")),
		"item_groups": _column(row.get("item_groups"), "item_group"),
		"items": _column(row.get("items"), "item"),
	}


def _rule_from_row(row):
	return {
		"name": _text(row.get("name")) or None,
		"enabled": _flag(row.get("enabled")),
		"label": _text(row.get("label")),
		"zone": _text(row.get("zone")),
		"item_set": _text(row.get("item_set")) or None,
		**{key: _float(row.get(key)) for key in _LIMITS},
		"method": _to_code("method", row.get("method")),
		"formula": _text(row.get("formula")),
		"round_up": _flag(row.get("round_up")),
	}


def policy_from_rows(
	policy_row: dict, zone_rows: list[dict], set_rows: list[dict], country_codes: dict[str, str]
) -> dict:
	"""The policy (in the shape `shipping_engine` documents) from the rows of the three doctypes.

	`policy_row` is the Shipping Policy single with its `rules` rows, `zone_rows` the Shipping Zones with
	their `countries` rows, `set_rows` the Shipping Item Sets with their `item_groups` and `items` rows,
	all in the order they are to be kept. `country_codes` maps a Country name to its ISO code; the codes
	in the policy are upper case, and a Country name the map has no code for is left out of its zone.

	Flags become bools, an empty number 0.0, an empty text "", and an empty `item_set` None. A rule keeps
	its row `name` (None when it has none). Select labels become codes; a label that is not one of the
	doctype's passes through unchanged. The row's `rate_card` text becomes the card (a dict), or None when
	there is none or the text is not a JSON object. It does not raise on odd content.
	"""
	policy_row, codes = _dict(policy_row), _dict(country_codes)
	return {
		"active": _flag(policy_row.get("active")),
		"price_source": _to_code("price_source", policy_row.get("price_source")),
		"rate_card": _card_from(policy_row.get("rate_card")),
		"not_covered": _to_code("not_covered", policy_row.get("not_covered")),
		"default_fallback": {
			"method": _to_code("default_fallback", policy_row.get("default_fallback_method")),
			"amount": _float(policy_row.get("default_fallback_amount")),
		},
		"zones": [_zone_from_row(row, codes) for row in _list(zone_rows) if isinstance(row, dict)],
		"item_sets": [_set_from_row(row) for row in _list(set_rows) if isinstance(row, dict)],
		"rules": [_rule_from_row(row) for row in _list(policy_row.get("rules")) if isinstance(row, dict)],
	}


# ---------------------------------------------------------------- policy -> database rows
def _zone_row(zone, names):
	fallback = _dict(zone.get("fallback"))
	return {
		"zone_name": _text(zone.get("name")),
		"enabled": _check(zone.get("enabled")),
		"not_shipped": _check(zone.get("not_shipped")),
		"everywhere_else": _check(zone.get("everywhere_else")),
		"countries": [{"country": names.get(code, code)} for code in _codes(zone.get("countries"))],
		"customer_line": _text(zone.get("customer_line")),
		"not_offered_message": _text(zone.get("not_offered_message")),
		"fallback_method": _to_label("zone_fallback", fallback.get("method")),
		"fallback_amount": _float(fallback.get("amount")),
	}


def _set_row(item_set):
	return {
		"set_name": _text(item_set.get("name")),
		"item_groups": [{"item_group": group} for group in _texts(_list(item_set.get("item_groups")))],
		"items": [{"item": code} for code in _texts(_list(item_set.get("items")))],
	}


def _rule_row(rule):
	row = {
		"enabled": _check(rule.get("enabled")),
		"label": _text(rule.get("label")),
		"zone": _text(rule.get("zone")),
		"item_set": _text(rule.get("item_set")) or None,
		**{key: _float(rule.get(key)) for key in _LIMITS},
		"method": _to_label("method", rule.get("method")),
		"formula": _text(rule.get("formula")),
		"round_up": _check(rule.get("round_up")),
	}
	name = _text(rule.get("name"))
	return {"name": name, **row} if name else row


def _card_text(card):
	"""The rate card as the JSON text its field holds, written to be read in the form: indented, keys in
	order, letters as they are. No card, and a value that is not a card, are "". A card JSON cannot hold (a
	loop, keys of two kinds, a NaN or an infinity, which Python would write as words that are not JSON and a
	browser refuses) is "" too, so that writing never raises."""
	if not isinstance(card, dict):
		return ""
	try:
		return json.dumps(card, ensure_ascii=False, sort_keys=True, indent=1, default=str, allow_nan=False)
	except (TypeError, ValueError, RecursionError):
		return ""


def rows_from_policy(policy: dict, country_names: dict[str, str]) -> dict:
	"""The rows of the three doctypes for a policy: {"policy": policy row (with its `rules` rows),
	"zones": [zone row], "item_sets": [set row]}. The exact inverse of `policy_from_rows`.

	`country_names` maps an ISO code (in either case) to its Country name; a code it has no name for is
	written as the code, so that saving the row fails loudly rather than the country being lost quietly.
	Flags become the 1 and 0 a database holds, read as the engine reads them (anything Python counts as true
	is 1), Select codes become their labels (a code that has no label is written as it is), the rate card
	becomes JSON text, and a rule row has a `name` only when the rule has one. It does not raise on odd
	content.
	"""
	policy, names = _dict(policy), _upper_keys(country_names)
	fallback = _dict(policy.get("default_fallback"))
	return {
		"policy": {
			"active": _check(policy.get("active")),
			"price_source": _to_label("price_source", policy.get("price_source")),
			"rate_card": _card_text(policy.get("rate_card")),
			"not_covered": _to_label("not_covered", policy.get("not_covered")),
			"default_fallback_method": _to_label("default_fallback", fallback.get("method")),
			"default_fallback_amount": _float(fallback.get("amount")),
			"rules": [_rule_row(rule) for rule in _list(policy.get("rules")) if isinstance(rule, dict)],
		},
		"zones": [_zone_row(zone, names) for zone in _list(policy.get("zones")) if isinstance(zone, dict)],
		"item_sets": [_set_row(entry) for entry in _list(policy.get("item_sets")) if isinstance(entry, dict)],
	}


# ---------------------------------------------------------------- what a customer is told
def _say(text, translate):
	"""`text` in the customer's language, or "" when there is nothing to say."""
	text = _text(text)
	return translate(text) if text.strip() else ""


def public_summary_from(policy: dict, country_names: dict[str, str], translate) -> dict:
	"""What the FAQ and the Shipping page may show: {"active": False, "zones": []} while the policy is off,
	otherwise {"active": True, "zones": [{"name", "countries": [{"code", "name"}], "customer_line",
	"delivers"}]}.

	Only enabled zones are listed, in the policy's order. `delivers` is False for a Not shipped zone. An
	Everywhere-else zone lists no countries. `country_names` maps a code to its Country name (the code
	itself is shown when there is none) and `translate(text)` puts a customer line in the customer's
	language. No rule, formula, fallback, amount or message is part of it: each field is picked by name.
	"""
	policy = _dict(policy)
	if not policy.get("active"):  # as `resolve` reads it
		return {"active": False, "zones": []}
	names = _upper_keys(country_names)
	zones = []
	for zone in _list(policy.get("zones")):
		if not isinstance(zone, dict) or not zone.get("enabled"):
			continue
		codes = [] if zone.get("everywhere_else") else _codes(zone.get("countries"))
		zones.append(
			{
				"name": _text(zone.get("name")),
				"countries": [{"code": code, "name": names.get(code, code)} for code in codes],
				"customer_line": _say(zone.get("customer_line"), translate),
				"delivers": not zone.get("not_shipped"),
			}
		)
	return {"active": True, "zones": zones}


def response_from(result: dict, translate) -> dict:
	"""What a guest is told about one order, from the engine's result: {"policy_active", "price_source",
	"zone", "offered", "reason", "message", "offers": [{"service_id", "name", "amount", "source", "reason",
	"estimated_days"}]}.

	`price_source` is "rate_card" or "carrier" (the carrier for anything else). `message` is put through
	`translate(text)` when there is one. The result's `rule` position and each offer's `trace` are not copied:
	they hold formulas, the lines of the rate card and the policy's inner workings. Each field is picked by
	name, so nothing else can reach the guest, and nothing of the rate card but the price it gave.
	"""
	result = _dict(result)
	return {
		"policy_active": bool(result.get("policy_active")),
		"price_source": "rate_card" if result.get("price_source") == "rate_card" else "carrier",
		"zone": result.get("zone"),
		"offered": bool(result.get("offered")),
		"reason": result.get("reason"),
		"message": _say(result.get("message"), translate),
		"offers": [
			{key: offer.get(key) for key in _OFFER_KEYS}
			for offer in _list(result.get("offers"))
			if isinstance(offer, dict)
		],
	}


# ---------------------------------------------------------------- versions and changes
def snapshot_version(policy: dict) -> str:
	"""A name for the policy's content: the first 16 hex characters of the SHA-256 of its compact JSON with
	sorted keys. The order of the keys does not matter; the order of a list does (rules are ordered)."""
	text = json.dumps(policy, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
	return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _by_name(entries):
	"""The dict entries of a list by name (the first when a name is repeated; a missing name is "")."""
	named = {}
	for entry in _list(entries):
		if isinstance(entry, dict):
			named.setdefault(_text(entry.get("name")), entry)
	return named


def _diff_named(current, draft):
	before, after = _by_name(current), _by_name(draft)
	return {
		"added": sorted(after.keys() - before.keys()),
		"removed": sorted(before.keys() - after.keys()),
		"changed": sorted(name for name in before.keys() & after.keys() if before[name] != after[name]),
	}


def _rule_entry(position, rule):
	return {
		"index": position,
		"name": _text(rule.get("name")) or None,
		"label": _text(rule.get("label")),
		"zone": _text(rule.get("zone")),
	}


def _is_the_rule(old, new):
	"""Is `old`, a rule of the current policy, the rule `new` of the draft? A named rule is the one with the
	same name. A rule with no name has no identity: only an identical rule with no name is the same."""
	name = _text(new.get("name"))
	return _text(old.get("name")) == name if name else old == new


def _diff_rules(current, draft):
	unpaired = {position: rule for position, rule in enumerate(_list(current)) if isinstance(rule, dict)}
	paired = []  # (position in the current policy, position in the draft) of each rule found in both
	added, changed = [], []
	for position, rule in enumerate(_list(draft)):
		if not isinstance(rule, dict):
			continue
		old = next((place for place, candidate in unpaired.items() if _is_the_rule(candidate, rule)), None)
		if old is None:
			added.append(_rule_entry(position, rule))
			continue
		paired.append((old, position))
		if unpaired.pop(old) != rule:
			changed.append(_rule_entry(position, rule))
	removed = [_rule_entry(position, rule) for position, rule in unpaired.items()]
	# where the rules found in both sit in the draft, listed in the order they have in the current policy
	order = [new for _, new in sorted(paired)]
	return {"added": added, "removed": removed, "changed": changed, "order_changed": order != sorted(order)}


def _setting(policy, key):
	if key == "active":
		return bool(policy.get(key))
	if key == "price_source":  # a policy that does not say prices from the carrier, as the engine reads it
		value = policy.get(key)
		return "carrier" if value is None or value == "" else value
	if key == "rate_card":  # what is not a card is no card
		card = policy.get(key)
		return card if isinstance(card, dict) else None
	return policy.get(key)


def _diff_card(current, draft):
	"""The rate card's profiles that a draft adds, removes and changes, by name."""
	return _diff_named(
		_dict(current.get("rate_card")).get("profiles"), _dict(draft.get("rate_card")).get("profiles")
	)


def diff_policy(current: dict, draft: dict) -> dict:
	"""What applying `draft` over `current` would change. Neither is altered. The result is

		{"zones":     {"added": [name], "removed": [name], "changed": [name]},
		 "item_sets": {"added": [name], "removed": [name], "changed": [name]},
		 "rules":     {"added": [entry], "removed": [entry], "changed": [entry], "order_changed": bool},
		 "rate_card": {"added": [name], "removed": [name], "changed": [name]},
		 "settings":  [key]}

	Zones and item sets are identified by `name`; the name lists are sorted. "Changed" means the same
	name with different content (the order of the countries or items counts), so a rename is one removed
	and one added.

	Rules are identified by `name` (the rule row's name in the database). Each rule entry is
	{"index": int, "name": str | None, "label": str, "zone": str}, listed in order of `index`. `index` is
	the rule's position (from 0) in the draft for added and changed rules, and in the current policy for
	removed ones. A rule whose name is missing or not in the current policy counts as added; so does a
	second rule with a name already taken. The exception is a rule with no name that is identical to a
	rule with no name in the current policy: it is the same rule (one for one). "Changed" means the
	same name with different content. `order_changed` is True when the rules present in both sit in a
	different relative order; adding, removing or editing rules does not change it.

	The rate card's profiles are identified by `name`, like zones: `rate_card` lists, sorted, the profiles the
	draft's card adds and removes, and those it changes (the same name with other patterns, zones or tiers).
	A card that is missing or not a card has no profiles. What the lists cannot show, such as the profiles
	trading places (the first one that matches an item takes it), is in `settings`.

	`settings` lists, sorted, the top-level settings among `active`, `default_fallback`, `not_covered`,
	`price_source` and `rate_card` whose value differs; a policy that does not say what its price source is
	prices from the carrier. Identical policies give empty lists and False.
	"""
	current, draft = _dict(current), _dict(draft)
	return {
		"zones": _diff_named(current.get("zones"), draft.get("zones")),
		"item_sets": _diff_named(current.get("item_sets"), draft.get("item_sets")),
		"rules": _diff_rules(current.get("rules"), draft.get("rules")),
		"rate_card": _diff_card(current, draft),
		"settings": [key for key in _SETTINGS if _setting(current, key) != _setting(draft, key)],
	}
