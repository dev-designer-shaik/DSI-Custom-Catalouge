"""Carla owns website edits. Saving a draft does not replace the live item."""
import json
import frappe

EDITOR = 'carla@designershaik.com'
CONTENT_FIELDS = (
    'web_item_name', 'route', 'published', 'website_content', 'website_image',
    'website_image_alt', 'thumbnail', 'short_description', 'web_long_description',
    'custom_show_website_description', 'custom_index_key', 'custom_repository_path',
    'custom_seo_title', 'custom_seo_description', 'custom_seo_keywords',
    'custom_user_guide', 'custom_swatch_image', 'slideshow', 'show_tabbed_section',
    'ranking', 'website_specifications', 'tabs', 'recommended_items', 'offers',
    'website_item_groups', 'custom_gallery_images', 'custom_translations',
)

# Roles that may READ a pending review. Publishing stays with the editor (only_editor).
REVIEW_READERS = {'Storefront Reviewer', 'Website Manager', 'System Manager'}

def only_reviewer_readers():
    if not set(frappe.get_roles()) & REVIEW_READERS:
        frappe.throw('Website review access required', frappe.PermissionError)

def changed_fields(candidate, previous):
    """The content fields this draft actually changes, against what it was written from.

    Approval used to compare the WHOLE item with the snapshot and then overwrite every
    field with the draft. Translation rows are touched in the background (is_stale
    flags, fingerprints), so an untouched translation made any draft unapprovable
    ("The Website Item changed"), and approving would have overwritten concurrent edits
    to fields the draft never meant to change.
    """
    return {f: v for f, v in candidate.items() if f in CONTENT_FIELDS and v != (previous or {}).get(f)}

def only_editor():
    if frappe.session.user != EDITOR and not frappe.flags.get('dsi_storefront_bootstrap'):
        frappe.throw('Only Carla can edit or publish website content.', frappe.PermissionError)

# --- Item -> Website Item copy -------------------------------------------------------------------
# webshop's Item on_update hook copies these Item fields onto the item's Website Item and saves it AS
# WHOEVER SAVED THE ITEM.  With "only Carla writes a Website Item" that made every other user's Item edit
# fail ("You need the 'write' permission on Website Item") on all items that have a Website Item.
# Item.validate opens a window naming the ONE Item being saved; inside it the copy may write the fields
# below to THAT item's Website Item, and nothing else (guard() refuses anything more).  Published can
# only go DOWN: disabling an item unpublishes it at once, enabling it never republishes -- that stays
# Carla's review.
ITEM_SYNC_FIELDS = frozenset(('item_name', 'item_group', 'stock_uom', 'brand', 'description'))
_STD_FIELDS = frozenset(('name', 'owner', 'creation', 'modified', 'modified_by', 'docstatus', 'idx',
                         'parent', 'parentfield', 'parenttype', 'doctype'))

def item_sync_begin(doc, method=None):
    """Item validate: webshop is about to copy this Item onto its Website Item."""
    if not doc.is_new() and frappe.db.exists('Website Item', {'item_code': doc.name}):
        frappe.flags.dsi_item_sync = doc.name

def item_sync_end(doc, method=None):
    """Item on_change (after every on_update): close the window."""
    frappe.flags.pop('dsi_item_sync', None)

def item_sync_active(web_item):
    """True while the Item that owns this Website Item is the one being saved."""
    code = web_item.get('item_code') if hasattr(web_item, 'get') else getattr(web_item, 'item_code', None)
    return bool(code) and frappe.flags.get('dsi_item_sync') == code

def _row_values(row):
    return {k: v for k, v in row.as_dict().items() if k not in _STD_FIELDS and not k.startswith('_')}

def limit_to_item_sync(doc):
    """Inside the Item window a non-editor may change ONLY ITEM_SYNC_FIELDS (+ unpublish)."""
    before = doc.get_doc_before_save()
    if before is None:  # a new Website Item is never part of the copy
        only_editor()
        return
    skip = set(frappe.model.no_value_fields)
    changed = set()
    for df in doc.meta.fields:
        if df.fieldname in _STD_FIELDS or df.fieldtype in skip:
            continue
        if df.fieldtype in frappe.model.table_fields:
            if [_row_values(r) for r in doc.get(df.fieldname) or []] != [_row_values(r) for r in before.get(df.fieldname) or []]:
                changed.add(df.fieldname)
        elif (doc.get(df.fieldname) or '') != (before.get(df.fieldname) or ''):
            changed.add(df.fieldname)
    if doc.get('published') and not before.get('published'):
        doc.published = before.get('published')  # enabling an item never republishes it
        changed.discard('published')
    extra = changed - ITEM_SYNC_FIELDS - {'published'}
    if extra:
        frappe.throw('An Item save may change only %s on its Website Item; it tried to change %s. Only Carla edits website content.'
                     % (', '.join(sorted(ITEM_SYNC_FIELDS | {'published'})), ', '.join(sorted(extra))), frappe.PermissionError)

