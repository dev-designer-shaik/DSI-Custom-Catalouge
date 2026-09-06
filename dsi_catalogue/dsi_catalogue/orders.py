"""
Tier 4 — atomic, idempotent order creation.

create_order_atomic resolves/creates Customer + Contact + Address and creates the Sales
Order in ONE Frappe request = ONE DB transaction. Either everything commits or (on any
error) nothing does — no more orphaned customer-without-order from a mid-sequence failure.

Idempotency: keyed by custom_idempotency_key on Sales Order. A repeat call with the same
key returns the already-created order instead of duplicating it — so checkout retries and
Tap payment-webhook retries are safe.

PARITY CONTRACT (2026-09-02): this endpoint replicates the website's legacy multi-call
path field-for-field — lib/erpnext/orders.ts (getOrCreateGuestCustomer,
createOrUpdateContact, ensureAddressExists, createSalesOrderManual) is the spec. The
website sends its OWN Sales Order payload under `sales_order` (passthrough); ERP only
overlays the resolver outputs (customer, addresses, contact, idempotency key). The
drift harness (tools/orders/drift-check.ts) proves old ≡ new before each deploy.

Payload v2:
{ idempotency_key (req), customer?: str,           # explicit Customer docname — bypasses resolution
  email, customer_name, phone, contact{first_name,last_name},
  billing_address{}, shipping_address{},            # website address dicts (line1/line2/city/state/country_code/pincode + email/phone)
  shipping_quote{...},                              # for the summary comment + provider mapping
  sales_order: {...},                               # website-built SO payload (see buildSalesOrderPayload)
  payment?: {charge_id, amount, currency, method} } # captured Tap charge -> Payment Entry (see _record_payment)
"""
import frappe
import json

PRICE_LIST = "USD - Online"
DEFAULT_COMPANY = "DESIGNER SHAIK INC. WLL"
# Pinned 2026-09-02 with the user: every configured group default was empty and
# 'Customer - Online' does not exist on this site, so both old fallbacks resolved to an
# arbitrary leaf. B2C - Online exists and matches the channel. site_config
# erp_customer_group still overrides.
DEFAULT_CUSTOMER_GROUP = "B2C - Online"
# Child doctypes the sales_order passthrough may legally reference.
SO_CHILD_DOCTYPES = {"Sales Order Item", "Sales Taxes and Charges"}


def _valid_link(doctype, preferred, leaf=None):
    """Return `preferred` if it exists, else a leaf/any valid record — so names that
    differ between erp1 and the erp2 mirror (Customer Group, Territory, Company, Price
    List) all resolve."""
    if preferred and frappe.db.exists(doctype, preferred):
        return preferred
    if leaf:
        v = frappe.db.get_value(doctype, leaf, "name")
        if v:
            return v
    return frappe.db.get_value(doctype, {}, "name")


def _find_customer_by_email(email):
    """Port of the website's findCustomerByEmail (orders.ts:219-250): email → Contact
    (via the Contact Email child) → Dynamic Link → Customer. This is the lookup that
    finds legacy customers whose email lives only on the Contact, which a plain
    Customer.email_id query would miss."""
    if not email:
        return None
    rows = frappe.db.sql(
        """select dl.link_name
           from `tabDynamic Link` dl
           join `tabContact Email` ce on ce.parent = dl.parent and ce.parenttype = 'Contact'
           where dl.parenttype = 'Contact' and dl.link_doctype = 'Customer'
             and lower(ce.email_id) = lower(%(email)s)
           limit 1""",
        {"email": email},
    )
    if rows and frappe.db.exists("Customer", rows[0][0]):
        return rows[0][0]
    return None


