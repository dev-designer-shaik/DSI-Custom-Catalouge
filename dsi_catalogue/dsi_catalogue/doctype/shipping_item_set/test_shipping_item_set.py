import frappe

from dsi_catalogue.dsi_catalogue.doctype.shipping_policy.test_shipping_policy import ShippingPolicyTestCase


class TestShippingItemSet(ShippingPolicyTestCase):
	def test_live_pattern_and_empty_group_can_be_saved(self):
		doc = frappe.get_doc(
			{
				"doctype": "Shipping Item Set",
				"set_name": "One-page live filter",
				"patterns": [{"pattern": "TEST-*"}],
			}
		)
		doc.insert()
		self.assertEqual(doc.patterns[0].pattern, "TEST-*")
		doc.set("patterns", [])
		doc.save()
