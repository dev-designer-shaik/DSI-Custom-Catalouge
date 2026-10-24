"""The shipping policy on the ERP side: reading it out of the database, and pricing an order with it.

`load_policy` assembles the policy (the shape `shipping_engine` documents) from the three doctypes that hold
it: the Shipping Policy single with its rules, the Shipping Zones with their countries, and the Shipping
Item Sets with their groups and items. Everything else (the guest methods, the wizard) prices an order or
edits the policy through it, so what the quote path sees and what the form saves cannot drift apart.

Two read-only methods are open to guests, so that the website can call them without logging anyone in (it
calls only `price_delivery` so far):

	price_delivery   what a customer pays for delivery: the carrier's quotes run through the policy, or, when
	                 the policy's price source is the rate card, the card's price for the items ordered
	public_summary   the zones, countries and customer lines meant for the FAQ and the Shipping page, which
	                 the website does not show yet (nothing calls this method there)

What a guest gets back is public. Each method hands over only the fields `response_from` and
`public_summary_from` pick by name: never a trace, a rule number, a formula, a traceback or a line of the
rate card. What a guest sends is cleaned first (`InputError` becomes a plain `frappe.ValidationError`), and
every lookup is one query over what the guest listed, so the cost of a call is bounded by the limits of the
cleaners.

`build_order` is the one place a carrier order is assembled (cleaned inputs, item groups, order value, served
flag). `price_delivery` calls it for a carrier price, and so does the wizard's Try an order. Its lookups
(`item_groups_for`, `order_value_for`, `country_is_served`, `served_country_codes`, `translate`) are plain
functions, not open to guests.

The rate card prices from what the ERP knows about each item, not from what the website posts: the product
code (the index key of its Website Item) and the unit weight (`item_facts_for`). In rate-card mode the carrier
plays no part, so `price_delivery` reads nothing but the items (no cartons, quotes, item groups or order value,
and not the carrier's tables) and prices the lean order that `_card_order` makes. The wizard's Try an order
builds its order with `build_order(..., item_facts=True)` instead, and also when a fallback may price the order
from the card (`fallback_may_use_the_card`). So the live price and the preview are not made from one assembly of
the order, and what keeps them from drifting apart are the `test_try_order_and_price_delivery_...` parity tests
of `test_shipping_policy_wizard.py`: the saved policy, a rate-card policy, and a carrier policy whose fallback
is the card, each priced by Try an order and by `price_delivery` and compared.

Nothing is cached, here or in the quote path: every call reads the database again, so a policy change takes
effect on the next call.
"""

import re
from datetime import date
from functools import partial

import frappe
from frappe.model.base_document import BaseDocument
from frappe.utils import add_to_date, flt, getdate, now_datetime

from dsi_catalogue.shipping_engine import normalize_country, resolve
from dsi_catalogue.shipping_policy_data import (
	InputError,
	clean_boxes,
	clean_items,
	clean_quotes,
	policy_from_rows,
	public_summary_from,
	response_from,
)

POLICY_DOCTYPE = "Shipping Policy"
# The price list the storefront prices from. An order's value, which the rules' order-value limits compare
# against, is read from it and never taken from the caller.
PRICE_LIST = "USD - Online"

# A formula that cannot be calculated is noted in the Error Log, for a given rule at most once in this many
# minutes. What triggers it is a guest's input, and the table is MyISAM (nothing rolls a row back), so a row for
# every failing request would let anyone fill it.
FORMULA_ERROR_LOG_MINUTES = 10
FORMULA_ERROR_TITLE = "Shipping policy formula error"
_FORMULA_ERROR_TEXT_LIMIT = 2000

_ZONE_FIELDS = [
	"name",
	"enabled",
	"not_shipped",
	"everywhere_else",
	"customer_line",
	"not_offered_message",
	"fallback_method",
	"fallback_amount",
]


