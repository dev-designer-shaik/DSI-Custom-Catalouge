"""The shipping pricing engine: what a customer pays for delivery, given a policy and an order.

Pure Python with no Frappe import, so it is tested standalone. The ERP layer reads the policy and the
order out of the database and calls `resolve`; the wizard calls the same functions on a draft. Formulas
are calculated by `shipping_formula`, never by `eval`.

The inputs are plain dicts (the ERP layer fills every key; a missing one reads as its default):

	policy = {"active", "not_covered", "default_fallback": {"method", "amount"},
	          "zones": [{"name", "enabled", "countries", "not_shipped", "everywhere_else",
	                     "customer_line", "not_offered_message", "fallback": {"method", "amount"}}],
	          "item_sets": [{"name", "item_groups", "items"}],
	          "rules": [{"name", "enabled", "label", "zone", "item_set", "min_order_value",
	                     "max_order_value", "min_weight_kg", "max_weight_kg", "min_volume_l",
	                     "max_volume_l", "method", "formula", "round_up"}]}
	order  = {"country_code", "items": [{"item_code", "qty", "groups"}], "order_value",
	          "boxes": [{"weight_kg", "volume_l"}],
	          "quotes": [{"service_id", "name", "amount", "boxes_quoted"}], "served"}

`resolve` prices an order like this:

1. A policy that is off returns every quote exactly as posted.
2. A country code that is not two letters, or a destination no carrier serves, is `not_served`.
   No fallback applies: the policy does not sell to places nobody delivers to.
3. The zone is the destination's enabled zone, else the enabled Everywhere-else zone, else none.
   A Not shipped zone offers no delivery (`not_shipped`).
4. A service that did not quote every carton is dropped, because its total would be too low.
5. With a zone but no whole quote, the zone's fallback decides and no rule is used.
6. Otherwise the first enabled rule of the zone whose conditions hold prices every service, each with
   its own quote. When no rule holds, or there is no zone, "Anything not covered" decides: pass the
   carrier's price through, or offer no delivery (`not_covered`).
7. A rule whose formula cannot be calculated sends the whole order to the fallback (`formula_error`).
   A failure never becomes a price of zero.

The weight and volume a rule's bands and a formula see are the sums over the cartons, rounded to six
decimals. `boxes` is the number of cartons and `item_count` the sum of the item quantities.

Nothing here raises on a half-finished policy: the wizard prices an unfinished draft on every
keystroke. Where a setting cannot be read the answer is "no delivery", not a free one.
"""

import math
from decimal import ROUND_CEILING, ROUND_HALF_UP, Context, Decimal

from dsi_catalogue.shipping_formula import FormulaError, check_formula, evaluate

# The Select strings the doctypes store, mapped to the codes used here. The order is the order of the
# Select options, and the first string of each is that field's default.
LABELS = {
	"method": {"Carrier price": "carrier_price", "Free": "free", "Custom formula": "formula"},
	"zone_fallback": {
		"Use the policy default": "policy_default",
		"Don't offer delivery": "no_delivery",
		"Charge a fixed amount": "fixed",
		"Free": "free",
	},
	"default_fallback": {
		"Don't offer delivery": "no_delivery",
		"Charge a fixed amount": "fixed",
		"Free": "free",
	},
	"not_covered": {"Pass the carrier price through": "pass_through", "Don't offer delivery": "no_delivery"},
}

REASONS = ("not_served", "not_shipped", "not_offered", "not_covered", "fallback_offer", "formula_error")

# The delivery method that stands in for a carrier that gave no price.
FALLBACK_SERVICE_ID = "policy-fallback"
FALLBACK_SERVICE_NAME = "Standard delivery"

# English text for each reason. The website translates the reason codes itself.
DEFAULT_MESSAGES = {
	"not_served": "Delivery is not available to this destination.",
	"not_shipped": "We do not deliver to this country.",
	"not_offered": "Delivery is not available for this order right now.",
	"not_covered": "No delivery option applies to this order.",
	"fallback_offer": "The carrier could not give a price, so the standard delivery charge applies.",
	"formula_error": "The delivery price could not be worked out for this order.",
}

