"""Authored product translations for Website Item.

MODEL
English is the canonical copy and lives ONLY in the Website Item's own fields.
Every other language lives in the `custom_translations` child table (one row per
language, doctype `Website Item Translated Copy`). The desk form shows one set of
fields and swaps the displayed language via a Client Script; this module is the
only sanctioned way those translations are written.

THE INVARIANT
A write must never leave a translated string in a canonical English column.
Three things enforce it, in increasing order of authority:

  1. the Client Script reconciles before it lets a save through;
  2. `website_item_events.i18n_guard` (before_validate) restores English and
     reroutes any translated text that leaks past the client;
  3. `put_copy` below never loads or saves the parent at all - it writes the
     child row with frappe.db.set_value, so the canonical columns are not in the
     UPDATE statement. That is the strongest guarantee available, because it does
     not depend on any hook running.

Prefer (3). The other two exist because the desk form can be driven by a browser
we do not control.
"""

import hashlib
import json

import frappe
from frappe import _

# The English columns this feature is responsible for protecting. One definition,
# imported by the guard, so the two can never drift apart.
CANONICAL_FIELDS = (
	"web_item_name",
	"website_content",
	"website_image_alt",
	"short_description",
	"web_long_description",
	"custom_seo_title",
	"custom_seo_description",
	"custom_seo_keywords",
)

# Mirrors on the child row. Same order; `custom_seo_*` drops its prefix there
# because the child doctype has no naming collision to avoid.
TRANSLATED_FIELDS = {
	"web_item_name": "web_item_name",
	"website_content": "website_content",
	"website_image_alt": "website_image_alt",
	"short_description": "short_description",
	"web_long_description": "web_long_description",
	"custom_seo_title": "seo_title",
	"custom_seo_description": "seo_description",
	"custom_seo_keywords": "seo_keywords",
}

TRANSLATABLE_LANGUAGES = ("ar", "es", "fr", "ru")
PARENTFIELD = "custom_translations"
CHILD_DOCTYPE = "Website Item Translated Copy"


# ---------------------------------------------------------------- fingerprints


def spec_rows(doc):
	"""Canonical specification rows as plain dicts, in display order."""
	out = []
	for r in doc.get("website_specifications") or []:
		out.append(
			{
				"name": r.get("name"),
				"idx": int(r.get("idx") or 0),
				"label": r.get("label") or "",
				"description": r.get("description") or "",
			}
		)
	out.sort(key=lambda r: r["idx"])
	return out


def english_bundle(doc):
	"""Everything this feature protects, as a serialisable dict."""
	bundle = {f: (doc.get(f) or "") for f in CANONICAL_FIELDS}
	# row `name`s are kept so the guard can restore rows by identity rather than
	# deleting and reinserting them (which would churn idx and bust caches).
	bundle["specifications"] = spec_rows(doc)
	return bundle


def fingerprint(bundle):
	"""Stable hash of an English bundle. Excludes row names - identity is not content."""
	payload = {k: v for k, v in bundle.items() if k != "specifications"}
	payload["specifications"] = [
		{"idx": r["idx"], "label": r["label"], "description": r["description"]}
		for r in bundle.get("specifications") or []
	]
	blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
	return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def stamp_snapshot(doc):
	"""Record the English that is about to be committed, for later recovery."""
	bundle = english_bundle(doc)
	doc.custom_en_snapshot = json.dumps(bundle, ensure_ascii=False)
	doc.custom_en_fingerprint = fingerprint(bundle)


# ------------------------------------------------------------------- row access


def _row_name(website_item, language):
	rows = frappe.get_all(
		CHILD_DOCTYPE,
		filters={
			"parent": website_item,
			"parenttype": "Website Item",
			"parentfield": PARENTFIELD,
			"language": language,
		},
		fields=["name", "modified"],
		order_by="idx asc",
	)
	# Child tables cannot carry a unique constraint, so duplicates are possible.
	# Last row wins, consistently with the storefront read.
	return rows[-1] if rows else None


@frappe.whitelist()
def get_copy(website_item, language):
	"""Read one language's authored copy, plus the freshness context the editor needs."""
	frappe.has_permission("Website Item", "read", doc=website_item, throw=True)
	if language not in TRANSLATABLE_LANGUAGES:
		frappe.throw(_("Unsupported language {0}").format(language))

	parent_fp = frappe.db.get_value("Website Item", website_item, "custom_en_fingerprint")
	row = _row_name(website_item, language)
	if not row:
		return {"exists": False, "parent_fingerprint": parent_fp}

	doc = frappe.get_doc(CHILD_DOCTYPE, row["name"])
	out = {mirror: (doc.get(mirror) or "") for mirror in TRANSLATED_FIELDS.values()}
	out.update(
		{
			"exists": True,
			"row_name": doc.name,
			"row_modified": str(doc.modified),
			"review_status": doc.review_status,
			"is_stale": int(doc.is_stale or 0),
			"base_en_fingerprint": doc.base_en_fingerprint or "",
			"parent_fingerprint": parent_fp,
			"specifications": _load_specs(doc.specifications_json),
		}
	)
	return out