def load_policy(overrides: dict | None = None) -> dict:
	"""The saved policy, read fresh from the database as `policy_from_rows` shapes it.

	Zones and item sets come back sorted by name and rules in row order, so the same data always gives the
	same policy (and the same `snapshot_version`). Country codes are upper case. A rule keeps the name of
	its database row, which is what lets a draft be compared with the saved policy.

	`overrides` replaces fields of the policy row, `rules` included. A controller passes the unsaved document
	(`doc.as_dict()`, or the document itself) to check what a save would produce. The zones and item sets are
	always the saved ones.
	"""
	policy_row = frappe.get_doc(POLICY_DOCTYPE).as_dict()
	policy_row.update(_plain(overrides) or {})
	zone_rows = _zone_rows()
	return policy_from_rows(policy_row, zone_rows, _item_set_rows(), _country_codes(zone_rows))


def _plain(value):
	"""Documents, alone or in lists and dicts, as plain dicts: `policy_from_rows` reads a Document as nothing."""
	if isinstance(value, BaseDocument):
		return value.as_dict()
	if isinstance(value, dict):
		return {key: _plain(item) for key, item in value.items()}
	if isinstance(value, list | tuple):
		return [_plain(item) for item in value]
	return value


def _child_values(parent_doctype, child_doctype, fieldname, parents):
	"""The `fieldname` values of the rows of a child table, as {parent name: [value]} in row order."""
	found = {}
	if not parents:
		return found
	rows = frappe.get_all(
		child_doctype,
		filters={"parenttype": parent_doctype, "parent": ["in", parents]},
		fields=["parent", fieldname],
		order_by="idx asc",
		parent_doctype=parent_doctype,
	)
	for row in rows:
		found.setdefault(row["parent"], []).append(row[fieldname])
	return found


def _zone_rows():
	zones = sorted(frappe.get_all("Shipping Zone", fields=_ZONE_FIELDS), key=lambda zone: zone["name"])
	countries = _child_values("Shipping Zone", "Shipping Zone Country", "country", [z["name"] for z in zones])
	return [
		# A zone is known by its name, which is what a rule links to, whatever its name field says.
		{
			**zone,
			"zone_name": zone["name"],
			"countries": [{"country": c} for c in countries.get(zone["name"], [])],
		}
		for zone in zones
	]


def _item_set_rows():
	sets = sorted(frappe.get_all("Shipping Item Set", fields=["name"]), key=lambda item_set: item_set["name"])
	names = [item_set["name"] for item_set in sets]
	groups = _child_values("Shipping Item Set", "Shipping Item Set Group", "item_group", names)
	items = _child_values("Shipping Item Set", "Shipping Item Set Item", "item", names)
	return [
		{
			"set_name": item_set["name"],
			"item_groups": [{"item_group": g} for g in groups.get(item_set["name"], [])],
			"items": [{"item": i} for i in items.get(item_set["name"], [])],
		}
		for item_set in sets
	]


def _country_codes(zone_rows):
	"""The ISO code of each Country the zones use, as {Country name: code}. Only those rows are read."""
	used = sorted({row["country"] for zone in zone_rows for row in zone["countries"]})
	if not used:
		return {}
	rows = frappe.get_all("Country", filters={"name": ["in", used]}, fields=["name", "code"])
	return {row["name"]: row["code"] for row in rows}


# ---------------------------------------------------------------- the carriers that serve a country
# dsi_shipping's own doctypes. Pricing only reads them, and only to know whether anybody delivers somewhere.
_CARRIER_DOCTYPES = ("Shipping Service", "Shipping Service Country", "Shipping Provider")


def carrier_tables_exist() -> bool:
	"""Are dsi_shipping's doctypes installed on this site? The wizard asks too (call it through this module,
	so that one patch of it covers both)."""
	found = frappe.get_all("DocType", filters={"name": ["in", list(_CARRIER_DOCTYPES)]}, pluck="name")
	return len(found) == len(_CARRIER_DOCTYPES)


