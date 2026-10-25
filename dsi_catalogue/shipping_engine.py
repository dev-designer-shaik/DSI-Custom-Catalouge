"""One policy: product groups own products; their country rows price separate shipments.

Pure, bounded and fail-closed. Currency uses Decimal; an absent quote is never zero.
"""

import fnmatch
import math
import re
import unicodedata
from decimal import ROUND_HALF_UP, Decimal

from dsi_catalogue.shipping_formula import FormulaError, check_formula, evaluate

LABELS = {
	"method": {"Weight table": "table", "Carrier price": "carrier", "Formula": "formula"},
	"fallback": {"Don't offer delivery": "no_delivery", "Charge a flat amount": "fixed"},
}
DEFAULT_MESSAGES = {
	"not_served": "No selected delivery service can carry this order to this country.",
	"not_shipped": "We do not deliver to this country.",
	"not_covered": "These products are not available for delivery to this country.",
	"too_heavy": "This order exceeds the delivery weight limit.",
	"not_offered": "Delivery could not be priced. Please try again later.",
	"invalid": "The delivery request could not be priced.",
}
RATE_CARD_SERVICE_ID = "rate-card"
REASONS = (*tuple(DEFAULT_MESSAGES), "fallback_offer", "inactive", "stale_plan")


def number(v):
	return type(v) in (int, float) and 0 <= v <= 10**9 and math.isfinite(v)


