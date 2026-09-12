"""Website Item lifecycle logic, ported out of Frappe Server Scripts (2026-08-06).

WHY THIS FILE EXISTS
These three behaviours lived as Server Scripts until `server_script_enabled` was set to 0 estate-wide
on 2026-08-04 (the 2026-08-02 compromise used Server Scripts as its RCE primitive). Two of them are
`before_save` hooks that mutate or veto the document in-process, so they cannot be reimplemented as
n8n webhooks — a webhook fires after the fact and cannot change what is written. They therefore had to
become app code.

The logic is a faithful port. Event mapping is exact: a Server Script "Before Save" is `before_save`
and "After Save" is `on_update`, so ordering relative to `validate` (where
`dsi_catalogue.api.website_item_precompute` runs) is unchanged.

Originals, for reference, are preserved as disabled Server Script records of the same name.
"""

import frappe


def publish_gate(doc, method=None):
    """Nothing publishes unless the workflow says Approved.

    Ported from Server Script "Website Item Publish Gate" (Before Save). Uses doc.get() rather than
    attribute access so a missing/renamed workflow field degrades to "not approved" instead of raising
    and blocking every save of the doctype.
    """
    if doc.get("workflow_state") != "Approved":
        doc.published = 0


def gallery_gender_stamp(doc, method=None):
    """Gallery row hygiene on save.

    Ported from Server Script "Website Item Gallery Gender Stamp" (Before Save).

    1) Gender stamp — the storefront pools the whole group's galleries and filters by variant_code;
       empty codes leak across gendered pages, so default them to this item's own gender (M/W from the
       index key). Rows explicitly marked shared_type=inclusive are for both — skipped.
    2) file_name — editor-attached rows leave it empty, which breaks the website's rank matching (rank
       is matched by file name). Fill it from the image URL's basename.

    Swallows exceptions exactly as the original did: gallery hygiene must never block a save.
    """
    try:
        ik = (doc.get("custom_index_key") or "").strip("{}")
        segs = ik.split("-")
        gender = "M" if "M" in segs else ("W" if "W" in segs else "")
        for r in (doc.get("custom_gallery_images") or []):
            if gender and not (r.variant_code or "").strip() and (r.shared_type or "") != "inclusive":
                r.variant_code = gender
            if not (r.file_name or "").strip() and (r.image or "").strip():
                base = (r.image or "").split("?")[0].rstrip("/").split("/")[-1]
                if base:
                    r.file_name = base
    except Exception:
        frappe.log_error(frappe.get_traceback(), "dsi_catalogue gallery_gender_stamp")


def variant_content_sync(doc, method=None):
    """Cross-sync SHARED product data to variant siblings in the same group.

    Ported from Server Script "Website Item Variant Content Sync" (After Save -> on_update).

    Syncs, within the same custom_grouping_key AND the same gender: the shared intro paragraph
    (website_content) and the gallery images including their order (row order, rank, hero flag).
    PER-VARIANT fields (short_description = Product Details, tabs, set includes, specs) are
    intentionally NOT synced — each SKU owns its own. A request flag prevents recursion; a signature
    compare skips no-op saves. Guarded: never blocks a save.
    """
    try:
        if frappe.flags.get("dsi_variant_sync"):
            return
        ik = doc.get("custom_index_key") or ""
        gkey = doc.get("custom_grouping_key") or ""
        if not (gkey and ik):
            return

        segs = ik.strip("{}").split("-")
        gender = "M" if "M" in segs else ("W" if "W" in segs else "")
        src_wc = doc.get("website_content") or ""
        src_gal = []
        for r in (doc.get("custom_gallery_images") or []):
            src_gal.append(((r.image or ""), (r.file_name or ""), int(r.rank or 0),
                            int(r.is_hero or 0), (r.shared_type or ""),
                            (r.variant_code or ""), (r.alt_text or "")))
        src_gsig = repr(src_gal)

        siblings = frappe.get_all("Website Item",
            filters={"custom_grouping_key": gkey, "name": ["!=", doc.name]},
            fields=["name", "custom_index_key"])

        matched_siblings = []
        frappe.flags.dsi_variant_sync = True
        try:
            for s in siblings:
                ssegs = (s["custom_index_key"] or "").strip("{}").split("-")
                sgender = "M" if "M" in ssegs else ("W" if "W" in ssegs else "")
                if sgender != gender:
                    continue
                matched_siblings.append(s["name"])
                sib = frappe.get_doc("Website Item", s["name"])
                ex_gal = []
                for r in (sib.custom_gallery_images or []):
                    ex_gal.append(((r.image or ""), (r.file_name or ""), int(r.rank or 0),
                                   int(r.is_hero or 0), (r.shared_type or ""),
                                   (r.variant_code or ""), (r.alt_text or "")))
                same_wc = (sib.website_content or "") == src_wc
                same_gal = repr(ex_gal) == src_gsig
                if same_wc and same_gal:
                    continue
                if not same_wc:
                    sib.website_content = src_wc
                if not same_gal:
                    sib.custom_gallery_images = []
                    for img, fn, rk, hero, st, vc, alt in src_gal:
                        sib.append("custom_gallery_images", {
                            "image": img, "file_name": fn, "rank": rk,
                            "is_hero": hero, "shared_type": st,
                            "variant_code": vc, "alt_text": alt})
                sib.save(ignore_permissions=True)
        finally:
            frappe.flags.dsi_variant_sync = False

        # website_content is the one SHARED field, so its translations are shared too.
        # Everything else on a translation row is per-variant and must not propagate.
        _i18n_sync_shared_translations(doc, matched_siblings)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "dsi_catalogue variant_content_sync")


