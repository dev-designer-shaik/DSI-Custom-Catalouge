import frappe

from dsi_catalogue.dsi_catalogue.doctype.shipping_policy.test_shipping_policy import ShippingPolicyTestCase


class TestShippingZone(ShippingPolicyTestCase):
	def test_country_groups_may_overlap(self):
		country = frappe.db.get_value("Country", {"code": "us"}, "name")
		for name in ["One-page overlap A", "One-page overlap B"]:
			frappe.get_doc(
				{
					"doctype": "Shipping Zone",
					"zone_name": name,
					"enabled": 1,
					"countries": [{"country": country}],
				}
			).insert()
		self.assertTrue(frappe.db.exists("Shipping Zone", "One-page overlap B"))
