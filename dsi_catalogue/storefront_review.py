"""Storefront drafts, approvals and public metadata. No reviewer notes in public reads."""
import hashlib
import json
import re
from urllib.parse import urlparse

import frappe
from dsi_catalogue.website_item_review import only_editor, CONTENT_FIELDS

LOCALES = ('en', 'ar', 'fr', 'ru', 'es')
FIELDS = {
    'Website Item': set(CONTENT_FIELDS),
    'Page SEO': {'title', 'description', 'image', 'imageAlt'},
    'Image': {'alt', 'decorative'},
    'Default OG': {'image', 'imageAlt'},
    'Translation': {'web_item_name', 'website_content', 'website_image_alt', 'short_description', 'web_long_description', 'seo_title', 'seo_description', 'seo_keywords', 'care_instructions', 'item_format', 'specifications_json'},
}

def payload(value):
    return frappe.parse_json(value) if isinstance(value, str) else (value or {})

def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

def image_key(url):
    # Remove delivery transforms; an image retains its identity across viewport sizes.
    parts = urlparse('https://dsi-web-n.netlify.app' + url if url.startswith('/') else url)
    path = parts.path
    if 'res.cloudinary.com' in parts.netloc and '/upload/' in path:
        base, tail = path.split('/upload/', 1)
        tail = re.sub(r'^(?:(?:[^/]*[,=:][^/]*|f_auto|q_auto|vc_auto)/)+', '', tail)
        path = base + '/upload/' + tail
    return parts.netloc.lower() + path

def invalidate(doc=None, method=None):
    from dsi_core import outbox
    stamp = str(doc.modified) if doc else frappe.utils.now()
    name = doc.name if doc else 'storefront'
    outbox.enqueue('website-revalidate', {'tags': ['storefront', 'catalogue'], 'paths': ['/sitemap.xml', '/robots.txt', '/llm.txt', '/llms.txt'], 'layouts': ['/[locale]']}, dedup_key='storefront|' + fingerprint([name, stamp]), source=(doc.doctype, name) if doc else None)

def validate_review(doc, method=None):
    only_editor()
    if doc.kind not in FIELDS or doc.locale not in LOCALES:
        frappe.throw('Unsupported review type or language')
    draft = payload(doc.draft_json)
    if not isinstance(draft, dict) or set(draft) - FIELDS[doc.kind]:
        frappe.throw('Candidate values contain unsupported fields')
    if doc.kind != 'Website Item' and any(not isinstance(value, str) for key, value in draft.items() if key != 'decorative'):
        frappe.throw('Candidate text and image URLs must be text')
    if 'decorative' in draft and not isinstance(draft['decorative'], (bool, int)):
        frappe.throw('Decorative must be a checkbox value')
    if doc.kind == 'Translation' and (doc.locale == 'en' or not doc.website_item):
        frappe.throw('Translations require a Website Item and a non-English language')
    for key in ('image',):
        value = draft.get(key)
        if value and not ((value.startswith('/') and not value.startswith('//') and not value.startswith('/private/')) or (urlparse(value).scheme == 'https' and not urlparse(value).username)):
            frappe.throw('Use a public image URL over HTTPS or a public website file')
    if doc.kind == 'Image' and not draft.get('decorative') and not str(draft.get('alt') or '').strip():
        # Blank drafts are allowed; approval checks completeness.
        if frappe.flags.get('dsi_storefront_approving'):
            frappe.throw('Add an image description or mark the image decorative')
    before = doc.get_doc_before_save()
    protected = ('published_json', 'approved_by', 'approved_on')
    if not frappe.flags.get('dsi_storefront_approving'):
        for field in protected:
            current = payload(doc.get(field)) if field == 'published_json' else doc.get(field)
            previous = payload(before.get(field)) if field == 'published_json' and before else (before.get(field) if before else ({} if field == 'published_json' else None))
            if current != previous:
                # Empty JSON and empty fields are equivalent on newly imported drafts.
                if not before and not doc.get(field):
                    continue
                frappe.throw('Published values can only change through Approve and Publish')
        if not before and doc.status == 'Approved':
            frappe.throw('Use Approve and Publish to approve this record')
        if before and any(doc.get(field) != before.get(field) for field in ('kind', 'target', 'locale', 'website_item', 'review_key')):
            frappe.throw('Create a new review to change its page, image, item or language')
    if before and not frappe.flags.get('dsi_storefront_approving'):
        if payload(before.draft_json) != draft or before.source_hash != doc.source_hash:
            doc.status = 'Needs Review'
            doc.is_stale = int(before.source_hash != doc.source_hash)
        elif doc.status == 'Approved' and before.status != 'Approved':
            frappe.throw('Use Approve and Publish to approve this record')
    for field, key in [('candidate_title', 'title'), ('candidate_description', 'description'), ('candidate_image', 'image'), ('candidate_image_alt', 'imageAlt'), ('candidate_alt', 'alt'), ('candidate_decorative', 'decorative')]:
        doc.set(field, draft.get(key) or (0 if key == 'decorative' else ''))

