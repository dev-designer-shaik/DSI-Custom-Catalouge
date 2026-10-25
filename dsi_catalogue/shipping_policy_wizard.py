"""Manager-only methods for the one-page shipping policy. Drafts never write before Save."""

import json
import math
import socket
import time
from urllib.parse import urlsplit

import frappe
import requests
import urllib3
from frappe import _
from frappe.utils import escape_html, flt, get_url, sbool
from frappe.utils.file_lock import LockTimeoutError
from frappe.utils.synchronization import filelock

from dsi_catalogue import shipping_integration_check, shipping_policy
from dsi_catalogue.shipping_engine import (
	item_in_set,
	name_key,
	normalize_country,
	number,
	pattern_matches,
	policy_warnings,
	preview_coverage_message,
	resolve,
	validate_policy,
)
from dsi_catalogue.shipping_policy import build_order, load_policy, price_delivery, services_directory
from dsi_catalogue.shipping_policy_data import (
	MAX_BOXES,
	MAX_NUMBER,
	MAX_TEXT,
	InputError,
	clean_boxes,
	clean_items,
	clean_quotes,
	diff_policy,
	parse_json_arg,
	policy_from_rows,
	read_json,
	rows_from_policy,
	snapshot_version,
)
from dsi_catalogue.shipping_shopify_import import RateCardError, policy_from_export

MANAGER_ROLES = ("System Manager", "Website Manager")
POLICY, ZONE, ITEM_SET = "Shipping Policy", "Shipping Zone", "Shipping Item Set"
APPLY_LOCK, APPLY_LOCK_SECONDS, _SAVEPOINT = "shipping_policy_apply", 30, "shipping_policy_apply"
_CHILD_TABLES = {
	ZONE: {"countries": "country"},
	ITEM_SET: {"item_groups": "item_group", "items": "item", "patterns": "pattern"},
}
MAX_PACKED_UNITS, MAX_SAMPLE_ITEMS = 1000, 10
SAMPLE_PARCEL = {"weight_kg": 1.0, "box": {"length_cm": 20, "width_cm": 15, "height_cm": 10}}
PRICE_API = "/api/method/dsi_catalogue.shipping_policy.price_delivery"
RATES_ROUTE = "/api/webshop/shipping/rates"
HTTP_TIMEOUT_SECONDS = shipping_integration_check.HTTP_TIMEOUT_SECONDS
MAX_BODY_BYTES = shipping_integration_check.MAX_BODY_BYTES
_resolve = socket.getaddrinfo
_is_number = number


def _read_draft(draft):
	try:
		d = read_json(draft) if isinstance(draft, str) else draft
		if not isinstance(d, dict) or len(json.dumps(d, allow_nan=False)) > 2000000:
			raise ValueError()
		if not all(isinstance(d.get(k), list) for k in ("zones", "item_sets", "rules")):
			raise ValueError()
		return d
	except (ValueError, TypeError, RecursionError):
		frappe.throw("The draft is not in the expected shape.")


def _problems(policy, countries, saved):
	problems = validate_policy(policy)
	if problems:
		return problems
	for z in policy["zones"]:
		for c in z["countries"]:
			if c not in countries:
				problems.append(f"{z['name']}: country {c} is not in the ERP's Country list.")
	directory = services_directory()
	if directory is not None:
		known = {s["name"] for s in directory}
		for r in policy["rules"]:
			for service in r.get("services", []):
				if service not in known:
					problems.append(f"Unknown delivery service: {service}")
	for key in ("zones", "item_sets"):
		existing = {name_key(x["name"]): x["name"] for x in saved.get(key, [])}
		for x in policy[key]:
			if name_key(x["name"]) in existing and existing[name_key(x["name"])] != x["name"]:
				problems.append("Keep the existing spelling of " + existing[name_key(x["name"])])
	return problems


@frappe.whitelist()
def wizard_state():
	frappe.only_for(MANAGER_ROLES)
	policy = load_policy()
	directory = services_directory()
	providers = (
		frappe.get_all("Shipping Provider", fields=["name", "enabled", "carrier_code"], order_by="name asc")
		if shipping_policy.carrier_tables_exist()
		else []
	)
	return {
		"policy": policy,
		"version": snapshot_version(policy),
		"countries": _country_list(),
		"providers": providers,
		"services": directory,
		"item_groups": frappe.get_all("Item Group", pluck="name", order_by="name asc"),
	}


