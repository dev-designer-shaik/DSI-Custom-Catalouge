"""Refill requests from the website.

A refill is arranged by hand, not bought at checkout. The website's Refill
Service form calls `request_refill`: the request becomes (or is added to) a Lead
and every enabled System User holding the Sales User role is emailed what they
need to follow up. The ERP decides who is told, so adding or removing a person
from the role is the only change ever needed.
"""

import re

import frappe
from frappe import _
from frappe.utils import escape_html, now_datetime

ROLE = "Sales User"
EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def clean_request(payload) -> dict:
	"""Validate what the form sent; raises ValueError naming the first bad field."""
	data = payload if isinstance(payload, dict) else {}

	def text(key, limit):
		return str(data.get(key) or "").strip()[:limit]

	request = {
		"first_name": text("first_name", 100),
		"last_name": text("last_name", 100),
		"email": text("email", 140).lower(),
		"phone": text("phone", 40),
		"country": text("country", 140),
		"product_name": text("product_name", 200),
		"item_code": text("item_code", 140),
		"message": text("message", 5000),
	}
	for key in ("first_name", "last_name", "email", "phone", "product_name", "message"):
		if not request[key]:
			raise ValueError(key)
	if not EMAIL.match(request["email"]):
		raise ValueError("email")
	return request


def sales_recipients(users) -> list[str]:
	"""Emails of the enabled System Users in `users`, lower-cased, no duplicates."""
	seen: dict[str, None] = {}
	for user in users:
		email = str(user.get("email") or user.get("name") or "").strip().lower()
		if user.get("enabled") and user.get("user_type") == "System User" and EMAIL.match(email):
			seen[email] = None
	return list(seen)


def render_email(request: dict, lead: str) -> tuple[str, str]:
	subject = f"Refill request: {request['product_name']} · {request['first_name']} {request['last_name']}"
	rows = [
		("Fragrance", request["product_name"]),
		("Refill item code", request["item_code"] or "—"),
		("Customer", f"{request['first_name']} {request['last_name']}"),
		("Phone", request["phone"]),
		("Email", request["email"]),
		("Country", request["country"] or "—"),
		("Message", request["message"]),
		("Lead", lead),
	]
	body = "".join(
		f"<tr><td style='padding:4px 16px 4px 0;color:#777;vertical-align:top'>{escape_html(k)}</td>"
		f"<td style='padding:4px 0'>{escape_html(v).replace(chr(10), '<br>')}</td></tr>"
		for k, v in rows
	)
	html = (
		"<p>A customer asked for a refill on the website. Please contact them to arrange it, "
		"then record the outcome on the Lead.</p>"
		f"<table>{body}</table>"
	)
	return subject, html


@frappe.whitelist(methods=["POST"])
def request_refill(payload):
	"""Record a refill request as a Lead and email every Sales User.

	Returns {"lead": name, "emailed": count}. A request that nobody could be
	emailed about is still recorded, and the gap is written to the Error Log so
	it is not silent.
	"""
	import json

	try:
		request = clean_request(json.loads(payload) if isinstance(payload, str) else payload)
	except ValueError as bad:
		frappe.throw(_("Missing or invalid: {0}").format(bad), frappe.ValidationError)

	lead = _record_lead(request)

	holders = frappe.get_all("Has Role", filters={"role": ROLE, "parenttype": "User"}, pluck="parent")
	users = (
		frappe.get_all("User", filters={"name": ["in", holders]}, fields=["name", "email", "enabled", "user_type"])
		if holders
		else []
	)
	recipients = sales_recipients(users)
	if not recipients:
		frappe.log_error(f"Refill request {lead} has no Sales User with an email to notify.", "Refill request")
		return {"lead": lead, "emailed": 0}

	subject, html = render_email(request, lead)
	frappe.sendmail(
		recipients=recipients,
		subject=subject,
		message=html,
		reply_to=request["email"],
		reference_doctype="Lead",
		reference_name=lead,
	)
	return {"lead": lead, "emailed": len(recipients)}


def _record_lead(request: dict) -> str:
	"""Reuse the Lead with this email (Lead emails are unique) or create one."""
	note = f"Refill request for {request['product_name']} ({request['item_code'] or 'no item code'}), {now_datetime():%Y-%m-%d %H:%M}:\n{request['message']}"
	name = frappe.db.get_value("Lead", {"email_id": request["email"]}, "name")
	if name:
		lead = frappe.get_doc("Lead", name)
	else:
		lead = frappe.get_doc(
			{
				"doctype": "Lead",
				"first_name": request["first_name"],
				"last_name": request["last_name"],
				"email_id": request["email"],
				# mobile_no is mandatory on this site's Lead.
				"mobile_no": request["phone"],
				"status": "Open",
			}
		)
		if request["country"] and frappe.db.exists("Country", request["country"]):
			lead.country = request["country"]
		lead.insert(ignore_permissions=True)
	lead.add_comment("Comment", note)
	return lead.name