def _i18n_sync_shared_translations(doc, sibling_names):
    """Copy ONLY the translated website_content to sibling variants, per language.

    Mirrors what the English sync does for website_content. Deliberately does not
    touch short_description / web_long_description / specifications_json / seo_* /
    website_image_alt on the sibling's row - those are per-variant, exactly as the
    English sync leaves their English counterparts alone.

    Writes with db.set_value so no sibling save is triggered: no recursion, no
    publish_gate re-run, no extra revalidate storm.
    """
    if not sibling_names:
        return
    try:
        src = {}
        for r in (doc.get("custom_translations") or []):
            lang = r.get("language")
            if lang:
                src[lang] = r.get("website_content") or ""
        if not src:
            return

        for sib_name in sibling_names:
            rows = frappe.get_all(
                "Website Item Translated Copy",
                filters={"parent": sib_name, "parenttype": "Website Item",
                         "parentfield": "custom_translations"},
                fields=["name", "language", "website_content"],
            )
            existing = {r["language"]: r for r in rows}
            for lang, content in src.items():
                row = existing.get(lang)
                if row:
                    if (row["website_content"] or "") != content:
                        frappe.db.set_value("Website Item Translated Copy", row["name"],
                                            "website_content", content, update_modified=False)
                elif content:
                    frappe.get_doc({
                        "doctype": "Website Item Translated Copy",
                        "parent": sib_name, "parenttype": "Website Item",
                        "parentfield": "custom_translations",
                        "idx": len(rows) + 1,
                        "language": lang,
                        "website_content": content,
                        "review_status": "Machine (unreviewed)",
                        "source": "Human",
                    }).insert(ignore_permissions=True)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "dsi_catalogue i18n shared translation sync")


# ---------------------------------------------------------------------------
# Authored translations (2026-08-29). See dsi_catalogue/i18n.py for the model.
# ---------------------------------------------------------------------------


def _i18n_changed_canonical(doc, prev):
    """Which protected English fields differ from what is in the database."""
    from dsi_catalogue.i18n import CANONICAL_FIELDS, spec_rows

    changed = {}
    for f in CANONICAL_FIELDS:
        old, new = (prev.get(f) or ""), (doc.get(f) or "")
        if old != new:
            changed[f] = (old, new)

    old_specs = [(r["label"], r["description"]) for r in spec_rows(prev)]
    new_specs = [(r["label"], r["description"]) for r in spec_rows(doc)]
    if old_specs != new_specs:
        changed["website_specifications"] = (old_specs, new_specs)
    return changed