def _carrier_countries(code: str | None = None) -> list[tuple[str, float]] | None:
	"""(ISO code, weight limit in kg) for every country that an enabled Shipping Service of an enabled
	Shipping Provider lists, read the way `dsi_shipping.api.get_shipping_rates` reads it: one entry for each
	listing, the scope is not looked at, and a limit of 0 means there is none. `code` narrows it to one country
	(an upper-case ISO code). None when dsi_shipping's doctypes are not installed on this site.
	"""
	if not carrier_tables_exist():
		return None
	service, listing, provider = (frappe.qb.DocType(name) for name in _CARRIER_DOCTYPES)
	query = (
		frappe.qb.from_(listing)
		.join(service)
		.on(service.name == listing.parent)
		.join(provider)
		.on(provider.name == service.shipping_provider)
		.select(listing.country_code, service.max_weight_kg)
		.where((service.enabled == 1) & (provider.enabled == 1))
	)
	if code:
		query = query.where(listing.country_code == code)
	listed = [
		(normalize_country(row["country_code"]), flt(row["max_weight_kg"])) for row in query.run(as_dict=True)
	]
	return [(country, limit) for country, limit in listed if country]


def country_is_served(country_code, heaviest_box_kg) -> bool:
	"""Can a carrier deliver to this country, with cartons of up to this weight?

	True when some enabled Shipping Service of an enabled Shipping Provider lists the country and its weight
	limit is 0 (none) or at least the heaviest carton: the services `get_shipping_rates` would quote, which
	skips a service when `max_weight_kg and weight > max_weight_kg` (so a negative limit, which the form
	refuses and the database can hold, serves no carton). A code that is not two letters is not a country, so
	not served. When dsi_shipping is not installed there is nothing to check against and a country counts as
	served. Never raises on odd input.
	"""
	code = normalize_country(country_code)
	if not code:
		return False
	listed = _carrier_countries(code)
	if listed is None:
		return True
	weight = flt(heaviest_box_kg)
	return any(not limit or limit >= weight for _, limit in listed)


def served_country_codes() -> list[str]:
	"""The upper-case ISO codes of every country some enabled service of an enabled provider lists, sorted,
	whatever its weight limit. Every Country code when dsi_shipping is not installed."""
	listed = _carrier_countries()
	if listed is None:
		codes = (normalize_country(code) for code in frappe.get_all("Country", pluck="code"))
	else:
		codes = (code for code, _ in listed)
	return sorted({code for code in codes if code})


# ---------------------------------------------------------------- items: their groups and their price
def _is_utf8(text: str) -> bool:
	try:
		text.encode("utf-8")
	except UnicodeEncodeError:
		return False
	return True


def _database_codes(values) -> list[str]:
	"""The distinct, non-empty texts among `values`, in order, that the database can be asked about.

	A lone surrogate ("\\ud800") is text Python holds but nothing can write as UTF-8, so the database driver
	raises on it. The cleaners refuse such text, but these lookups are plain functions that anybody may call
	with anything: it is no item code the ERP knows, so it is left out of the query.
	"""
	return list(
		dict.fromkeys(value for value in values if isinstance(value, str) and value and _is_utf8(value))
	)


