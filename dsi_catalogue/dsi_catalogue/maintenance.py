"""Maintenance entry points — one-off, bench-executable operations that do not
belong in the request-facing api module. Run e.g.:
bench --site <site> execute dsi_catalogue.maintenance.backfill_precompute_fields
"""
import frappe


@frappe.whitelist()
def backfill_precompute_fields(limit=None):
    """One-off: populate the precompute fields on all published Website Items.
    Uses db.set_value (no full save) for speed; run via bench execute."""
    from dsi_catalogue import index_key as ik
    names = frappe.get_all("Website Item", filters={"published": 1}, pluck="name")
    if limit:
        names = names[: int(limit)]
    updated = 0
    for nm in names:
        key = frappe.db.get_value("Website Item", nm, "custom_index_key")
        if not key:
            continue
        fields = ik.decode_for_website_item(key)
        if fields:
            frappe.db.set_value("Website Item", nm, fields, update_modified=False)
            updated += 1
    frappe.db.commit()
    frappe.cache().delete_value("dsi_shop_filters")
    return {"updated": updated, "total": len(names)}