# For these reasons a zone's own message, when it has one, replaces the standard text.
_ZONE_MESSAGE_REASONS = ("not_shipped", "not_offered", "not_covered")

# Each band: the name in `_totals`, the "from" key, the "up to" key, and how a sentence names it.
_BANDS = (
	("order_value", "min_order_value", "max_order_value", "order value"),
	("weight_kg", "min_weight_kg", "max_weight_kg", "weight"),
	("volume_l", "min_volume_l", "max_volume_l", "volume"),
)

_CHARGES = {
	"carrier_price": "the carrier's price",
	"free": "nothing (free)",
	"formula": "by a custom formula",
}


# ---------------------------------------------------------------- reading loose data
def _as_dict(value):
	return value if isinstance(value, dict) else {}


def _as_list(value):
	return value if isinstance(value, (list, tuple)) else []


def _is_number(value):
	"""An int, float or Decimal. A bool is not a number here, and neither is text."""
	return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def _number(value):
	"""A finite float for a number, otherwise None. A bool, some text or a NaN is not a number."""
	if not _is_number(value):
		return None
	try:
		number = float(value)
	except (OverflowError, ValueError):
		return None
	return number if math.isfinite(number) else None


def _key(value):
	"""How item groups and item codes are compared: the ERP's database ignores case and outer spaces."""
	return "" if value is None else str(value).strip().casefold()


# ---------------------------------------------------------------- money
_CENTS = Decimal("0.01")
# A float has at most 309 digits before the point, and the cents add two more. Decimal's own default of
# 28 digits would make `quantize` raise for any amount above 1e26, and a guest can post 1e300.
_ROOM = Context(prec=400)


def _decimal(amount):
	"""The decimal a person reads in `amount`: 1.005 is 1.005, not the binary number nearest to it."""
	if not _is_number(amount):
		raise ValueError("An amount has to be a number.")
	value = Decimal(str(amount))
	if not value.is_finite():
		raise ValueError("An amount has to be a finite number.")
	return value


def _cents(amount):
	"""`amount` rounded to cents, half up, as a Decimal that is never below zero."""
	value = _decimal(amount)
	if value <= 0:
		return Decimal(0)
	return value.quantize(_CENTS, rounding=ROUND_HALF_UP, context=_ROOM)


def to_cents(amount: float) -> float:
	"""Round to cents, half up, from the decimal text of the amount. Never negative (and never -0.0).

	Raises ValueError for something that is not a finite number.
	"""
	return float(_cents(amount))


def round_up_dollar(amount: float) -> float:
	"""Round to cents (half up), then up to the next whole dollar. Whole prices and zero stay as they are.

	Raises ValueError for something that is not a finite number.
	"""
	return float(_cents(amount).to_integral_value(rounding=ROUND_CEILING))


def _difference(larger, smaller):
	return float(_ROOM.subtract(Decimal(str(larger)), Decimal(str(smaller))))


def _fmt(amount):
	"""An amount as a receipt prints it, with two decimals."""
	try:
		return f"{to_cents(amount):.2f}"
	except ValueError:
		return str(amount)


# ---------------------------------------------------------------- zones
def normalize_country(code) -> str:
	"""A country code as two upper-case letters, or "" when it is not one. Never raises."""
	if not isinstance(code, str):
		return ""
	code = code.strip()
	# Checked before upper-casing: a dotless i or a sharp s turns into plain letters when upper-cased.
	return code.upper() if len(code) == 2 and code.isascii() and code.isalpha() else ""


def select_zone(policy: dict, country_code: str) -> dict | None:
	"""The enabled zone that lists the country; else the enabled Everywhere-else zone; else None.

	`country_code` may be written loosely (" ae "). A value that is not two letters is no country and
	has no zone, not even Everywhere else. If a draft lists a country twice, the first zone wins.
	"""
	code = normalize_country(country_code)
	if not code:
		return None
	zones = [
		zone
		for zone in _as_list(_as_dict(policy).get("zones"))
		if isinstance(zone, dict) and zone.get("enabled")
	]
	for zone in zones:
		# An Everywhere-else zone catches whatever the other zones do not list, so its own list is moot.
		if not zone.get("everywhere_else") and code in {
			normalize_country(c) for c in _as_list(zone.get("countries"))
		}:
			return zone
	return next((zone for zone in zones if zone.get("everywhere_else")), None)


