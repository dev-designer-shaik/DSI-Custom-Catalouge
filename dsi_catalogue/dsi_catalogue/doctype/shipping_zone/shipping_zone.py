"""Shipping Zone: a named group of countries whose delivery is priced by the same rules.

An enabled zone lists only countries that have a two-letter code, because the policy finds a country by its
code. A zone saved disabled is out of the live policy and always saves, whatever its countries' codes, so that
switching a zone off always works; switching it on again checks them. A country can be in only one enabled
zone, and at most one enabled zone is Everywhere else. Zone names and country names are typed by people, so
they are escaped before they go into a message. The fixed amount is kept to the cent, and an amount that is
not a number (NaN, an infinity, text such as "abc") is refused, never rounded to 0.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, escape_html

from dsi_catalogue.shipping_engine import normalize_country
from dsi_catalogue.shipping_policy_data import is_not_a_number

# The fields `round_floats_in` rounds.
_ROUNDED = {"fieldtype": ["in", ["Currency", "Float", "Percent"]]}


def _bold(text):
	return frappe.bold(escape_html(text))


class ShippingZone(Document):
	def validate(self):
		self.zone_name = (self.zone_name or "").strip()
		self.check_numbers_are_numbers()
		self.round_floats_in(self)  # the fallback amount as it is stored, to the cent
		self.check_everywhere_else_lists_no_countries()
		if cint(self.enabled):  # read as the database will keep it
			self.check_countries_have_two_letter_codes()
		# `apply_policy` (the wizard) checks the two rules below across the whole draft with
		# `validate_policy`, then saves the zones one by one inside one transaction. Checked one zone at a
		# time, a valid draft that moves a country from one zone to another would be refused half way.
		# It sets this flag after its own check; a direct edit never sets it and keeps every check.
		if self.flags.get("policy_draft_validated"):
			return
		self.check_country_is_in_one_enabled_zone()
		self.check_one_everywhere_else_zone()

	def check_numbers_are_numbers(self):
		"""Rounded to the cent, NaN, an infinity or text that is not a number would become 0 without a word
		(Frappe's `flt` reads what it cannot read or round as 0), and a fixed amount of 0.00 is free delivery.
		Refused before anything is rounded, enabled or not (`is_not_a_number`)."""
		for field in self.meta.get("fields", _ROUNDED):
			if is_not_a_number(self.get(field.fieldname)):
				frappe.throw(_("{0} is not a number.").format(_bold(_(field.label))))

	def check_everywhere_else_lists_no_countries(self):
		"""Everywhere else means every country that is in no other zone, so it lists none of its own."""
		if self.everywhere_else and any(row.country for row in self.countries):
			frappe.throw(
				_(
					"{0} is set to Everywhere else, so it cannot also list countries. "
					"Remove its countries, or turn Everywhere else off."
				).format(_bold(self.zone_name))
			)

	def check_countries_have_two_letter_codes(self):
		"""A Country with no code, or with a code that is not two letters (gbr), could never be matched to an
		order. Checked when the zone is saved enabled, in a draft the wizard has validated as well (the wizard
		checks the codes it was sent, not the Country records behind them). A zone saved disabled prices no
		order, so it is not checked: the wizard switches off a zone its draft drops before deleting it, and
		that must work even when a country's code went bad after the zone was saved."""
		names = list(dict.fromkeys(row.country for row in self.countries if row.country))
		if not names:
			return
		codes = dict(
			frappe.get_all("Country", filters={"name": ["in", names]}, fields=["name", "code"], as_list=True)
		)
		lines = []
		for name in names:
			code = codes.get(name)
			if normalize_country(code):
				continue
			if isinstance(code, str) and code.strip():
				lines.append(
					_("{0} has the country code {1}, which is not two letters.").format(
						_bold(name), _bold(code)
					)
				)
			else:
				lines.append(_("{0} has no country code.").format(_bold(name)))
		if lines:
			lines.append(
				_(
					"A zone can list only countries that have a two-letter code: give the country its code in "
					"the Country list, or remove it from the zone."
				)
			)
			frappe.throw("<br>".join(lines))

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
