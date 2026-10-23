"""The shipping policy wizard's methods: what the wizard page reads, prices and, on Apply, writes.

Five methods, for the System Manager role only. Each one checks the role before it does anything else.

	wizard_state     the saved policy, its version, and the lists the page needs
	try_order        prices a sample order with the draft, as if the draft were switched on
	check_formula    whether a formula can be used, and why not when it cannot
	preview_changes  what applying the draft would change, and what stops it from being applied
	apply_policy     writes the whole draft at once, or nothing at all

The draft is a policy in the shape `shipping_engine` documents, as `wizard_state` hands it out and the page
edits it in the browser. Only `apply_policy` writes. It takes the whole draft in one request, refuses it when
someone changed the policy after the wizard loaded it or when the draft has a problem, and writes it inside
one savepoint, so that a failure at any step rolls back every step before it: a half-finished policy never
goes live. Applies are taken one at a time.

Before a draft is priced, checked, compared or written it is read as it will be stored: names and codes
without outer spaces, country codes in capitals, amounts rounded to cents and weights and volumes to the
three decimals their fields keep. The Review step therefore never shows rounding as a change, a band such as
2.0001 up to 2.0004 kg is judged as the 2 up to 2 it would be stored as, and what is checked is what is
written. A draft whose parts have the wrong types (a list given as text, a flag given as text, a number that
is not one) is not read at all: it is "not in the expected shape".

The desk shows a refusal as HTML, so the text people typed is escaped in the messages these methods raise.
The sentences `preview_changes` and `check_formula` return are plain text, for the page to show as text.
"""

import json
import math

import frappe
from frappe import _
from frappe.utils import escape_html, flt, sbool
from frappe.utils.file_lock import LockTimeoutError
from frappe.utils.synchronization import filelock

from dsi_catalogue import shipping_formula
from dsi_catalogue.shipping_engine import normalize_country, resolve, rule_previews, validate_policy
from dsi_catalogue.shipping_formula import FormulaError
from dsi_catalogue.shipping_policy import PRICE_LIST, build_order, load_policy, served_country_codes
from dsi_catalogue.shipping_policy_data import (
	MAX_BOXES,
	MAX_NUMBER,
	MAX_TEXT,
	InputError,
	clean_boxes,
	clean_items,
	clean_quotes,
	diff_policy,
	parse_json_arg,
	policy_from_rows,
	rows_from_policy,
	snapshot_version,
)

POLICY = "Shipping Policy"
ZONE = "Shipping Zone"
ITEM_SET = "Shipping Item Set"

# One apply at a time, across every worker of the site: a second one waits this long for the first to
# finish, and is then refused.
APPLY_LOCK = "shipping_policy_apply"
APPLY_LOCK_SECONDS = 30
_SAVEPOINT = "shipping_policy_apply"

# Try an order packs at most this many units into cartons: packing takes longer with every unit, and every
# carton is one question to the carrier.
MAX_PACKED_UNITS = 1000

SHAPE_PROBLEM = "The draft is not in the expected shape."
_CANNOT_APPLY = "The shipping policy cannot be applied"
_STALE = (
	"Someone changed the shipping policy after you opened the wizard. Reload the wizard to see the change, "
	"then apply again."
)
_BUSY = "Someone else is applying a change right now. Try again in a moment."

# The decimals each number is stored with: the precision of its doctype field.
_MONEY, _MEASURE = 2, 3
_LIMITS = {
	"min_order_value": _MONEY,
	"max_order_value": _MONEY,
	"min_weight_kg": _MEASURE,
	"max_weight_kg": _MEASURE,
	"min_volume_l": _MEASURE,
	"max_volume_l": _MEASURE,
}

