"""Frappe-native tests for the Shipping Zone: what its save-time checks refuse and what they let through.

	bench --site erp.shaik.net run-tests --module dsi_catalogue.dsi_catalogue.doctype.shipping_zone.test_shipping_zone

The helpers and the clean-site guard are in the Shipping Policy's test module.
"""

import math

import frappe
from frappe.utils import escape_html

from dsi_catalogue.dsi_catalogue.doctype.shipping_policy.test_shipping_policy import (
	ShippingPolicyTestCase,
	country_name,
	recode,
)

# These tests list Country names directly; Frappe need not make test records for Country first.
IGNORE_TEST_RECORD_DEPENDENCIES = ["Country"]

TWO_LETTER_CODES = (
	"A zone can list only countries that have a two-letter code: give the country its code in the Country list, "
	"or remove it from the zone."
)


def zone(name, countries=(), **fields):
	"""A Shipping Zone document, not yet saved. `countries` are ISO codes."""
	rows = [{"country": country_name(code)} for code in countries]
	return frappe.get_doc({"doctype": "Shipping Zone", "zone_name": name, "countries": rows, **fields})


def bold(text):
	return frappe.bold(escape_html(text))


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

	def test_an_enabled_zone_lists_only_countries_with_a_two_letter_code(self):
		# The policy finds a country by its two-letter code, so a Country with no code, or with a code such as
		# gbr, could never be matched to an order: an enabled zone refuses it, also in a draft the wizard has
		# validated (the wizard checks the codes it was sent, not the Country records behind them).
		britain, oman, france = country_name("GB"), country_name("OM"), country_name("FR")
		recode(self, britain, "gbr")
		recode(self, france, None)
		refusal = "<br>".join(
			[
				f"{bold(britain)} has the country code {bold('gbr')}, which is not two letters.",
				f"{bold(france)} has no country code.",
				TWO_LETTER_CODES,
			]
		)
		for validated in (False, True):
			with self.subTest(validated=validated):
				europe = frappe.get_doc(
					{
						"doctype": "Shipping Zone",
						"zone_name": "Europe",
						"countries": [{"country": name} for name in (britain, oman, france)],
					}
				)
				europe.flags.policy_draft_validated = validated
				with self.assertRaises(frappe.ValidationError) as caught:
					europe.insert()
				self.assertEqual(str(caught.exception), refusal)
		self.assertFalse(frappe.db.exists("Shipping Zone", "Europe"))
		# A zone saved before its country lost its code is refused on its next save, until the country is fixed.
		gulf = zone("Gulf", ["OM"]).insert()
		recode(self, oman, "")
		gulf.customer_line = "Gulf delivery."
		with self.assertRaises(frappe.ValidationError) as caught:
			gulf.save()
		self.assertEqual(str(caught.exception), f"{bold(oman)} has no country code.<br>{TWO_LETTER_CODES}")
		recode(self, oman, " om ")  # read as OM, as the policy reads it
		frappe.get_doc("Shipping Zone", "Gulf").update({"customer_line": "Gulf delivery."}).save()
		self.assertEqual(frappe.db.get_value("Shipping Zone", "Gulf", "customer_line"), "Gulf delivery.")

	def test_a_zone_saved_disabled_always_saves_and_is_checked_once_it_is_enabled(self):
		# A disabled zone is out of the live policy, so a zone saved with enabled = 0 saves whatever the codes
		# of its countries are: switching a zone off must always work (the wizard does it to a zone its draft
		# drops, before it deletes it). Switching it on runs the check, and is refused until the country has
		# its code.
		britain, oman = country_name("GB"), country_name("OM")
		recode(self, britain, "gbr")
		refusal = f"{bold(britain)} has the country code {bold('gbr')}, which is not two letters."
		refusal += f"<br>{TWO_LETTER_CODES}"
		for validated in (False, True):
			with self.subTest(validated=validated):
				parked = frappe.get_doc(
					{
						"doctype": "Shipping Zone",
						"zone_name": f"Parked {validated}",
						"enabled": 0,
						"countries": [{"country": britain}, {"country": oman}],
					}
				)
				parked.flags.policy_draft_validated = validated
				parked.insert()
				self.assertEqual(frappe.db.get_value("Shipping Zone", parked.name, "enabled"), 0)
		parked = frappe.get_doc("Shipping Zone", "Parked False")
		parked.enabled = 1
		with self.assertRaises(frappe.ValidationError) as caught:
			parked.save()
		self.assertEqual(str(caught.exception), refusal)
		self.assertEqual(frappe.db.get_value("Shipping Zone", "Parked False", "enabled"), 0, "still off")
		# An enabled zone whose country loses its code after it was saved can still be switched off, as the
		# wizard switches off a zone its draft drops.
		gulf = zone("Gulf", ["OM"]).insert()
		recode(self, oman, "")
		gulf.enabled = 0
		gulf.flags.policy_draft_validated = True
		gulf.save()
		self.assertEqual(frappe.db.get_value("Shipping Zone", "Gulf", "enabled"), 0)
		# Once every country has its code again, the parked zone can be switched on.
		recode(self, britain, "gb")
		recode(self, oman, "om")
		frappe.get_doc("Shipping Zone", "Parked False").update({"enabled": 1}).save()
		self.assertEqual(frappe.db.get_value("Shipping Zone", "Parked False", "enabled"), 1)

	def test_the_fallback_amount_is_kept_to_the_cent(self):
		# The field keeps two decimals, so the saved zone holds the amount the database stores.
		gulf = zone("Gulf", ["BH"], fallback_method="Charge a fixed amount", fallback_amount=12.3456).insert()
		self.assertEqual(gulf.fallback_amount, 12.35)
		self.assertEqual(frappe.db.get_value("Shipping Zone", "Gulf", "fallback_amount"), 12.35)

	def test_a_fallback_amount_that_is_not_a_finite_number_is_refused_not_kept_as_zero(self):
		# Rounded to its two decimals, NaN or an infinity would become 0 without a word (Frappe's `flt` reads
		# a number it cannot round as 0), and a fixed amount of 0.00 is free delivery: it is refused first, in
		# a zone that is on or off, as is a number too big to be held at all.
		amounts = (math.nan, math.inf, -math.inf, "NaN", "-Infinity", 10**400)
		for number, amount in enumerate(amounts, start=1):
			for enabled in (1, 0):
				with self.subTest(amount=amount, enabled=enabled):
					fallback = {"fallback_method": "Charge a fixed amount", "fallback_amount": amount}
					gulf = zone(f"Gulf {number} {enabled}", ["BH"], enabled=enabled, **fallback)
					with self.assertRaises(frappe.ValidationError) as caught:
						gulf.insert()
					self.assertEqual(str(caught.exception), f"{bold('Fixed amount (USD)')} is not a number.")
		self.assertEqual(frappe.db.count("Shipping Zone"), 0, "nothing was saved")