def item_groups_for(item_codes) -> dict[str, list[str]]:
	"""Each item's group and every group above it, as {item code: [group, ...]}, nearest group first and
	"All Item Groups" last (the Item Group tree is a nested set, so its `lft` and `rgt` give the ancestors).

	An item the ERP does not know, or one that has no group, maps to []. Every text in `item_codes` is a key.
	One query reads the whole list.
	"""
	texts = [code for code in item_codes if isinstance(code, str)]
	found = {code: [] for code in texts}
	codes = _database_codes(texts)
	if not codes:
		return found
	item, group = frappe.qb.DocType("Item"), frappe.qb.DocType("Item Group")
	above = frappe.qb.DocType("Item Group", alias="above")
	rows = (
		frappe.qb.from_(item)
		.left_join(group)
		.on(group.name == item.item_group)
		.left_join(above)
		.on((above.lft <= group.lft) & (above.rgt >= group.rgt))
		.select(item.name, item.item_group, above.name.as_("above_name"), above.lft.as_("above_lft"))
		.where(item.name.isin(codes))
	).run(as_dict=True)
	chains = {}
	for row in rows:
		chain = chains.setdefault(row["name"], {"own": row["item_group"], "above": []})
		if row["above_name"]:
			chain["above"].append((row["above_lft"], row["above_name"]))
	for code, chain in chains.items():
		if code in found:
			nearest_first = [name for _, name in sorted(chain["above"], reverse=True)]
			found[code] = list(dict.fromkeys(name for name in (chain["own"], *nearest_first) if name))
	return found


def _prices_for(item_codes) -> dict[str, float]:
	"""{item code: its selling price in the USD - Online price list}, read with one query.

	An item may have several price rows. The one used is the one valid today (no start, or a start that has
	come, and no end, or an end that has not passed), then the one with the latest start, then the one
	changed last. An item with no row is not in the result.
	"""
	codes = _database_codes(item_codes)
	if not codes:
		return {}
	rows = frappe.get_all(
		"Item Price",
		filters={"price_list": PRICE_LIST, "selling": 1, "item_code": ["in", codes]},
		fields=["name", "item_code", "price_list_rate", "valid_from", "valid_upto", "modified"],
	)
	today = getdate()

	def preference(row):
		start = getdate(row["valid_from"]) if row["valid_from"] else None
		end = getdate(row["valid_upto"]) if row["valid_upto"] else None
		valid_today = (start is None or start <= today) and (end is None or end >= today)
		return valid_today, start or date.min, row["modified"], row["name"]

	best = {}
	for row in rows:
		held = best.get(row["item_code"])
		if held is None or preference(row) > preference(held):
			best[row["item_code"]] = row
	return {code: flt(row["price_list_rate"]) for code, row in best.items()}


def order_value_for(items) -> float:
	"""What the items cost in USD - Online: each item's selling price times its quantity, added up and rounded
	to cents. An item the ERP does not know, or one with no price there, counts as 0. One query reads the
	prices of the whole list. `items` are cleaned items, [{"item_code", "qty"}].
	"""
	lines = [
		(item["item_code"], flt(item.get("qty")))
		for item in items
		if isinstance(item, dict) and isinstance(item.get("item_code"), str)
	]
	prices = _prices_for(code for code, _ in lines)
	return flt(sum(prices.get(code, 0.0) * qty for code, qty in lines), 2)


def item_facts_for(item_codes) -> dict[str, dict]:
	"""What the rate card prices from, as {item code: {"index_key", "weight_kg"}}.

	`index_key` is the `custom_index_key` of the item's Website Item, as stored ("{F-CR-OSA-M-PT}": the card
	takes off the braces), whether or not the Website Item is published; if there are several, the first by
	name. `weight_kg` is the Item's `weight_per_unit` as a number. An item with no Website Item, or none with
	a key, has the key "" and one the ERP has no weight for has 0.0: the card puts an empty key in the
	profile that takes everything and counts a weight of 0 as unknown (it uses its default weight).

	Every text in `item_codes` is a key of the result. The lookup matches an item code the way
	`item_groups_for` does: the database ignores case, so it may return a row whose name differs in case from
	what was asked, and only an exact match is used. Two queries read the whole list (one on Website Item, one
	on Item), and none is run when there is nothing the database could be asked.
	"""
	texts = [code for code in item_codes if isinstance(code, str)]
	keys, weights = {}, {}
	if codes := _database_codes(texts):
		web_items = frappe.get_all(
			"Website Item",
			filters={"item_code": ["in", codes]},
			fields=["item_code", "custom_index_key"],
			order_by="name asc",
		)
		for row in web_items:
			keys.setdefault(row["item_code"], (row["custom_index_key"] or "").strip())
		items = frappe.get_all("Item", filters={"name": ["in", codes]}, fields=["name", "weight_per_unit"])
		weights = {row["name"]: flt(row["weight_per_unit"]) for row in items}
	return {code: {"index_key": keys.get(code, ""), "weight_kg": weights.get(code, 0.0)} for code in texts}