# What each part of a draft holds. A policy has every one of its parts. Inside a fallback, a zone, an item
# set or a rule a part may be missing or None, which reads as empty; a part that is there has its type.
_NUMBER, _TEXTS = "a number", "a list of texts"
_SHAPES = {
	"policy": {
		"active": bool,
		"not_covered": str,
		"default_fallback": dict,
		"zones": list,
		"item_sets": list,
		"rules": list,
	},
	"fallback": {"method": str, "amount": _NUMBER},
	"zone": {
		"name": str,
		"enabled": bool,
		"countries": _TEXTS,
		"not_shipped": bool,
		"everywhere_else": bool,
		"customer_line": str,
		"not_offered_message": str,
		"fallback": dict,
	},
	"item set": {"name": str, "item_groups": _TEXTS, "items": _TEXTS},
	"rule": {
		"name": str,
		"enabled": bool,
		"label": str,
		"zone": str,
		"item_set": str,
		**dict.fromkeys(_LIMITS, _NUMBER),
		"method": str,
		"formula": str,
		"round_up": bool,
	},
}

# The child tables of a zone and of an item set, each with the field that holds its value.
_CHILD_TABLES = {ZONE: {"countries": "country"}, ITEM_SET: {"item_groups": "item_group", "items": "item"}}
_CARRIER_DOCTYPES = ("Shipping Service", "Shipping Provider")


# ---------------------------------------------------------------- the five methods
@frappe.whitelist()
def wizard_state() -> dict:
	"""What the wizard starts from: {"policy", "version", "countries": [{"code", "name"}], "served_codes",
	"price_list", "carrier_available"}.

	`policy` is the saved policy as `load_policy` reads it and `version` names it (Apply gives it back, so a
	change made meanwhile is noticed). `countries` is every Country with a code, by name, for the zone
	chips; `served_codes` the countries some carrier service delivers to; `price_list` the list an order's
	value comes from; `carrier_available` whether Try an order can ask the carrier. Plain data only.
	"""
	frappe.only_for("System Manager")
	policy = load_policy()
	return {
		"policy": policy,
		"version": snapshot_version(policy),
		"countries": _country_list(),
		"served_codes": served_country_codes(),
		"price_list": PRICE_LIST,
		"carrier_available": _carrier_available(),
	}


@frappe.whitelist()
def try_order(draft, order, ask_carrier=False, city=None) -> dict:
	"""Price a sample order with the draft, not with the saved policy: {"result", "previews", "order"}.

	The draft is priced as if it were switched on, so that it can be tried before it goes live; the page
	reads on and off from the draft itself. `order` is {"country_code", "items": [{"item_code", "qty"}],
	"order_value", "boxes", "quotes"} (a dict or JSON text). What is not typed comes from the ERP: the items'
	groups and their value in USD - Online, and the cartons dsi_shipping packs them into. A typed value,
	typed cartons and typed quotes (an empty list too) win. With `ask_carrier` and no typed quotes the
	carrier is asked for each carton (it needs a `city`), and any failure of the carrier means no quotes.

	`result` is the engine's whole answer, the trace of each offer included; `previews` says, rule by rule,
	what it would charge this order; `order` is the order as it was priced. Input that cannot be read is
	refused with a plain sentence. Nothing is written, and a formula that fails is not logged: the page
	prices the draft on every keystroke.
	"""
	frappe.only_for("System Manager")
	policy = _read_draft(draft)
	if policy is None:
		frappe.throw(_(SHAPE_PROBLEM))
	sample = _sample_order(order, _is_yes(ask_carrier), city)
	as_if_on = {**policy, "active": True}
	return {"result": resolve(as_if_on, sample), "previews": rule_previews(as_if_on, sample), "order": sample}


@frappe.whitelist()
def check_formula(source) -> dict:
	"""{"ok": True, "error": None} for a formula a rule can use, otherwise {"ok": False, "error": <why, in a
	plain sentence>}. The same check a rule's formula gets when the policy is saved. It never raises,
	whatever it is given, and nothing is logged."""
	frappe.only_for("System Manager")
	try:
		shipping_formula.check_formula(source)
	except FormulaError as error:
		return {"ok": False, "error": str(error)}
	except Exception:  # the checker raises nothing else; if it ever did, the page still gets an answer
		return {"ok": False, "error": "The formula could not be checked."}
	return {"ok": True, "error": None}


