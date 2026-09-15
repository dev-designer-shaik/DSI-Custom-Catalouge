"""
Tier 4 — atomic, idempotent order creation.

create_order_atomic resolves/creates Customer + Contact + Address and creates the Sales
Order in ONE Frappe request = ONE DB transaction. Either everything commits or (on any
error) nothing does — no more orphaned customer-without-order from a mid-sequence failure.

Idempotency: keyed by custom_idempotency_key on Sales Order. A repeat call with the same
key returns the already-created order instead of duplicating it — so checkout retries and
Tap payment-webhook retries are safe.

THE one definition of a web Sales Order (since 2026-09-13): the website's legacy
multi-call path and its ERP_ATOMIC_ORDERS rollback flag were deleted — this endpoint
is the only order path. The website sends its OWN Sales Order payload under
`sales_order` (passthrough); ERP only overlays the resolver outputs (customer,
addresses, contact, idempotency key, notify channel).

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
	"""Return `preferred` if it exists, else the named leaf. NO silent fallback to
	'any record' (2026-09-13): the old tail booked orders to an arbitrary Company
	or Price List when the preferred one was missing — a silent misfiling. If the
	fallback also misses, throw loudly and fix the site's data."""
	if preferred and frappe.db.exists(doctype, preferred):
		return preferred
	if leaf:
		v = frappe.db.get_value(doctype, leaf, "name")
		if v:
			return v
	frappe.throw(
		f"create_order_atomic: no usable {doctype} found (wanted {preferred or leaf!r}) — create it "
		"on this site before taking web orders"
	)


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


_PHONE_MIN_DIGITS = 8  # below this a trailing-digit match is chance, not evidence
_PHONE_MAX_PREFIX = 3  # one country code, 1-3 digits; trunk 0 is stripped separately


def _phone_digits(value):
	return re.sub(r"^00", "", re.sub(r"\D+", "", value or ""))


def _phones_match(a, b):
	"""Same number in a different format? Port of the website's phonesMatch
	(lib/utils/identity-match.ts): equal digits, or one is the other with a
	1-3 digit country prefix in front, trunk-0 variants included. Accepts
	+97335066012 <-> 35066012; rejects +17827444784 <-> +447827444784."""

	def variants(d):
		out = {d}
		if d.startswith("0"):
			out.add(d.lstrip("0"))
		return [v for v in out if v]

	for x in variants(_phone_digits(a)):
		for y in variants(_phone_digits(b)):
			if x == y:
				return True
			longer, shorter = (x, y) if len(x) >= len(y) else (y, x)
			if (
				len(shorter) >= _PHONE_MIN_DIGITS
				and 0 < len(longer) - len(shorter) <= _PHONE_MAX_PREFIX
				and longer.endswith(shorter)
			):
				return True
	return False


def _find_customer_by_phone(phone):
	"""Phone evidence: no SQL filter expresses "same number, different format",
	so read the Customers that carry a mobile and compare in process (the
	website does the same in findCustomerByHardEvidence). One hit adopts;
	several = refuse to guess, like the email path."""
	wanted = _phone_digits(phone)
	if len(wanted) < _PHONE_MIN_DIGITS:
		return None
	rows = frappe.db.get_all(
		"Customer",
		filters={"mobile_no": ["!=", ""]},
		fields=["name", "mobile_no"],
		limit_page_length=2000,
		order_by="creation asc",
	)
	hits = [r.name for r in rows if _phones_match(r.mobile_no, phone)]
	if len(hits) == 1:
		return hits[0]
	if len(hits) > 1:
		frappe.log_error(
			title="create_order_atomic: ambiguous phone identity",
			message="phone {} matches {} customers ({}) - refusing to guess".format(
				phone, len(hits), ", ".join(hits)
			),
		)
	return None


