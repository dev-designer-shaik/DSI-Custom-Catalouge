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

import json
import re

import frappe

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
	"""Port of the website's findCustomerByEmail (lib/erpnext/orders.ts): email →
	Contact (via the Contact Email child) → Dynamic Link → Customer. This is the
	lookup that finds legacy customers whose email lives only on the Contact,
	which a plain Customer.email_id query would miss.

	It answers ONLY when the chain is unambiguous. The old form took `limit 1`
	with no ordering, so an email carried by several offline-entered Contacts
	resolved to whichever row MySQL happened to return — that is how a website
	account came to be filed under an unrelated imported Customer. An ambiguous
	match now returns None and the caller creates a fresh Customer: a duplicate
	customer is a bookkeeping annoyance, the wrong customer is someone else's
	order."""
	if not email:
		return None
	rows = frappe.db.sql(
		"""select distinct dl.link_name
           from `tabDynamic Link` dl
           join `tabContact Email` ce on ce.parent = dl.parent and ce.parenttype = 'Contact'
           where dl.parenttype = 'Contact' and dl.link_doctype = 'Customer'
             and lower(ce.email_id) = lower(%(email)s)
           limit 10""",
		{"email": email},
	)
	matches = [r[0] for r in (rows or []) if r[0] and frappe.db.exists("Customer", r[0])]
	if len(matches) == 1:
		return matches[0]
	if len(matches) > 1:
		frappe.logger().warning(
			"create_order_atomic: {} resolves to {} customers ({}) — refusing to guess an identity".format(
				email, len(matches), ", ".join(matches)
			)
		)
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
	doc = frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": customer_name or email or "Guest",
			"customer_type": "Individual",
			"customer_group": _valid_link(
				"Customer Group",
				customer_group or frappe.conf.get("erp_customer_group") or DEFAULT_CUSTOMER_GROUP,
				{"is_group": 0},
			),
			"territory": _valid_link("Territory", territory or "All Territories", {"is_group": 0}),
			"email_id": email,
			"mobile_no": phone,
			"custom_company": _valid_link(
				"Company", company or frappe.conf.get("erp_company") or DEFAULT_COMPANY
			),
			"default_currency": "USD",
			"default_price_list": _valid_link("Price List", PRICE_LIST),
		}
	)
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
		"phone_nos": ([{"phone": phone, "is_primary_mobile_no": 1, "is_primary_phone": 1}] if phone else []),
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
	values.update(
		{
			"doctype": "Contact",
			"first_name": first,
			"last_name": last,
			"links": [{"link_doctype": "Customer", "link_name": customer}],
		}
	)
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


# The informal country spellings this pipeline actually receives, folded onto one
# form. Paired with COUNTRY_SYNONYMS in lib/erpnext/address-identity.ts — a country
# that disagreed between the two would split one address into two documents, which
# is the bug this whole mechanism exists to stop.
COUNTRY_SYNONYMS = {
	"uae": "united arab emirates",
	"ae": "united arab emirates",
	"ksa": "saudi arabia",
	"sa": "saudi arabia",
	"usa": "united states",
	"us": "united states",
	"uk": "united kingdom",
	"gb": "united kingdom",
	"bh": "bahrain",
	"kw": "kuwait",
	"kwt": "kuwait",
	"om": "oman",
	"qa": "qatar",
	"in": "india",
}


def _norm(value):
	"""Paired with norm() in lib/erpnext/address-identity.ts. Change both or neither."""
	text = value if value is not None else ""
	if not isinstance(text, str):
		text = str(text)
	text = re.sub(r"[^a-z0-9؀-ۿ]+", " ", text.lower(), flags=re.UNICODE)
	return " ".join(text.split())


def _normalize_address_key(a):
	"""The address fingerprint. Deliberately excludes `state` (some writers send it,
	some don't) and every label-ish field, so one physical address hashes the same
	however it arrived. Paired with normalizeAddressKey in address-identity.ts."""
	country = _norm(a.get("country"))
	return "|".join(
		[
			_norm(a.get("address_line1")),
			_norm(a.get("address_line2")),
			_norm(a.get("city")),
			_norm(a.get("pincode")),
			COUNTRY_SYNONYMS.get(country, country),
		]
	)