@frappe.whitelist()
def preview_changes(draft) -> dict:
	"""What applying the draft would change, and what stops it from being applied: {"changes", "problems"}.

	`changes` is `diff_policy(saved policy, draft as it will be stored)` (see its docstring for the shape).
	`problems` are plain sentences: `validate_policy`'s, one for each country code that is not a Country of
	the ERP, and those the doctypes' own checks would raise on saving. Apply is refused while there is one.
	It never raises on a malformed draft: one that is not a policy gives the single problem "The draft is not
	in the expected shape." and no changes. Nothing is written.
	"""
	frappe.only_for("System Manager")
	policy = _read_draft(draft)
	if policy is None:
		return {"changes": diff_policy({}, {}), "problems": [SHAPE_PROBLEM]}
	countries, saved = _country_names(), load_policy()
	return {
		"changes": diff_policy(saved, _as_loaded(policy, countries)),
		"problems": _problems(policy, countries, saved),
	}


@frappe.whitelist(methods=["POST"])
def apply_policy(draft, expected_version) -> dict:
	"""Write the whole draft, or nothing: {"version", "changes"}.

	Refused, before anything is written, when `expected_version` is not the version of the saved policy
	(someone changed it after the wizard loaded it: `frappe.TimestampMismatchError`), and when the draft has
	any of the problems `preview_changes` reports. Otherwise written inside one savepoint, in this order:

	1. the item sets, inserted or updated;
	2. every saved zone the draft drops is switched off, so that the policy's own check in step 4 finds it
	   inert (a country it lists may now be in another zone);
	3. the draft's zones, inserted or updated, one by one, without the zone form's checks across zones (the
	   draft was checked as a whole: one at a time, a country moving between two zones would be refused);
	4. the policy's settings and rules, in one save that the policy's own check validates;
	5. the zones and item sets the draft drops are deleted: no rule points at them any more;
	6. the saved policy is read back and checked once more.

	A failure at any step rolls everything back and is raised. A zone or item set that would not change is
	left alone, so the history of each document records only real changes. Afterwards the website is asked
	to refresh through dsi_core's outbox, when the site has it. Returns the new version and the changes
	(`diff_policy(policy before, draft)`).

	Only one apply runs at a time: a second waits for the first, and is refused if that takes over 30 seconds.
	It is the one method that writes, so it takes POST requests only (a link cannot apply a policy).
	"""
	frappe.only_for("System Manager")
	try:
		with filelock(APPLY_LOCK, timeout=APPLY_LOCK_SECONDS):
			return _apply(draft, expected_version)
	except LockTimeoutError:
		frappe.throw(_(_BUSY), title=_(_CANNOT_APPLY))


# ---------------------------------------------------------------- reading a draft
def _is_text(value) -> bool:
	"""Text that can be stored and sent back: JSON can carry a lone surrogate, which nothing can write."""
	if not isinstance(value, str):
		return False
	try:
		value.encode("utf-8")
	except UnicodeEncodeError:
		return False
	return True


def _is_number(value) -> bool:
	"""A real, finite number of at most a billion either way: an int or a float, and not a bool."""
	if isinstance(value, bool) or not isinstance(value, int | float):
		return False
	try:
		return math.isfinite(value) and abs(value) <= MAX_NUMBER
	except OverflowError:  # an int too big for a float
		return False


def _holds(value, kind) -> bool:
	if kind == _NUMBER:
		return _is_number(value)
	if kind == _TEXTS:
		return isinstance(value, list) and all(_is_text(entry) for entry in value)
	if kind is str:
		return _is_text(value)
	return isinstance(value, kind)


def _fits(entry, shape, *, whole=False) -> bool:
	"""Is `entry` a dict whose parts have the types of `shape`? With `whole`, every part has to be there."""
	if not isinstance(entry, dict):
		return False
	for part, kind in _SHAPES[shape].items():
		value = entry.get(part)
		if value is None:
			if whole:
				return False
		elif not _holds(value, kind):
			return False
	return True


