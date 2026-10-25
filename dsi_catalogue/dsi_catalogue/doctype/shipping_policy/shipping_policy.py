import frappe
from frappe.model.document import Document
from frappe.utils import escape_html

from dsi_catalogue.shipping_engine import validate_policy
from dsi_catalogue.shipping_policy import load_policy


class ShippingPolicy(Document):
	def validate(self):
		if self.flags.get("policy_draft_validated"):
			return
		if problems := validate_policy(load_policy(overrides=self.as_dict())):
			frappe.throw([escape_html(p) for p in problems], as_list=True)