# ---------------------------------------------------------------- the order the engine prices
def build_order(country_code, items, boxes, quotes, order_value=None, item_facts=False) -> dict:
	"""The order dict `shipping_engine.resolve` reads for a carrier's price, the one place it is assembled
	(the rate card needs far less than this and has its own, `_card_order`).

	`items` (as `clean_items` returns them), `boxes` (`clean_boxes`) and `quotes` (`clean_quotes`) are already
	cleaned. Each item gets its groups, the order's value is the one `order_value_for` finds, and `served` says
	whether a carrier can deliver to the country with the heaviest carton. `country_code` is passed on as it
	was given: the engine reads it loosely. `order_value` replaces the computed value (the wizard's Try an
	order passes one a person typed; a typed 0 counts); `price_delivery` never passes it.

	With `item_facts` each item also gets the `index_key` and `weight_kg` of `item_facts_for`, which the rate
	card prices from, at the cost of two more queries for the whole order. Without it the order is exactly
	what it was before the rate card existed.
	"""
	codes = [item["item_code"] for item in items]
	groups = item_groups_for(codes)
	facts = item_facts_for(codes) if item_facts else {}
	return {
		"country_code": country_code,
		"items": [
			{
				"item_code": item["item_code"],
				"qty": item["qty"],
				"groups": groups.get(item["item_code"], []),
				**facts.get(item["item_code"], {}),
			}
			for item in items
		],
		"order_value": order_value_for(items) if order_value is None else order_value,
		"boxes": boxes,
		"quotes": quotes,
		"served": country_is_served(country_code, max((box["weight_kg"] for box in boxes), default=0.0)),
	}


def _card_order(country_code, items) -> dict:
	"""The order the rate card prices, and nothing more: the destination and each item with its product code
	and unit weight. The card reads no item group, order value, carton or quote, and it does not ask whether
	a carrier serves the country, so none of that is looked up (`served` is only there for the engine)."""
	facts = item_facts_for(item["item_code"] for item in items)
	return {
		"country_code": country_code,
		"items": [
			{"item_code": item["item_code"], "qty": item["qty"], **facts[item["item_code"]]} for item in items
		],
		"served": True,
	}


def fallback_may_use_the_card(policy) -> bool:
	"""Does this policy price an order from the rate card when the carrier cannot quote it? That is so when
	the policy default is "Use the rate card", or the fallback of an enabled zone is.

	Public because the wizard's Try an order asks the same question of a draft, so that the preview reads the
	item facts exactly when the live method does."""
	if policy["default_fallback"]["method"] == "rate_card":
		return True
	return any(zone["enabled"] and zone["fallback"]["method"] == "rate_card" for zone in policy["zones"])


def _order_for(policy, country_code, items, boxes, quotes) -> dict:
	"""The order `resolve` prices, from what a guest posted. Only what the policy reads is cleaned and looked
	up.

	A policy that is off reads the quotes alone. With the rate card as the price source it reads the items
	alone, so a bad `boxes` or `quotes` cannot fail the call. Otherwise it reads everything, and the items'
	facts as well when a fallback may price the order from the card. Raises `InputError` for bad input.
	"""
	if not policy["active"]:
		return {"country_code": country_code, "quotes": clean_quotes(quotes)}
	if policy["price_source"] == "rate_card":
		return _card_order(country_code, clean_items(items))
	return build_order(
		country_code,
		clean_items(items),
		clean_boxes(boxes),
		clean_quotes(quotes),
		item_facts=fallback_may_use_the_card(policy),
	)