def _is_policy(value) -> bool:
	if not _fits(value, "policy", whole=True):
		return False
	entries = [
		*((zone, "zone") for zone in value["zones"]),
		*((item_set, "item set") for item_set in value["item_sets"]),
		*((rule, "rule") for rule in value["rules"]),
	]
	if not all(_fits(entry, shape) for entry, shape in entries):
		return False
	fallbacks = [value["default_fallback"], *(zone.get("fallback") for zone in value["zones"])]
	return all(fallback is None or _fits(fallback, "fallback") for fallback in fallbacks)


def _trimmed(text):
	return text.strip() if isinstance(text, str) else text


def _rounded(value, places):
	return flt(value, places) if _is_number(value) else value


def _code(text):
	"""A country code as two upper-case letters. What is not one stays as it was typed (without outer spaces),
	for `validate_policy` to say so: upper-casing first would turn "\u00df" into SS, South Sudan."""
	return normalize_country(text) or text.strip()


def _tidied(policy) -> dict:
	"""The draft as the doctypes will hold it: names and codes without outer spaces, empty entries left out,
	country codes in capitals, and every number rounded to the decimals of its field. The draft itself is left
	as it is, and nothing the draft holds beyond the parts read here is looked into."""

	def fallback(entry):
		return None if entry is None else {**entry, "amount": _rounded(entry.get("amount"), _MONEY)}

	def stripped(entries):
		return [entry.strip() for entry in entries or [] if entry.strip()]

	return {
		**policy,
		"default_fallback": fallback(policy["default_fallback"]),
		"zones": [
			{
				**zone,
				"name": _trimmed(zone.get("name")),
				"countries": [_code(code) for code in zone.get("countries") or [] if code.strip()],
				"fallback": fallback(zone.get("fallback")),
			}
			for zone in policy["zones"]
		],
		"item_sets": [
			{
				**item_set,
				"name": _trimmed(item_set.get("name")),
				"item_groups": stripped(item_set.get("item_groups")),
				"items": stripped(item_set.get("items")),
			}
			for item_set in policy["item_sets"]
		],
		"rules": [
			{
				**rule,
				"zone": _trimmed(rule.get("zone")),
				"item_set": _trimmed(rule.get("item_set")) or None,
				**{limit: _rounded(rule.get(limit), places) for limit, places in _LIMITS.items()},
			}
			for rule in policy["rules"]
		],
	}


def _read_draft(draft) -> dict | None:
	"""The draft (a dict or JSON text) as a policy read the way it will be stored, or None when it is not a
	policy in the expected shape."""
	try:
		value = parse_json_arg(draft, None)
	except InputError:
		return None
	return _tidied(value) if _is_policy(value) else None


def _country_list() -> list[dict]:
	"""[{"code", "name"}] for every Country with a two-letter code, by name."""
	rows = frappe.get_all("Country", fields=["name", "code"], order_by="name asc")
	return [{"code": code, "name": row["name"]} for row in rows if (code := normalize_country(row["code"]))]


def _country_names() -> dict[str, str]:
	"""{ISO code: Country name}."""
	return {country["code"]: country["name"] for country in _country_list()}


def _rows_and_loaded(policy, countries) -> tuple[dict, dict]:
	"""The rows the draft is written as, and the policy `load_policy` will read back from them (zones and
	item sets in the draft's order)."""
	rows = rows_from_policy(policy, countries)
	codes = {name: code for code, name in countries.items()}
	return rows, policy_from_rows(rows["policy"], rows["zones"], rows["item_sets"], codes)


def _as_loaded(policy, countries) -> dict:
	return _rows_and_loaded(policy, countries)[1]


# ---------------------------------------------------------------- what stops a draft
def _problems(policy, countries, saved) -> list[str]:
	"""Everything that stops the draft from being applied, as plain sentences: `validate_policy`'s, then the
	countries the ERP does not know, then what the doctypes would refuse to save although the engine has no
	quarrel with it. `saved` is the policy as it is in the ERP now."""
	return [
		*validate_policy(policy),
		*_unknown_countries(policy, countries),
		*_saving_problems(policy, saved),
	]