def to_cents(v):
	return float(Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def normalize_country(code):
	return (
		code.strip().upper() if isinstance(code, str) and re.fullmatch(r"[A-Za-z]{2}", code.strip()) else ""
	)


def name_key(value):
	return "".join(
		c for c in unicodedata.normalize("NFKD", str(value)).casefold() if not unicodedata.combining(c)
	)


def pattern_matches(pattern, code):
	"""Bounded glob DP: O(80 * code length), no backtracking or regex from user input."""
	if (
		not isinstance(pattern, str)
		or not isinstance(code, str)
		or not pattern
		or len(pattern) > 80
		or pattern.count("*") > 12
	):
		return False
	pattern, code = pattern.upper(), code.strip().strip("{}").upper()
	tokens = []
	i = 0
	while i < len(pattern):
		end = pattern.find("]", i + 2) if pattern[i] == "[" else -1
		if end >= 0:
			tokens.append(pattern[i : end + 1])
			i = end + 1
		else:
			tokens.append(pattern[i])
			i += 1
	prev = [True] + [False] * len(tokens)
	for j, token in enumerate(tokens, 1):
		prev[j] = prev[j - 1] and token == "*"
	for char in code:
		curr = [False] * (len(tokens) + 1)
		for j, token in enumerate(tokens, 1):
			curr[j] = (
				(curr[j - 1] or prev[j])
				if token == "*"
				else prev[j - 1]
				and (
					token == "?"
					or token == char
					or (token.startswith("[") and fnmatch.fnmatchcase(char, token))
				)
			)
		prev = curr
	return prev[-1]


def item_in_set(item, group):
	return (
		item.get("item_code") in group.get("items", [])
		or bool(set(item.get("groups", [])) & set(group.get("item_groups", [])))
		or any(pattern_matches(p, item.get("index_key", "")) for p in group.get("patterns", []))
	)


def tier_price(table, weight_lb):
	for tier in table:
		bound = tier.get("up_to_lb")
		if bound is None or weight_lb <= bound:
			return to_cents(tier["price"])
	return None


def _result(reason=None, offers=None, *, active=True, shipments=None, needs_quotes=False, message=None):
	return {
		"policy_active": active,
		"offered": bool(offers),
		"reason": reason,
		"message": message or DEFAULT_MESSAGES.get(reason, ""),
		"offers": offers or [],
		"needs_quotes": needs_quotes,
		"shipments": shipments or [],
	}


def shipments_of(policy, order):
	groups = {g["name"]: g for g in policy["item_sets"]}
	keys = list(groups) + ([""] if any(not r.get("item_set") for r in policy["rules"]) else [])
	shipments = {}
	for item in order["items"]:
		key = next((k for k in keys if not k or item_in_set(item, groups.get(k, {}))), None)
		if key is None:
			raise ValueError("not_covered")
		s = shipments.setdefault(
			key, {"index": len(shipments), "set": key, "items": [], "weight_kg": Decimal(0), "warnings": []}
		)
		s["items"].append({"item_code": item["item_code"], "qty": item["qty"]})
		weight = item.get("weight_kg")
		if not number(weight) or weight == 0:
			weight = 0.35
			s["warnings"].append("Weight estimated for " + item["item_code"])
		s["weight_kg"] += Decimal(str(weight)) * Decimal(str(item["qty"]))
	zones = {z["name"]: z for z in policy["zones"] if z.get("enabled", True)}
	country = normalize_country(order["country_code"])
	for key, s in shipments.items():
		rows = [(i, r) for i, r in enumerate(policy["rules"]) if (r.get("item_set") or "") == key]
		chosen = next(
			((i, r) for i, r in rows if r.get("zone") in zones and country in zones[r["zone"]]["countries"]),
			None,
		)
		chosen = chosen or next(((i, r) for i, r in rows if not r.get("zone")), None)
		if chosen is None:
			raise ValueError("not_covered")
		i, row = chosen
		s.update(
			row=i,
			services=row.get("services") or None,
			quote=row["method"] in ("carrier", "formula"),
			weight_lb=float(
				(s["weight_kg"] * Decimal("2.20462262")).quantize(Decimal(".001"), rounding=ROUND_HALF_UP)
			),
			weight_kg=float(s["weight_kg"]),
		)
	return list(shipments.values())


def serves(service, country, weight):
	return bool(
		service.get("enabled")
		and country in service.get("countries", [])
		and (not service.get("max_weight_kg") or weight <= service["max_weight_kg"])
	)


def _eligible(directory, selected, country, weight):
	if directory is None:
		return set(selected) if selected else None
	return {
		s["name"] for s in directory if (not selected or s["name"] in selected) and serves(s, country, weight)
	}


def _resolve(policy, order):
	if not policy.get("active"):
		return _result("inactive", active=False)
	country = normalize_country(order.get("country_code"))
	if not country:
		return _result("not_served")
	for z in policy["zones"]:
		if z.get("enabled", True) and z.get("not_shipped") and country in z["countries"]:
			return _result("not_shipped", message=z.get("not_offered_message"))
	if not order.get("items") or len(order["items"]) > 200:
		return _result("invalid")
	for item in order["items"]:
		if not isinstance(item.get("item_code"), str) or not number(item.get("qty")) or item["qty"] == 0:
			return _result("invalid")
	try:
		shipments = shipments_of(policy, order)
	except ValueError as e:
		return _result(str(e))
	if len(shipments) > 10:
		return _result("invalid")
	directory = order.get("directory")
	pending, parts, fallback_used = [], [], False
	for s in shipments:
		r = policy["rules"][s["row"]]
		given = order.get("shipment_quotes", {}).get(str(s["index"]))
		weight = (
			max((b["weight_kg"] for b in given.get("boxes", [])), default=0)
			if s["quote"] and given is not None
			else (0 if s["quote"] else s["weight_kg"])
		)
		eligible = _eligible(directory, s["services"], country, weight)
		if s["quote"] and eligible == set():
			return _result("not_served", shipments=shipments)
		if s["quote"] and given is None:
			pending.append(s)
			continue
		prices = {}
		flat = None
		if s["quote"]:
			boxes = given.get("boxes", [])
			for q in given.get("quotes", []):
				name = q.get("service_id")
				if (
					boxes
					and number(q.get("amount"))
					and q.get("boxes_quoted") == len(boxes)
					and (eligible is None or name in eligible)
				):
					try:
						amount = (
							evaluate(
								r.get("formula", ""),
								{
									"carrier_price": q["amount"],
									"order_value": order.get("order_value", 0),
									"weight_kg": sum(b["weight_kg"] for b in boxes),
									"volume_l": sum(b.get("volume_l", 0) for b in boxes),
									"boxes": len(boxes),
									"item_count": sum(i["qty"] for i in s["items"]),
								},
							)
							if r["method"] == "formula"
							else q["amount"]
						)
						if not number(amount):
							raise FormulaError("The formula did not produce a valid price.")
						prices[name] = to_cents(amount)
					except FormulaError:
						s["warnings"].append("The formula could not price " + name)
			if not prices:
				fb = policy.get("fallback", {})
				if fb.get("method") != "fixed" or not number(fb.get("amount")):
					return _result("not_offered", shipments=shipments)
				flat = to_cents(fb["amount"])
				fallback_used = True
				eligible = None
			else:
				eligible = set(prices)
		else:
			flat = tier_price(r["price_table"], s["weight_lb"])
			if flat is None:
				return _result("too_heavy", shipments=shipments)
			if not s["services"] or not eligible:
				if s["services"]:
					s["warnings"].append(
						"Selected services cannot carry this shipment; delivery will be arranged by hand."
					)
				eligible = None
		parts.append({"shipment": s, "row": r, "services": eligible, "prices": prices, "flat": flat})
	if pending:
		return _result(shipments=shipments, needs_quotes=True)
	concrete = [p["services"] for p in parts if p["services"] is not None]
	common = set.intersection(*concrete) if concrete else set()
	carrier = any(p["shipment"]["quote"] for p in parts)
	if not common and carrier and not fallback_used:
		return _result("not_covered", shipments=shipments)
	if fallback_used:
		# A carrier shipment with real quotes still contributes its lowest usable quote to a manual offer.
		common = set()
	by_service = {s["name"]: s for s in directory or []}
	offers = []
	for service_id in sorted(common) if common else ["rate-card"]:
		amounts = [
			p["flat"] if p["flat"] is not None else p["prices"].get(service_id, min(p["prices"].values()))
			for p in parts
		]
		amount = to_cents(sum(Decimal(str(v)) for v in amounts))
		longest = max((p["row"] for p in parts), key=lambda r: r.get("days") or 0)
		service = by_service.get(service_id, {})
		plain = service_id == "rate-card"
		trace = [
			{
				"kind": "shipment",
				"text": (p["shipment"]["set"] or "Everything else"),
				"amount": v,
				"row": p["shipment"]["row"],
				"weight_lb": p["shipment"]["weight_lb"],
				"warnings": p["shipment"]["warnings"],
			}
			for p, v in zip(parts, amounts, strict=False)
		]
		offers.append(
			{
				"service_id": service_id,
				"name": (longest.get("label") or ("Standard delivery" if fallback_used else "Delivery"))
				if plain
				else service.get("label", service_id),
				"amount": amount,
				"source": "fallback"
				if fallback_used
				else (
					"formula"
					if any(p["row"]["method"] == "formula" for p in parts)
					else "carrier"
					if carrier
					else "table"
				),
				"reason": "fallback_offer" if fallback_used else None,
				"estimated_days": longest.get("days") if plain else service.get("estimated_days"),
				"provider": None if plain else service.get("provider"),
				"carrier_code": None if plain else service.get("carrier_code"),
				"scope": None if plain else service.get("scope"),
				"trace": trace,
			}
		)
	return _result("fallback_offer" if fallback_used else None, offers, shipments=shipments)


def resolve(policy, order):
	try:
		return _resolve(policy, order)
	except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError, RecursionError):
		return _result("invalid")