# ---------------------------------------------------------------- languages
# What a Frappe language is called: ar, fr, es-CO, zh-TW. Anything else a guest sends is not a language, and
# at 21 characters at most nothing long reaches the database.
_LANGUAGE_CODE = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,2}")


def _known_language(language) -> str | None:
	"""The name of the Language record `language` points at, or None (so: English).

	A guest chooses this text. It is looked up only when it is shaped like a language code, which keeps odd
	text away from the database and keeps Frappe, which stores the translations of each language it is
	asked about, from storing them for texts that are no language. The database matches the name as it
	matches any name, so "AR" finds the language "ar", and the stored name is what comes back.
	"""
	if not isinstance(language, str) or not _LANGUAGE_CODE.fullmatch(language):
		return None
	return frappe.db.exists("Language", language) or None


def _translated(text, language):
	return text if language is None else frappe._(text, lang=language)


def translate(text, language):
	"""`text` in `language`: the ERP's Translation record of it when there is one, else the English text.

	`language` is whatever a guest sent. Unless it names a Language record the text stays English, and
	nothing here raises because of it.
	"""
	return _translated(text, _known_language(language))


# ---------------------------------------------------------------- the guest methods
def _log_formula_error(policy, result, country_code):
	"""Note in the Error Log that a rule's formula could not be calculated for an order, which then went to
	the fallback. For a given rule that is done at most once in FORMULA_ERROR_LOG_MINUTES.

	The entry names the rule, its formula, the zone and the country, so that whoever keeps the policy can fix
	it, and nothing about the customer or what was ordered. It is inserted as a document of its own, never
	through `frappe.log_error`, which copies the whole request (the cart, the cartons, the quotes) into the
	entry's metadata. It refers to the rule's row (`Shipping Policy Rule`), and the throttle is a look at the
	Error Log for an entry that refers to the same row and is newer than the window (no cache is involved).

	It is best-effort: whatever goes wrong here, the customer's answer is not affected. The `except` does not
	call `frappe.log_error` either, because that would bring the flood back.
	"""
	try:
		index = result.get("rule")
		rules = policy.get("rules") or []
		rule = rules[index] if isinstance(index, int) and 0 <= index < len(rules) else {}
		reference = {
			"reference_doctype": "Shipping Policy Rule",
			"reference_name": rule.get("name") or f"rule-{index}",
		}
		since = add_to_date(now_datetime(), minutes=-FORMULA_ERROR_LOG_MINUTES)
		if frappe.db.exists("Error Log", {**reference, "creation": [">", since]}):
			return
		label = (rule.get("label") or "").strip()
		named = f' ("{label}")' if label else ""
		number = index + 1 if isinstance(index, int) else "?"
		text = (
			f"Rule {number}{named} could not be priced, so the order went to the zone's fallback.\n"
			f"Formula: {rule.get('formula') or ''}\n"
			f"Zone: {result.get('zone')}\n"
			f"Country: {normalize_country(country_code)}"
		)
		frappe.get_doc(
			{
				"doctype": "Error Log",
				"method": FORMULA_ERROR_TITLE,
				"error": text[:_FORMULA_ERROR_TEXT_LIMIT],
				**reference,
			}
		).insert(ignore_permissions=True)
	except Exception:
		return