def _unknown_countries(policy, countries) -> list[str]:
	"""One sentence for each two-letter code that is no Country of the ERP. (A code that is not two letters
	gets the engine's own sentence.)"""
	unknown = dict.fromkeys(
		code
		for zone in policy["zones"]
		for code in zone["countries"]
		if normalize_country(code) and code not in countries
	)
	return [f"Country code {code} is not a country in the ERP." for code in unknown]


def _capital_letter_clashes(kind, names, saved_names) -> list[str]:
	"""One sentence for each name the ERP would take for another although the text is not the same, because it
	ignores capital letters in a name: two names of the draft, or a name of the draft and the saved document of
	that name which the draft drops (it is still there when the new one is written). Names that are exactly the
	same are the engine's to report."""
	problems, first = [], {}
	for name in filter(None, names):
		earlier = first.setdefault(name.casefold(), name)
		if earlier != name:
			problems.append(
				f'{kind}s "{earlier}" and "{name}" differ only in capital letters, which the ERP reads as one '
				f"name. {kind} names have to be different."
			)
	for name in saved_names:
		held = first.get(name.casefold())
		if held and held != name and name not in names:
			problems.append(
				f'{kind} "{held}" is the saved {kind.lower()} "{name}" with other capital letters. The ERP '
				f'reads them as one name, so keep "{name}" or choose another name.'
			)
	return problems


def _saving_problems(policy, saved) -> list[str]:
	"""What the zone and policy doctypes would refuse to save although `validate_policy` has no quarrel with it,
	said now instead of half way through the write: names the ERP reads as one, a disabled Everywhere else zone
	that lists countries, and a fallback amount below zero that no fixed charge uses."""
	problems = [
		*_capital_letter_clashes(
			"Zone", [zone.get("name") for zone in policy["zones"]], [zone["name"] for zone in saved["zones"]]
		),
		*_capital_letter_clashes(
			"Item set",
			[item_set.get("name") for item_set in policy["item_sets"]],
			[item_set["name"] for item_set in saved["item_sets"]],
		),
	]
	fallbacks = [("The policy default", policy["default_fallback"])]
	for position, zone in enumerate(policy["zones"], start=1):
		ref = f'"{zone["name"]}"' if zone.get("name") else f"number {position}"
		# The zone form refuses countries on any Everywhere else zone; the engine looks at enabled zones only.
		if zone.get("everywhere_else") and not zone.get("enabled") and zone["countries"]:
			problems.append(f"Zone {ref} is an Everywhere else zone, so it cannot list countries.")
		fallbacks.append((f"Zone {ref}", zone.get("fallback")))
	for subject, fallback in fallbacks:
		amount = (fallback or {}).get("amount")
		# A fixed amount below zero is the engine's to report. Any other is not used, but no field takes it.
		if (fallback or {}).get("method") != "fixed" and _is_number(amount) and amount < 0:
			problems.append(
				f"{subject} has an amount below zero for when the carrier can't quote. Make it 0 or more."
			)
	return problems


# ---------------------------------------------------------------- the sample order
def _is_yes(value) -> bool:
	"""A yes or no as a JSON body or a form post sends it: True, 1, "1" and "true" are yes."""
	if isinstance(value, str):
		value = sbool(value.strip())
	return value is True or (type(value) is int and value == 1)


def _order_value(value):
	"""A typed order value in cents, or None to take the value of the items."""
	if value is None:
		return None
	if not _is_number(value) or value < 0:
		raise InputError(f"The order value has to be a number from 0 to {MAX_NUMBER}.")
	return flt(value, _MONEY)


def _country(value) -> str:
	"""The sample's country as it was typed, for the engine to read loosely (" ae " is AE). Text that could
	not be sent back is no country."""
	return value if _is_text(value) and len(value) <= MAX_TEXT else ""