def validate_policy(policy):
	try:
		return _validate(policy)
	except (TypeError, ValueError, KeyError, AttributeError, ArithmeticError):
		return ["The policy is not in the expected shape."]


def _validate(policy):
	problems = []
	zones, sets, rules = policy["zones"], policy["item_sets"], policy["rules"]
	if (
		not all(isinstance(v, list) for v in (zones, sets, rules))
		or len(zones) > 160
		or len(sets) > 40
		or len(rules) > 400
	):
		return ["The policy contains too many groups or rows."]
	for kind, entries in (("Country group", zones), ("Product group", sets)):
		seen = set()
		for entry in entries:
			name = entry["name"]
			if not isinstance(name, str) or not name.strip() or len(name) > 140 or not name.isprintable():
				problems.append(f"{kind} needs a name of at most 140 characters.")
			if name_key(name) in seen:
				problems.append(f"{kind} names must be unique: {name}.")
			seen.add(name_key(name))
	for z in zones:
		if len(z["countries"]) > 260 or any(not normalize_country(c) for c in z["countries"]):
			problems.append(f"{z['name']}: countries must have two-letter codes.")
	for group in sets:
		if len(group.get("patterns", [])) > 12 or any(
			not isinstance(p, str) or not p or len(p) > 80 or p.count("*") > 12
			for p in group.get("patterns", [])
		):
			problems.append(f"{group['name']}: a pattern allows at most 80 characters and 12 stars.")
		if len(group.get("items", [])) > 10000 or len(group.get("item_groups", [])) > 500:
			problems.append(f"{group['name']}: too many products or item groups.")
	zmap = {z["name"]: z for z in zones}
	names = {s["name"] for s in sets}
	covered = {}
	catch_all = False
	seen_groups = set()
	previous = None
	for i, r in enumerate(rules, 1):
		group, zone = r.get("item_set") or "", r.get("zone") or ""
		if group and group not in names:
			problems.append(f"Row {i}: choose an existing product group.")
		if catch_all and group:
			problems.append("Everything else must be the last product group.")
		catch_all |= not bool(group)
		if group != previous and group in seen_groups:
			problems.append("Keep each product group's rows together.")
		seen_groups.add(group)
		previous = group
		if zone and (zone not in zmap or zmap[zone].get("not_shipped")):
			problems.append(f"Row {i}: choose a delivery country group.")
			continue
		countries = set(zmap[zone]["countries"]) if zone else {"*"}
		used = covered.setdefault(group, set())
		if used & countries:
			problems.append(f"Row {i}: a country may appear only once in this product group.")
		used.update(countries)
		if r.get("method") not in ("table", "carrier", "formula"):
			problems.append(f"Row {i}: choose a price type.")
		if (
			not isinstance(r.get("services", []), list)
			or len(r.get("services", [])) > 100
			or any(not isinstance(s, str) or len(s) > 140 for s in r.get("services", []))
		):
			problems.append(f"Row {i}: choose valid services.")
		if r.get("method") == "formula":
			try:
				check_formula(r.get("formula", ""))
			except FormulaError as e:
				problems.append(f"Row {i}: {e}")
		if r.get("method") == "table":
			table = r.get("price_table", [])
			if not table or len(table) > 60:
				problems.append(f"Row {i}: add a price tier.")
			last = 0
			for n, tier in enumerate(table):
				bound = tier.get("up_to_lb")
				if not number(tier.get("price")):
					problems.append(f"Row {i}: prices must be numbers of zero or more.")
				if bound is None:
					if n != len(table) - 1:
						problems.append(f"Row {i}: only the last tier can have no weight limit.")
				elif not number(bound) or bound <= last:
					problems.append(f"Row {i}: weight limits must increase.")
				else:
					last = bound
		if r.get("days") is not None and (not number(r["days"]) or r["days"] > 365):
			problems.append(f"Row {i}: enter valid delivery days.")
	if policy.get("active") and not rules:
		problems.append("Add a country row before using this policy at checkout.")
	fb = policy.get("fallback", {})
	if fb.get("method") not in ("fixed", "no_delivery"):
		problems.append("Choose what happens if the carrier gives no price.")
	if not number(fb.get("amount", 0)):
		problems.append("The fallback amount must be zero or more.")
	return list(dict.fromkeys(problems))


def policy_warnings(policy):
	warnings = []
	for g in policy.get("item_sets", []):
		if not any(g.get(k) for k in ("items", "item_groups", "patterns")):
			warnings.append(g["name"] + " has no products and will not be used.")
	for z in policy.get("zones", []):
		if not z.get("countries"):
			warnings.append(z["name"] + " has no countries.")
	return warnings