@frappe.whitelist()
def preview_changes(draft):
	frappe.only_for(MANAGER_ROLES)
	policy = _read_draft(draft)
	saved = load_policy()
	return {
		"changes": diff_policy(saved, policy),
		"problems": _problems(policy, _country_names(), saved),
		"warnings": policy_warnings(policy),
	}


@frappe.whitelist(methods=["POST"])
def apply_policy(draft, expected_version):
	frappe.only_for(MANAGER_ROLES)
	try:
		with filelock(APPLY_LOCK, timeout=APPLY_LOCK_SECONDS):
			# Held by the transaction until Frappe commits the request, beyond the file lock's lifetime.
			frappe.db.sql("SELECT field FROM tabSingles WHERE doctype=%s FOR UPDATE", (POLICY,))
			before = load_policy()
			if expected_version != snapshot_version(before):
				frappe.throw(
					"Someone changed the policy. Reload before saving your draft.",
					frappe.TimestampMismatchError,
				)
			policy = _read_draft(draft)
			if problems := _problems(policy, _country_names(), before):
				frappe.throw([escape_html(p) for p in problems], as_list=True)
			rows, loaded = _rows_and_loaded(policy, _country_names())
			frappe.db.savepoint(_SAVEPOINT)
			try:
				_write(rows, loaded, before)
				written = load_policy()
				if problems := validate_policy(written):
					frappe.throw([escape_html(p) for p in problems], as_list=True)
			except BaseException:
				frappe.db.rollback(save_point=_SAVEPOINT)
				raise
			frappe.db.release_savepoint(_SAVEPOINT)
			return {"version": snapshot_version(written), "changes": diff_policy(before, written)}
	except LockTimeoutError:
		frappe.throw("Someone else is saving. Please try again in a moment.")


def _write(rows, loaded, before):
	zones_before = {z["name"]: z for z in before["zones"]}
	sets_before = {g["name"]: g for g in before["item_sets"]}
	for row, group in zip(rows["item_sets"], loaded["item_sets"], strict=False):
		_upsert(ITEM_SET, row, group, sets_before)
	for row, zone in zip(rows["zones"], loaded["zones"], strict=False):
		_upsert(ZONE, row, zone, zones_before)
	doc = frappe.get_doc(POLICY)
	free = {r.name for r in doc.rules}
	for r in rows["policy"]["rules"]:
		if r.get("name") in free:
			free.remove(r["name"])
		else:
			r.pop("name", None)
	doc.update(rows["policy"])
	doc.flags.policy_draft_validated = True
	doc.save()
	for name in zones_before.keys() - {z["name"] for z in loaded["zones"]}:
		frappe.delete_doc(ZONE, name)
	for name in sets_before.keys() - {g["name"] for g in loaded["item_sets"]}:
		frappe.delete_doc(ITEM_SET, name)


@frappe.whitelist()
def import_shopify(config_csv, not_delivered_csv=None):
	frappe.only_for(MANAGER_ROLES)
	if (
		not isinstance(config_csv, str)
		or len(config_csv) > 1000000
		or (
			not_delivered_csv is not None
			and (not isinstance(not_delivered_csv, str) or len(not_delivered_csv) > 1000000)
		)
	):
		frappe.throw("Choose CSV files of at most 1 MB each.")
	try:
		imported = policy_from_export(config_csv, not_delivered_csv)
	except RateCardError as e:
		frappe.throw(str(e))
	known = _country_names()
	imported["problems_for_countries"] = sorted(
		{c for z in imported["zones"] for c in z["countries"] if c not in known}
	)
	return imported


