"""
Tier 4 — atomic, idempotent order creation.

create_order_atomic resolves/creates Customer + Contact + Address and creates the Sales
Order in ONE Frappe request = ONE DB transaction. Either everything commits or (on any
error) nothing does — no more orphaned customer-without-order from a mid-sequence failure.

Idempotency: keyed by custom_idempotency_key on Sales Order. A repeat call with the same
key returns the already-created order instead of duplicating it — so checkout retries and
Tap payment-webhook retries are safe.

Replaces the website's 3-6 separate, non-atomic round-trips (getOrCreateGuestCustomer →
createOrUpdateContact → ensureAddressExists → createSalesOrder).
"""
import frappe
import json

PRICE_LIST = "USD - Online"
DEFAULT_COMPANY = "DESIGNER SHAIK INC. WLL"


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


def _resolve_customer(email, customer_name, phone, customer_group=None, territory=None, company=None):
    name = None
    if email:
        name = frappe.db.get_value("Customer", {"email_id": email}, "name")
    if not name and customer_name:
        name = frappe.db.get_value("Customer", {"customer_name": customer_name}, "name")
    if name:
        return name
    doc = frappe.get_doc({
        "doctype": "Customer",
        "customer_name": customer_name or email or "Guest",
        "customer_type": "Individual",
        "customer_group": _valid_link("Customer Group",
                                      customer_group or frappe.conf.get("erp_customer_group") or "Customer - Online",
                                      {"is_group": 0}),
        "territory": _valid_link("Territory", territory or "All Territories", {"is_group": 0}),
        "email_id": email,
        "mobile_no": phone,
        "custom_company": _valid_link("Company", company or frappe.conf.get("erp_company") or DEFAULT_COMPANY),
        "default_currency": "USD",
        "default_price_list": _valid_link("Price List", PRICE_LIST),
    })
    doc.insert(ignore_permissions=True)
    return doc.name


def _resolve_contact(customer, contact, email, phone):
    if not (email or phone):
        return None
    existing = None
    if email:
        existing = frappe.db.get_value("Contact", {"email_id": email}, "name")
    if existing:
        return existing
    doc = frappe.get_doc({
        "doctype": "Contact",
        "first_name": contact.get("first_name") or (email.split("@")[0] if email else "Customer"),
        "last_name": contact.get("last_name") or "",
        "email_ids": [{"email_id": email, "is_primary": 1}] if email else [],
        "phone_nos": [{"phone": phone, "is_primary_mobile_no": 1}] if phone else [],
        "links": [{"link_doctype": "Customer", "link_name": customer}],
    })
    doc.insert(ignore_permissions=True)
    return doc.name


def _resolve_address(customer, addr, addr_type):
    if not addr:
        return None
    line1 = addr.get("address_line1") or addr.get("address") or ""
    city = addr.get("city") or ""
    # Reuse an identical existing address for this customer.
    if line1:
        existing = frappe.db.sql(
            """select dl.parent from `tabDynamic Link` dl
               join `tabAddress` da on da.name = dl.parent
               where dl.parenttype='Address' and dl.link_doctype='Customer' and dl.link_name=%s
                 and da.address_line1=%s and ifnull(da.city,'')=%s limit 1""",
            (customer, line1, city),
        )
        if existing:
            return existing[0][0]
    doc = frappe.get_doc({
        "doctype": "Address",
        "address_title": addr.get("address_title") or customer,
        "address_type": addr_type,
        "address_line1": line1,
        "address_line2": addr.get("address_line2") or "",
        "city": city,
        "state": addr.get("state") or "",
        "country": addr.get("country") or "Bahrain",
        "pincode": addr.get("pincode") or addr.get("zip") or "",
        "phone": addr.get("phone") or "",
        "email_id": addr.get("email_id") or "",
        "links": [{"link_doctype": "Customer", "link_name": customer}],
    })
    doc.insert(ignore_permissions=True)
    return doc.name


@frappe.whitelist()
def create_order_atomic(payload=None):
    """One transactional, idempotent order. payload (JSON or dict):
    { idempotency_key (req), email, customer_name, phone, currency, company?,
      delivery_date?, items:[{item_code, qty, rate?}], billing_address{}, shipping_address{},
      contact{first_name,last_name}, shipping_quote{serviceId,providerCode,provider,shippingAmount,currency} }
    Returns {sales_order, customer, created, idempotent}."""
    if isinstance(payload, str):
        payload = json.loads(payload or "{}")
    payload = payload or {}
    key = (payload.get("idempotency_key") or "").strip()
    if not key:
        frappe.throw("idempotency_key is required")
    items = payload.get("items") or []
    if not items:
        frappe.throw("items are required")

    # Idempotency short-circuit — never create a second order for the same key.
    existing = frappe.db.get_value("Sales Order", {"custom_idempotency_key": key},
                                   ["name", "customer", "grand_total"], as_dict=True)
    if existing:
        return {"sales_order": existing.name, "customer": existing.customer,
                "grand_total": existing.grand_total, "created": False, "idempotent": True}

    # All writes below are in this single request's transaction: any throw rolls back all.
    email = payload.get("email")
    customer = _resolve_customer(email, payload.get("customer_name") or email, payload.get("phone"),
                                 payload.get("customer_group"), payload.get("territory"))
    _resolve_contact(customer, payload.get("contact") or {}, email, payload.get("phone"))
    billing = _resolve_address(customer, payload.get("billing_address") or {}, "Billing")
    shipping = _resolve_address(customer, payload.get("shipping_address") or payload.get("billing_address") or {}, "Shipping")

    company = _valid_link("Company", payload.get("company") or frappe.conf.get("erp_company")
                          or frappe.defaults.get_global_default("company") or DEFAULT_COMPANY)
    warehouse = payload.get("warehouse") or frappe.conf.get("erp_warehouse")
    if not warehouse or not frappe.db.exists("Warehouse", warehouse):
        warehouse = frappe.db.get_value("Warehouse", {"is_group": 0, "company": company}, "name") \
            or frappe.db.get_value("Warehouse", {"is_group": 0}, "name")

    delivery_date = payload.get("delivery_date") or frappe.utils.add_days(frappe.utils.nowdate(), 7)
    so_items = [{"item_code": i["item_code"], "qty": i.get("qty") or 1,
                 "rate": i.get("rate"), "delivery_date": delivery_date,
                 "warehouse": warehouse} for i in items]
    so = frappe.get_doc({
        "doctype": "Sales Order",
        "customer": customer,
        "company": company,
        "set_warehouse": warehouse,
        "order_type": "Shopping Cart",
        "transaction_date": frappe.utils.nowdate(),
        "delivery_date": delivery_date,
        "currency": payload.get("currency") or "USD",
        "selling_price_list": PRICE_LIST,
        "items": so_items,
        "custom_idempotency_key": key,
    })
    if billing:
        so.customer_address = billing
    if shipping:
        so.shipping_address_name = shipping
    sq = payload.get("shipping_quote") or {}
    if sq.get("serviceId"):
        so.custom_shipping_service = sq.get("serviceId")
    if sq.get("providerCode"):
        so.custom_shipping_provider = "Aramex BH" if sq.get("providerCode") == "ARM" else (sq.get("provider") or "")
    if sq.get("shippingAmount") is not None:
        so.custom_shipping_amount = sq.get("shippingAmount")
    if sq.get("currency"):
        so.custom_shipping_currency = sq.get("currency")

    so.insert(ignore_permissions=True)
    # No explicit commit: the request boundary commits atomically on success, rolls back on error.
    return {"sales_order": so.name, "customer": customer,
            "grand_total": so.grand_total, "created": True, "idempotent": False}