def _resolve_customer(email, customer_name, phone, customer_group=None, territory=None, company=None):
	"""Resolution order: explicit caller-supplied docname is handled by the caller;
	here: Contact-email chain -> Customer.email_id -> Customer.mobile_no (format-
	insensitive; the live web accounts are phone-only and used to mint a fresh
	Customer per order) -> exact name -> create. Returns (name, created)."""
	name = _find_customer_by_email(email)
	if not name and email:
		name = frappe.db.get_value("Customer", {"email_id": email}, "name")
	if not name and phone:
		name = _find_customer_by_phone(phone)
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
	"""Port of the website's createOrUpdateContact (lib/erpnext/orders.ts): dedupe by (Contact Email =
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


def _order_remarks(items):
	"""The one-line "what is this sale" note, in the estate's own register.

	Live ERP fills `remarks` by hand on Sales Invoices — "Against Customer Order
	3694777", "For Emirates order" — so a human scanning a document knows what it
	is without opening the item table. Web orders arrived with the field empty and
	read as anonymous next to them.

	The label is the alias product code (`Item.custom_alias_product_code`), not the
	ERP item code, because the alias is what the catalogue, the warehouse and the
	old system all call the product. An item without one falls back to its item
	code — a remark naming the wrong thing is worse than a plain one, and a remark
	reading "for item None" is worse than both.

	One query for the whole cart, deduped, order preserved.
	"""
	codes = []
	for row in items or []:
		code = ((row or {}).get("item_code") or "").strip()
		if code and code not in codes:
			codes.append(code)
	if not codes:
		return ""

	alias = {
		r.name: (r.custom_alias_product_code or "").strip()
		for r in frappe.db.get_all(
			"Item", filters={"name": ["in", codes]}, fields=["name", "custom_alias_product_code"]
		)
	}
	labels = []
	for code in codes:
		label = alias.get(code) or code
		if label not in labels:
			labels.append(label)

	noun = "item" if len(labels) == 1 else "items"
	return "Online purchase for {} {}".format(noun, ", ".join(labels))


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
		# Title first, body second: Frappe truncates the TITLE, so the traceback
		# belongs in the body or the useful half of the log is what gets cut.
		frappe.log_error(
			f"{so.name}: {frappe.get_traceback()}",
			"create_order_atomic: Payment Entry failed",
		)
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

	payment = payload.get("payment")
	if payment and (payment.get("charge_id") or "").strip() != key:
		# The SO dedupes on custom_idempotency_key while the Payment Entry dedupes
		# on reference_no == charge_id. They only coincide by caller convention —
		# enforce it here or a mismatched caller books one order's payment against
		# another order.
		frappe.throw("create_order_atomic: payment.charge_id must equal idempotency_key")

	# Idempotency short-circuit — never create a second order for the same key.
	existing = frappe.db.get_value(
		"Sales Order", {"custom_idempotency_key": key}, ["name", "customer", "grand_total"], as_dict=True
	)
	if existing:
		# The first attempt may have created the SO but failed its Payment Entry
		# (missing site_config, accounting error) — _record_payment swallows the
		# failure into a comment by design, so a retry MUST re-attempt it or the
		# order stays unpaid forever with everything reporting success.
		pe = None
		if payment:
			existing_so = frappe.get_doc("Sales Order", existing.name)
			pe = _record_payment(existing_so, payment)
		return {
			"sales_order": existing.name,
			"customer": existing.customer,
			"customer_created": False,
			"grand_total": existing.grand_total,
			"payment_entry": pe,
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
	# Shipping-update channel the customer chose at checkout — drives whether
	# tracking notifications go by email, SMS or WhatsApp. Stored in the
	# Select's display casing; anything unknown (including wrong casing) reads
	# as the default. The Custom Field ships as a dsi_catalogue fixture; writing
	# it on a site that lacks it would be silently dropped by db_insert, so the
	# field's presence is verified, loudly, first.
	_channel = {"email": "Email", "sms": "SMS", "whatsapp": "WhatsApp"}.get(
		str(payload.get("notify_channel") or "").strip().lower(), "Email"
	)
	if not frappe.get_meta("Sales Order").has_field("custom_notify_channel"):
		frappe.throw(
			"Sales Order is missing the custom_notify_channel field — "
			"run bench --site <site> migrate with the dsi_catalogue "
			"fixtures before taking web orders"
		)
	so_payload["custom_notify_channel"] = _channel
	# Only when the caller left it empty: a remark the website sent on purpose
	# outranks a generated one.
	if not (so_payload.get("remarks") or "").strip():
		remarks = _order_remarks(so_payload.get("items"))
		if remarks:
			so_payload["remarks"] = remarks
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
