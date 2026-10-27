"""Captured order currency must not be mistaken for receivable-account currency."""

import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def case(monkeypatch):
	f = types.ModuleType("frappe")
	f.whitelist = lambda *a, **kw: lambda fn: fn
	f.conf = {"tap_mode_of_payment": "tap", "tap_receiving_account": "Bank"}
	f.db = SimpleNamespace(savepoint=Mock(), rollback=Mock(), sql=Mock())
	f.get_all = Mock(return_value=[])
	f.log_error = Mock()
	f.get_traceback = lambda: "validation failed"
	f.utils = SimpleNamespace(nowdate=lambda: "2026-10-27")
	comment = SimpleNamespace(insert=Mock())
	f.get_doc = Mock(return_value=comment)
	monkeypatch.setitem(sys.modules, "frappe", f)
	modname = "tap_currency_test_orders"
	spec = importlib.util.spec_from_file_location(
		modname, Path(__file__).resolve().parents[2] / "dsi_catalogue/orders.py"
	)
	mod = importlib.util.module_from_spec(spec)
	monkeypatch.setitem(sys.modules, modname, mod)
	spec.loader.exec_module(mod)
	so = SimpleNamespace(
		name="SO-1",
		docstatus=1,
		currency="USD",
		conversion_rate=0.376,
		grand_total=100,
		customer="Customer",
		company="Company",
		reload=Mock(),
	)
	pe = SimpleNamespace(
		name="PE-1",
		payment_type="Receive",
		party="Customer",
		company="Company",
		paid_from_account_currency="BHD",
		paid_amount=37.6,
		source_exchange_rate=1,
		references=[
			SimpleNamespace(reference_doctype="Sales Order", reference_name="SO-1", allocated_amount=37.6)
		],
		flags=SimpleNamespace(),
		insert=Mock(),
		submit=Mock(),
	)
	helper = types.ModuleType("erpnext.accounts.doctype.payment_entry.payment_entry")
	helper.get_payment_entry = Mock(return_value=pe)
	monkeypatch.setitem(sys.modules, helper.__name__, helper)
	return mod, f, so, pe, helper


def test_erpnext_converts_order_currency_without_an_override(case):
	mod, f, so, pe, helper = case
	assert mod._record_payment(so, {"charge_id": "chg_test", "amount": 100, "currency": "USD"}) == "PE-1"
	helper.get_payment_entry.assert_called_once_with("Sales Order", "SO-1", bank_account="Bank")
	assert pe.paid_amount == 37.6
	pe.insert.assert_called_once()
	pe.submit.assert_called_once()
	f.db.sql.assert_called_once()  # lock precedes the receipt lookup/insert
	assert pe.reference_no == "chg_test"


@pytest.mark.parametrize(
	"bad", ["cancelled", "draft", "wrong_currency", "wrong_amount", "nan", "wrong_receipt_amount"]
)
def test_invalid_order_or_receipt_never_posts(case, bad):
	mod, f, so, pe, helper = case
	payment = {"charge_id": "chg_test", "amount": 100, "currency": "USD"}
	if bad == "cancelled":
		so.docstatus = 2
	if bad == "draft":
		so.docstatus = 0
	if bad == "wrong_currency":
		payment["currency"] = "BHD"
	if bad == "wrong_amount":
		payment["amount"] = 50
	if bad == "nan":
		payment["amount"] = float("nan")
	if bad == "wrong_receipt_amount":
		pe.paid_amount = 100
	assert mod._record_payment(so, payment) is None
	pe.insert.assert_not_called()
	pe.submit.assert_not_called()
	f.db.rollback.assert_called_once_with(save_point="tap_payment_entry")
	assert f.log_error.call_args.kwargs["title"] == "create_order_atomic: Payment Entry failed"


def test_repeat_uses_valid_existing_receipt_without_inserting(case):
	mod, f, so, pe, helper = case
	f.get_all.return_value = ["PE-1"]
	f.get_doc.return_value = pe
	assert mod._record_payment(so, {"charge_id": "chg_test", "amount": 100, "currency": "USD"}) == "PE-1"
	helper.get_payment_entry.assert_not_called()
	pe.insert.assert_not_called()
