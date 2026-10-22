"""Frappe-native tests for the Shipping Item Set: it needs something in it.

	bench --site erp.shaik.net run-tests --module dsi_catalogue.dsi_catalogue.doctype.shipping_item_set.test_shipping_item_set

The helpers and the clean-site guard are in the Shipping Policy's test module.
"""

import frappe

from dsi_catalogue.dsi_catalogue.doctype.shipping_policy.test_shipping_policy import ShippingPolicyTestCase

# These tests name an item group and an item directly; Frappe need not make test records for them first.
IGNORE_TEST_RECORD_DEPENDENCIES = ["Item Group", "Item"]


def item_set(name, groups=(), items=()):
	"""A Shipping Item Set document, not yet saved."""
	return frappe.get_doc(
		{
			"doctype": "Shipping Item Set",
			"set_name": name,
			"item_groups": [{"item_group": group} for group in groups],
			"items": [{"item": item} for item in items],
		}
	)


class TestShippingItemSet(ShippingPolicyTestCase):
	def test_item_set_needs_a_group_or_an_item(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			item_set("Nothing").insert()
		self.assertIn("Nothing", str(caught.exception))
		self.assertFalse(frappe.db.exists("Shipping Item Set", "Nothing"))

	def test_a_group_alone_is_enough(self):
		saved = item_set("Everything", groups=["All Item Groups"]).insert()
		self.assertEqual([row.item_group for row in saved.item_groups], ["All Item Groups"])

	def test_an_item_alone_is_enough(self):
		items = frappe.get_all("Item", pluck="name", limit=1)
		if not items:
			self.skipTest("this site has no Item to put in a set")
		saved = item_set("One thing", items=items).insert()
		self.assertEqual([row.item for row in saved.items], items)

	def test_a_set_that_loses_its_last_group_is_refused(self):
		saved = item_set("Everything", groups=["All Item Groups"]).insert()
		saved.item_groups = []
		with self.assertRaises(frappe.ValidationError):
			saved.save()

	def test_messages_escape_the_names_people_typed(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			item_set("Gulf & Co").insert()
		self.assertIn("Gulf &amp; Co", str(caught.exception))
