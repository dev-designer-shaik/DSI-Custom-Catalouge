"""Shipping Zone: a named group of countries whose delivery is priced by the same rules.

A country can be in only one enabled zone, and at most one enabled zone is Everywhere else. Zone names
and country names are typed by people, so they are escaped before they go into a message.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import escape_html


def _bold(text):
	return frappe.bold(escape_html(text))


class ShippingZone(Document):
	def validate(self):
		self.zone_name = (self.zone_name or "").strip()
		self.check_everywhere_else_lists_no_countries()
		# `apply_policy` (the wizard) checks the two rules below across the whole draft with
		# `validate_policy`, then saves the zones one by one inside one transaction. Checked one zone at a
		# time, a valid draft that moves a country from one zone to another would be refused half way.
		# It sets this flag after its own check; a direct edit never sets it and keeps every check.
		if self.flags.get("policy_draft_validated"):
			return
		self.check_country_is_in_one_enabled_zone()
		self.check_one_everywhere_else_zone()

	def check_everywhere_else_lists_no_countries(self):
		"""Everywhere else means every country that is in no other zone, so it lists none of its own."""
		if self.everywhere_else and any(row.country for row in self.countries):
			frappe.throw(
				_(
					"{0} is set to Everywhere else, so it cannot also list countries. "
					"Remove its countries, or turn Everywhere else off."
				).format(_bold(self.zone_name))
			)

	def check_country_is_in_one_enabled_zone(self):
		"""An enabled zone may not list a country that another enabled zone lists. A disabled zone may."""
		countries = [row.country for row in self.countries if row.country]
		if not (self.enabled and countries):
			return
		zone, listed = frappe.qb.DocType("Shipping Zone"), frappe.qb.DocType("Shipping Zone Country")
		taken = (
			frappe.qb.from_(listed)
			.join(zone)
			.on(zone.name == listed.parent)
			.select(listed.country, zone.name)
			.where((listed.parenttype == "Shipping Zone") & (zone.enabled == 1))
			.where(zone.name != (self.name or ""))
			.where(listed.country.isin(countries))
			.orderby(zone.name)
			.run()
		)
		owners = {}
		for country, owner in taken:
			owners.setdefault(country.lower(), owner)
		clashes = {country.lower(): country for country in countries if country.lower() in owners}
		if clashes:
			lines = [
				_("{0} is already in the enabled zone {1}.").format(_bold(country), _bold(owners[key]))
				for key, country in clashes.items()
			]
			lines.append(
				_(
					"A country can be in only one enabled zone: remove it from one of the zones, "
					"or disable one of them."
				)
			)
			frappe.throw("<br>".join(lines))

	def check_one_everywhere_else_zone(self):
		"""At most one enabled zone catches every other country."""
		if not (self.enabled and self.everywhere_else):
			return
		filters = {"enabled": 1, "everywhere_else": 1, "name": ["!=", self.name or ""]}
		if other := frappe.db.get_value("Shipping Zone", filters, "name"):
			frappe.throw(
				_(
					"{0} is already the enabled Everywhere else zone. Only one enabled zone can be "
					"Everywhere else: turn it off there first, or disable one of the zones."
				).format(_bold(other))
			)