def _in_zone(rule, zone):
	name = rule.get("zone")
	return zone is not None and bool(name) and name == zone.get("name")


# ---------------------------------------------------------------- what the order looks like to a rule
def _totals(order):
	"""The figures a rule's bands and a formula see, as plain floats (the names formulas use)."""
	boxes = [_as_dict(box) for box in _as_list(order.get("boxes"))]
	items = [_as_dict(item) for item in _as_list(order.get("items"))]
	return {
		"order_value": _number(order.get("order_value")) or 0.0,
		# rounded so that 0.7 + 0.1, which floats make 0.7999999999999999, still reaches an edge of 0.8
		"weight_kg": round(sum((_number(box.get("weight_kg")) or 0.0 for box in boxes), 0.0), 6),
		"volume_l": round(sum((_number(box.get("volume_l")) or 0.0 for box in boxes), 0.0), 6),
		"boxes": float(len(boxes)),
		"item_count": sum((_number(item.get("qty")) or 0.0 for item in items), 0.0),
	}


def _usable_quotes(order):
	"""(service_id, name, amount) for each service that quoted every carton, in the order given.

	With no cartons any quote is whole. An amount that is not a finite number of at least zero is
	dropped, as a service that gave no price would be.
	"""
	cartons = len(_as_list(order.get("boxes")))
	usable = []
	for quote in _as_list(order.get("quotes")):
		quote = _as_dict(quote)
		amount = _number(quote.get("amount"))
		quoted = _number(quote.get("boxes_quoted"))
		if amount is None or amount < 0:
			continue
		if cartons and (quoted is None or quoted < cartons):
			continue
		usable.append((quote.get("service_id"), quote.get("name"), amount))
	return usable


def _items_allowed(rule, order, item_sets):
	"""A rule with an item set holds only if every item is in the set. A rule without one holds always."""
	set_name = rule.get("item_set")
	if not set_name:
		return True
	item_set = next(
		(s for s in _as_list(item_sets) if isinstance(s, dict) and s.get("name") == set_name), None
	)
	items = [_as_dict(item) for item in _as_list(order.get("items"))]
	if item_set is None or not items:  # no such set, or nothing to be in it
		return False
	groups = {_key(g) for g in _as_list(item_set.get("item_groups"))} - {""}
	codes = {_key(c) for c in _as_list(item_set.get("items"))} - {""}
	return all(
		_key(item.get("item_code")) in codes or any(_key(g) in groups for g in _as_list(item.get("groups")))
		for item in items
	)


def _limit(rule, key):
	"""A band edge, or None for no limit: an empty, missing, zero or unreadable edge."""
	return _number(rule.get(key)) or None


def _within(total, low, high):
	return (low is None or total >= low) and (high is None or total < high)


def _holds(rule, order, totals, item_sets):
	if not _items_allowed(rule, order, item_sets):
		return False
	return all(
		_within(totals[total], _limit(rule, low), _limit(rule, high)) for total, low, high, _ in _BANDS
	)


def rule_matches(rule: dict, order: dict, item_sets: list[dict]) -> bool:
	"""Do the rule's conditions (its item set and its bands) hold for the order?

	`item_sets` is the policy's list of item sets; the rule's set is found by name. Whether the rule is
	enabled and in the order's zone is for the caller to judge.
	"""
	order = _as_dict(order)
	return _holds(_as_dict(rule), order, _totals(order), item_sets)


def _first_rule(policy, zone, order, totals):
	"""The position in the policy of the first enabled rule of `zone` whose conditions hold, or None."""
	item_sets = _as_list(policy.get("item_sets"))
	for index, rule in enumerate(_as_list(policy.get("rules"))):
		rule = _as_dict(rule)
		if rule.get("enabled") and _in_zone(rule, zone) and _holds(rule, order, totals, item_sets):
			return index
	return None


