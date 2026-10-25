"""What the assistant's shipping tools accept and say (pure; no Frappe).

`simulate_shipping` prices one scenario with the shipping policy's own "Try an
order" engine, so an assistant gets the same answer a manager gets on the page.
These helpers turn the tool's arguments into that call and its result into a
short, readable summary.
"""

import json

MAX_SCENARIO_ITEMS = 50


def order_argument(arguments: dict) -> str:
	"""The JSON text `shipping_policy_wizard.try_order` takes as `order`. Raises ValueError for a bad scenario."""
	country = str(arguments.get("country_code") or "").strip().upper()
	if len(country) != 2 or not country.isalpha():
		raise ValueError("country_code must be a two-letter ISO code such as BH or US.")
	items = arguments.get("items")
	if isinstance(items, str):
		try:
			items = json.loads(items)
		except ValueError:
			raise ValueError("items must be a list of {item_code, qty}.") from None
	if not isinstance(items, list) or not items:
		raise ValueError("items must be a non-empty list of {item_code, qty}.")
	if len(items) > MAX_SCENARIO_ITEMS:
		raise ValueError(f"At most {MAX_SCENARIO_ITEMS} items per scenario.")
	return json.dumps(
		{
			"country_code": country,
			"items": [{"item_code": i.get("item_code"), "qty": i.get("qty", 1)} for i in items if isinstance(i, dict)],
			"city": str(arguments.get("city") or "").strip(),
			"postcode": str(arguments.get("postcode") or "").strip(),
		}
	)


def draft_argument(arguments: dict, saved_policy: dict):
	"""The policy to price against: the caller's `policy` (object or JSON text) or the saved one."""
	given = arguments.get("policy")
	if given in (None, "", {}):
		return saved_policy
	if isinstance(given, str):
		try:
			given = json.loads(given)
		except ValueError:
			raise ValueError("policy must be the policy object from get_shipping_policy.") from None
	if not isinstance(given, dict):
		raise ValueError("policy must be the policy object from get_shipping_policy.")
	return given


def summarize(result: dict) -> dict:
	"""The answer, in the words a person would use: what is offered, at what price, and why not."""
	offers = [
		{
			"service": o.get("name") or o.get("service_id"),
			"provider": o.get("provider"),
			"price_usd": o.get("amount"),
			"days": o.get("estimated_days"),
			"basis": o.get("source"),
		}
		for o in result.get("offers") or []
	]
	if offers:
		cheapest = min((o for o in offers if o["price_usd"] is not None), key=lambda o: o["price_usd"], default=None)
		line = f"{len(offers)} delivery option(s)" + (
			f"; cheapest {cheapest['service']} at USD {cheapest['price_usd']}" if cheapest else ""
		)
	else:
		line = "No delivery is offered: " + (result.get("message") or result.get("reason") or "no reason given")
	return {
		"offered": bool(result.get("offered")),
		"reason": result.get("reason"),
		"summary": line,
		"offers": offers,
		"note": result.get("preview_note"),
	}