def validate_settings(doc, method=None):
    only_editor()
    for line in (doc.robots_text or '').splitlines():
        directive, _, value = line.partition(':')
        if directive.strip().lower() == 'disallow' and value.split('#', 1)[0].strip():
            frappe.throw('All crawlers must remain allowed. Remove the Disallow rule.')
    before = doc.get_doc_before_save()
    if before and not frappe.flags.get('dsi_storefront_approving'):
        if any(doc.get(f) != before.get(f) for f in ('published_default_og_image', 'published_default_og_alt')):
            frappe.throw('Use a Default OG review to publish the social image')

def review_updated(doc, method=None):
    if frappe.flags.get('dsi_storefront_approving'):
        invalidate(doc)

@frappe.whitelist()
def approve(name, modified):
    only_editor()
    doc = frappe.get_doc('Storefront Review', name)
    doc.check_permission('write')
    if not set(frappe.get_roles()) & {'Storefront Reviewer', 'System Manager'}:
        frappe.throw('Reviewer access required', frappe.PermissionError)
    if str(doc.modified) != str(modified):
        frappe.throw('This review changed. Reload before approving.')
    frappe.flags.dsi_storefront_approving = True
    try:
        return _publish_review(doc)
    finally:
        frappe.flags.dsi_storefront_approving = False

def _publish_review(doc):
    draft = payload(doc.draft_json)
    validate_review(doc)
    if doc.kind == 'Website Item':
        from dsi_catalogue.website_item_review import approve_item
        approve_item(doc, draft)
    if doc.kind == 'Page SEO' and (not str(draft.get('title') or '').strip() or not str(draft.get('description') or '').strip()):
        frappe.throw('Add a title and description before approving')
    if doc.kind == 'Default OG' and not draft.get('image'):
        frappe.throw('Choose an image before approving')
    if doc.kind == 'Translation':
        if not draft or any(not str(value).strip() for value in draft.values()):
            frappe.throw('Fill the missing translated values before approving')
        from dsi_catalogue.i18n import CHILD_DOCTYPE, PARENTFIELD, _row_name
        row = _row_name(doc.website_item, doc.locale)
        values = {k: v for k, v in draft.items() if k in FIELDS['Translation']}
        values.update(review_status='Approved', source='Machine', is_stale=0, base_en_fingerprint=frappe.db.get_value('Website Item', doc.website_item, 'custom_en_fingerprint') or '')
        existing = frappe.get_doc(CHILD_DOCTYPE, row['name']) if row else None
        source = payload(doc.source_json)
        for field in draft:
            current = existing.get(field) or '' if existing else ''
            expected = source.get('existing', {}).get(field) or ''
            if current != expected:
                frappe.throw('The translation changed since this candidate was created. Refresh its source before approval.')
        if row:
            frappe.db.set_value(CHILD_DOCTYPE, row['name'], values)
        else:
            frappe.get_doc(dict(doctype=CHILD_DOCTYPE, parent=doc.website_item, parenttype='Website Item', parentfield=PARENTFIELD, language=doc.locale, **values)).insert(ignore_permissions=True)
        frappe.db.set_value('Website Item', doc.website_item, 'modified', frappe.utils.now(), update_modified=False)
    if doc.kind == 'Default OG':
        settings = frappe.get_single('Storefront Settings')
        settings.published_default_og_image = draft['image']
        settings.published_default_og_alt = draft.get('imageAlt') or 'Designer Shaik'
        settings.save(ignore_permissions=True)
    doc.published_json = draft
    doc.status = 'Approved'
    doc.is_stale = 0
    doc.approved_by = frappe.session.user
    doc.approved_on = frappe.utils.now()
    doc.save()
    return {'ok': True, 'name': doc.name}

