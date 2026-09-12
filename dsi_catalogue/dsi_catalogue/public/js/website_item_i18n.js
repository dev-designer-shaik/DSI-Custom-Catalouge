// Authored translations for Website Item — in-place language switching.
//
// MODEL
// English is canonical and lives in the Website Item's own fields. Other
// languages live in the `custom_translations` child table and are written ONLY
// through dsi_catalogue.i18n.put_copy.
//
// THE ONE RULE
// `frm.doc` is a rendering surface, never the store. Authoritative state lives
// in `frm.__i18n`. That is what makes a mid-edit reload, a stray Ctrl+S or a
// silent `debounced_reload_doc` non-destructive: the buffer survives and is
// re-applied on refresh.
//
// Two structural safeties, both relying on framework behaviour rather than on
// this file being correct:
//   1. After a language swap the doc is left CLEAN (`__unsaved = 0`). Frappe's
//      save.js refuses to save a clean doc at all, so the native path is closed.
//   2. `custom_display_language` is stored hidden and is un-hidden ONLY here, at
//      runtime. If this script fails to load the dropdown never appears, so the
//      hazard cannot be reached. (This started as `depends_on: eval:doc.__i18n_ui`
//      — the eval does support dunder access, but the field stayed hidden in
//      practice, so it now uses set_df_property, which is a documented API and
//      can actually be verified.)
// The server guard (dsi_catalogue.website_item_events.i18n_guard) is the backstop
// for everything else.

frappe.provide('dsi.i18n');

dsi.i18n.LANGS = { ar: 'Arabic', es: 'Spanish', fr: 'French', ru: 'Russian' };

// Canonical field -> child-row mirror. Must match i18n.py TRANSLATED_FIELDS.
dsi.i18n.FIELDS = {
	web_item_name: 'web_item_name',
	website_content: 'website_content',
	website_image_alt: 'website_image_alt',
	short_description: 'short_description',
	web_long_description: 'web_long_description',
	custom_seo_title: 'seo_title',
	custom_seo_description: 'seo_description',
	custom_seo_keywords: 'seo_keywords',
};

dsi.i18n.snapshot_english = function (frm) {
	const en = {};
	Object.keys(dsi.i18n.FIELDS).forEach((f) => {
		en[f] = frm.doc[f] || '';
	});
	en.specifications = (frm.doc.website_specifications || []).map((r) => ({
		name: r.name,
		idx: r.idx,
		label: r.label || '',
		description: r.description || '',
	}));
	return en;
};

// Write without marking the form dirty. skip_dirty_trigger is the 6th arg of
// frappe.model.set_value; without it every swap would dirty the doc and re-arm
// the native Save button.
dsi.i18n.set_quiet = function (frm, field, value) {
	frappe.model.set_value(frm.doctype, frm.docname, field, value || '', null, true);
};

// The dropdown only exists once this has run — that IS the safety property.
dsi.i18n.reveal_switcher = function (frm) {
	frm.set_df_property('custom_display_language', 'hidden', 0);
	frm.refresh_field('custom_display_language');
};

// Has the EDITOR actually changed English copy?
//
// frm.is_dirty() cannot answer this from inside a field handler: Frappe's
// watch_model_updates calls dirty() BEFORE firing the field trigger, so simply
// choosing a language marks the doc dirty and any is_dirty() check fires every
// single time. Compare against the snapshot instead — that is what "English has
// unsaved edits" actually means.
dsi.i18n.english_is_dirty = function (frm) {
	const en = frm.__i18n && frm.__i18n.en;
	if (!en) return false;
	const changed = Object.keys(dsi.i18n.FIELDS).some(
		(f) => (frm.doc[f] || '') !== (en[f] || '')
	);
	if (changed) return true;
	const now = (frm.doc.website_specifications || []).map((r) => [r.label || '', r.description || '']);
	const was = (en.specifications || []).map((r) => [r.label || '', r.description || '']);
	return JSON.stringify(now) !== JSON.stringify(was);
};

