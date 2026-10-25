"""Shipping tools for the Frappe Assistant Core MCP server.

Both are read-only. They run as the signed-in user, so the shipping policy's own
role rule (System Manager, Website Manager or Sales User) applies: anyone else is
refused. Registered through the `assistant_tools` hook in hooks.py.
"""

from typing import Any

import frappe
from frappe_assistant_core.core.base_tool import BaseTool

from dsi_catalogue import shipping_policy_wizard as wizard
from dsi_catalogue.assistant_tools import simulation


class GetShippingPolicy(BaseTool):
	def __init__(self):
		super().__init__()
		self.name = "get_shipping_policy"
		self.category = "Shipping"
		self.source_app = "dsi_catalogue"
		self.description = (
			"Read the saved Shipping Policy: whether it is Active, the fallback, the country groups, the product "
			"groups and every delivery row (country group, delivered by, customer pays). Use it to explain how "
			"delivery is priced, or to take the policy object that simulate_shipping can price against with edits."
		)
		self.inputSchema = {"type": "object", "properties": {}}

	def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
		state = wizard.wizard_state()
		return {"success": True, "policy": state["policy"], "version": state.get("version")}


class SimulateShipping(BaseTool):
	def __init__(self):
		super().__init__()
		self.name = "simulate_shipping"
		self.category = "Shipping"
		self.source_app = "dsi_catalogue"
		self.description = (
			"Price one delivery scenario with the Shipping Policy engine (the page's 'Try an order'): a destination "
			"country plus ERP items with quantities gives every delivery option with its provider, price in USD and "
			"days, or the reason nothing is offered. Rows that use the carrier's price or a formula ask the carrier "
			"for live quotes, so give the destination city (and postcode where the country needs one). Pass "
			"'policy' (from get_shipping_policy, edited) to test a change before it is saved; omit it to use the "
			"saved policy, which is priced as if Active even while it is switched off. Nothing is saved, booked or sent."
		)
		self.inputSchema = {
			"type": "object",
			"properties": {
				"country_code": {"type": "string", "description": "Destination ISO country code, e.g. BH, US, GB."},
				"items": {
					"type": "array",
					"description": "What is in the basket.",
					"items": {
						"type": "object",
						"properties": {
							"item_code": {"type": "string", "description": "ERP Item code."},
							"qty": {"type": "number", "description": "Quantity (default 1)."},
						},
						"required": ["item_code"],
					},
				},
				"city": {"type": "string", "description": "Destination city; needed for carrier-priced rows."},
				"postcode": {"type": "string", "description": "Destination postcode where required."},
				"policy": {"type": "object", "description": "Optional policy object to price against instead of the saved one."},
			},
			"required": ["country_code", "items"],
		}

	def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
		try:
			order = simulation.order_argument(arguments)
			draft = simulation.draft_argument(arguments, wizard.wizard_state()["policy"])
		except ValueError as bad:
			return {"success": False, "error": str(bad)}
		try:
			result = wizard.try_order(draft=draft, order=order)["result"]
		except frappe.ValidationError as problem:
			return {"success": False, "error": str(problem)}
		return {"success": True, **simulation.summarize(result), "detail": result}