def _i18n_restore_canonical(doc, prev):
    """Put the database's English back onto the in-flight document.

    Specification rows are restored BY IDENTITY (row name) rather than rebuilt, so
    the save does not delete and reinsert them - that would churn idx, bump
    modified on every row and needlessly invalidate the storefront cache.
    """
    from dsi_catalogue.i18n import CANONICAL_FIELDS, spec_rows

    for f in CANONICAL_FIELDS:
        doc.set(f, prev.get(f) or "")

    by_name = {r["name"]: r for r in spec_rows(prev) if r.get("name")}
    for row in doc.get("website_specifications") or []:
        src = by_name.get(row.get("name"))
        if src:
            row.label = src["label"]
            row.description = src["description"]


def _i18n_dedupe_rows(doc):
    """Child tables cannot be unique-constrained; keep the last row per language."""
    seen, keep = {}, []
    for row in doc.get("custom_translations") or []:
        seen[row.get("language")] = row
    if len(seen) != len(doc.get("custom_translations") or []):
        keep = list(seen.values())
        for i, row in enumerate(keep, start=1):
            row.idx = i
        doc.set("custom_translations", keep)
        frappe.log_error(f"deduped translation rows on {doc.name}", "dsi_catalogue i18n_guard")


def _i18n_restore_dropped_rows(doc, prev):
    """Guard the wipe case that child-table storage makes possible.

    Any write that assigns `custom_translations` - a Data Import carrying that
    column, a partial REST PUT, future code doing `= []` - silently deletes every
    translation for the item. Nothing else would notice. If languages present in
    the database are missing from the payload, put them back.
    """
    prev_rows = {r.get("language"): r for r in (prev.get("custom_translations") or [])}
    if not prev_rows:
        return
    have = {r.get("language") for r in (doc.get("custom_translations") or [])}
    missing = [lang for lang in prev_rows if lang not in have]
    if not missing:
        return
    for lang in missing:
        src = prev_rows[lang]
        payload = {k: v for k, v in (src.as_dict() if hasattr(src, "as_dict") else dict(src)).items()
                   if k not in ("name", "idx", "parent", "parenttype", "parentfield",
                                "doctype", "owner", "creation", "modified", "modified_by")}
        doc.append("custom_translations", payload)
    frappe.log_error(
        f"restored dropped translation rows {missing} on {doc.name}",
        "dsi_catalogue i18n_guard",
    )


def _i18n_looks_like_form_save(doc):
    """Is this payload from the desk form?

    Detected by payload SHAPE, not by request path. variant_content_sync calls
    sib.save() inside the same desk request, so a path check would misclassify
    every sibling save as a form save and block it.
    """
    return bool(doc.get("__last_sync_on") or doc.get("__islocal"))


def _i18n_script_mismatch(changed):
    """Arabic/Cyrillic landing in an English column is unambiguous. Latin is not."""
    for _f, (_old, new) in changed.items():
        if not isinstance(new, str) or not new:
            continue
        foreign = sum(1 for ch in new if "؀" <= ch <= "ۿ" or "Ѐ" <= ch <= "ӿ")
        letters = sum(1 for ch in new if ch.isalpha())
        if letters and foreign / letters > 0.3:
            return True
    return False