def _resolve_customer(email, customer_name, phone, customer_group=None, territory=None, company=None):
    """Resolution order (parity with the webhook): explicit caller-supplied docname is
    handled by the caller; here: Contact-email chain → Customer.email_id → exact name →
    create. Returns (name, created)."""
    name = _find_customer_by_email(email)
    if not name and email:
        name = frappe.db.get_value("Customer", {"email_id": email}, "name")
    if not name and customer_name:
        name = frappe.db.get_value("Customer", {"customer_name": customer_name}, "name")
    if name:
        return name, False
    doc = frappe.get_doc({
        "doctype": "Customer",
        "customer_name": customer_name or email or "Guest",
        "customer_type": "Individual",
        "customer_group": _valid_link("Customer Group",
                                      customer_group or frappe.conf.get("erp_customer_group") or DEFAULT_CUSTOMER_GROUP,
                                      {"is_group": 0}),
        "territory": _valid_link("Territory", territory or "All Territories", {"is_group": 0}),
        "email_id": email,
        "mobile_no": phone,
        "custom_company": _valid_link("Company", company or frappe.conf.get("erp_company") or DEFAULT_COMPANY),
        "default_currency": "USD",
        "default_price_list": _valid_link("Price List", PRICE_LIST),
    })
    doc.insert(ignore_permissions=True)
    return doc.name, True


def _resolve_contact(customer, contact, email, phone):
    """Port of createOrUpdateContact (orders.ts:580-682): dedupe by (Contact Email =
    email) AND (Dynamic Link → this customer); UPDATE an existing contact (refresh
    email/phone rows and the link) rather than returning it untouched."""
    if not (email or phone):
        return None
    existing = None
    if email:
        rows = frappe.db.sql(
            """select dl.parent
               from `tabContact` c
               join `tabContact Email` ce on ce.parent = c.name and ce.parenttype = 'Contact'
               join `tabDynamic Link` dl on dl.parent = c.name and dl.parenttype = 'Contact'
               where lower(ce.email_id) = lower(%(email)s)
                 and dl.link_doctype = 'Customer' and dl.link_name = %(customer)s
               limit 1""",
            {"email": email, "customer": customer},
        )
        existing = rows[0][0] if rows else None
    values = {
        "is_primary_contact": 1,
        "email_ids": [{"email_id": email.lower(), "is_primary": 1}] if email else [],
        "phone_nos": ([{"phone": phone, "is_primary_mobile_no": 1, "is_primary_phone": 1}]
                      if phone else []),
    }
    if existing:
        doc = frappe.get_doc("Contact", existing)
        linked = any(l.link_doctype == "Customer" and l.link_name == customer for l in doc.links or [])
        if not linked:
            doc.append("links", {"link_doctype": "Customer", "link_name": customer})
        doc.update(values)
        doc.save(ignore_permissions=True)
        return doc.name
    first = (contact or {}).get("first_name") or (email.split("@")[0] if email else "Customer")
    last = (contact or {}).get("last_name") or ""
    values.update({
        "doctype": "Contact",
        "first_name": first,
        "last_name": last,
        "links": [{"link_doctype": "Customer", "link_name": customer}],
    })
    doc = frappe.get_doc(values)
    doc.insert(ignore_permissions=True)
    return doc.name


COUNTRY_ALIASES = {
    "UAE": "United Arab Emirates",
    "KSA": "Saudi Arabia",
    "USA": "United States",
    "UK": "United Kingdom",
    "KWT": "Kuwait",
}


def _ensure_country(country_input):
    """Port of ensureCountryExists (website orders.ts) — same strategy order, so
    both paths resolve the same inputs: alias/exact name, then the ERP ISO `code`
    column, then LIKE, else the raw string (Frappe validates the link)."""
    if not country_input:
        return "Bahrain"
    trimmed = country_input.strip()
    aliased = COUNTRY_ALIASES.get(trimmed.upper(), trimmed)
    exact = frappe.db.get_value("Country", {"name": aliased})
    if exact:
        return exact
    if len(trimmed) == 2:
        by_code = frappe.db.get_value("Country", {"code": trimmed.lower()})
        if by_code:
            return by_code
    like = frappe.db.get_value("Country", {"name": ["like", "%" + aliased + "%"]})
    return like or aliased