def _search(text="", item_group="", pattern="", published_only=False):
	for v in (text, item_group, pattern):
		if not isinstance(v, str) or len(v) > 140:
			frappe.throw("Product filters must be text of at most 140 characters.")
	if pattern and (len(pattern) > 80 or pattern.count("*") > 12):
		frappe.throw("A pattern allows at most 80 characters and 12 stars.")
	clauses = ["i.disabled=0"]
	args = []
	if text:
		clauses.append("(i.name LIKE %s OR i.item_name LIKE %s OR w.custom_index_key LIKE %s)")
		args += ["%" + text + "%"] * 3
	if item_group:
		bounds = frappe.db.get_value("Item Group", item_group, ["lft", "rgt"])
		if not bounds:
			return []
		clauses.append("g.lft >= %s AND g.rgt <= %s")
		args += list(bounds)
	if published_only is True or str(published_only).lower() in ("1", "true"):
		clauses.append("w.published=1")
	if pattern:
		# Broad SQL prefilter, followed by the exact bounded glob matcher.
		sql_pattern = "".join(
			"%" if c == "*" else "_" if c == "?" else "\\" + c if c in "%_" else c for c in pattern.upper()
		)
		if "[" not in pattern:
			clauses.append("TRIM(BOTH '}' FROM TRIM(LEADING '{' FROM UPPER(w.custom_index_key))) LIKE %s")
			args.append(sql_pattern)
	rows = frappe.db.sql(
		"SELECT i.name AS item_code, i.item_name, i.item_group, COALESCE(w.custom_index_key,'') AS index_key, COALESCE(w.published,0) AS published FROM tabItem i LEFT JOIN `tabWebsite Item` w ON w.item_code=i.name LEFT JOIN `tabItem Group` g ON g.name=i.item_group WHERE "
		+ " AND ".join(clauses)
		+ " ORDER BY w.published DESC, i.item_name, i.name LIMIT 10001",
		tuple(args),
		as_dict=True,
	)
	if len(rows) > 10000:
		frappe.throw("Narrow the product filters; more than 10,000 products match.")
	seen = set()
	out = []
	for row in rows:
		if row.item_code in seen or (pattern and not pattern_matches(pattern, row.index_key)):
			continue
		seen.add(row.item_code)
		out.append(dict(row))
	return out


@frappe.whitelist()
def search_products(text="", item_group="", pattern="", published_only=False, sets=None, start=0, limit=50):
	frappe.only_for(MANAGER_ROLES)
	start = max(0, min(int(start), 10000))
	limit = max(1, min(int(limit), 100))
	rows = _search(text, item_group, pattern, published_only)
	page = rows[start : start + limit]
	draft_sets = parse_json_arg(sets, [])
	if not isinstance(draft_sets, list) or len(draft_sets) > 40:
		frappe.throw("Invalid product groups.")
	groups = shipping_policy.item_groups_for([r["item_code"] for r in page])
	for r in page:
		r["listed_by"] = next(
			(
				g.get("name", "")
				for g in draft_sets
				if item_in_set({**r, "groups": groups.get(r["item_code"], [])}, g)
			),
			"",
		)
	return {"rows": page, "more": start + limit < len(rows), "count": len(rows)}


@frappe.whitelist()
def count_products(text="", item_group="", pattern="", published_only=False):
	frappe.only_for(MANAGER_ROLES)
	return {"count": len(_search(text, item_group, pattern, published_only))}


@frappe.whitelist(methods=["POST"])
def try_order(draft, order):
	frappe.only_for(MANAGER_ROLES)
	policy = {**_read_draft(draft), "active": True}
	given = parse_json_arg(order, {})
	try:
		items = clean_items(given.get("items"))
	except InputError as e:
		frappe.throw(str(e))
	if sum(i["qty"] for i in items) > MAX_PACKED_UNITS:
		frappe.throw("Try at most 1,000 units at a time.")
	sample = build_order(given.get("country_code"), items)
	result = resolve(policy, sample)
	if result.get("needs_quotes"):
		quoted = {}
		city = (given.get("city") or "").strip()
		if not city:
			frappe.throw("Enter the destination city to ask the providers for live prices.")
		for s in result["shipments"]:
			if not s["quote"]:
				continue
			boxes = _packed_cartons(s["items"])
			quotes = (
				_carrier_quotes(sample["country_code"], city, boxes, given.get("postcode", ""))
				if boxes
				else []
			)
			quoted[str(s["index"])] = {"boxes": clean_boxes(boxes), "quotes": quotes}
		sample["shipment_quotes"] = quoted
		result = resolve(policy, sample)
		result["preview_note"] = (
			(
				"Simulated provider prices — sandbox carrier simulation is enabled. "
				if frappe.conf.get("shipping_mock_mode")
				else "Live provider quotes. "
			)
			+ city
			+ "."
		)
	if result.get("reason") == "not_covered":
		result["message"] = preview_coverage_message(policy, sample)
	return {"result": result}


