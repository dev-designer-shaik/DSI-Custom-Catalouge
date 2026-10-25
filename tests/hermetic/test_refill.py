"""Refill requests: who is emailed and what they are told."""

import sys
import types

import pytest

# Other hermetic tests install their own bare `frappe` stub first; add what
# this module needs to whichever one is there rather than replacing it.
_frappe = sys.modules.setdefault("frappe", types.ModuleType("frappe"))
for _name, _value in {
	"_": lambda s: s,
	"whitelist": lambda **kw: (lambda f: f),
	"ValidationError": type("ValidationError", (Exception,), {}),
}.items():
	if not hasattr(_frappe, _name):
		setattr(_frappe, _name, _value)
_utils = sys.modules.setdefault("frappe.utils", types.ModuleType("frappe.utils"))
_frappe.utils = _utils
for _name, _value in {
	"escape_html": lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"),
	"now_datetime": lambda: None,
}.items():
	if not hasattr(_utils, _name):
		setattr(_utils, _name, _value)

from dsi_catalogue import refill  # noqa: E402

GOOD = {
	"first_name": "Jane", "last_name": "Doe", "email": "Jane@Example.com", "phone": "+97312345678",
	"product_name": "Sapphire No.77", "message": "Empty 100ml bottle",
}


def test_a_complete_request_is_cleaned():
	request = refill.clean_request(GOOD)
	assert request["email"] == "jane@example.com"
	assert request["item_code"] == "" and request["country"] == ""


@pytest.mark.parametrize("missing", ["first_name", "last_name", "email", "phone", "product_name", "message"])
def test_each_required_field_is_enforced(missing):
	with pytest.raises(ValueError) as bad:
		refill.clean_request({**GOOD, missing: "  "})
	assert str(bad.value) == missing


def test_a_bad_email_is_refused():
	with pytest.raises(ValueError):
		refill.clean_request({**GOOD, "email": "not-an-email"})


def test_only_enabled_system_users_with_an_email_are_told_once():
	users = [
		{"name": "a@x.com", "email": "A@x.com", "enabled": 1, "user_type": "System User"},
		{"name": "a2", "email": "a@x.com", "enabled": 1, "user_type": "System User"},
		{"name": "off@x.com", "email": "off@x.com", "enabled": 0, "user_type": "System User"},
		{"name": "web@x.com", "email": "web@x.com", "enabled": 1, "user_type": "Website User"},
		{"name": "Administrator", "email": "", "enabled": 1, "user_type": "System User"},
	]
	assert refill.sales_recipients(users) == ["a@x.com"]


def test_the_email_names_the_customer_and_escapes_what_they_typed():
	subject, html = refill.render_email({**refill.clean_request(GOOD), "message": "<b>hi</b>"}, "CRM-LEAD-1")
	assert "Sapphire No.77" in subject and "Jane Doe" in subject
	assert "+97312345678" in html and "CRM-LEAD-1" in html
	assert "<b>hi</b>" not in html and "&lt;b&gt;hi&lt;/b&gt;" in html