# ---------------------------------------------------------------- results
def _step(kind, text, amount):
	return {"kind": kind, "text": text, "amount": amount}


def _offer(service_id, name, amount, source, reason, trace):
	return {
		"service_id": service_id,
		"name": name,
		"amount": amount,
		"source": source,
		"reason": reason,
		"trace": trace,
	}


def _result(zone, rule, reason, message, offers, *, active=True):
	return {
		"policy_active": active,
		"zone": None if zone is None else zone.get("name"),
		"rule": rule,
		"offered": bool(offers),
		"reason": reason,
		"message": message,
		"offers": offers,
	}


def _message(zone, reason):
	own = _as_dict(zone).get("not_offered_message")
	if reason in _ZONE_MESSAGE_REASONS and isinstance(own, str) and own.strip():
		return own.strip()
	return DEFAULT_MESSAGES[reason]


def _declined(zone, reason, rule=None):
	"""No delivery is offered, and the customer is told why."""
	return _result(zone, rule, reason, _message(zone, reason), [])


def _unchanged(order):
	"""The policy is off: every quote comes back as posted, partial ones too."""
	offers = []
	for quote in _as_list(order.get("quotes")):
		if not isinstance(quote, dict):
			continue
		amount = quote.get("amount")
		trace = [
			_step(
				"carrier",
				f"The carrier quoted {_fmt(amount)}. The shipping policy is off, so this is the price.",
				amount,
			),
			_step("result", f"Customer pays {_fmt(amount)}.", amount),
		]
		offers.append(_offer(quote.get("service_id"), quote.get("name"), amount, "carrier", None, trace))
	return _result(None, None, None, "", offers, active=False)


# ---------------------------------------------------------------- pricing
def _price(rule, carrier, totals):
	"""What `rule` charges for a carrier price. Raises FormulaError when it cannot say.

	Returns (the price in cents, the price after any round-up, the formula's own result or None).
	"""
	method = rule.get("method") or "carrier_price"
	formula_result = None
	if method == "carrier_price":
		price = carrier
	elif method == "free":
		price = 0.0
	elif method == "formula":
		formula = rule.get("formula")
		price = formula_result = evaluate(
			formula if isinstance(formula, str) else "", {**totals, "carrier_price": carrier}
		)
	else:
		raise FormulaError("The rule does not say how to charge.")
	base = to_cents(price)  # a price below zero is zero
	final = round_up_dollar(price) if rule.get("round_up") else base
	return base, final, formula_result


def _rule_text(index, rule, zone):
	label = str(rule.get("label") or "").strip()
	named = f' ("{label}")' if label else ""
	items = f"selected items ({rule['item_set']})" if rule.get("item_set") else "every item"
	rounding = ", rounded up" if rule.get("round_up") else ""
	charge = _CHARGES[rule.get("method") or "carrier_price"]
	return f"Rule {index + 1}{named} applies: {zone.get('name')}, {items}, charges {charge}{rounding}."


def _formula_text(rule, value):
	formula = rule["formula"].strip()
	if round(value, 2) < 0:
		return f"The formula {formula} gives {value:.2f}. A price is never below 0.00, so it is 0.00."
	return f"The formula {formula} gives {_fmt(value)}."


def _rule_offer(index, rule, zone, quote, totals):
	service_id, name, carrier = quote
	base, final, formula_result = _price(rule, carrier, totals)
	trace = [
		_step("carrier", f"The carrier quoted {_fmt(carrier)}.", to_cents(carrier)),
		_step("rule", _rule_text(index, rule, zone), None),
	]
	if formula_result is not None:
		trace.append(_step("formula", _formula_text(rule, formula_result), to_cents(formula_result)))
	if final > base:
		added = _difference(final, base)
		trace.append(_step("round", f"Rounded up to the next whole dollar, which adds {_fmt(added)}.", added))
	trace.append(_step("result", f"Customer pays {_fmt(final)}.", final))
	return _offer(service_id, name, final, "rule", None, trace)