def _resolve_address(customer, addr, addr_type, email=None, phone=None):
    """Port of ensureAddressExists (orders.ts:689-778): dedupe by
    address_title '{customer} - {type}' + Customer link; UPDATE on match; Billing sets
    is_primary_address, Shipping sets is_shipping_address."""
    if not addr:
        return None
    line1 = addr.get("line1") or addr.get("address_line1") or addr.get("address") or ""
    line2 = addr.get("line2") or addr.get("address_line2") or ""
    city = addr.get("city") or ""
    state = addr.get("state") or ""
    pincode = addr.get("pincode") or addr.get("postalCode") or addr.get("zip") or ""
    country = _ensure_country(
        addr.get("countryCode") or addr.get("country_code") or addr.get("country") or "AE"
    )
    address_title = "{0} - {1}".format(customer, addr_type)
    values = {
        "address_title": address_title,
        "address_type": addr_type,
        "address_line1": line1,
        "address_line2": line2 or "",
        "city": city,
        "state": state or "",
        "country": country,
        "pincode": pincode or "",
        "is_primary_address": 1 if addr_type == "Billing" else 0,
        "is_shipping_address": 1 if addr_type == "Shipping" else 0,
        "email_id": email or "",
        "phone": phone or "",
    }
    existing = frappe.db.sql(
        """select dl.parent from `tabDynamic Link` dl
           join `tabAddress` da on da.name = dl.parent
           where dl.parenttype = 'Address' and dl.link_doctype = 'Customer'
             and dl.link_name = %(customer)s and da.address_title = %(title)s
           limit 1""",
        {"customer": customer, "title": address_title},
    )
    if existing:
        doc = frappe.get_doc("Address", existing[0][0])
        doc.update(values)
        doc.save(ignore_permissions=True)
        return doc.name
    values.update({
        "doctype": "Address",
        "links": [{"link_doctype": "Customer", "link_name": customer}],
    })
    doc = frappe.get_doc(values)
    doc.insert(ignore_permissions=True)
    return doc.name


def _validate_so_passthrough(so):
    """The passthrough must describe exactly one Sales Order. Any 'doctype' key found
    in it (top level or nested rows) must be Sales Order or one of its child tables —
    this endpoint must never become a generic write primitive."""
    def check(node):
        if isinstance(node, dict):
            dt = node.get("doctype")
            if dt and dt != "Sales Order" and dt not in SO_CHILD_DOCTYPES:
                frappe.throw("create_order_atomic: unexpected doctype {0} in sales_order payload".format(dt))
            for v in node.values():
                check(v)
        elif isinstance(node, list):
            for v in node:
                check(v)
    check(so)
    so["doctype"] = "Sales Order"


def _record_payment(so, payment):
    """Post the captured Tap charge as a submitted Payment Entry against the SO.

    Opt-in per site: needs site_config `tap_mode_of_payment` (Mode of Payment
    docname) and `tap_receiving_account` (the Account the Tap payout lands in,
    any currency — get_payment_entry handles the exchange rate). Failures are
    isolated with a savepoint and logged as an SO comment so the order itself
    is never lost to an accounting-setup problem. Returns the PE name or None.
    """
    if not payment or not isinstance(payment, dict):
        return None
    mode = frappe.conf.get("tap_mode_of_payment")
    account = frappe.conf.get("tap_receiving_account")
    if not (mode and account):
        return None
    charge_id = (payment.get("charge_id") or "").strip()
    amount = float(payment.get("amount") or 0)
    if not charge_id or amount <= 0:
        return None
    if frappe.db.exists("Payment Entry", {"reference_no": charge_id, "docstatus": 1}):
        return frappe.db.get_value("Payment Entry", {"reference_no": charge_id, "docstatus": 1}, "name")

    frappe.db.savepoint("tap_payment_entry")
    try:
        from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

        pe = get_payment_entry("Sales Order", so.name, party_amount=amount, bank_account=account)
        pe.mode_of_payment = mode
        pe.reference_no = charge_id
        pe.reference_date = frappe.utils.nowdate()
        pe.remarks = "Tap charge {0} ({1} {2:.2f}) captured at website checkout".format(
            charge_id, (payment.get("currency") or so.currency or "").upper(), amount)
        pe.flags.ignore_permissions = True
        pe.insert(ignore_permissions=True)
        pe.submit()
        return pe.name
    except Exception:
        frappe.db.rollback(save_point="tap_payment_entry")
        frappe.log_error(frappe.get_traceback(), "create_order_atomic: Payment Entry failed for {0}".format(so.name))
        frappe.get_doc({
            "doctype": "Comment", "comment_type": "Comment",
            "reference_doctype": "Sales Order", "reference_name": so.name,
            "content": "PAYMENT ENTRY FAILED for Tap charge {0} — record the payment manually. See Error Log.".format(charge_id),
        }).insert(ignore_permissions=True)
        return None


