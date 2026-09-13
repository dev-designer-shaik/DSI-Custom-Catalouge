const reviewDraft = frm => {
  try { return typeof frm.doc.draft_json === 'string' ? JSON.parse(frm.doc.draft_json || '{}') : (frm.doc.draft_json || {}); }
  catch { return {}; }
};
const updateCandidate = async (frm, key, value) => {
  const draft = reviewDraft(frm); draft[key] = value;
  await frm.set_value('draft_json', JSON.stringify(draft, null, 2));
};
frappe.ui.form.on('Storefront Review', {
  refresh(frm) {
    const draft = reviewDraft(frm);
    const fields = {candidate_title:['Page SEO'],candidate_description:['Page SEO'],candidate_image:['Page SEO','Default OG'],candidate_image_alt:['Page SEO','Default OG'],candidate_alt:['Image'],candidate_decorative:['Image']};
    for (const [field, kinds] of Object.entries(fields)) frm.toggle_display(field, kinds.includes(frm.doc.kind));
    const url = draft.image || draft.website_image || frm.doc.image_url;
    frm.fields_dict.preview.$wrapper.empty();
    if (url && (url.startsWith('https://') || (url.startsWith('/') && !url.startsWith('//')))) {
      const escaped = frappe.utils.escape_html(url);
      frm.fields_dict.preview.$wrapper.html(`<div style="max-width:720px"><p>Check the image, composition and description before publishing.</p><img src="${escaped}" alt="Review image" style="display:block;max-width:100%;max-height:360px;object-fit:contain;background:#0a0a0c"><p>${frappe.utils.escape_html(draft.imageAlt || draft.alt || draft.website_image_alt || '')}</p><p>${frappe.utils.escape_html(draft.title || draft.custom_seo_title || draft.web_item_name || '')}</p><p>${frappe.utils.escape_html(draft.description || draft.custom_seo_description || '')}</p></div>`);
    }
    if (!frm.is_new() && frappe.session.user === 'carla@designershaik.com') {
      frm.add_custom_button('Approve and Publish', async () => {
        if (frm.is_dirty()) await frm.save();
        await frappe.call({method:'dsi_catalogue.storefront_review.approve', args:{name:frm.doc.name, modified:frm.doc.modified}, freeze:true});
        await frm.reload_doc();
      }).addClass('btn-primary');
      frm.add_custom_button('Request Changes', async () => {
        if (frm.is_dirty()) await frm.save();
        await frappe.call({method:'dsi_catalogue.storefront_review.request_changes', args:{name:frm.doc.name, modified:frm.doc.modified}, freeze:true});
        await frm.reload_doc();
      });
    }
  },
  candidate_title: frm => updateCandidate(frm,'title',frm.doc.candidate_title || ''),
  candidate_description: frm => updateCandidate(frm,'description',frm.doc.candidate_description || ''),
  candidate_image: frm => updateCandidate(frm,'image',frm.doc.candidate_image || ''),
  candidate_image_alt: frm => updateCandidate(frm,'imageAlt',frm.doc.candidate_image_alt || ''),
  candidate_alt: frm => updateCandidate(frm,'alt',frm.doc.candidate_alt || ''),
  candidate_decorative: frm => updateCandidate(frm,'decorative',!!frm.doc.candidate_decorative)
});