def _priced(policy, zone, index, quotes, totals):
	"""Price every usable quote with the rule at `index`, or send the whole order to the fallback."""
	rule = _as_dict(_as_list(policy.get("rules"))[index])
	try:
		offers = [_rule_offer(index, rule, zone, quote, totals) for quote in quotes]
	except FormulaError as error:
		return _fallback(policy, zone, index, f"Rule {index + 1} could not be priced. {error}")
	return _result(zone, index, None, "", offers)


# ---------------------------------------------------------------- fallbacks
def _fallback_choice(policy, zone):
	"""How the fallback ends: ("none", 0.0), ("free", 0.0) or ("fixed", the amount in cents).

	A fixed amount that is missing, not a number or below zero gives "none" rather than a free delivery.
	"""
	own = _as_dict(_as_dict(zone).get("fallback"))
	method = own.get("method") or "policy_default"
	if method == "policy_default":
		own = _as_dict(policy.get("default_fallback"))
		method = own.get("method") or "no_delivery"
	if method == "free":
		return "free", 0.0
	if method == "fixed":
		amount = _number(own.get("amount"))
		if amount is not None and amount >= 0:
			return "fixed", to_cents(amount)
	return "none", 0.0


def _fallback(policy, zone, rule=None, failure=None):
	"""The carrier gave no usable price, or a rule could not be priced: the fallback decides.

	`failure` is the sentence saying why a rule could not be priced; `rule` is that rule's position.
	"""
	kind, amount = _fallback_choice(policy, zone)
	if kind == "none":
		return _declined(zone, "formula_error" if failure else "not_offered", rule)
	reason = "formula_error" if failure else "fallback_offer"
	cause = failure or "The carrier gave no price for the whole shipment."
	what = "free delivery" if kind == "free" else f"a fixed charge of {_fmt(amount)}"
	trace = [
		_step("fallback", f"{cause} The fallback applies: {what}.", amount),
		_step("result", f"Customer pays {_fmt(amount)}.", amount),
	]
	offer = _offer(FALLBACK_SERVICE_ID, FALLBACK_SERVICE_NAME, amount, "fallback", reason, trace)
	return _result(zone, rule, reason, "", [offer])


def _not_covered(policy, zone, quotes):
	"""No zone, or no rule in the zone, covers the order: pass the carrier's price on, or offer nothing."""
	if (policy.get("not_covered") or "pass_through") != "pass_through":
		return _declined(zone, "not_covered")
	if not quotes:  # there is nothing to pass through, so the policy default decides
		return _fallback(policy, zone)
	offers = []
	for service_id, name, amount in quotes:
		price = to_cents(amount)
		trace = [
			_step("carrier", f"The carrier quoted {_fmt(amount)}.", price),
			_step("result", f"No rule applies, so the carrier's price is used: {_fmt(price)}.", price),
		]
		offers.append(_offer(service_id, name, price, "carrier", None, trace))
	return _result(zone, None, None, "", offers)


# ---------------------------------------------------------------- the public entry points
def resolve(policy: dict, order: dict) -> dict:
	"""Price an order. The result is {"policy_active", "zone", "rule", "offered", "reason", "message",
	"offers": [{"service_id", "name", "amount", "source", "reason", "trace"}]}.

	`rule` is the position (from 0) of the rule that priced the order, or of the rule that held but
	could not be priced (`formula_error`). `message` is empty whenever delivery is offered. Each trace
	step is {"kind", "text", "amount"}: what the carrier quoted, the rule, any formula, any rounding and
	what the customer pays.
	"""
	policy, order = _as_dict(policy), _as_dict(order)
	if not policy.get("active"):
		return _unchanged(order)
	country = normalize_country(order.get("country_code"))
	if not country or not order.get("served"):
		return _declined(None, "not_served")
	zone = select_zone(policy, country)
	if zone is not None and zone.get("not_shipped"):
		return _declined(zone, "not_shipped")
	quotes = _usable_quotes(order)
	if zone is None:
		return _not_covered(policy, None, quotes)
	if not quotes:
		return _fallback(policy, zone)
	totals = _totals(order)
	index = _first_rule(policy, zone, order, totals)
	if index is None:
		return _not_covered(policy, zone, quotes)
	return _priced(policy, zone, index, quotes, totals)


