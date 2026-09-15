"""Pins the Sales Order remark that web orders carry.

Live ERP fills `remarks` by hand ("Against Customer Order 3694777"); web orders
used to leave it empty. `_order_remarks` writes the same kind of one-line note,
labelled by alias product code. These are the rules that matter downstream:
the alias wins over the item code, a missing alias still yields a usable line,
and the cart's order and uniqueness survive.
"""

import sys
import types
from typing import ClassVar

import pytest

# orders.py imports frappe at module scope. The remark builder only touches
# frappe.db.get_all, so a stub is enough to keep this tier hermetic.
_frappe = types.ModuleType("frappe")


class _Row(dict):
	__getattr__ = dict.get


class _DB:
	rows: ClassVar[dict] = {}
	calls = 0

	@classmethod
	def get_all(cls, doctype, filters=None, fields=None, **kwargs):
		cls.calls += 1
		wanted = (filters or {}).get("name", [None, []])[1]
		return [_Row(name=n, custom_alias_product_code=cls.rows.get(n)) for n in wanted if n in cls.rows]


_frappe.db = _DB
_frappe.whitelist = lambda *a, **k: lambda fn: fn
_frappe.throw = lambda msg, *a, **k: (_ for _ in ()).throw(Exception(msg))
_frappe.get_doc = _frappe.get_all = _frappe.log_error = lambda *a, **k: None
_frappe.logger = lambda *a, **k: types.SimpleNamespace(warning=lambda *a, **k: None)
sys.modules.setdefault("frappe", _frappe)

from dsi_catalogue.orders import _order_remarks


@pytest.fixture(autouse=True)
def _catalogue():
	_DB.rows = {
		"10000005-Candle": "KROUD-CANDLE-001-4A",
		"10000013-V5-100ML": "C77-OSDE-10B-4A",
		"10000019": "DQ01.IVSTBL.01.01.01",
		"NO-ALIAS": "",
	}
	_DB.calls = 0
	yield


class TestOrderRemarks:
	def test_single_item_uses_the_alias_code(self):
		assert (
			_order_remarks([{"item_code": "10000005-Candle"}])
			== "Online purchase for item KROUD-CANDLE-001-4A"
		)

	def test_several_items_pluralise_and_keep_cart_order(self):
		assert (
			_order_remarks([{"item_code": "10000019"}, {"item_code": "10000005-Candle"}])
			== "Online purchase for items DQ01.IVSTBL.01.01.01, KROUD-CANDLE-001-4A"
		)

	def test_repeated_lines_are_named_once(self):
		"""Two lines of the same product is one product, not a stutter."""
		assert (
			_order_remarks([{"item_code": "10000019"}, {"item_code": "10000019"}])
			== "Online purchase for item DQ01.IVSTBL.01.01.01"
		)

	def test_missing_alias_falls_back_to_the_item_code(self):
		"""44 of 6944 live Items carry no alias. The remark must still name
		something real rather than read 'for item None'."""
		assert _order_remarks([{"item_code": "NO-ALIAS"}]) == "Online purchase for item NO-ALIAS"

	def test_unknown_item_still_names_the_code(self):
		assert _order_remarks([{"item_code": "GHOST"}]) == "Online purchase for item GHOST"

	def test_one_query_for_the_whole_cart(self):
		"""Not one lookup per line — this runs inside the order transaction."""
		_order_remarks([{"item_code": c} for c in ("10000019", "10000005-Candle", "NO-ALIAS")])
		assert _DB.calls == 1

	@pytest.mark.parametrize("items", [None, [], [{}], [{"item_code": ""}], [{"item_code": "  "}]])
	def test_nothing_to_name_yields_no_remark(self, items):
		"""Empty string, never a half-formed sentence — the caller skips the
		field entirely when this returns falsy."""
		assert _order_remarks(items) == ""
