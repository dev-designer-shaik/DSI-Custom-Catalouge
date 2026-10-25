"""Input bounds, storage mapping and public responses for one-page shipping."""

import copy
import hashlib
import json
import math
from decimal import Decimal

from dsi_catalogue.shipping_engine import LABELS

MAX_ITEMS, MAX_BOXES, MAX_QUOTES, MAX_NUMBER, MAX_TEXT = 200, 50, 20, 10**9, 140
_LABEL_OF_CODE = {k: {v: label for label, v in values.items()} for k, values in LABELS.items()}


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


def is_not_a_number(value) -> bool:
	"""Would Frappe keep `value`, given for a Float field, as 0 without a word? Frappe's `flt` reads it as
	`float` does, with the commas taken out of text, and keeps 0 for what it cannot read or cannot round to
	the field's decimals. So these are not numbers: NaN, an infinity or an integer too big for a float, as a
	number or as text, and text that is not blank and not a number ("abc"). The doctypes refuse them before
	they round. Text holding a number ("1,000.5"), blank text and None (a field left empty: 0, or no limit)
	are not refused; nor is any other value, which this does not look at."""
	if isinstance(value, str):
		value = value.replace(",", "")
		if not value.strip():
			return False
		try:
			return not math.isfinite(float(value))
		except ValueError:  # text that is not a number
			return True
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


def _upper_keys(names):
	"""A map of code to Country name, keyed by upper-case code: the ERP stores country codes in lower case."""
	return {_text(code).upper(): name for code, name in _dict(names).items()}


def _json_list(value):
	try:
		value = read_json(value) if isinstance(value, str) else value
	except (ValueError, TypeError, RecursionError):
		return []
	return value if isinstance(value, list) else []


def policy_from_rows(policy_row, zone_rows, set_rows, country_codes):
	return {
		"active": _flag(policy_row.get("active")),
		"fallback": {
			"method": _to_code("fallback", policy_row.get("fallback_method") or "Don't offer delivery"),
			"amount": _float(policy_row.get("fallback_amount")),
		},
		"zones": [
			{
				"name": z.get("zone_name") or z.get("name"),
				"enabled": _flag(z.get("enabled")),
				"countries": [country_codes.get(c["country"], "").upper() for c in z.get("countries", [])],
				"not_shipped": _flag(z.get("not_shipped")),
				"customer_line": z.get("customer_line") or "",
				"not_offered_message": z.get("not_offered_message") or "",
			}
			for z in zone_rows
		],
		"item_sets": [
			{
				"name": g.get("set_name") or g.get("name"),
				"item_groups": _column(g.get("item_groups"), "item_group"),
				"items": _column(g.get("items"), "item"),
				"patterns": _column(g.get("patterns"), "pattern"),
			}
			for g in set_rows
		],
		"rules": [
			{
				"name": r.get("name"),
				"zone": r.get("zone") or "",
				"item_set": r.get("item_set") or None,
				"services": _json_list(r.get("services")),
				"method": _to_code("method", r.get("method")),
				"formula": r.get("formula") or "",
				"price_table": _json_list(r.get("price_table")),
				"label": r.get("label") or "",
				"days": r.get("days") or None,
			}
			for r in policy_row.get("rules", [])
		],
	}