def _list_customer_addresses(customer):
	"""Every Address linked to this customer, disabled ones included — a soft-deleted
	match should be revived, not duplicated. Ordered oldest-first so the survivor of a
	historical duplicate pair is stable."""
	return (
		frappe.db.sql(
			"""select da.name, da.address_line1, da.address_line2, da.city, da.state,
                  da.country, da.pincode, da.is_primary_address, da.is_shipping_address,
                  da.disabled
           from `tabDynamic Link` dl
           join `tabAddress` da on da.name = dl.parent
           where dl.parenttype = 'Address' and dl.link_doctype = 'Customer'
             and dl.link_name = %(customer)s
           order by da.creation asc
           limit 100""",
			{"customer": customer},
			as_dict=True,
		)
		or []
	)


def _resolve_address(customer, addr, addr_type, email=None, phone=None, rows=None):
	"""One Address document per physical address, per customer. Billing and Shipping
	are FLAGS on that one document, not two documents — a Sales Order may point
	`customer_address` and `shipping_address_name` at the same doc, and the website's
	readers already select on the flags.

	Identity is the ERP docname the caller sent (scoped to this customer's own links,
	which doubles as the ownership check), then the content fingerprint. The old form
	deduped on an invented `address_title` of '{customer} - {type}', so the Billing
	call and the Shipping call could never match each other and one address became two
	documents in the same second. `address_title` is now a label and never decides
	identity.

	`rows` is a working set: pass the same list to the Billing and Shipping calls and
	the second sees what the first created. Paired with upsertCustomerAddress in
	lib/erpnext/address-identity.ts."""
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
	values = {
		"address_line1": line1,
		"address_line2": line2 or "",
		"city": city,
		"state": state or "",
		"country": country,
		"pincode": pincode or "",
		"email_id": email or "",
		"phone": phone or "",
	}

	if rows is None:
		rows = _list_customer_addresses(customer)

	wanted_name = addr.get("name") or addr.get("address_name")
	existing = None
	if wanted_name:
		existing = next((r for r in rows if r.get("name") == wanted_name), None)
		if not existing:
			frappe.logger().warning(
				f"create_order_atomic: address {wanted_name} is not linked to {customer} — falling back "
				"to a content match"
			)
	if not existing:
		key = _normalize_address_key(values)
		# An all-empty fingerprint must never match anything.
		if key.replace("|", "").strip():
			matches = [r for r in rows if _normalize_address_key(r) == key]
			existing = next((r for r in matches if not r.get("disabled")), None) or (
				matches[0] if matches else None
			)

	want_billing = addr_type == "Billing"
	want_shipping = addr_type == "Shipping"

	if existing:
		# OR the roles: a document serving both is the whole point. Forcing the other
		# flag off is what made a saved billing address stop being offered for
		# shipping the moment an order touched it.
		is_billing = 1 if (want_billing or existing.get("is_primary_address")) else 0
		is_shipping = 1 if (want_shipping or existing.get("is_shipping_address")) else 0
		doc = frappe.get_doc("Address", existing["name"])
		doc.update(values)
		# address_title is left alone — it is a label, and rewriting it churns the doc.
		doc.is_primary_address = is_billing
		doc.is_shipping_address = is_shipping
		doc.disabled = 0
		doc.save(ignore_permissions=True)

		# Keep the working set truthful for the follow-up call in this request.
		existing.update(values)
		existing["is_primary_address"] = is_billing
		existing["is_shipping_address"] = is_shipping
		existing["disabled"] = 0
		return doc.name

	title = ", ".join([p for p in [line1, city] if p]).strip() or customer
	values.update(
		{
			"doctype": "Address",
			# A label, nothing more. Frappe appends -1/-2 on collision, which is fine now
			# that nothing reads the title to decide identity.
			"address_title": title,
			"address_type": addr_type,
			"is_primary_address": 1 if want_billing else 0,
			"is_shipping_address": 1 if want_shipping else 0,
			"links": [{"link_doctype": "Customer", "link_name": customer}],
		}
	)
	doc = frappe.get_doc(values)
	doc.insert(ignore_permissions=True)

	rows.append(
		{
			"name": doc.name,
			"address_line1": line1,
			"address_line2": line2 or "",
			"city": city,
			"state": state or "",
			"country": country,
			"pincode": pincode or "",
			"is_primary_address": 1 if want_billing else 0,
			"is_shipping_address": 1 if want_shipping else 0,
			"disabled": 0,
		}
	)
	return doc.name