@frappe.whitelist(methods=["POST"])
def test_integration(order=None):
	frappe.only_for(MANAGER_ROLES)
	sample = _integration_sample(order)
	erp = _run_check("erp_price_api", _check_erp, sample)
	carrier = _run_check("carrier", _check_carrier, sample)
	website = _run_check("website", _check_website, sample, erp)
	return {"checks": [erp, carrier, website], "sample": sample}


def _check_erp(sample):
	args = {"country_code": sample["country_code"], "items": json.dumps(sample["items"])}
	status, ms, body, problem = _post_json(get_url(allow_header_override=False) + PRICE_API, args)
	own = price_delivery(**args)
	return shipping_integration_check.summarize_policy_response(
		status, ms, body, _first_amount(own), problem=problem
	)


def _country_list() -> list[dict]:
	"""[{"code", "name"}] for every Country with a two-letter code, by name."""
	rows = frappe.get_all("Country", fields=["name", "code"], order_by="name asc")
	return [{"code": code, "name": row["name"]} for row in rows if (code := normalize_country(row["code"]))]


def _country_names() -> dict[str, str]:
	"""{ISO code: Country name}."""
	return {country["code"]: country["name"] for country in _country_list()}


def _rows_and_loaded(policy, countries) -> tuple[dict, dict]:
	"""The rows the draft is written as, and the policy `load_policy` will read back from them (zones and
	item sets in the draft's order)."""
	rows = rows_from_policy(policy, countries)
	codes = {name: code for code, name in countries.items()}
	return rows, policy_from_rows(rows["policy"], rows["zones"], rows["item_sets"], codes)


def _as_loaded(policy, countries) -> dict:
	return _rows_and_loaded(policy, countries)[1]


def _packed_cartons(items) -> list[dict]:
	"""The cartons dsi_shipping packs the items into, the same cartons a real order is rated and booked in
	(its packer reads each item's weight per unit and package size, with its own fallbacks). Each carton is
	{"weight_kg": its gross weight, "volume_l", "size": {"box_code", "length_cm", "width_cm", "height_cm"}}.
	No cartons without items, or on a site without the packer.
	"""
	if not items:
		return []
	try:
		from dsi_shipping.packing import pack_delivery_note
	except ImportError:
		return []
	if sum(item["qty"] for item in items) > MAX_PACKED_UNITS:
		frappe.throw(
			_("Try an order packs at most {0} units into cartons. Reduce the quantities.").format(
				MAX_PACKED_UNITS
			)
		)
	note = frappe._dict(items=[{"item_code": item["item_code"], "qty": item["qty"]} for item in items])
	cartons = []
	for box in pack_delivery_note(note).get("boxes") or []:
		size = box.get("dimensions") or {}
		length, width, height = (flt(size.get(side)) for side in ("length_cm", "width_cm", "height_cm"))
		cartons.append(
			{
				"weight_kg": flt(box.get("gross_weight_kg")),
				"volume_l": length * width * height / 1000,
				"size": {
					"box_code": box.get("box_code"),
					"length_cm": length,
					"width_cm": width,
					"height_cm": height,
				},
			}
		)
	if len(cartons) > MAX_BOXES:
		frappe.throw(_("The items fill more than {0} cartons. Reduce the quantities.").format(MAX_BOXES))
	return cartons


class _CarrierFailed(Exception):
	"""The carrier's answer is not a list of prices."""


def _carrier_quotes(country, city, cartons, postcode="") -> list[dict]:
	"""The carrier's quotes for the cartons, asked one carton at a time as the website asks: each service's
	prices added up, with the number of cartons it quoted, cheapest first. When the carrier fails in any way
	(an error, an answer that says it failed, or one that is not a list of prices) there are no quotes at
	all, never some of them. The failure is noted, with its traceback, in the shipping_policy log file (not
	the Error Log, and never on the page), so that a change in the carrier's call does not go unseen."""
	code = normalize_country(country)
	if not code:
		frappe.throw(_("Asking the carrier needs a country."))
	place = city.strip() if isinstance(city, str) else ""
	if not place:
		frappe.throw(_("Asking the carrier needs a city."))
	if len(place) > MAX_TEXT or not place.isprintable():
		frappe.throw(_("The city has to be plain text of at most {0} characters.").format(MAX_TEXT))
	if not any(carton["weight_kg"] > 0 for carton in cartons):
		frappe.throw(_("Asking the carrier needs a carton that weighs more than 0 kg."))
	try:
		from dsi_shipping.api import get_shipping_rates
	except ImportError:
		return []
	shown = len(frappe.local.message_log)
	try:
		return _quoted(get_shipping_rates, code, place, cartons, postcode)
	except Exception:
		del frappe.local.message_log[shown:]  # the carrier's own message (a SOAP fault, say) is not passed on
		# At error level: outside a development server Frappe's loggers keep nothing below it.
		frappe.logger("shipping_policy").error(
			"Try an order could not use the carrier's answer, so it priced the order with no quotes.",
			exc_info=True,
		)
		return []


