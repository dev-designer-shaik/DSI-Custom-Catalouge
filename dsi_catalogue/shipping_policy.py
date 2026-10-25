"""Fresh database reads and the two-call shipping contract. No price-path cache."""

import json
import re
from datetime import date

import frappe
from frappe.model.base_document import BaseDocument
from frappe.utils import flt, getdate

from dsi_catalogue.shipping_engine import normalize_country, resolve, shipments_of
from dsi_catalogue.shipping_policy_data import (
	InputError,
	clean_items,
	clean_shipment_quotes,
	policy_from_rows,
	public_summary_from,
	response_from,
	snapshot_version,
)

POLICY_DOCTYPE = "Shipping Policy"
PRICE_LIST = "USD - Online"
_ZONE_FIELDS = ["name", "enabled", "not_shipped", "customer_line", "not_offered_message"]
_CARRIER_DOCTYPES = ("Shipping Service", "Shipping Service Country", "Shipping Provider")


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
	sets = frappe.get_all("Shipping Item Set", fields=["name"], order_by="priority asc, name asc")
	names = [item_set["name"] for item_set in sets]
	groups = _child_values("Shipping Item Set", "Shipping Item Set Group", "item_group", names)
	items = _child_values("Shipping Item Set", "Shipping Item Set Item", "item", names)
	patterns = _child_values("Shipping Item Set", "Shipping Item Set Pattern", "pattern", names)
	return [
		{
			"set_name": item_set["name"],
			"patterns": [{"pattern": p} for p in patterns.get(item_set["name"], [])],
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


def carrier_tables_exist() -> bool:
	"""Are dsi_shipping's doctypes installed on this site? The wizard asks too (call it through this module,
	so that one patch of it covers both)."""
	found = frappe.get_all("DocType", filters={"name": ["in", list(_CARRIER_DOCTYPES)]}, pluck="name")
	return len(found) == len(_CARRIER_DOCTYPES)


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


def _country_names(policy) -> dict[str, str]:
	"""{ISO code: Country name} for the countries of the zones a customer may be told about."""
	codes = {
		code
		for zone in policy["zones"]
		if zone["enabled"]
		for code in zone["countries"]
	}
	if not codes:
		return {}
	rows = frappe.get_all(
		"Country", filters={"code": ["in", sorted(code.lower() for code in codes)]}, fields=["name", "code"]
	)
	return {row["code"].upper(): row["name"] for row in rows if row["code"]}


def services_directory():
	if not carrier_tables_exist():
		return None
	providers = {
		p["name"]: p for p in frappe.get_all("Shipping Provider", fields=["name", "enabled", "carrier_code"])
	}
	extra_fields = []
	if frappe.get_meta("Shipping Service").has_field("restricted_item_group"):
		extra_fields = ["restricted_item_group", "required_for_group", "live_rates_only", "requirement_scope"]
	services = frappe.get_all(
		"Shipping Service",
		fields=[
			"name",
			"service_name",
			"shipping_provider",
			"scope",
			"enabled",
			"estimated_days",
			"max_weight_kg",
			*extra_fields,
		],
		order_by="name asc",
	)
	if not services:
		return None
	countries = _child_values(
		"Shipping Service", "Shipping Service Country", "country_code", [s["name"] for s in services]
	)
	return [
		{
			"name": s["name"],
			"label": s["service_name"],
			"provider": s["shipping_provider"],
			"carrier_code": providers.get(s["shipping_provider"], {}).get("carrier_code"),
			"scope": s["scope"],
			"enabled": bool(s["enabled"] and providers.get(s["shipping_provider"], {}).get("enabled")),
			"estimated_days": s["estimated_days"],
			"max_weight_kg": flt(s["max_weight_kg"]),
			"countries": [normalize_country(c) for c in countries.get(s["name"], [])],
			**{field: s.get(field) for field in extra_fields},
		}
		for s in services
	]


def build_order(country_code, items):
	codes = [i["item_code"] for i in items]
	groups, facts = item_groups_for(codes), item_facts_for(codes)
	directory = services_directory()
	access = None
	if directory is not None:
		try:
			from dsi_shipping.service_restrictions import restriction_context
		except ImportError:
			pass  # Older shipping installations have no product restrictions.
		else:
			access = restriction_context(items, normalize_country(country_code))
	if access is not None:
		directory = [s for s in directory if s["name"] in access["allowed_services"]]
	return {
		"country_code": normalize_country(country_code),
		"items": [
			{**i, **facts.get(i["item_code"], {}), "groups": groups.get(i["item_code"], [])} for i in items
		],
		"directory": directory,
		"required_service": bool(access and access["required"]),
		"live_rates_only": bool(directory)
		and all(s.get("live_rates_only") for s in directory if s["enabled"]),
		"order_value": order_value_for(items),
	}


def plan_token(policy, order, shipments):
	# Includes item facts and service eligibility, not just the rule ids: a changed weight/directory invalidates it.
	return snapshot_version(
		{
			"policy": policy,
			"country": order["country_code"],
			"items": order["items"],
			"directory": order["directory"],
			"required_service": order.get("required_service", False),
			"shipments": shipments,
		}
	)


def translate(text, language=None):
	if isinstance(language, str) and re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?", language):
		return frappe._(text, lang=language)
	return frappe._(text)


@frappe.whitelist(allow_guest=True)
def price_delivery(country_code=None, items=None, shipment_quotes=None, plan_token=None, language=None):
	try:
		policy = load_policy()
		if not policy["active"]:
			return response_from(resolve(policy, {}), lambda s: translate(s, language))
		cleaned = clean_items(items)
		order = build_order(country_code, cleaned)
		result = resolve(policy, order)
		if result.get("needs_quotes"):
			token = globals()["plan_token"](policy, order, result["shipments"])
			if shipment_quotes is not None:
				quotes = clean_shipment_quotes(shipment_quotes)
				wanted = {str(s["index"]) for s in result["shipments"] if s["quote"]}
				if plan_token != token or set(quotes) != wanted:
					return {
						"policy_active": True,
						"offered": False,
						"needs_quotes": False,
						"offers": [],
						"shipments": [],
						"reason": "stale_plan",
						"message": "Delivery changed. Please request a fresh quote.",
					}
				order["shipment_quotes"] = quotes
				result = resolve(policy, order)
			answer = response_from(result, lambda s: translate(s, language))
			if result.get("needs_quotes"):
				answer["plan_token"] = token
			return answer
		return response_from(result, lambda s: translate(s, language))
	except InputError as e:
		frappe.throw(str(e))


@frappe.whitelist(allow_guest=True)
def public_summary(language=None):
	policy = load_policy()
	return public_summary_from(policy, _country_names(policy), lambda s: translate(s, language))


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
		if not preference(row)[0]:
			continue
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


@frappe.whitelist(allow_guest=True)
def delivery_services():
	"""Public booking identity only; never expose provider credentials or account settings."""
	return [
		{k: service.get(k) for k in ("name", "provider", "carrier_code")}
		for service in services_directory() or []
		if service["enabled"]
	]