def rows_from_policy(policy, country_names):
	names = _upper_keys(country_names)
	return {
		"policy": {
			"active": int(bool(policy.get("active"))),
			"fallback_method": _to_label("fallback", policy["fallback"]["method"]),
			"fallback_amount": policy["fallback"].get("amount", 0),
			"rules": [
				{
					**{k: r.get(k) for k in ("name", "zone", "item_set", "label", "days", "formula")},
					"method": _to_label("method", r["method"]),
					"services": json.dumps(r.get("services", [])),
					"price_table": json.dumps(r.get("price_table", [])),
				}
				for r in policy["rules"]
			],
		},
		"zones": [
			{
				"zone_name": z["name"],
				"enabled": int(z.get("enabled", True)),
				"countries": [{"country": names.get(c.upper(), c)} for c in z["countries"]],
				"not_shipped": int(bool(z.get("not_shipped"))),
				"customer_line": z.get("customer_line", ""),
				"not_offered_message": z.get("not_offered_message", ""),
			}
			for z in policy["zones"]
		],
		"item_sets": [
			{
				"set_name": g["name"],
				"priority": position,
				"item_groups": [{"item_group": v} for v in g.get("item_groups", [])],
				"items": [{"item": v} for v in g.get("items", [])],
				"patterns": [{"pattern": v} for v in g.get("patterns", [])],
			}
			for position, g in enumerate(policy["item_sets"])
		],
	}


def response_from(result, translate):
	keys = (
		"service_id",
		"name",
		"amount",
		"source",
		"reason",
		"estimated_days",
		"provider",
		"carrier_code",
		"scope",
	)
	return {
		"policy_active": bool(result.get("policy_active")),
		"offered": bool(result.get("offered")),
		"reason": result.get("reason"),
		"message": translate(result.get("message") or ""),
		"needs_quotes": bool(result.get("needs_quotes")),
		"shipments": [
			{k: s.get(k) for k in ("index", "items", "services", "quote")}
			for s in result.get("shipments", [])
			if s.get("quote")
		]
		if result.get("needs_quotes")
		else [],
		"offers": [{k: o.get(k) for k in keys} for o in result.get("offers", [])],
	}


def public_summary_from(policy, country_names, translate):
	return {
		"active": bool(policy.get("active")),
		"zones": [
			{
				"name": z["name"],
				"countries": [{"code": c, "name": country_names.get(c, c)} for c in z["countries"]],
				"customer_line": translate(z.get("customer_line") or ""),
				"delivers": not z.get("not_shipped"),
			}
			for z in policy.get("zones", [])
			if policy.get("active") and z.get("enabled", True)
		],
	}


def snapshot_version(policy):
	return hashlib.sha256(
		json.dumps(policy, sort_keys=True, separators=(",", ":"), default=str).encode()
	).hexdigest()[:24]


def diff_policy(current, draft):
	changes = []
	for key, label in (("zones", "Country group"), ("item_sets", "Product group")):
		before = {x["name"]: x for x in current.get(key, [])}
		after = {x["name"]: x for x in draft.get(key, [])}
		for n in after.keys() - before.keys():
			changes.append(f"Add {label.lower()}: {n}")
		for n in before.keys() - after.keys():
			changes.append(f"Remove {label.lower()}: {n}")
		for n in before.keys() & after.keys():
			if before[n] != after[n]:
				changes.append(f"Change {label.lower()}: {n}")
	if current.get("rules") != draft.get("rules"):
		changes.append(f"Update delivery rows ({len(draft.get('rules', []))} rows)")
	if current.get("active") != draft.get("active"):
		changes.append(
			"Use this policy at checkout" if draft.get("active") else "Switch off this policy at checkout"
		)
	if current.get("fallback") != draft.get("fallback"):
		changes.append("Change what happens if the carrier gives no price")
	return changes


def clean_shipment_quotes(value):
	value = parse_json_arg(value, {})
	if not isinstance(value, dict) or len(value) > 10:
		raise InputError("At most 10 shipment quotes are allowed.")
	out = {}
	for key, data in value.items():
		if not isinstance(key, str) or not key.isdigit() or len(key) > 2 or not isinstance(data, dict):
			raise InputError("Invalid shipment index.")
		boxes = clean_boxes(data.get("boxes"))
		quotes = clean_quotes(data.get("quotes"))
		if len({q["service_id"] for q in quotes}) != len(quotes):
			raise InputError("A service may quote a shipment only once.")
		out[key] = {"boxes": boxes, "quotes": quotes}
	if sum(len(d["boxes"]) for d in out.values()) > 50:
		raise InputError("At most 50 cartons are allowed.")
	return out