def _quoted(get_shipping_rates, code, city, cartons, postcode="") -> list[dict]:
	totals = {}
	for carton in cartons:
		reply = get_shipping_rates(
			country_code=code,
			city=city,
			postcode=postcode,
			weight_kg=carton["weight_kg"],
			dimensions=json.dumps(carton["size"]),
			cod_amount=0,
			currency="USD",
		)
		rates = reply.get("rates") if isinstance(reply, dict) and reply.get("success") else None
		if not isinstance(rates, list):
			raise _CarrierFailed
		quoted = set()
		for rate in rates:
			service_id = rate.get("service_id") if isinstance(rate, dict) else None
			cost = rate.get("cost") if isinstance(rate, dict) else None
			if not isinstance(service_id, str) or service_id in quoted or not _is_number(cost) or cost < 0:
				raise _CarrierFailed
			quoted.add(service_id)
			name = rate.get("service_name") or service_id
			total = totals.setdefault(
				service_id, {"service_id": service_id, "name": name, "amount": 0.0, "boxes_quoted": 0}
			)
			total["amount"] += cost
			total["boxes_quoted"] += 1
	# Added up to three decimals, as the website adds them, then cleaned like any quote.
	quotes = [{**total, "amount": flt(total["amount"], 3)} for total in totals.values()]
	return clean_quotes(sorted(quotes, key=lambda quote: quote["amount"]))


def _upsert(doctype, row, entry, saved):
	"""Insert the zone or item set `row`, or update the saved one of the same name. One that would not change
	is left alone. A zone is saved with the flag that leaves its checks across zones to the draft's check."""
	name = entry["name"]
	if name in saved and saved[name] == entry and doctype != ITEM_SET:
		return
	if name in saved:
		doc = frappe.get_doc(doctype, name)
		doc.update(_keeping_row_names(doc, row))
		write = doc.save
	else:
		doc = frappe.get_doc({"doctype": doctype, **row})
		write = doc.insert
	if doctype == ZONE:
		doc.flags.policy_draft_validated = True
	write()


def _keeping_row_names(doc, row) -> dict:
	"""`row` with each child row given the name of a saved row of `doc` that holds the same value, so that the
	document's history shows what changed, not every row removed and added again. Only names of rows the
	document has now are used: Frappe takes a named row for one to update."""
	row = dict(row)
	for table, field in _CHILD_TABLES[doc.doctype].items():
		free = {}
		for child in doc.get(table):
			free.setdefault(child.get(field), []).append(child.name)
		row[table] = [
			{**child, "name": free[child[field]].pop(0)} if free.get(child[field]) else child
			for child in row[table]
		]
	return row


def _switch_off(name):
	doc = frappe.get_doc(ZONE, name)
	if doc.enabled:
		doc.enabled = 0
		doc.flags.policy_draft_validated = True
		doc.save()


def _is_blank(value) -> bool:
	"""No file given: None, or text of nothing but white space (what a page sends for a file it was not given)."""
	return value is None or (isinstance(value, str) and not value.strip())


def _website_address() -> str:
	"""The address of the website as Storefront Settings keeps it ("Current Website"), exactly as stored. It is
	"" when there is none, or when this site has no such settings."""
	try:
		if not frappe.db.exists("DocType", "Storefront Settings"):
			return ""
		address = frappe.get_single("Storefront Settings").active_domain
	except Exception:
		return ""
	return address if isinstance(address, str) else ""


def _website_items() -> list[dict]:
	"""The Website Items as `pick_sample_order` reads them: {"name", "item_code", "index_key", "published"}."""
	rows = frappe.get_all(
		"Website Item", fields=["name", "item_code", "custom_index_key", "published"], order_by="name asc"
	)
	return [
		{
			"name": row["name"],
			"item_code": row["item_code"],
			"index_key": row["custom_index_key"],
			"published": row["published"],
		}
		for row in rows
	]