def _sample_order(order, ask, city) -> dict:
	"""The sample, cleaned and completed as `build_order` completes an order, with the cartons and the
	carrier's quotes filled in when they are not typed."""
	try:
		order = parse_json_arg(order, None)
		if not isinstance(order, dict):
			raise InputError("The sample order is not in the expected shape.")
		items = clean_items(order.get("items"))
		boxes = None if order.get("boxes") is None else clean_boxes(order["boxes"])
		quotes = None if order.get("quotes") is None else clean_quotes(order["quotes"])
		value = _order_value(order.get("order_value"))
	except InputError as error:
		frappe.throw(str(error), frappe.ValidationError)
	country = _country(order.get("country_code"))
	cartons = _packed_cartons(items) if boxes is None else [{**box, "size": _cube(box)} for box in boxes]
	if quotes is None:
		quotes = _carrier_quotes(country, city, cartons) if ask else []
	shipped = [{"weight_kg": carton["weight_kg"], "volume_l": carton["volume_l"]} for carton in cartons]
	return build_order(country, items, shipped, quotes, order_value=value)


def _cube(box) -> dict:
	"""The size a typed carton is sent to the carrier with: a cube that holds exactly its volume. The carrier
	works out a volumetric weight from length x width x height, so it sees the volume that was typed."""
	side = (box["volume_l"] * 1000) ** (1 / 3)
	return {"box_code": None, "length_cm": side, "width_cm": side, "height_cm": side}


def _packed_cartons(items) -> list[dict]:
	"""The cartons dsi_shipping packs the items into, the same cartons a real order is rated and booked in
	(its packer reads each item's weight per unit and package size, with its own fallbacks). Each carton is
	{"weight_kg": its gross weight, "volume_l", "size": {"box_code", "length_cm", "width_cm", "height_cm"}}.
	No cartons without items, or on a site without the packer.
	"""
	if not items:
		return []
	try:
		from dsi_shipping.packing import pack_delivery_note
	except ImportError:
		return []
	if sum(item["qty"] for item in items) > MAX_PACKED_UNITS:
		frappe.throw(
			_("Try an order packs at most {0} units into cartons. Type the cartons instead.").format(
				MAX_PACKED_UNITS
			)
		)
	note = frappe._dict(items=[{"item_code": item["item_code"], "qty": item["qty"]} for item in items])
	cartons = []
	for box in pack_delivery_note(note).get("boxes") or []:
		size = box.get("dimensions") or {}
		length, width, height = (flt(size.get(side)) for side in ("length_cm", "width_cm", "height_cm"))
		cartons.append(
			{
				"weight_kg": flt(box.get("gross_weight_kg")),
				"volume_l": length * width * height / 1000,
				"size": {
					"box_code": box.get("box_code"),
					"length_cm": length,
					"width_cm": width,
					"height_cm": height,
				},
			}
		)
	if len(cartons) > MAX_BOXES:
		frappe.throw(_("The items fill more than {0} cartons. Type the cartons instead.").format(MAX_BOXES))
	return cartons


class _CarrierFailed(Exception):
	"""The carrier's answer is not a list of prices."""


def _carrier_quotes(country, city, cartons) -> list[dict]:
	"""The carrier's quotes for the cartons, asked one carton at a time as the website asks: each service's
	prices added up, with the number of cartons it quoted, cheapest first. When the carrier fails in any way
	(an error, an answer that says it failed, or one that is not a list of prices) there are no quotes at
	all, never some of them."""
	code = normalize_country(country)
	if not code:
		frappe.throw(_("Asking the carrier needs a country."))
	place = city.strip() if isinstance(city, str) else ""
	if not place:
		frappe.throw(_("Asking the carrier needs a city."))
	if len(place) > MAX_TEXT or not place.isprintable():
		frappe.throw(_("The city has to be plain text of at most {0} characters.").format(MAX_TEXT))
	if not any(carton["weight_kg"] > 0 for carton in cartons):
		frappe.throw(_("Asking the carrier needs a carton that weighs more than 0 kg."))
	try:
		from dsi_shipping.api import get_shipping_rates
	except ImportError:
		return []
	shown = len(frappe.local.message_log)
	try:
		return _quoted(get_shipping_rates, code, place, cartons)
	except Exception:
		del frappe.local.message_log[shown:]  # the carrier's own message (a SOAP fault, say) is not passed on
		return []


