"""Shipping Policy: the single document that switches the policy on and holds its rules in order.

Saving it checks the whole policy, not only its own fields: the policy as it would be after this save
(these rules, with the saved zones and item sets) goes through `validate_policy`, so a rule that names a
missing zone, a band the wrong way round or a formula that cannot run is refused with a plain sentence.
Every rule is checked, enabled or not, so a half-filled rule is never stored.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import escape_html

from dsi_catalogue.shipping_engine import validate_policy
from dsi_catalogue.shipping_policy import load_policy


class ShippingPolicy(Document):
	def validate(self):
		problems = validate_policy(load_policy(overrides=self.as_dict()))
		if problems:
			# The sentences name zones, labels and formulas that people typed, so they are escaped.
			frappe.throw(
				[escape_html(problem) for problem in problems],
				title=_("The shipping policy cannot be saved"),
				as_list=True,
			)
