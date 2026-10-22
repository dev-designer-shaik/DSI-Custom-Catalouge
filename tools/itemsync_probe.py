"""Always rolled back. Proves who can save an Item that has a Website Item, and what reaches the Website Item."""
import os, frappe
SITE = os.environ.get("SITE", "erp.shaik.net")
frappe.init(site=SITE); frappe.connect(); frappe.set_user("Administrator")
ITEM = "10000012-40ML"
WEB = frappe.db.get_value("Website Item", {"item_code": ITEM}, "name")
NEWDESC = "OPULENT SHAIK NO.33 CLASSIC PARFUM WOMEN (40ML)"
q = lambda s: frappe.db.sql(s, as_list=True)
res = []
def check(label, ok, extra=""):
    res.append(ok); print(("PASS " if ok else "FAIL ") + label, extra, flush=True)
def state():
    return frappe.db.get_value("Website Item", WEB, ["description", "item_name", "published", "web_long_description", "modified_by"], as_dict=True)
def reviews(): return frappe.db.count("Storefront Review")
def as_user(u, fn):
    frappe.set_user(u)
    try: return fn()
    finally: frappe.set_user("Administrator")
base = state(); nrev = reviews()
print("Website Item", WEB, "published", base.published, "| item disabled:", frappe.db.get_value("Item", ITEM, "disabled"), "| old description:", repr(frappe.db.get_value("Item", ITEM, "description")))

for user in ("karthika@designershaik.com", "carla@designershaik.com", "Administrator"):
    # 1. description edit
    def edit():
        d = frappe.get_doc("Item", ITEM); d.description = NEWDESC; d.save()
    try:
        as_user(user, edit); s = state()
        check(f"{user}: Item description save", s.description == NEWDESC and s.published == base.published and reviews() == nrev,
              f"web.description={s.description!r} published={s.published} drafts+{reviews()-nrev}")
        check(f"{user}: window closed after save", frappe.flags.get("dsi_item_sync") is None)
    except Exception as e: check(f"{user}: Item description save", False, f"{type(e).__name__}: {str(e)[:200]}")
    frappe.db.rollback()
    # 2. disable then re-enable
    try:
        def dis():
            d = frappe.get_doc("Item", ITEM); d.disabled = 1; d.save()
        as_user(user, dis); s = state()
        check(f"{user}: disabling unpublishes at once, no draft", s.published == 0 and reviews() == nrev, f"published={s.published}")
        def en():
            d = frappe.get_doc("Item", ITEM); d.disabled = 0; d.save()
        as_user(user, en); s = state()
        check(f"{user}: re-enabling does not republish", s.published == 0 and reviews() == nrev, f"published={s.published}")
    except Exception as e: check(f"{user}: disable/enable", False, f"{type(e).__name__}: {str(e)[:200]}")
    frappe.db.rollback()

# 3. direct Website Item edits by a non-editor still refused (outside the window)
for field, val in (("web_long_description", "hacked"), ("description", "direct")):
    def direct():
        w = frappe.get_doc("Website Item", WEB); w.set(field, val); w.save()
    try: as_user("karthika@designershaik.com", direct); check(f"karthika direct edit of Website Item.{field} refused", False, "SAVED")
    except frappe.PermissionError as e: check(f"karthika direct edit of Website Item.{field} refused", True, str(e)[:70])
    except Exception as e: check(f"karthika direct edit of Website Item.{field} refused", False, f"{type(e).__name__}: {str(e)[:120]}")
    frappe.db.rollback()
# 4. item without a Website Item still saves for karthika
other = frappe.db.sql("select name from `tabItem` i where disabled=0 and not exists(select 1 from `tabWebsite Item` w where w.item_code=i.name) limit 1")[0][0]
def other_edit():
    d = frappe.get_doc("Item", other); d.description = (d.description or "") + " "; d.save()
try: as_user("karthika@designershaik.com", other_edit); check(f"karthika saves {other} (no Website Item)", True)
except Exception as e: check(f"karthika saves {other} (no Website Item)", False, f"{type(e).__name__}: {str(e)[:150]}")
frappe.db.rollback()
after = state()
check("nothing committed: Website Item unchanged", after == base)
print("ALL PASS" if all(res) else "SOME FAILED", f"({sum(res)}/{len(res)})")