def _known_country(value, names) -> tuple[str, str]:
	"""(ISO code, Country name) of a country typed as a code or as a name; refused if it is neither."""
	if not isinstance(value, str):
		frappe.throw(_("The country has to be text."))
	code = normalize_country(value)
	if not code:
		code = next(
			(known for known, name in names.items() if name.casefold() == value.strip().casefold()), ""
		)
		if not code:
			frappe.throw(
				_("The country has to be a country code such as GB, or the name of a country of the ERP.")
			)
	elif code not in names:
		frappe.throw(_("The country code {0} is not a country in the ERP.").format(code))
	return code, names[code]


def _integration_sample(order) -> dict:
	"""Use the order chosen on the page; no preset products, prices or destinations."""
	try:
		given = parse_json_arg(order, {})
		items = clean_items(given.get("items"))
	except (InputError, AttributeError) as error:
		frappe.throw(str(error), frappe.ValidationError)
	if not items or len(items) > MAX_SAMPLE_ITEMS:
		frappe.throw("Choose between 1 and 10 products in Try an order first.")
	code, country = _known_country(given.get("country_code") or given.get("country") or "", _country_names())
	city = given.get("city") or ""
	postcode = given.get("postcode") or ""
	if not isinstance(city, str) or not city.strip() or len(city) > MAX_TEXT or not city.isprintable():
		frappe.throw("Enter a destination city in Try an order first.")
	if not isinstance(postcode, str) or len(postcode) > MAX_TEXT:
		frappe.throw("Enter a valid postcode.")
	return {
		"country_code": code,
		"country": country,
		"city": city.strip(),
		"postcode": postcode,
		"items": items,
		"note": "",
	}


def _milliseconds(started) -> int:
	return int((time.monotonic() - started) * 1000)


def _no_credentials(request):
	"""An `auth` that adds nothing to the request. Giving `requests` one stops it from looking for a .netrc entry
	for the host, which would send somebody's credentials to an address it was never meant for."""
	return request


def _not_a_number(constant):
	raise ValueError(constant)


def _is_a_read_timeout(error) -> bool:
	"""Is this error the answer having stopped coming? Reading the body of an answer that stalls for longer than
	the timeout, `requests` re-raises urllib3's ReadTimeoutError as a ConnectionError that holds it as its first
	argument (not a Timeout), so the error, what it holds and what it was raised from or while handling are
	looked at, a few steps deep, for a timeout; failing that, whether it says so in words."""
	seen, pending = [], [error]
	while pending and len(seen) < 8:
		current = pending.pop()
		if any(current is known for known in seen):
			continue
		seen.append(current)
		if isinstance(current, urllib3.exceptions.TimeoutError | TimeoutError):
			return True
		pending.extend(
			argument for argument in getattr(current, "args", ()) if isinstance(argument, BaseException)
		)
		pending.extend(link for link in (current.__cause__, current.__context__) if link is not None)
	return "read timed out" in str(error).casefold()


def _read_capped(response, deadline) -> tuple[bytes | None, str | None]:
	"""(the body, problem): at most MAX_BODY_BYTES of it, read before `deadline`. A body over the cap is
	"too_large", one not finished in time or that stalls for longer than the timeout "timeout", one that breaks
	off "unreadable".

	It is read a byte at a time. The timeout of a call only limits each wait for the server, so a server that
	sends a byte every 14 seconds would keep the call for as long as it liked; this way the deadline is looked
	at between the bytes. (A server that sends its headers that slowly still can: that is `requests`.)
	"""
	chunks, size = [], 0
	try:
		for chunk in response.iter_content(chunk_size=1):
			size += len(chunk)
			if size > MAX_BODY_BYTES:
				return None, "too_large"
			chunks.append(chunk)
			if time.monotonic() > deadline:
				return None, "timeout"
	except requests.exceptions.Timeout:
		return None, "timeout"
	except requests.exceptions.RequestException as error:
		return None, "timeout" if _is_a_read_timeout(error) else "unreadable"
	return b"".join(chunks), None