def _quoted(get_shipping_rates, code, city, cartons) -> list[dict]:
	# No postcode is sent: the wizard has none, the carrier's default is an empty one, and erp2's older
	# dsi_shipping takes no such argument at all.
	totals = {}
	for carton in cartons:
		reply = get_shipping_rates(
			country_code=code,
			city=city,
			weight_kg=carton["weight_kg"],
			dimensions=json.dumps(carton["size"]),
			cod_amount=0,
			currency="USD",
		)
		rates = reply.get("rates") if isinstance(reply, dict) and reply.get("success") else None
		if not isinstance(rates, list):
			raise _CarrierFailed
		quoted = set()
		for rate in rates:
			service_id = rate.get("service_id") if isinstance(rate, dict) else None
			cost = rate.get("cost") if isinstance(rate, dict) else None
			if not isinstance(service_id, str) or service_id in quoted or not _is_number(cost) or cost < 0:
				raise _CarrierFailed
			quoted.add(service_id)
			name = rate.get("service_name") or service_id
			total = totals.setdefault(
				service_id, {"service_id": service_id, "name": name, "amount": 0.0, "boxes_quoted": 0}
			)
			total["amount"] += cost
			total["boxes_quoted"] += 1
	# Added up to three decimals, as the website adds them, then cleaned like any quote.
	quotes = [{**total, "amount": flt(total["amount"], 3)} for total in totals.values()]
	return clean_quotes(sorted(quotes, key=lambda quote: quote["amount"]))


def _carrier_doctypes_exist() -> bool:
	found = frappe.get_all("DocType", filters={"name": ["in", list(_CARRIER_DOCTYPES)]}, pluck="name")
	return len(found) == len(_CARRIER_DOCTYPES)


def _carrier_available() -> bool:
	"""Can Try an order ask the carrier? Only where dsi_shipping is installed and an enabled service of an
	enabled provider exists."""
	if not _carrier_doctypes_exist():
		return False
	service, provider = (frappe.qb.DocType(name) for name in _CARRIER_DOCTYPES)
	found = (
		frappe.qb.from_(service)
		.join(provider)
		.on(provider.name == service.shipping_provider)
		.select(service.name)
		.where((service.enabled == 1) & (provider.enabled == 1))
		.limit(1)
		.run()
	)
	return bool(found)


# ---------------------------------------------------------------- Apply
def _refuse(problems):
	"""Refuse the apply with these sentences, escaped: the desk shows them as HTML, and they quote names,
	labels, formulas and codes people typed."""
	frappe.throw([escape_html(problem) for problem in problems], title=_(_CANNOT_APPLY), as_list=True)


def _apply(draft, expected_version) -> dict:
	before = load_policy()
	if expected_version != snapshot_version(before):
		frappe.throw(_(_STALE), frappe.TimestampMismatchError, title=_(_CANNOT_APPLY))
	policy = _read_draft(draft)
	if policy is None:
		_refuse([SHAPE_PROBLEM])
	countries = _country_names()
	if problems := _problems(policy, countries, before):
		_refuse(problems)
	rows, loaded = _rows_and_loaded(policy, countries)
	frappe.db.savepoint(_SAVEPOINT)
	try:
		_write(rows, loaded, before)
		written = load_policy()
		if found := validate_policy(written):
			_refuse(found)
	except BaseException:
		frappe.db.rollback(save_point=_SAVEPOINT)
		raise
	frappe.db.release_savepoint(_SAVEPOINT)
	version = snapshot_version(written)
	_refresh_the_website(version)
	return {"version": version, "changes": diff_policy(before, loaded)}


