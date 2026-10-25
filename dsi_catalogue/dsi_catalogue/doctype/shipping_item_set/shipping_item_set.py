import frappe
from frappe.model.document import Document


class ShippingItemSet(Document):
	def validate(self):
		self.set_name = (self.set_name or "").strip()
		for row in self.patterns:
			if not row.pattern or len(row.pattern) > 80 or row.pattern.count("*") > 12:
				frappe.throw("A product code pattern allows at most 80 characters and 12 stars.")
