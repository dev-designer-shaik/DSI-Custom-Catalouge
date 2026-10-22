"""Shipping Item Set: a named group of item groups and items that a pricing rule can be limited to."""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import escape_html


class ShippingItemSet(Document):
	def validate(self):
		self.set_name = (self.set_name or "").strip()
		if not (any(row.item_group for row in self.item_groups) or any(row.item for row in self.items)):
			frappe.throw(
				_(
					"{0} has no item group and no item, so it would match nothing. "
					"Add at least one item group or item."
				).format(frappe.bold(escape_html(self.set_name)))
			)