def i18n_guard(doc, method=None):
    """Keep canonical English English. Primary seat: before_validate.

    before_validate is used rather than before_save because
    frappe.model.document.run_before_save_methods runs before_validate BEFORE the
    flags.ignore_validate early return - validate and before_save are both skipped
    by that flag, so a before_save-only guard is bypassable.
    """
    try:
        from dsi_catalogue.i18n import stamp_snapshot

        if frappe.flags.get("dsi_variant_sync") or frappe.flags.get("dsi_i18n_write"):
            return

        prev = doc.get_doc_before_save()
        lang = (doc.get("custom_display_language") or "en").strip() or "en"
        from_form = _i18n_looks_like_form_save(doc)
        has_beacon = bool(doc.get("__i18n_ui"))

        _i18n_dedupe_rows(doc)

        if prev is None:                      # insert
            doc.custom_display_language = "en"
            stamp_snapshot(doc)
            return

        _i18n_restore_dropped_rows(doc, prev)
        changed = _i18n_changed_canonical(doc, prev)

        # A translation-mode save leaked through the client.
        # Harvest, restore, continue - deliberately NOT frappe.throw: a throw rolls
        # back the transaction, which would also discard what the editor just typed.
        # That is the very failure this feature exists to prevent.
        if lang != "en":
            if changed:
                _i18n_reroute_to_translation(doc, lang, changed)
                _i18n_restore_canonical(doc, prev)
                doc.custom_i18n_last_incident = (
                    f"{frappe.utils.now()} {frappe.session.user} restored en; "
                    f"rerouted {sorted(changed)} -> {lang}"
                )
                frappe.msgprint(
                    frappe._("Your {0} edits were saved as a translation and the English copy was restored.").format(lang),
                    indicator="orange",
                )
                frappe.log_error(doc.custom_i18n_last_incident, "dsi_catalogue i18n_guard reroute")
            doc.custom_display_language = "en"
            stamp_snapshot(doc)
            return

        # A form save whose translation UI never loaded. Nothing to harvest, and no
        # language context to harvest it into, so refuse rather than guess.
        if from_form and not has_beacon and changed:
            frappe.throw(
                frappe._("The translation editor did not load. Reload this page (Ctrl+Shift+R) before editing product copy."),
                title=frappe._("Translation UI not loaded"),
            )

        # Non-form writes (REST, bench, n8n, import, sibling sync) are legitimate
        # English refreshes. Normalise the view flag and block the one case we can
        # detect with certainty.
        if not from_form:
            doc.custom_display_language = "en"
            if changed and _i18n_script_mismatch(changed):
                frappe.throw(
                    frappe._("Non-Latin text was written into an English field. Use the translation editor."),
                    title=frappe._("Wrong field for translated text"),
                )

        stamp_snapshot(doc)
    except frappe.ValidationError:
        raise
    except Exception:
        # A bug in the guard must not make Website Item unsaveable estate-wide.
        frappe.log_error(frappe.get_traceback(), "dsi_catalogue i18n_guard")


def _i18n_reroute_to_translation(doc, language, changed):
    """Move leaked edits into the right language row instead of dropping them."""
    from dsi_catalogue.i18n import TRANSLATED_FIELDS
    import json as _json

    row = None
    for r in doc.get("custom_translations") or []:
        if r.get("language") == language:
            row = r
            break
    if row is None:
        row = doc.append("custom_translations", {"language": language,
                                                 "review_status": "Machine (unreviewed)",
                                                 "source": "Human"})

    for field, (_old, new) in changed.items():
        if field == "website_specifications":
            row.specifications_json = _json.dumps(
                [{"idx": i + 1, "label": l, "description": d} for i, (l, d) in enumerate(new)],
                ensure_ascii=False,
            )
        elif field in TRANSLATED_FIELDS:
            row.set(TRANSLATED_FIELDS[field], new)


def i18n_guard_assert(doc, method=None):
    """Cheap idempotent re-check, FIRST in before_save.

    Catches anything in the validate stage that mutated a protected field after
    i18n_guard ran. Must precede publish_gate so the publish decision is taken
    against the restored document, and precede gallery_gender_stamp because that
    hook swallows its exceptions.
    """
    try:
        from dsi_catalogue.i18n import english_bundle, fingerprint

        if frappe.flags.get("dsi_variant_sync") or frappe.flags.get("dsi_i18n_write"):
            return
        if (doc.get("custom_display_language") or "en") != "en":
            doc.custom_display_language = "en"
        expected = doc.get("custom_en_fingerprint")
        actual = fingerprint(english_bundle(doc))
        if expected and actual != expected:
            # Legitimate when the user really is editing English; the snapshot is
            # re-stamped here so the two never drift silently.
            doc.custom_en_fingerprint = actual
            from dsi_catalogue.i18n import stamp_snapshot
            stamp_snapshot(doc)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "dsi_catalogue i18n_guard_assert")


def i18n_mark_stale(doc, method=None):
    """Flag translations authored against an older English. LAST in on_update.

    Runs last so it observes any sibling saves variant_content_sync already made.
    Writes with db.set_value on the child row - no parent re-save, no recursion.
    """
    try:
        current = doc.get("custom_en_fingerprint")
        if not current:
            return
        for row in doc.get("custom_translations") or []:
            stale = 1 if (row.get("base_en_fingerprint") or "") != current else 0
            if int(row.get("is_stale") or 0) != stale:
                frappe.db.set_value("Website Item Translated Copy", row.name,
                                    "is_stale", stale, update_modified=False)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "dsi_catalogue i18n_mark_stale")
