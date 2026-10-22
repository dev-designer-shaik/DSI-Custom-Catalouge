"""Set Item.description (through a normal Item save, so every hook runs) and read it back.

Run INSIDE the backend container:  SITE=<site> [DRY_ONLY=1] ../env/bin/python /tmp/set_item_description.py
Env: ITEM, DESCRIPTION (required) | DRY_ONLY=1 (save, verify, ROLL BACK) | RESTORE_FROM=<snapshot.json> (put the old value back)
Idempotent: nothing to do when the description already equals the target.
"""
import json, os, sys
import frappe

site = os.environ.get("SITE", "erp.shaik.net")
frappe.init(site=site); frappe.connect(); frappe.set_user("Administrator")
item = os.environ["ITEM"]
dry = os.environ.get("DRY_ONLY") == "1"

def web_state():
    return frappe.db.get_value("Website Item", {"item_code": item}, ["name", "description", "published", "workflow_state"], as_dict=True)

if os.environ.get("RESTORE_FROM"):
    snap = json.load(open(os.environ["RESTORE_FROM"]))
    d = frappe.get_doc("Item", item); d.description = snap["item_description"]; d.save()
    frappe.db.commit()  # nosemgrep -- the rollback path itself
    print("RESTORED", item, "->", repr(frappe.db.get_value("Item", item, "description"))); sys.exit(0)

target = os.environ["DESCRIPTION"]
old = frappe.db.get_value("Item", item, "description")
web0 = web_state()
snap = {"site": site, "item": item, "item_description": old, "website_item": dict(web0 or {}), "item_modified": str(frappe.db.get_value("Item", item, "modified"))}
path = "/tmp/set_item_description_snapshot_%s.json" % frappe.utils.now_datetime().strftime("%Y%m%d_%H%M%S")
json.dump(snap, open(path, "w"), indent=1, default=str)
print("SNAPSHOT", path, "| old description:", repr(old), "| website item:", web0)
if old == target:
    print("already set -- nothing to do"); sys.exit(0)
drafts = frappe.db.count("Storefront Review")
d = frappe.get_doc("Item", item); d.description = target; d.save()
now, web = frappe.db.get_value("Item", item, "description"), web_state()
ok = now == target and (web is None or (web.description == target and web.published == web0.published
                                         and web.workflow_state == web0.workflow_state)) and frappe.db.count("Storefront Review") == drafts
print("RESULT item.description=%r | website item=%s | published unchanged=%s | no review draft made=%s" % (
    now, (web or {}).get("description"), web is None or web.published == web0.published, frappe.db.count("Storefront Review") == drafts))
if not ok:
    frappe.db.rollback(); print("FAILED: read-back does not match -- rolled back"); sys.exit(1)
if dry:
    frappe.db.rollback(); print("DRY_ONLY: verified, rolled back"); sys.exit(0)
frappe.db.commit()  # nosemgrep -- the one real write
print("DONE | rollback: RESTORE_FROM=%s" % path)
