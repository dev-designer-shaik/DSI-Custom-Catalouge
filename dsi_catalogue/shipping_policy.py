"""The shipping policy on the ERP side: reading it out of the database.

`load_policy` assembles the policy (the shape `shipping_engine` documents) from the three doctypes that hold
it: the Shipping Policy single with its rules, the Shipping Zones with their countries, and the Shipping
Item Sets with their groups and items. Everything else (the guest methods, the wizard) prices an order or
edits the policy through it, so what the quote path sees and what the form saves cannot drift apart.

Nothing is cached, here or in the quote path: every call reads the database again, so a policy change takes
effect on the next call.
"""

import frappe
from frappe.model.base_document import BaseDocument

from dsi_catalogue.shipping_policy_data import policy_from_rows

POLICY_DOCTYPE = "Shipping Policy"

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
