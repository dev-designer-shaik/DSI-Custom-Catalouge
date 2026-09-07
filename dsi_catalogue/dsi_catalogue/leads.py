"""Web leads: abandoned carts and inquiries land in ERPNext as Leads.

Supabase is the capture layer (cart_items / cart_snapshots / inquiries);
this module is the ERP landing surface, called by the n8n "Web Leads Sync"
workflow every 15 minutes. ERP is the system of record: every lead-worthy
website surface ends up here, structured and linked.

Idempotency: one OPEN Lead per (data_source, website_user_id|guest_session_id)
— re-syncs update the same Lead as the cart grows or shrinks. A Lead is never
promoted to a Customer by this module; the atomic order path stays the only
Customer creator (avoids the known duplicate-customer defect).
"""

import json
from datetime import datetime

import frappe
from frappe import _
from frappe.utils import now_datetime


def _to_frappe_datetime(value):
    """Supabase sends ISO-8601 with a timezone (+00:00); MariaDB DATETIME
    columns reject the offset. Strip to naive site-time string."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=None)
    except ValueError:
        return text or None

CART_SOURCE = "Website — Abandoned Cart"
INQUIRY_SOURCE = "Website Inquiry"

# Lead custom fieldnames this module owns (fixture-installed).
CART_FIELDS = (
    "cart_value",
    "cart_items_json",
    "cart_item_count",
    "cart_first_added",
    "cart_last_activity",
    "typed_address_json",
    "website_user_id",
    "guest_session_id",
    "recovery_token",
    "recovered",
    "recovered_order",
    "linked_customer",
    "data_source",
    "marketing_consent",
    "inquiry_message",
)


def _find_customer_by_email(email):
    """Email-only match against ERP Customers (Contact + Customer.email_id).

    Mirrors the atomic order path's resolver but NEVER creates a customer:
    a Lead is a prospect, not a master record.
    """
    if not email:
        return None
    contact = frappe.db.sql(
        """select link_name from `tabContact Email` ce
           join `tabDynamic Link` dl on dl.parent = ce.parent
           where lower(ce.email_id) = lower(%(email)s)
             and dl.link_doctype = 'Customer' limit 1""",
        {"email": email.strip()},
    )
    if contact:
        return contact[0][0]
    return frappe.db.get_value("Customer", {"email_id": email.strip()}, "name")


def _find_open_lead(data_source, website_user_id=None, guest_session_id=None):
    filters = {"data_source": data_source, "recovered": 0}
    if website_user_id:
        filters["website_user_id"] = website_user_id
    elif guest_session_id:
        filters["guest_session_id"] = guest_session_id
    else:
        return None
    return frappe.db.get_value("Lead", filters, "name")


def _recent_sales_order(email, customer, since):
    """Any order evidence of recovery: a Sales Order for this customer
    (email-resolved) created after the cart was first built."""
    if not customer:
        return None
    so = frappe.db.sql(
        """select so.name from `tabSales Order` so
           where so.customer = %(customer)s and so.creation > %(since)s
           order by so.creation desc limit 1""",
        {"customer": customer, "since": since},
    )
    return so[0][0] if so else None


@frappe.whitelist()
def capture_web_lead(payload=None):
    """Create or update the Lead for one website capture event.

    payload (JSON or dict):
      { data_source: "Website Cart" | "Website Inquiry",
        website_user_id?, guest_session_id?,
        email?, first_name?, last_name?, phone?,
        lead_name?,                       # fallback display name
        items: [{item_code, name, qty, price, currency}],
        cart_value?, currency?,           # derived by the caller when absent
        first_added?, last_activity?,
        typed_address?,                   # dict as typed at checkout
        recovery_token?, marketing_consent?,
        message? }                        # inquiry body
    """
    if isinstance(payload, str):
        payload = json.loads(payload or "{}")
    payload = payload or {}

    data_source = payload.get("data_source") or CART_SOURCE
    website_user_id = (payload.get("website_user_id") or "").strip()
    guest_session_id = (payload.get("guest_session_id") or "").strip()
    email = (payload.get("email") or "").strip()
    if not (website_user_id or guest_session_id) and not email:
        frappe.throw(_("capture_web_lead: one of website_user_id, guest_session_id, email is required"))

    items = payload.get("items") or []
    cart_value = payload.get("cart_value")
    if cart_value is None:
        cart_value = sum(
            float(i.get("price") or 0) * float(i.get("qty") or 1) for i in items
        )

    linked_customer = _find_customer_by_email(email)
    existing = _find_open_lead(data_source, website_user_id, guest_session_id)
    if not existing and email:
        # ERPNext enforces global email uniqueness on Lead: reuse ANY existing
        # Lead with this email (any source) rather than failing the insert.
        existing = frappe.db.get_value(
            "Lead", {"email_id": email.strip()}, "name"
        )

    fields = {
        "data_source": data_source,
        "website_user_id": website_user_id,
        "guest_session_id": guest_session_id,
        "email_id": email,
        # mobile_no is MANDATORY on this site's Lead (ops customization);
        # website leads carry the typed phone when we have it, else a visible
        # placeholder rather than a fake number.
        "mobile_no": (payload.get("phone") or "").strip() or "-",
        "cart_value": round(float(cart_value or 0), 2),
        "cart_items_json": json.dumps(items, ensure_ascii=False),
        "cart_item_count": len(items),
        "cart_first_added": _to_frappe_datetime(payload.get("first_added")) or now_datetime(),
        "cart_last_activity": _to_frappe_datetime(payload.get("last_activity")) or now_datetime(),
        # omit when absent so a re-sync without an address keeps the last one
        **({} if not payload.get("typed_address") else
           {"typed_address_json": json.dumps(payload["typed_address"], ensure_ascii=False)}),
        "recovery_token": (payload.get("recovery_token") or "").strip(),
        "marketing_consent": 1 if payload.get("marketing_consent") else 0,
        "inquiry_message": (payload.get("message") or "").strip(),
        "linked_customer": linked_customer or "",
    }

    lead_name_src = None
    if data_source == CART_SOURCE:
        lead_name = (
            payload.get("lead_name")
            or " ".join(x for x in [payload.get("first_name"), payload.get("last_name")] if x)
            or (email.split("@")[0] if email else "Website guest")
        )
    else:
        lead_name = (
            " ".join(x for x in [payload.get("first_name"), payload.get("last_name")] if x)
            or (email.split("@")[0] if email else "Website inquirer")
        )

    if existing:
        lead = frappe.get_doc("Lead", existing)
        for key, value in fields.items():
            if value not in (None, ""):
                lead.set(key, value)
        lead.lead_name = lead_name
        lead.save(ignore_permissions=True)
    else:
        lead = frappe.get_doc(
            {
                "doctype": "Lead",
                "lead_name": lead_name,
                "status": "Open",
                "__isnew": 1,
                **{k: v for k, v in fields.items()},
            }
        )
        lead.insert(ignore_permissions=True)

    # Recovery marking for carts: an order after the cart started = converted.
    if data_source == CART_SOURCE and not lead.recovered:
        since = lead.cart_first_added or now_datetime()
        so = _recent_sales_order(email, linked_customer, since)
        if so:
            lead.db_set("recovered", 1, update_modified=False)
            lead.db_set("recovered_order", so, update_modified=False)
            lead.db_set("status", "Converted", update_modified=False)
            lead.db_set("cart_last_activity", now_datetime(), update_modified=False)

    frappe.db.commit()
    return {"lead": lead.name, "created": not existing, "recovered": bool(lead.recovered),
            "linked_customer": linked_customer or ""}