def _post_json(url, payload) -> tuple[int | None, int, object, str | None]:
	"""POST `payload` as JSON to `url` and read the answer: (status, milliseconds, body, problem).

	The call has no cookie and no credential, only a content type for header, follows no redirect, and has
	HTTP_TIMEOUT_SECONDS in all. `body` is the answer parsed as JSON, or None. `problem` says why there is no
	answer to read: "timeout", "unreachable" (no answer at all), "too_large", or "unreadable" (a status 200
	that is not JSON, or an answer that breaks off); any other status without JSON has none, the status says
	what there is to say. The answer is closed as soon as it is read.
	"""
	started = time.monotonic()
	response, status, body, problem = None, None, None, None
	try:
		response = requests.post(
			url,
			data=json.dumps(payload),
			headers={"Content-Type": "application/json"},
			timeout=HTTP_TIMEOUT_SECONDS,
			allow_redirects=False,
			stream=True,
			auth=_no_credentials,
		)
		status = response.status_code
		raw, problem = _read_capped(response, started + HTTP_TIMEOUT_SECONDS)
		if problem is None:
			try:
				body = json.loads(raw.decode("utf-8"), parse_constant=_not_a_number)
			except (ValueError, RecursionError):
				# not UTF-8, not JSON, an integer of over 4,300 digits, or nested too deep
				problem = "unreadable" if status == 200 else None
	except requests.exceptions.Timeout:
		problem = "timeout"
	except requests.exceptions.RequestException:
		problem = "unreachable"
	finally:
		if response is not None:
			response.close()
	return status, _milliseconds(started), body, problem


def _first_amount(answer):
	"""The first offer's amount in an answer of `price_delivery`, or None when it offers nothing."""
	offers = answer.get("offers") or []
	return offers[0].get("amount") if offers else None


def _check_carrier(sample) -> dict:
	"""The second check: the shipping module asked in this process for one sample parcel. Its messages are not
	passed on, and nor is the text of any error it raises."""
	started, shown = time.monotonic(), len(frappe.local.message_log)
	try:
		from dsi_shipping.api import get_shipping_rates

		reply = get_shipping_rates(
			country_code=sample["country_code"],
			city=sample["city"],
			postcode=sample.get("postcode", ""),
			weight_kg=SAMPLE_PARCEL["weight_kg"],
			dimensions=json.dumps(SAMPLE_PARCEL["box"]),
			cod_amount=0,
			currency="USD",
		)
	except Exception as error:
		reply = error
	finally:
		del frappe.local.message_log[shown:]
	return shipping_integration_check.summarize_carrier_response(reply, ms=_milliseconds(started))


def _origin(address) -> str:
	"""The address the guard let through, written the one way any parser reads it: scheme, host and port."""
	parts = urlsplit(address.strip())
	port = f":{parts.port}" if parts.port else ""
	return f"{parts.scheme.lower()}://{(parts.hostname or '').rstrip('.')}{port}"


def _check_website(sample, erp) -> dict:
	"""The third check: the sample cart posted to the rates route of the website in Storefront Settings, when
	that address passes the guard, and its first amount compared with the one the ERP's check found."""
	address = _website_address()
	allowed, reason = shipping_integration_check.check_website_url(address, resolver=_resolve)
	if not allowed:
		return shipping_integration_check.website_not_asked(reason)
	payload = {
		"cartItems": sample["items"],
		"shippingAddress": {
			"city": sample["city"],
			"postcode": sample.get("postcode", ""),
			"country": sample["country"],
		},
		"currency": "USD",
	}
	status, ms, body, problem = _post_json(_origin(address) + RATES_ROUTE, payload)
	expected = shipping_integration_check.policy_amount(erp)
	return shipping_integration_check.summarize_website_response(status, ms, body, expected, problem=problem)


def _run_check(name, check, *arguments) -> dict:
	"""One check; an unexpected error in it is written to the Error Log and reported without its text (nor is
	any message it left for the desk passed on)."""
	started, shown = time.monotonic(), len(frappe.local.message_log)
	try:
		return check(*arguments)
	except Exception:
		del frappe.local.message_log[shown:]
		try:
			frappe.log_error(title="shipping policy integration test failed", message=frappe.get_traceback())
		except Exception:  # the Error Log failing does not fail the answer
			pass
		return shipping_integration_check.unexpected_failure(name, _milliseconds(started))


@frappe.whitelist()
def check_formula(source):
	frappe.only_for(MANAGER_ROLES)
	from dsi_catalogue.shipping_formula import FormulaError
	from dsi_catalogue.shipping_formula import check_formula as check

	try:
		check(source)
		return {"ok": True, "error": None}
	except FormulaError as e:
		return {"ok": False, "error": str(e)}