dsi.i18n.mark_clean = function (frm) {
	frm.doc.__unsaved = 0;
	if (frm.toolbar) frm.toolbar.set_indicator();
};

dsi.i18n.apply_bundle = function (frm, bundle) {
	Object.keys(dsi.i18n.FIELDS).forEach((f) => {
		dsi.i18n.set_quiet(frm, f, bundle[f] || '');
	});
	dsi.i18n.mark_clean(frm);
};

// Restore English BY ROW IDENTITY for specs — rebuilding rows would make Frappe
// delete and reinsert every spec row on the next save.
dsi.i18n.restore_english = function (frm) {
	const en = frm.__i18n && frm.__i18n.en;
	if (!en) return;
	Object.keys(dsi.i18n.FIELDS).forEach((f) => {
		dsi.i18n.set_quiet(frm, f, en[f] || '');
	});
	const by_name = {};
	(en.specifications || []).forEach((r) => {
		if (r.name) by_name[r.name] = r;
	});
	(frm.doc.website_specifications || []).forEach((row) => {
		const src = by_name[row.name];
		if (src) {
			row.label = src.label;
			row.description = src.description;
		}
	});
	dsi.i18n.mark_clean(frm);
};

dsi.i18n.enter_translation_mode = function (frm, lang) {
	const label = dsi.i18n.LANGS[lang] || lang;
	frm.disable_save();
	frm.page.set_primary_action(__('Save {0} Translation', [label]), () =>
		dsi.i18n.save_translation(frm)
	);
	frm.page.set_secondary_action(__('Back to English'), () => {
		frm.set_value('custom_display_language', 'en');
	});
	frm.dashboard.clear_headline();
	frm.dashboard.set_headline_alert(
		__('Editing <b>{0}</b>. The English copy is safe — "Save {0} Translation" writes only the translation.', [label]),
		'orange'
	);
	frm.$wrapper.addClass('dsi-i18n-mode');
	// Arabic reads right-to-left; without this the editor is proofreading blind.
	const rtl = lang === 'ar';
	Object.keys(dsi.i18n.FIELDS).forEach((f) => {
		const ctrl = frm.fields_dict[f];
		if (ctrl && ctrl.$wrapper) ctrl.$wrapper.css('direction', rtl ? 'rtl' : '');
	});
};

dsi.i18n.leave_translation_mode = function (frm) {
	frm.enable_save();
	frm.page.clear_secondary_action();
	frm.dashboard.clear_headline();
	frm.$wrapper.removeClass('dsi-i18n-mode');
	Object.keys(dsi.i18n.FIELDS).forEach((f) => {
		const ctrl = frm.fields_dict[f];
		if (ctrl && ctrl.$wrapper) ctrl.$wrapper.css('direction', '');
	});
	frm.refresh();
};

dsi.i18n.save_translation = function (frm) {
	const mode = frm.__i18n && frm.__i18n.mode;
	if (!mode || mode === 'en') return;
	const buf = frm.__i18n.buffer[mode] || {};
	const payload = {};
	Object.keys(dsi.i18n.FIELDS).forEach((f) => {
		payload[dsi.i18n.FIELDS[f]] = frm.doc[f] || '';
	});
	payload.specifications = buf.specifications || frm.__i18n.specs[mode] || [];
	payload.review_status = buf.review_status || frm.__i18n.review[mode] || 'Machine (unreviewed)';

	return frappe
		.call({
			method: 'dsi_catalogue.i18n.put_copy',
			args: {
				website_item: frm.docname,
				language: mode,
				payload: JSON.stringify(payload),
				base_en_fingerprint: frm.__i18n.base_fp || '',
				row_modified: frm.__i18n.rowmod[mode] || '',
			},
			freeze: true,
			freeze_message: __('Saving translation…'),
		})
		.then((r) => {
			if (r && r.message && r.message.ok) {
				frm.__i18n.rowmod[mode] = r.message.row_modified;
				frm.__i18n.buffer[mode] = {};
				dsi.i18n.mark_clean(frm);
				frappe.show_alert({
					message: __('{0} translation saved', [dsi.i18n.LANGS[mode] || mode]),
					indicator: 'green',
				});
			}
		});
};