@frappe.whitelist()
def request_changes(name, modified):
    only_editor()
    doc = frappe.get_doc('Storefront Review', name)
    doc.check_permission('write')
    if str(doc.modified) != str(modified):
        frappe.throw('This review changed. Reload before saving.')
    doc.status = 'Changes Requested'
    doc.save()
    return {'ok': True}

@frappe.whitelist(allow_guest=True)
def get_public_manifest():
    settings = frappe.get_single('Storefront Settings')
    pages, images = {}, {}
    for row in frappe.get_all('Storefront Review', fields=['kind', 'target', 'locale', 'published_json', 'modified'], filters=[['published_json', 'is', 'set']], limit_page_length=0):
        value = payload(row.published_json)
        if row.kind == 'Page SEO':
            pages[row.locale + ':' + row.target.lstrip('/')] = dict(value, modified=str(row.modified))
        elif row.kind == 'Image':
            images[row.locale + ':' + row.target] = value
    products = []
    for row in frappe.get_all('Website Item', filters={'published': 1}, fields=['custom_palace_slug', 'custom_product_slug', 'custom_grouping_key', 'modified'], order_by='modified desc', limit_page_length=0):
        if row.custom_palace_slug and row.custom_product_slug:
            products.append({'palace': row.custom_palace_slug, 'slug': row.custom_product_slug, 'group': row.custom_grouping_key, 'modified': str(row.modified)})
    return {'version': 1, 'robotsText': settings.robots_text or 'User-agent: *\nAllow: /\n', 'llmText': settings.llm_text or '# Designer Shaik\n', 'defaultImage': settings.published_default_og_image or '', 'defaultImageAlt': settings.published_default_og_alt or 'Designer Shaik', 'pages': pages, 'images': images, 'products': products}

def upsert_draft(kind, target, locale, draft, source, **values):
    key = fingerprint([kind, target, locale])
    name = frappe.db.get_value('Storefront Review', {'review_key': key}, 'name')
    doc = frappe.get_doc('Storefront Review', name) if name else frappe.get_doc({'doctype':'Storefront Review','kind':kind,'target':target,'locale':locale,'review_key':key})
    doc.update(dict(draft_json=json.dumps(draft,ensure_ascii=False),source_json=json.dumps(source,ensure_ascii=False),source_hash=fingerprint(source),status='Needs Review',**values))
    doc.flags.ignore_links = True
    doc.save(ignore_permissions=True)
    return doc

def seed(path):
    """Bench-only importer. Existing reviewer edits and published values are preserved."""
    data = json.loads(open(path).read())
    frappe.flags.dsi_storefront_bootstrap = True
    if not frappe.db.exists('Role', 'Storefront Reviewer'):
        frappe.get_doc({'doctype': 'Role', 'role_name': 'Storefront Reviewer', 'desk_access': 1}).insert(ignore_permissions=True)
    user = frappe.get_doc('User', 'carla@designershaik.com')
    if 'Storefront Reviewer' not in [r.role for r in user.roles]:
        user.add_roles('Storefront Reviewer')
    settings = frappe.get_single('Storefront Settings')
    if not settings.robots_text:
        settings.robots_text = 'User-agent: *\nAllow: /\n'
    if not settings.llm_text:
        settings.llm_text = data['llmText']
    settings.active_domain = 'https://dsi-web-n.netlify.app'
    settings.production_domain = 'https://designershaik.com'
    settings.save(ignore_permissions=True)
    from frappe.permissions import add_permission, update_permission_property
    add_permission('Website Item', 'Storefront Reviewer', 0)
    for right in ('read','write','create','report','export','print'):
        update_permission_property('Website Item', 'Storefront Reviewer', 0, right, 1)
    created = 0
    for row in data['reviews']:
        if row['kind'] == 'Website Item':
            from dsi_catalogue.website_item_review import content
            current = content(frappe.get_doc('Website Item', row['website_item']))
            row['source_json'] = json.dumps(current, ensure_ascii=False)
            row['draft_json'] = json.dumps(current, ensure_ascii=False)
        key = fingerprint([row['kind'], row['target'], row['locale']])
        if frappe.db.exists('Storefront Review', {'review_key': key}):
            continue
        doc = frappe.get_doc(dict(doctype='Storefront Review', review_key=key, source_hash=fingerprint(row.get('source_json') or {}), status='Needs Review', **row))
        doc.insert(ignore_permissions=True)
        created += 1
    frappe.db.commit()
    return {'created': created, 'reviewer': user.name}
