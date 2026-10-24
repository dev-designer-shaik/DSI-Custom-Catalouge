"""Shipping Policy: the single document that switches the policy on and holds its rules in order.

Saving it checks the whole policy, not only its own fields: the policy as it would be after this save
(these rules, with the saved zones and item sets) goes through `validate_policy`, so a rule that names a
missing zone, a band the wrong way round or a formula that cannot run is refused with a plain sentence.
While the policy is on, every rule is checked, enabled or not, so a half-filled rule is never stored. The
numbers are checked as they are stored: rounded to the decimals their fields keep, so a weight band typed as
2.0001 up to 2.0004 kg is the 2 up to 2 it would be kept as.

A policy that is off is not checked at all. It changes no customer's price (the carrier's quotes pass through
as they are), so switching it off, and editing it while it is off, always work, even when a zone or the
stored card has gone bad. Every road back on is checked: saving the policy on is refused until the problems
are fixed, and the wizard checks its whole draft before it writes and the written policy after.

The rate card is stored on it as JSON text, and the text is read first: the loader reads text that is not
JSON as no card at all, which would send the person to the wrong sentence ("there is no rate card yet").
Once it parses, the card's own problems come out of `validate_policy` with everything else.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, escape_html

from dsi_catalogue.shipping_engine import validate_policy
from dsi_catalogue.shipping_policy import load_policy
from dsi_catalogue.shipping_policy_data import read_json

NOT_JSON = "The stored rate card is not valid JSON. Import the rates again in the wizard."
NOT_A_CARD = (
	"The stored rate card is not a rate card: it has to be a JSON object. "
	"Import the rates again in the wizard."
)


def stored_card_problem(value) -> str | None:
	"""A sentence when the stored rate card cannot be read as a JSON object, else None.

	No text, or nothing but blanks, is no card, which is fine. A dict (a document filled in by code) is read
	as it is, as the loader reads it. Text is read as the loader reads it (`read_json`), so NaN, Infinity and
	-Infinity, which are not JSON, make it text that is not JSON. Never raises.
	"""
	if value is None or isinstance(value, dict):
		return None
	if not isinstance(value, str):
		return NOT_JSON
	if not value.strip():
		return None
	try:
		card = read_json(value)
	except (ValueError, RecursionError):  # not JSON, NaN, a number of over 4300 digits, nesting too deep
		return NOT_JSON
	return None if isinstance(card, dict) else NOT_A_CARD


class ShippingPolicy(Document):
	def validate(self):
		self.round_floats_in(self)
		for rule in self.rules:
			self.round_floats_in(rule)
		if not cint(self.active):  # read as the database will keep it
			return
		unreadable = stored_card_problem(self.rate_card)
		problems = [unreadable] if unreadable else validate_policy(load_policy(overrides=self.as_dict()))
		if problems:
			# The sentences name zones, labels, formulas and profiles that people typed, so they are escaped.
			frappe.throw(
				[escape_html(problem) for problem in problems],
				title=_("The shipping policy cannot be saved"),
				as_list=True,
			)