dsi.i18n.edit_specs_dialog = function (frm) {
	const mode = frm.__i18n && frm.__i18n.mode;
	if (!mode || mode === 'en') return;
	const label = dsi.i18n.LANGS[mode] || mode;
	// Seeded from English LABELS with blank values — never English body copy, or
	// unedited English gets promoted to "authored translation" on the storefront.
	const seed =
		(frm.__i18n.specs[mode] || []).length
			? frm.__i18n.specs[mode]
			: (frm.__i18n.en.specifications || []).map((r, i) => ({
					idx: i + 1,
					label: '',
					description: '',
					__en_label: r.label,
			  }));

	const d = new frappe.ui.Dialog({
		title: __('{0} Specifications', [label]),
		size: 'large',
		fields: [
			{
				fieldname: 'rows',
				fieldtype: 'Table',
				cannot_add_rows: false,
				in_place_edit: true,
				data: seed,
				get_data: () => seed,
				fields: [
					{ fieldname: 'label', fieldtype: 'Data', label: __('Label'), in_list_view: 1, columns: 4 },
					{ fieldname: 'description', fieldtype: 'Small Text', label: __('Value'), in_list_view: 1, columns: 6 },
				],
			},
		],
		primary_action_label: __('Apply'),
		primary_action: () => {
			const rows = (d.get_value('rows') || []).map((r, i) => ({
				idx: i + 1,
				label: r.label || '',
				description: r.description || '',
			}));
			frm.__i18n.specs[mode] = rows;
			frm.__i18n.buffer[mode] = frm.__i18n.buffer[mode] || {};
			frm.__i18n.buffer[mode].specifications = rows;
			d.hide();
			frappe.show_alert(__('Specifications staged — save the translation to persist.'));
		},
	});
	d.show();
};