@frappe.whitelist()
def create_order_atomic(payload=None):
    """One transactional, idempotent order — parity with the website's legacy
    multi-call path (see module docstring). Returns
    {sales_order, customer, customer_created, grand_total, created, idempotent}."""
    if isinstance(payload, str):
        payload = json.loads(payload or "{}")
    payload = payload or {}
    key = (payload.get("idempotency_key") or "").strip()
    if not key:
        frappe.throw("idempotency_key is required")
    so_payload = payload.get("sales_order") or {}
    if not so_payload.get("items"):
        frappe.throw("sales_order.items are required")

    # Idempotency short-circuit — never create a second order for the same key.
    existing = frappe.db.get_value("Sales Order", {"custom_idempotency_key": key},
                                   ["name", "customer", "grand_total"], as_dict=True)
    if existing:
        return {"sales_order": existing.name, "customer": existing.customer,
                "customer_created": False, "grand_total": existing.grand_total,
                "created": False, "idempotent": True}

    # All writes below are in this single request's transaction: any throw rolls back all.
    email = payload.get("email")
    phone = payload.get("phone")
    explicit_customer = payload.get("customer")
    if explicit_customer:
        if not frappe.db.exists("Customer", explicit_customer):
            frappe.throw("create_order_atomic: customer {0} does not exist".format(explicit_customer))
        customer, customer_created = explicit_customer, False
    else:
        customer, customer_created = _resolve_customer(
            email, payload.get("customer_name") or email, phone)
    contact = _resolve_contact(customer, payload.get("contact") or {}, email, phone)
    billing = _resolve_address(customer, payload.get("billing_address") or {}, "Billing", email, phone)
    shipping = _resolve_address(customer, payload.get("shipping_address")
                                or payload.get("billing_address") or {}, "Shipping", email, phone)

    _validate_so_passthrough(so_payload)
    so_payload["customer"] = customer
    so_payload["custom_idempotency_key"] = key
    if billing:
        so_payload["customer_address"] = billing
    if shipping:
        so_payload["shipping_address_name"] = shipping
    if contact:
        so_payload["contact_person"] = contact

    so = frappe.get_doc(so_payload)
    # docstatus rides in the passthrough: inserting a new doc with docstatus=1 maps to
    # _action "submit" in Document.check_if_latest, so validate/before_submit/on_update/
    # on_submit all fire — byte-identical to the legacy REST POST with docstatus:1.
    so.insert(ignore_permissions=True)

    sq = payload.get("shipping_quote") or {}
    if sq:
        frappe.get_doc({
            "doctype": "Comment",
            "comment_type": "Comment",
            "reference_doctype": "Sales Order",
            "reference_name": so.name,
            "content": "\n".join([
                "Shipping quote selected at checkout",
                "Provider: {0}".format(sq.get("provider") or "N/A"),
                "Service: {0}".format(sq.get("serviceName") or "N/A"),
                "Scope: {0}".format(sq.get("scope") or "N/A"),
                "Cost: {0} USD".format(sq.get("shippingAmount") if sq.get("shippingAmount") is not None else 0),
                "Estimated Days: {0}".format(sq.get("estimatedDays") or "N/A"),
                "Provider Code: {0}".format(sq.get("providerCode") or "N/A"),
                "Service ID: {0}".format(sq.get("serviceId") or "N/A"),
            ]),
        }).insert(ignore_permissions=True)

    payment_entry = _record_payment(so, payload.get("payment"))

    return {"sales_order": so.name, "customer": customer,
            "customer_created": customer_created, "grand_total": so.grand_total,
            "payment_entry": payment_entry,
            "created": True, "idempotent": False}