def _load_specs(blob):
	"""Never let a malformed blob take down a page - degrade to no specs."""
	if not blob:
		return []
	try:
		data = json.loads(blob)
		return data if isinstance(data, list) else []
	except Exception:
		frappe.log_error(f"bad specifications_json: {blob[:200]}", "dsi_catalogue i18n")
		return []


@frappe.whitelist()
def put_copy(
	website_item, language, payload, base_en_fingerprint=None, row_modified=None, skip_revalidate=False
):
	"""Write one language's authored copy.

	Deliberately does NOT do frappe.get_doc('Website Item').save(): the canonical
	English columns are never part of the UPDATE, so this path cannot damage them
	even if every hook in the app were removed. It also avoids re-running
	publish_gate (which would silently unpublish a Draft item) and a redundant
	variant_content_sync sibling scan.
	"""
	frappe.has_permission("Website Item", "write", doc=website_item, throw=True)
	if language not in TRANSLATABLE_LANGUAGES:
		frappe.throw(_("Unsupported language {0}").format(language))

	data = json.loads(payload) if isinstance(payload, str) else (payload or {})
	parent_fp = frappe.db.get_value("Website Item", website_item, "custom_en_fingerprint") or ""

	values = {mirror: (data.get(mirror) or "") for mirror in TRANSLATED_FIELDS.values()}
	values["specifications_json"] = json.dumps(data.get("specifications") or [], ensure_ascii=False)
	values["base_en_fingerprint"] = parent_fp
	# If the editor authored against an older English, say so rather than pretend.
	values["is_stale"] = 0 if (base_en_fingerprint or parent_fp) == parent_fp else 1
	if data.get("review_status"):
		values["review_status"] = data["review_status"]

	existing = _row_name(website_item, language)
	if existing:
		# Optimistic lock: refuse to silently overwrite a concurrent edit.
		if row_modified and str(existing["modified"]) != str(row_modified):
			frappe.throw(
				_("This translation was changed in another tab or by another user. Reload before saving."),
				title=_("Translation is out of date"),
			)
		frappe.db.set_value(CHILD_DOCTYPE, existing["name"], values, update_modified=True)
		row_name = existing["name"]
	else:
		idx = frappe.db.count(CHILD_DOCTYPE, {"parent": website_item, "parentfield": PARENTFIELD}) + 1
		row = frappe.get_doc(
			{
				"doctype": CHILD_DOCTYPE,
				"parent": website_item,
				"parenttype": "Website Item",
				"parentfield": PARENTFIELD,
				"idx": idx,
				"language": language,
				**values,
			}
		)
		row.insert(ignore_permissions=True)
		row_name = row.name

	# Touch the parent so the storefront's ISR/tag invalidation has something to
	# see, without running the parent's save hooks.
	frappe.db.set_value("Website Item", website_item, "modified", frappe.utils.now(), update_modified=False)
	frappe.db.commit()

	if not skip_revalidate:
		_notify_revalidate(website_item)
	return {
		"ok": True,
		"row_name": row_name,
		"row_modified": str(frappe.db.get_value(CHILD_DOCTYPE, row_name, "modified")),
	}


def _notify_revalidate(website_item):
	"""Best-effort storefront cache purge; never fail the write because of it."""
	try:
		from dsi_catalogue.api import notify_revalidate

		notify_revalidate(frappe.get_doc("Website Item", website_item))
	except Exception:
		frappe.log_error(frappe.get_traceback(), "dsi_catalogue i18n notify_revalidate")


# --------------------------------------------------------------------- recovery


@frappe.whitelist()
def restore_english(website_item):
	"""Put the last committed English back, from custom_en_snapshot.

	The break-glass path for when something bypassed every guard. Callable as:
	  bench --site erp.shaik.net execute dsi_catalogue.i18n.restore_english --args "['WEB-ITM-0012']"
	"""
	frappe.only_for("System Manager")
	blob = frappe.db.get_value("Website Item", website_item, "custom_en_snapshot")
	if not blob:
		frappe.throw(_("No English snapshot stored for {0}").format(website_item))
	bundle = json.loads(blob)

	doc = frappe.get_doc("Website Item", website_item)
	for f in CANONICAL_FIELDS:
		doc.set(f, bundle.get(f) or "")
	for row in bundle.get("specifications") or []:
		if row.get("name") and frappe.db.exists("Item Website Specification", row["name"]):
			frappe.db.set_value(
				"Item Website Specification",
				row["name"],
				{"label": row.get("label") or "", "description": row.get("description") or ""},
				update_modified=False,
			)
	doc.flags.dsi_i18n_write = True
	frappe.flags.dsi_i18n_write = True
	try:
		doc.save(ignore_permissions=True)
		frappe.db.commit()
	finally:
		frappe.flags.dsi_i18n_write = False
	return {"ok": True, "restored": website_item}
