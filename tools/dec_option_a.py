"""Option A — per-currency decimal precision (DEC-001) — applier + verifier for erp1.

Applies, atomically and idempotently:
  1. System Settings.use_number_format_from_currency = 1        (FIRST)
  2. System Settings.currency_precision cleared                 (immediately after,
     same process, single commit — reversing this order makes tier 3 fall back to
     the GLOBAL #,###.## and destroys BHD fils for anything saved in the gap)
  3. Currency "USD".number_format -> #,###.##

ERP1-SPECIFIC: tabSingles is sync class B (never touched) so 1 and 2 PERSIST here.
tabCurrency is class A — the nightly 04:00 sync RESTORES prod's USD row
(#,###.###), so step 3 must be RE-RUN after each sync until prod is fixed.
The whole module is idempotent: re-run any time.

Verify (read-only) proves precision resolution per currency through the REAL
cascade (frappe.model.meta.get_field_precision) on live doctype fields.
"""

import frappe

FILS = ("BHD", "KWD", "OMR")          # 3-dp currencies
TWODP = ("SAR", "AED", "USD", "QAR", "EUR", "GBP")
EXPECT = {c: 3 for c in FILS} | {c: 2 for c in TWODP}


def apply():
    from frappe.model.meta import get_field_precision  # noqa: F401  (import check)

    before = {
        "use_number_format_from_currency": frappe.db.get_single_value(
            "System Settings", "use_number_format_from_currency"),
        "currency_precision": frappe.db.get_single_value(
            "System Settings", "currency_precision"),
        "usd_format": frappe.db.get_value("Currency", "USD", "number_format"),
    }

    # 1 THEN 2 via the DOCUMENT, not db.set_single_value: System Settings.save()
    # re-syncs the Redis defaults hash through the official path, and THAT hash is
    # what the browser boot serves. Measured 2026-09-02: after surgical singles
    # writes, fresh boots still carried use_number_format_from_currency='0' from
    # the hash (history's settings-save), silently disabling per-currency
    # formatting client-side even with the settings row correct.
    ss = frappe.get_doc("System Settings")
    ss.use_number_format_from_currency = 1
    ss.currency_precision = ""
    ss.save(ignore_permissions=True)
    # remove any bogus row a previous 2-arg call created
    frappe.db.sql("delete from tabSingles where doctype = %s",
                  ("use_number_format_from_currency",))
    # THE STALE GLOBAL DEFAULT: get_field_precision reads db.get_default(), which
    # serves the Redis defaults hash ("defaults::__default"), NOT tabSingles. An old
    # System Settings save left currency_precision=3 there, so clearing tabSingles
    # alone changes nothing for any worker (measured: USD still resolved 3dp with
    # the flag on and the Currency master correct). clear_default evicts it properly.
    from frappe.defaults import clear_default, set_global_default
    clear_default("currency_precision")   # evict the historical '3' outright:
    # doc.save() writes '' (falsy) but a stale non-empty hash entry must not survive
    # And the FLAG: System Settings.save() does NOT sync this field into the defaults
    # hash (measured — hash kept '0' after a doc save), yet the BROWSER keys its
    # per-currency formatting on the boot copy of exactly this hash. Without this
    # write, every fresh boot still ships use_number_format_from_currency='0' and
    # the client silently stays global-precision while the server is correct.
    set_global_default("use_number_format_from_currency", 1)
    frappe.db.set_value("Currency", "USD", "number_format", "#,###.##")
    frappe.db.commit()
    # frappe.clear_cache() does NOT evict the DEFAULTS cache that get_default()
    # reads — without these two deletions every worker keeps serving
    # currency_precision=3 from Redis and the change is invisible site-wide
    # (measured: verify still returned 3dp for USD after the rows were correct).
    frappe.clear_cache()
    frappe.cache().delete_value("system_settings")

    after = {
        "use_number_format_from_currency": frappe.db.get_single_value(
            "System Settings", "use_number_format_from_currency"),
        "currency_precision": frappe.db.get_single_value(
            "System Settings", "currency_precision") or "(cleared)",
        "usd_format": frappe.db.get_value("Currency", "USD", "number_format"),
    }
    return {"before": before, "after": after}


def verify():
    """Precision resolution through the real cascade, per currency, on live fields."""
    from frappe.model.meta import get_field_precision

    checks = []

    def check(name, ok, got=""):
        checks.append({"case": name, "pass": bool(ok), "got": str(got)[:120]})

    flag = frappe.db.get_single_value("System Settings", "use_number_format_from_currency")
    cp = frappe.db.get_single_value("System Settings", "currency_precision")
    check("flag use_number_format_from_currency=1", flag == 1, flag)
    check("currency_precision cleared (falsy)", not cp, cp if cp else "(cleared)")
    check("USD master fixed", frappe.db.get_value("Currency", "USD", "number_format")
          == "#,###.##", frappe.db.get_value("Currency", "USD", "number_format"))

    # real fields on real doctypes: SI parent, PE parent, SI Item child
    si_meta = frappe.get_meta("Sales Invoice")
    probes = [
        ("Sales Invoice", si_meta.get_field("grand_total")),
        ("Payment Entry", frappe.get_meta("Payment Entry").get_field("paid_amount")),
        ("Sales Invoice Item", frappe.get_meta("Sales Invoice Item").get_field("amount")),
    ]
    for dt, df in probes:
        if df is None:
            check("%s probe field exists" % dt, False, "meta returned None")
            continue
        check("%s.%s has no field-level precision override" % (dt, df.fieldname),
              not df.precision, df.precision or "(none)")
        for cur, want in sorted(EXPECT.items()):
            got = get_field_precision(df, currency=cur)
            check("%s.%s [%s] -> %ddp" % (dt, df.fieldname, cur, want),
                  got == want, got)

    # storage-side: rounding-on-save derives from the same function; spot-check
    # the public helper (frappe.get_precision — NOT frappe.model.meta.get_precision,
    # which does not exist) the tax pipeline uses for BHD and SAR contexts.
    # real documents, not _dict stubs: get_precision's internals query by the
    # doc and a stub raises OperationalError (Unknown column 'doctype').
    def _last_si(cur):
        try:
            return frappe.get_last_doc("Sales Invoice",
                                       {"currency": cur, "docstatus": 1})
        except Exception:                                            # noqa
            return None

    for cur, want in (("BHD", 3), ("SAR", 2), ("USD", 2)):
        doc = _last_si(cur)
        if not doc:
            check("real %s invoice available for pipeline check" % cur, False,
                  "none found")
            continue
        # keyword arg: get_precision(doctype, fieldname, currency=None, doc=None) —
        # a positional doc lands in the currency slot and blows up downstream.
        p = frappe.get_precision("Sales Invoice", "grand_total", doc=doc)
        check("frappe.get_precision on REAL %s invoice == %ddp" % (cur, want),
              p == want, p)
        if cur in ("SAR", "BHD"):
            tol = round(1 / (10 ** p), 4)
            want_tol = 0.01 if cur == "SAR" else 0.001
            check("overbill tolerance %s = %s" % (cur, want_tol), tol == want_tol, tol)

    passed = sum(1 for c in checks if c["pass"])
    return {"summary": {"passed": passed, "total": len(checks)}, "results": checks}