def _write(rows, loaded, before):
	"""Steps 1 to 5 of `apply_policy`. `rows` are the draft's rows, `loaded` the same draft as the loader will
	read it back, `before` the saved policy."""
	zones_before = {zone["name"]: zone for zone in before["zones"]}
	sets_before = {item_set["name"]: item_set for item_set in before["item_sets"]}
	dropped_zones = sorted(zones_before.keys() - {zone["name"] for zone in loaded["zones"]})
	dropped_sets = sorted(sets_before.keys() - {item_set["name"] for item_set in loaded["item_sets"]})
	for row, item_set in zip(rows["item_sets"], loaded["item_sets"], strict=True):
		_upsert(ITEM_SET, row, item_set, sets_before)
	for name in dropped_zones:
		_switch_off(name)
	for row, zone in zip(rows["zones"], loaded["zones"], strict=True):
		_upsert(ZONE, row, zone, zones_before)
	_save_policy(rows["policy"], loaded, before)
	for name in dropped_zones:
		frappe.delete_doc(ZONE, name)
	for name in dropped_sets:
		frappe.delete_doc(ITEM_SET, name)


def _upsert(doctype, row, entry, saved):
	"""Insert the zone or item set `row`, or update the saved one of the same name. One that would not change
	is left alone. A zone is saved with the flag that leaves its checks across zones to the draft's check."""
	name = entry["name"]
	if name in saved and saved[name] == entry:
		return
	if name in saved:
		doc = frappe.get_doc(doctype, name)
		doc.update(_keeping_row_names(doc, row))
		write = doc.save
	else:
		doc = frappe.get_doc({"doctype": doctype, **row})
		write = doc.insert
	if doctype == ZONE:
		doc.flags.policy_draft_validated = True
	write()


def _keeping_row_names(doc, row) -> dict:
	"""`row` with each child row given the name of a saved row of `doc` that holds the same value, so that the
	document's history shows what changed, not every row removed and added again. Only names of rows the
	document has now are used: Frappe takes a named row for one to update."""
	row = dict(row)
	for table, field in _CHILD_TABLES[doc.doctype].items():
		free = {}
		for child in doc.get(table):
			free.setdefault(child.get(field), []).append(child.name)
		row[table] = [
			{**child, "name": free[child[field]].pop(0)} if free.get(child[field]) else child
			for child in row[table]
		]
	return row


def _switch_off(name):
	doc = frappe.get_doc(ZONE, name)
	if doc.enabled:
		doc.enabled = 0
		doc.flags.policy_draft_validated = True
		doc.save()


def _save_policy(row, loaded, before):
	"""Save the settings and the rules in one save of the single (none when neither changes).

	A rule keeps its row when its name is the name of a saved row (the first rule to claim it); every other
	rule is a new row. Frappe takes a named row for one to update, so a name that is not a saved row's
	would lose the rule.
	"""
	if all(loaded[key] == before[key] for key in ("active", "not_covered", "default_fallback", "rules")):
		return
	doc = frappe.get_doc(POLICY)
	free = {rule.name for rule in doc.rules}
	rules = []
	for rule in row["rules"]:
		if rule.get("name") in free:
			free.discard(rule["name"])
			rules.append(rule)
		else:
			rules.append({key: value for key, value in rule.items() if key != "name"})
	doc.update({**row, "rules": rules})
	doc.save()


def _refresh_the_website(version):
	"""Ask the website to refresh the pages that show the policy, through dsi_core's outbox when the site has
	it, the way `api.notify_revalidate` does for products. It never fails the apply: a failure is logged."""
	try:
		from dsi_core import outbox
	except ImportError:
		return
	try:
		outbox.enqueue(
			"website-revalidate",
			{"tags": ["shipping-policy"]},
			dedup_key=f"shipping-policy|{version}",
			source=(POLICY, POLICY),
		)
	except Exception:
		frappe.log_error(title="shipping policy revalidate failed", message=frappe.get_traceback())
