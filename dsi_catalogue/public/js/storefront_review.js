const reviewDraft = frm => {
  try { return typeof frm.doc.draft_json === 'string' ? JSON.parse(frm.doc.draft_json || '{}') : (frm.doc.draft_json || {}); }
  catch { return {}; }
};
const updateCandidate = async (frm, key, value) => {
  const draft = reviewDraft(frm); draft[key] = value;
  await frm.set_value('draft_json', JSON.stringify(draft, null, 2));
};
// What this review changes, in words: field, what is live now, what is pending.
const PLAIN_SKIP = new Set(['name', 'idx', 'parent', 'parenttype', 'parentfield', 'doctype', 'docstatus', 'owner', 'creation', 'modified', 'modified_by']);
const plain = html => { const d = document.createElement('div'); d.innerHTML = String(html ?? ''); return (d.textContent || '').replace(/\s+/g, ' ').trim(); };
const lines = v => Array.isArray(v)
  ? v.map(r => ('label' in r && 'description' in r) ? `${plain(r.label)}: ${plain(r.description)}`
      : Object.entries(r).filter(([k, x]) => !PLAIN_SKIP.has(k) && x !== null && x !== '').map(([k, x]) => `${k}: ${plain(x)}`).join(', '))
  : [plain(v)];
const parseJson = v => { try { return typeof v === 'string' ? JSON.parse(v || '{}') : (v || {}); } catch { return {}; } };
const renderChanges = frm => {
  if (frm.doc.kind !== 'Website Item') return '';
  const draft = parseJson(frm.doc.draft_json), source = parseJson(frm.doc.source_json);
  const changed = Object.keys(draft).filter(f => !['published', 'workflow_state'].includes(f) && JSON.stringify(draft[f]) !== JSON.stringify(source[f]));
  if (!changed.length) return '<p>No content changes in this draft.</p>';
  const cell = v => lines(v).map(x => frappe.utils.escape_html(x) || '<i>empty</i>').join('<br>');
  const rows = changed.map(f => `<tr><td style="white-space:nowrap"><b>${frappe.utils.escape_html(frappe.meta.get_label('Website Item', f) || f)}</b></td><td>${cell(source[f])}</td><td>${cell(draft[f])}</td></tr>`).join('');
  return `<div style="max-width:960px"><p><b>What this review changes</b></p><table class="table table-bordered"><thead><tr><th>Field</th><th>Live now</th><th>Pending</th></tr></thead><tbody>${rows}</tbody></table></div>`;
};

frappe.ui.form.on('Storefront Review', {
  refresh(frm) {
    // The raw JSON is for the system; for a Website Item the readable table below replaces it.
    for (const f of ['draft_json', 'source_json', 'published_json']) if (frm.fields_dict[f]) frm.toggle_display(f, frm.doc.kind !== 'Website Item');
    const draft = reviewDraft(frm);
    const fields = {candidate_title:['Page SEO'],candidate_description:['Page SEO'],candidate_image:['Page SEO','Default OG'],candidate_image_alt:['Page SEO','Default OG'],candidate_alt:['Image'],candidate_decorative:['Image']};
    for (const [field, kinds] of Object.entries(fields)) frm.toggle_display(field, kinds.includes(frm.doc.kind));
    const url = draft.image || draft.website_image || frm.doc.image_url;
    frm.fields_dict.preview.$wrapper.empty();
    if (url && (url.startsWith('https://') || (url.startsWith('/') && !url.startsWith('//')))) {
      const escaped = frappe.utils.escape_html(url);
      frm.fields_dict.preview.$wrapper.html(`<div style="max-width:720px"><p>Check the image, composition and description before publishing.</p><img src="${escaped}" alt="Review image" style="display:block;max-width:100%;max-height:360px;object-fit:contain;background:#0a0a0c"><p>${frappe.utils.escape_html(draft.imageAlt || draft.alt || draft.website_image_alt || '')}</p><p>${frappe.utils.escape_html(draft.title || draft.custom_seo_title || draft.web_item_name || '')}</p><p>${frappe.utils.escape_html(draft.description || draft.custom_seo_description || '')}</p></div>`);
    }
    frm.fields_dict.preview.$wrapper.append(renderChanges(frm));
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