def _validate_so_passthrough(so):
	"""The passthrough must describe exactly one Sales Order. Any 'doctype' key found
	in it (top level or nested rows) must be Sales Order or one of its child tables —
	this endpoint must never become a generic write primitive."""

	def check(node):
		if isinstance(node, dict):
			dt = node.get("doctype")
			if dt and dt != "Sales Order" and dt not in SO_CHILD_DOCTYPES:
				frappe.throw(f"create_order_atomic: unexpected doctype {dt} in sales_order payload")
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
		pe.remarks = "Tap charge {} ({} {:.2f}) captured at website checkout".format(
			charge_id, (payment.get("currency") or so.currency or "").upper(), amount
		)
		pe.flags.ignore_permissions = True
		pe.insert(ignore_permissions=True)
		pe.submit()
		return pe.name
	except Exception:
		frappe.db.rollback(save_point="tap_payment_entry")
		frappe.log_error(frappe.get_traceback(), f"create_order_atomic: Payment Entry failed for {so.name}")
		frappe.get_doc(
			{
				"doctype": "Comment",
				"comment_type": "Comment",
				"reference_doctype": "Sales Order",
				"reference_name": so.name,
				"content": f"PAYMENT ENTRY FAILED for Tap charge {charge_id} — record the payment manually. See Error Log.",
			}
		).insert(ignore_permissions=True)
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
	existing = frappe.db.get_value(
		"Sales Order", {"custom_idempotency_key": key}, ["name", "customer", "grand_total"], as_dict=True
	)
	if existing:
		return {
			"sales_order": existing.name,
			"customer": existing.customer,
			"customer_created": False,
			"grand_total": existing.grand_total,
			"created": False,
			"idempotent": True,
		}

	# All writes below are in this single request's transaction: any throw rolls back all.
	email = payload.get("email")
	phone = payload.get("phone")
	explicit_customer = payload.get("customer")
	if explicit_customer:
		if not frappe.db.exists("Customer", explicit_customer):
			frappe.throw(f"create_order_atomic: customer {explicit_customer} does not exist")
		customer, customer_created = explicit_customer, False
	else:
		customer, customer_created = _resolve_customer(email, payload.get("customer_name") or email, phone)
	contact = _resolve_contact(customer, payload.get("contact") or {}, email, phone)
	# One shared working set, so the shipping call sees the document the billing call
	# just created or updated. When both addresses are the same physical place they
	# converge on one document carrying both role flags — instead of the pair of
	# '{customer} - Billing' / '{customer} - Shipping' records this used to mint on
	# every single order.
	address_rows = _list_customer_addresses(customer)
	billing = _resolve_address(
		customer, payload.get("billing_address") or {}, "Billing", email, phone, rows=address_rows
	)
	shipping = _resolve_address(
		customer,
		payload.get("shipping_address") or payload.get("billing_address") or {},
		"Shipping",
		email,
		phone,
		rows=address_rows,
	)

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
		frappe.get_doc(
			{
				"doctype": "Comment",
				"comment_type": "Comment",
				"reference_doctype": "Sales Order",
				"reference_name": so.name,
				"content": "\n".join(
					[
						"Shipping quote selected at checkout",
						"Provider: {}".format(sq.get("provider") or "N/A"),
						"Service: {}".format(sq.get("serviceName") or "N/A"),
						"Scope: {}".format(sq.get("scope") or "N/A"),
						"Cost: {} USD".format(
							sq.get("shippingAmount") if sq.get("shippingAmount") is not None else 0
						),
						"Estimated Days: {}".format(sq.get("estimatedDays") or "N/A"),
						"Provider Code: {}".format(sq.get("providerCode") or "N/A"),
						"Service ID: {}".format(sq.get("serviceId") or "N/A"),
					]
				),
			}
		).insert(ignore_permissions=True)

	payment_entry = _record_payment(so, payload.get("payment"))

	return {
		"sales_order": so.name,
		"customer": customer,
		"customer_created": customer_created,
		"grand_total": so.grand_total,
		"payment_entry": payment_entry,
		"created": True,
		"idempotent": False,
	}