def has_permission(doc, user=None, permission_type=None, ptype=None):
    pt = ptype or permission_type
    if pt in ('write', 'create', 'delete', 'submit', 'cancel', 'amend') and (user or frappe.session.user) != EDITOR:
        # Only the Item->Website Item copy of the item being saved, and only a plain write; guard() limits what it may change.
        return bool(pt == 'write' and getattr(doc, 'doctype', None) == 'Website Item' and item_sync_active(doc))
    # Frappe controller hooks can only deny; returning None also denies reads.
    # True preserves the role permissions that Frappe has already checked.
    return True

def guard(doc, method=None):
    if method == 'before_validate' and doc.doctype == 'Website Item' and item_sync_active(doc):
        return limit_to_item_sync(doc)
    only_editor()

def guard_child(doc, method=None):
    if doc.get('parenttype') != 'Website Item' or not doc.meta.istable:
        return
    only_editor()
    if not frappe.flags.get('dsi_storefront_bootstrap') and not frappe.flags.get('dsi_storefront_approving'):
        frappe.throw('Edit this content through its Website Item so it is saved for approval.')

def content(doc):
    from dsi_catalogue.storefront_review import payload
    result = {}
    for field in CONTENT_FIELDS:
        if not doc.meta.has_field(field):
            continue
        value = doc.get(field)
        if isinstance(value, list):
            value = [dict(r.as_dict()) for r in value]
            for row in value:
                for key in ('modified', 'modified_by', 'creation', 'owner'):
                    row.pop(key, None)
        result[field] = value
    return result

def stage(doc, method=None):
    if frappe.flags.get('dsi_storefront_approving') or frappe.flags.get('dsi_storefront_bootstrap'):
        return
    if item_sync_active(doc):  # the Item copy: limit_to_item_sync() has already bounded it
        return
    from dsi_catalogue.storefront_review import upsert_draft
    before = doc.get_doc_before_save()
    candidate = content(doc)
    if not before:
        candidate['published'] = 1
    previous = content(before) if before else {}
    if candidate == previous:
        return
    review = upsert_draft('Website Item', doc.name, 'en', candidate, previous, website_item=doc.name, image_url=doc.website_image or '')
    doc.custom_storefront_review_status = 'Needs Review'
    if before:
        for field in CONTENT_FIELDS:
            if doc.meta.has_field(field):
                doc.set(field, before.get(field))
        doc.workflow_state = before.get('workflow_state')
        for field in ('custom_palace_code','custom_palace_slug','custom_range_code','custom_range_slug','custom_product_code','custom_product_slug','custom_variant_slug','custom_grouping_key','custom_selectable_variant_codes','custom_sibling_gender_slug','custom_is_template'):
            doc.set(field, before.get(field))
    else:
        doc.published = 0
        doc.workflow_state = 'Draft'
    frappe.msgprint('Saved for Carla’s review. The published website copy is preserved. Review: ' + review.name, indicator='blue')

def approve_item(review, candidate):
    from dsi_catalogue.storefront_review import payload
    doc = frappe.get_doc('Website Item', review.website_item)
    previous = payload(review.source_json)
    changed = changed_fields(candidate, previous) if previous else {f: v for f, v in candidate.items() if f in CONTENT_FIELDS}
    if previous:
        live = content(doc)
        clash = [f for f in changed if live.get(f) != previous.get(f)]
        if clash:
            frappe.throw('Refresh this review before publishing. These fields changed on the Website Item after the draft was saved: ' + ', '.join(clash))
    previous_flag = frappe.flags.get('dsi_storefront_approving')
    frappe.flags.dsi_storefront_approving = True
    try:
        for field, value in changed.items():
            if doc.meta.has_field(field):
                doc.set(field, value)
        image = doc.get('website_image') or ''
        if image:
            from urllib.parse import urlparse
            parsed = urlparse(image)
            if image.startswith('/private/') or (parsed.scheme and (parsed.scheme != 'https' or parsed.username)):
                frappe.throw('Choose a public image over HTTPS or a public ERP file.')
            if parsed.scheme == 'https' and not frappe.db.exists('File', {'file_url':image,'is_private':0}):
                frappe.get_doc({'doctype':'File','file_url':image,'file_name':parsed.path.split('/')[-1] or 'website-image','is_private':0,'attached_to_doctype':'Website Item','attached_to_name':doc.name}).insert(ignore_permissions=True)
            if image != previous.get('website_image'):
                doc.thumbnail = image
        doc.workflow_state = 'Approved'
        doc.custom_storefront_review_status = 'Approved'
        doc.save(ignore_permissions=True)
    finally:
        frappe.flags.dsi_storefront_approving = previous_flag

def stage_translation(website_item, language, data):
    only_editor()
    from dsi_catalogue.i18n import get_copy
    from dsi_catalogue.storefront_review import upsert_draft, FIELDS
    existing = get_copy(website_item, language)
    draft = {k:v for k,v in data.items() if k in FIELDS['Translation']}
    review = upsert_draft('Translation', website_item, language, draft, {'existing': {k:existing.get(k) or '' for k in draft}}, website_item=website_item)
    return {'ok':True, 'review_name':review.name, 'review_status':'Needs Review', 'row_modified':existing.get('row_modified')}

@frappe.whitelist()
def get_item_review(website_item):
    only_reviewer_readers()
    rows = frappe.get_all('Storefront Review',filters={'kind':'Website Item','website_item':website_item},fields=['name','modified','status','draft_json','source_json','is_stale'],limit_page_length=1)
    return rows[0] if rows else None