def rule_previews(policy: dict, order: dict) -> list[dict]:
	"""One entry per rule, in the policy's order: {"index", "in_zone", "matches", "amount"}.

	`in_zone`: the rule is for the zone this order lands in. `matches`: the rule is enabled and its
	conditions hold. `amount`: what the rule would charge the first usable quote, or None when it is
	not in the zone, does not match, cannot be priced, or the order would not be priced by rules at
	all (not served, a Not shipped zone, no whole quote). It ignores whether the policy is active, so
	a draft can be previewed before it goes live, and it prices a rule that an earlier one would pre-empt.
	"""
	policy, order = _as_dict(policy), _as_dict(order)
	zone = select_zone(policy, order.get("country_code"))
	totals = _totals(order)
	item_sets = _as_list(policy.get("item_sets"))
	quotes = _usable_quotes(order)
	priced_by_rules = (
		zone is not None and not zone.get("not_shipped") and bool(order.get("served")) and bool(quotes)
	)
	previews = []
	for index, rule in enumerate(_as_list(policy.get("rules"))):
		rule = _as_dict(rule)
		in_zone = _in_zone(rule, zone)
		matches = bool(rule.get("enabled")) and _holds(rule, order, totals, item_sets)
		amount = None
		if priced_by_rules and in_zone and matches:
			try:
				amount = _price(rule, quotes[0][2], totals)[1]
			except FormulaError:
				amount = None
		previews.append({"index": index, "in_zone": in_zone, "matches": matches, "amount": amount})
	return previews


# ---------------------------------------------------------------- checking a policy
def _join(names):
	return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _repeated(values):
	"""The non-blank values that occur more than once, in order of first appearance."""
	repeated = []
	for position, value in enumerate(values):
		if value and value in values[:position] and value not in repeated:
			repeated.append(value)
	return repeated


def _name_of(entry):
	name = entry.get("name")
	return name.strip() if isinstance(name, str) else ""


def _fallback_problems(subject, fallback, allowed):
	"""`subject` starts the sentence: 'Zone "GCC"' or 'The policy default'."""
	fallback = _as_dict(fallback)
	method = fallback.get("method")
	if method in (None, ""):
		return []
	if method not in allowed.values():
		return [f"{subject} has a choice for when the carrier can't quote that is not valid."]
	if method != "fixed":
		return []
	amount = _number(fallback.get("amount"))
	if amount is None:
		return [
			f"{subject} charges a fixed amount when the carrier can't quote, but the amount is not a number."
		]
	if amount < 0:
		return [f"{subject} charges a fixed amount below zero when the carrier can't quote."]
	return []


def _setting_problems(policy):
	problems = []
	if policy.get("not_covered") not in (None, "", *LABELS["not_covered"].values()):
		problems.append(
			'The setting for "Anything not covered" is not valid. It has to pass the carrier price through, '
			"or not offer delivery."
		)
	problems += _fallback_problems(
		"The policy default", policy.get("default_fallback"), LABELS["default_fallback"]
	)
	return problems


def _zone_problems(zones):
	problems = []
	refs = []  # how a sentence names each zone
	for position, zone in enumerate(zones, start=1):
		name = _name_of(zone)
		refs.append(f'"{name}"' if name else f"number {position}")
		if not name:
			problems.append(f"Zone {position} has no name.")
	for name in _repeated([_name_of(zone) for zone in zones]):
		problems.append(f'More than one zone is called "{name}". Zone names have to be different.')
	owners = {}  # country code -> the zones that list it, among the enabled zones
	catching_all = []
	for ref, zone in zip(refs, zones, strict=True):
		listed = [country for country in _as_list(zone.get("countries")) if country not in (None, "")]
		for country in listed:
			if not normalize_country(country):
				problems.append(f'Zone {ref} lists "{country}", which is not a two-letter country code.')
		problems += _fallback_problems(f"Zone {ref}", zone.get("fallback"), LABELS["zone_fallback"])
		if not zone.get("enabled"):
			continue
		if zone.get("everywhere_else"):
			catching_all.append(ref)
			if listed:
				shown = ", ".join(str(country) for country in listed)
				problems.append(
					f"Zone {ref} is set to Everywhere else, so it cannot also list countries ({shown})."
				)
			continue
		for code in dict.fromkeys(normalize_country(country) for country in listed):
			if code:
				owners.setdefault(code, []).append(ref)
	for code, refs_listing in owners.items():
		if len(refs_listing) > 1:
			problems.append(
				f"The country {code} is in more than one enabled zone ({_join(refs_listing)}). "
				"A country can be in only one enabled zone."
			)
	if len(catching_all) > 1:
		problems.append(
			f"More than one enabled zone is set to Everywhere else ({_join(catching_all)}). Only one is allowed."
		)
	return problems


