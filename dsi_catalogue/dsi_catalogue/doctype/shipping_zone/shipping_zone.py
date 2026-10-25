import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, escape_html

from dsi_catalogue.shipping_engine import normalize_country


def _bold(text):
	return frappe.bold(escape_html(text))


class ShippingZone(Document):
	def validate(self):
		self.zone_name = (self.zone_name or "").strip()
		if cint(self.enabled):
			self.check_countries_have_two_letter_codes()

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
