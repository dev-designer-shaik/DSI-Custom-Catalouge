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

def only_editor():
    if frappe.session.user != EDITOR and not frappe.flags.get('dsi_storefront_bootstrap'):
        frappe.throw('Only Carla can edit or publish website content.', frappe.PermissionError)

def has_permission(doc, user=None, permission_type=None):
    if permission_type in ('write', 'create', 'delete', 'submit', 'cancel', 'amend') and (user or frappe.session.user) != EDITOR:
        return False
    return None

def guard(doc, method=None):
    only_editor()

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
    if previous and content(doc) != previous:
        frappe.throw('The Website Item changed. Refresh this review before publishing.')
    previous_flag = frappe.flags.get('dsi_storefront_approving')
    frappe.flags.dsi_storefront_approving = True
    try:
        for field, value in candidate.items():
            if field in CONTENT_FIELDS and doc.meta.has_field(field):
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
    only_editor()
    rows = frappe.get_all('Storefront Review',filters={'kind':'Website Item','website_item':website_item},fields=['name','modified','status','draft_json'],limit_page_length=1)
    return rows[0] if rows else None