def _item_set_problems(item_sets):
	problems = []
	for position, item_set in enumerate(item_sets, start=1):
		name = _name_of(item_set)
		if not name:
			problems.append(f"Item set {position} has no name.")
		entries = [*_as_list(item_set.get("item_groups")), *_as_list(item_set.get("items"))]
		if not any(_key(entry) for entry in entries):
			ref = f'"{name}"' if name else f"number {position}"
			problems.append(f"Item set {ref} has no item group and no item. Add at least one.")
	for name in _repeated([_name_of(item_set) for item_set in item_sets]):
		problems.append(f'More than one item set is called "{name}". Item set names have to be different.')
	return problems


def _band_problems(number, rule):
	problems = []
	for _, low_key, high_key, label in _BANDS:
		for key in (low_key, high_key):
			value = rule.get(key)
			if value in (None, ""):
				continue
			edge = _number(value)
			if edge is None:
				problems.append(f"Rule {number} has a limit on {label} that is not a number.")
			elif edge < 0:
				problems.append(f"Rule {number} has a negative limit on {label}.")
		low, high = _limit(rule, low_key), _limit(rule, high_key)
		if low and high and low >= high:
			problems.append(
				f"Rule {number} has its limits on {label} the wrong way round: from {low:.10g} up to {high:.10g}. "
				'"Up to" has to be higher than "from".'
			)
	return problems


def _charge_problems(number, rule):
	method = rule.get("method") or "carrier_price"
	if method not in LABELS["method"].values():
		return [
			f"Rule {number} does not say how to charge: it has to be the carrier price, free or a custom formula."
		]
	if method != "formula":
		return []
	formula = rule.get("formula")
	if not (isinstance(formula, str) and formula.strip()):
		return [f"Rule {number} charges by formula but has no formula."]
	try:
		check_formula(formula)
	except FormulaError as error:
		return [f"Rule {number} has a formula that cannot be used. {error}"]
	return []


def _rule_problems(rules, zones, item_sets):
	zone_names = [zone.get("name") for zone in zones]
	set_names = [item_set.get("name") for item_set in item_sets]
	problems = []
	for number, rule in enumerate(rules, start=1):
		rule = _as_dict(rule)
		zone, item_set = rule.get("zone"), rule.get("item_set")
		if not zone:
			problems.append(f"Rule {number} does not say which zone it is for.")
		elif zone not in zone_names:
			problems.append(f'Rule {number} is for the zone "{zone}", which does not exist.')
		if item_set and item_set not in set_names:
			problems.append(f'Rule {number} uses the item set "{item_set}", which does not exist.')
		problems += _charge_problems(number, rule)
		problems += _band_problems(number, rule)
	return problems


def validate_policy(policy: dict) -> list[str]:
	"""Everything wrong with a policy, as plain sentences naming the zone, the rule (counted from 1) or
	the country. An empty list means it can go live. It never raises, whatever the content.
	"""
	if not isinstance(policy, dict):
		return ["The policy is missing."]
	zones = [_as_dict(zone) for zone in _as_list(policy.get("zones"))]
	item_sets = [_as_dict(item_set) for item_set in _as_list(policy.get("item_sets"))]
	return [
		*_setting_problems(policy),
		*_zone_problems(zones),
		*_item_set_problems(item_sets),
		*_rule_problems(_as_list(policy.get("rules")), zones, item_sets),
	]