frappe.ui.form.on('Website Item', {
	setup(frm) {
		frm.__i18n = null;
	},

	onload(frm) {
		// The beacon. Reveals custom_display_language via its depends_on, and tells
		// the server guard that a reconciling client is present.
		frm.doc.__i18n_ui = 1;
		dsi.i18n.reveal_switcher(frm);
		frm.__i18n = {
			mode: 'en',
			en: dsi.i18n.snapshot_english(frm),
			buffer: {},
			specs: {},
			review: {},
			rowmod: {},
			base_fp: frm.doc.custom_en_fingerprint || '',
		};
		// Should be impossible — the server normalises it — but if a non-'en' value
		// ever persisted, do not start the form in a translated state.
		if (frm.doc.custom_display_language && frm.doc.custom_display_language !== 'en') {
			dsi.i18n.set_quiet(frm, 'custom_display_language', 'en');
		}
	},

	refresh(frm) {
		// A reload rebuilds frm.doc and the layout, so re-stamp and re-reveal.
		frm.doc.__i18n_ui = 1;
		dsi.i18n.reveal_switcher(frm);
		if (!frm.__i18n) return;

		if (frm.__i18n.mode === 'en') {
			if (!dsi.i18n.english_is_dirty(frm)) frm.__i18n.en = dsi.i18n.snapshot_english(frm);
			return;
		}

		// Got here via after_save's refresh() or a silent reload while translating.
		dsi.i18n.enter_translation_mode(frm, frm.__i18n.mode);
		const buf = frm.__i18n.buffer[frm.__i18n.mode];
		if (buf && Object.keys(buf).length) dsi.i18n.apply_bundle(frm, buf);
		dsi.i18n.mark_clean(frm);

		frm.add_custom_button(__('Edit Specifications'), () => dsi.i18n.edit_specs_dialog(frm));
	},

	custom_display_language(frm) {
		if (!frm.__i18n) return;
		const next = frm.doc.custom_display_language || 'en';
		const prev = frm.__i18n.mode;
		if (next === prev) return;

		// Refusing to switch while English is dirty is what makes frm.__i18n.en
		// provably equal to the database — which is what lets the guard restore
		// with confidence.
		if (prev === 'en' && dsi.i18n.english_is_dirty(frm)) {
			dsi.i18n.set_quiet(frm, 'custom_display_language', 'en');
			dsi.i18n.mark_clean(frm);
			frappe.msgprint({
				title: __('Unsaved English changes'),
				message: __('Save or discard your English changes before switching language.'),
				indicator: 'orange',
			});
			return;
		}

		if (next === 'en') {
			frm.__i18n.mode = 'en';
			dsi.i18n.restore_english(frm);   // also marks clean
			dsi.i18n.leave_translation_mode(frm);
			return;
		}

		frm.__i18n.en = dsi.i18n.snapshot_english(frm);
		frappe
			.call({
				method: 'dsi_catalogue.i18n.get_copy',
				args: { website_item: frm.docname, language: next },
			})
			.then((r) => {
				const m = (r && r.message) || {};
				if (m.parent_fingerprint && frm.__i18n.base_fp && m.parent_fingerprint !== frm.__i18n.base_fp) {
					frappe.msgprint(__('The English copy changed since this page loaded. Reload before translating.'));
				}
				const bundle = {};
				Object.keys(dsi.i18n.FIELDS).forEach((f) => {
					bundle[f] = m[dsi.i18n.FIELDS[f]] || '';
				});
				frm.__i18n.mode = next;
				frm.__i18n.specs[next] = m.specifications || [];
				frm.__i18n.review[next] = m.review_status || 'Machine (unreviewed)';
				frm.__i18n.rowmod[next] = m.row_modified || '';
				dsi.i18n.apply_bundle(frm, bundle);
				frm.refresh();
			})
			.catch(() => {
				// Never leave the form half-swapped.
				dsi.i18n.set_quiet(frm, 'custom_display_language', 'en');
				frm.__i18n.mode = 'en';
				dsi.i18n.restore_english(frm);
			});
	},

	// Both seats, deliberately. Either aborting is enough; having both means a
	// change in Frappe's client save sequence cannot silently open the hazard.
	validate(frm) {
		return dsi.i18n.block_english_save(frm);
	},

	before_save(frm) {
		return dsi.i18n.block_english_save(frm);
	},
});

dsi.i18n.block_english_save = function (frm) {
	frm.doc.__i18n_ui = 1;
	if (!frm.__i18n || frm.__i18n.mode === 'en') return;
	dsi.i18n.restore_english(frm);
	frm.doc.custom_display_language = 'en';
	frappe.validated = false; // aborts the save (form.js run_serially)
	frappe.msgprint({
		title: __('Use "Save Translation"'),
		message: __('You are editing a translation. The English copy has been restored; use the "Save Translation" button.'),
		indicator: 'orange',
	});
};

// Harvest edits into the buffer and immediately un-dirty, in the same tick as
// Frappe's own dirty() (watch_model_updates fires dirty() then the field
// trigger). A clean doc cannot be saved by the native path at all.
Object.keys(dsi.i18n.FIELDS).forEach((field) => {
	frappe.ui.form.on('Website Item', {
		[field]: function (frm) {
			if (!frm.__i18n || frm.__i18n.mode === 'en') return;
			const mode = frm.__i18n.mode;
			frm.__i18n.buffer[mode] = frm.__i18n.buffer[mode] || {};
			frm.__i18n.buffer[mode][field] = frm.doc[field] || '';
			dsi.i18n.mark_clean(frm);
		},
	});
});