@frappe.whitelist(allow_guest=True)
def price_delivery(country_code=None, items=None, boxes=None, quotes=None, language=None) -> dict:
	"""What a customer pays for delivery: the carrier's quotes for the order run through the shipping policy,
	or, when the policy's price source is the rate card, the card's price for the items ordered.

	Arguments (each list may be a list or JSON text of one, as a form post sends it; every argument is
	optional, and a missing one is answered like an empty one):

		country_code  where it goes, as the customer wrote it: " ae " is AE; anything that is not two letters,
		              or nothing, is a destination nobody delivers to, not an error
		items         [{"item_code", "qty"}]: at most 200; the ERP supplies each item's groups, price, product
		              code and weight
		boxes         [{"weight_kg", "volume_l"}]: the packed cartons, at most 50, both measures required
		quotes        [{"service_id", "name", "amount", "boxes_quoted"}]: the carrier's price per service and
		              how many cartons that service quoted, at most 20
		language      a Language the ERP has Translation records for. Anything else is English.

	Returns {"policy_active", "price_source", "zone", "offered", "reason", "message", "offers":
	[{"service_id", "name", "amount", "source", "reason", "estimated_days"}]}: whether the policy is on, its
	price source ("carrier" or "rate_card"), the zone the country falls in, whether delivery is offered at
	all, the reason code when it is not (or when a fallback priced it), the customer-facing message and one
	offer per delivery method, each with the days the rate card promises (null when it makes no promise).
	Nothing else is returned: no rule number, trace, formula or line of the rate card.

	With the policy on and the rate card as its price source, only `country_code` and `items` are read: the
	order is priced from the card, `boxes` and `quotes` are ignored (they are not even parsed, so bad ones
	cannot fail the call), the carrier's tables are not looked at and there is one offer, the service
	"rate-card". With the carrier as the source nothing changes, except that a policy whose fallback is "Use
	the rate card" also looks up the items' product codes and weights.

	Numbers that are negative, not finite or above a billion, lists over their limit, text where a number
	belongs and ids or names that are not plain text (a control character or a lone surrogate, which JSON can
	carry but nothing can write) are refused with a plain `frappe.ValidationError`. While the policy is off the
	quotes come back as they were posted, and only the quotes are checked. The policy is read afresh on every
	call. A rule whose formula cannot be calculated sends the order to the fallback and is noted in the Error
	Log, at most once in FORMULA_ERROR_LOG_MINUTES for each rule, whether the fallback then prices the order or
	not; a rate card that cannot price an order is an ordinary refusal and is not noted, unless a rule's formula
	failed first (the card was its fallback).
	"""
	policy = load_policy()
	try:
		order = _order_for(policy, country_code, items, boxes, quotes)
	except InputError as error:
		frappe.throw(str(error), frappe.ValidationError)
	result = resolve(policy, order)
	# A rule whose formula failed is named in the answer, whose reason is then "formula_error", unless the
	# fallback was the rate card and the card could not price the order either: the answer then carries the
	# card's reason, and a rule named with no offer is what shows a formula failed. (A rule that priced the
	# order always has offers.)
	if result.get("reason") == "formula_error" or (
		result.get("rule") is not None and not result.get("offered")
	):
		_log_formula_error(policy, result, order["country_code"])
	return response_from(result, partial(_translated, language=_known_language(language)))


def _country_names(policy) -> dict[str, str]:
	"""{ISO code: Country name} for the countries of the zones a customer may be told about."""
	codes = {
		code
		for zone in policy["zones"]
		if zone["enabled"] and not zone["everywhere_else"]
		for code in zone["countries"]
	}
	if not codes:
		return {}
	rows = frappe.get_all(
		"Country", filters={"code": ["in", sorted(code.lower() for code in codes)]}, fields=["name", "code"]
	)
	return {row["code"].upper(): row["name"] for row in rows if row["code"]}


@frappe.whitelist(allow_guest=True)
def public_summary(language=None) -> dict:
	"""What the FAQ and the Shipping page may show (the website does not call this yet): {"active": False,
	"zones": []} while the policy is off, otherwise {"active": True, "zones": [{"name", "countries":
	[{"code", "name"}], "customer_line", "delivers"}]}, the enabled zones only. Nothing else (no rule,
	formula, fallback or threshold).

	Each zone's customer line is in `language` when the ERP has a Translation record of it, else English.
	Country names are English: the website localises them from the codes. The policy is read afresh on
	every call.
	"""
	policy = load_policy()
	names = _country_names(policy) if policy["active"] else {}
	return public_summary_from(policy, names, partial(_translated, language=_known_language(language)))
