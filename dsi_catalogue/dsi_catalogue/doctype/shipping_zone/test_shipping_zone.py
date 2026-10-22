"""Frappe-native tests for the Shipping Zone: what its save-time checks refuse and what they let through.

	bench --site erp.shaik.net run-tests --module dsi_catalogue.dsi_catalogue.doctype.shipping_zone.test_shipping_zone

The helpers and the clean-site guard are in the Shipping Policy's test module.
"""

import frappe

from dsi_catalogue.dsi_catalogue.doctype.shipping_policy.test_shipping_policy import (
	ShippingPolicyTestCase,
	country_name,
)

# These tests list Country names directly; Frappe need not make test records for Country first.
IGNORE_TEST_RECORD_DEPENDENCIES = ["Country"]


def zone(name, countries=(), **fields):
	"""A Shipping Zone document, not yet saved. `countries` are ISO codes."""
	rows = [{"country": country_name(code)} for code in countries]
	return frappe.get_doc({"doctype": "Shipping Zone", "zone_name": name, "countries": rows, **fields})


class TestShippingZone(ShippingPolicyTestCase):
	def test_zone_refuses_a_country_in_another_enabled_zone(self):
		zone("Gulf", ["BH", "KW"]).insert()
		with self.assertRaises(frappe.ValidationError) as caught:
			zone("Islands", ["OM", "BH"]).insert()
		message = str(caught.exception)
		self.assertIn(country_name("BH"), message)
		self.assertIn("Gulf", message, "the message names the other zone")
		self.assertFalse(frappe.db.exists("Shipping Zone", "Islands"))

	def test_every_clashing_country_is_named_at_once(self):
		zone("Gulf", ["BH", "KW"]).insert()
		zone("Levant", ["LB"]).insert()
		with self.assertRaises(frappe.ValidationError) as caught:
			zone("Mixed", ["BH", "OM", "LB", "KW", "bh"]).insert()
		message = str(caught.exception)
		self.assertEqual(
			message.count("<br>"), 3, "a line each for Bahrain, Kuwait and Lebanon, then the rule"
		)
		for name in (country_name("BH"), country_name("KW"), country_name("LB"), "Gulf", "Levant"):
			self.assertIn(name, message)
		self.assertNotIn(country_name("OM"), message, "a country that is free is not mentioned")

	def test_a_zone_does_not_clash_with_itself(self):
		gulf = zone("Gulf", ["BH", "KW"]).insert()
		gulf.customer_line = "Gulf delivery."
		gulf.save()
		gulf.append("countries", {"country": country_name("OM")})
		gulf.save()
		self.assertEqual(len(frappe.get_doc("Shipping Zone", "Gulf").countries), 3)

	def test_disabled_zone_may_repeat_a_country(self):
		zone("Gulf", ["BH"]).insert()
		parked = zone("Parked", ["BH"], enabled=0).insert()
		self.assertEqual(parked.enabled, 0)
		parked.enabled = 1
		with self.assertRaises(frappe.ValidationError) as caught:
			parked.save()
		self.assertIn("Gulf", str(caught.exception), "switching it on is where the clash is found")
		# A save that was refused leaves its document out of date; with the other zone off, a fresh one saves.
		frappe.db.set_value("Shipping Zone", "Gulf", "enabled", 0)
		parked = frappe.get_doc("Shipping Zone", "Parked")
		parked.enabled = 1
		parked.save()
		self.assertEqual(frappe.db.get_value("Shipping Zone", "Parked", "enabled"), 1)

	def test_only_one_everywhere_else_zone(self):
		rest = zone("Rest", everywhere_else=1).insert()
		with self.assertRaises(frappe.ValidationError) as caught:
			zone("Anywhere", everywhere_else=1).insert()
		self.assertIn("Rest", str(caught.exception), "the message names the zone that already is")
		self.assertFalse(frappe.db.exists("Shipping Zone", "Anywhere"))
		zone("Spare", everywhere_else=1, enabled=0).insert()  # a disabled one may wait
		rest.customer_line = "International delivery."
		rest.save()  # and the enabled one can still be edited

	def test_an_everywhere_else_zone_lists_no_countries(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			zone("Rest", ["BH"], everywhere_else=1).insert()
		self.assertIn("Rest", str(caught.exception))
		with self.assertRaises(frappe.ValidationError):
			zone("Spare", ["BH"], everywhere_else=1, enabled=0).insert()

	def test_a_draft_the_wizard_has_validated_skips_the_two_checks_across_zones(self):
		zone("Gulf", ["BH"]).insert()
		zone("Rest", everywhere_else=1).insert()
		moved = zone("Moved", ["BH"])
		moved.flags.policy_draft_validated = True
		moved.insert()
		another = zone("Anywhere", everywhere_else=1)
		another.flags.policy_draft_validated = True
		another.insert()
		self.assertEqual(frappe.db.count("Shipping Zone", {"enabled": 1}), 4)

	def test_a_validated_draft_still_cannot_give_an_everywhere_else_zone_countries(self):
		bad = zone("Rest", ["BH"], everywhere_else=1)
		bad.flags.policy_draft_validated = True
		with self.assertRaises(frappe.ValidationError):
			bad.insert()

	def test_a_zone_is_known_by_its_trimmed_name(self):
		saved = zone("  Gulf  ", ["BH"]).insert()
		self.assertEqual((saved.name, saved.zone_name), ("Gulf", "Gulf"))

	def test_messages_escape_the_names_people_typed(self):
		zone("Gulf & Co", ["BH"]).insert()
		with self.assertRaises(frappe.ValidationError) as caught:
			zone("Islands", ["BH"]).insert()
		self.assertIn("Gulf &amp; Co", str(caught.exception))
		self.assertNotIn("Gulf & Co", str(caught.exception))
