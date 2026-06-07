"""
Bidirectional data-completion engine: Drive/Product Catalogue (L2) <-> Website Item (L3).

Join key across every layer is the index_key / grouping_key (see index_key.py).

Forward fill  (Catalogue -> Website Item): media auto-applies (real photography from
              Drive/Cloudinary); descriptions are seeded as DRAFTS for human review.
Backward fill (Website Item -> Catalogue.ai_metadata): curated copy flows upstream so
              the repository becomes the complete source of truth.

Everything is idempotent and dry-run by default. A Completeness Ledger reports, per
product, which fields are present at L2 vs L3 and the recommended fill direction.

NOTE: the L0<->L2 legs (n8n Drive crawler coverage + Drive product_manifest.yaml
write-back) live in n8n on the Mumbai host and are driven via the existing allow_guest
callbacks; this module is the ERP-side brain + the L2<->L3 fills.
"""
import frappe
import json
import re

from dsi_catalogue import index_key as ik

# Fields we track for completeness at the Website Item (L3) layer.
TEXT_FIELDS = ("web_long_description", "short_description", "website_content")


# ----------------------------------------------------------------- helpers
def _load(v):
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v or "null")
    except (ValueError, TypeError):
        return None


def _txt(v):
    if not v:
        return ""
    return re.sub(r"<[^>]+>", " ", str(v)).replace("&nbsp;", " ").strip()


def _pick_hero(imgs, index_key, grouping):
    """Choose the hero for a specific variant Website Item: prefer a flagged hero
    among images matching the item's gender (by variant_code token or filename hint),
    so a Women's item never stores a Men's hero from the shared grouping."""
    d = ik.decode_index_key(index_key or "")
    gender = None
    if d:
        for v in d["variants"]:
            if v in ("M", "W"):
                gender = v
                break

    def gender_ok(img):
        if not gender:
            return True
        toks = (img.get("variant_code") or "").split("-")
        if gender in toks:
            return True
        fn = (img.get("file_name") or "").lower()
        if gender == "M" and any(t in fn for t in ("man", "_men", "male", "for_men")):
            return True
        if gender == "W" and any(t in fn for t in ("woman", "women", "lady", "female", "for_women")):
            return True
        return False

    cands = [i for i in imgs if gender_ok(i)] or imgs
    return next((i for i in cands if i["is_hero"]), cands[0])


def _variant_from_keys(folder_key, grouping):
    """variant_code = the folder's index_key beyond the grouping prefix (e.g. folder
    {F-CR-OSA-M-100} under grouping {F-CR-OSA} -> 'M-100')."""
    fk = (folder_key or "").strip("{}")
    gk = (grouping or "").strip("{}")
    if fk.startswith(gk) and len(fk) > len(gk):
        return fk[len(gk):].lstrip("-")
    return ""


# ----------------------------------------------------------------- L2 read
def catalogue_images_for_grouping(grouping):
    """Aggregate Product Catalogue cloudinary_images across every folder under a
    grouping, mapped to the gallery-row shape. variant_code derived per source folder.
    Returned hero-first then quality-desc, with rank assigned by final order."""
    if not grouping:
        return []
    like = grouping.rstrip("}")
    cats = frappe.get_all(
        "Product Catalogue",
        filters=[["index_key", "like", like + "%"]],
        fields=["index_key", "hero_image", "cloudinary_images"],
    )
    rows, seen = [], set()
    for c in cats:
        vcode = _variant_from_keys(c.index_key, grouping)
        hero_url = (c.hero_image or "").strip()
        imgs = _load(c.cloudinary_images) or []
        if not isinstance(imgs, list):
            continue
        for im in imgs:
            if not isinstance(im, dict):
                continue
            url = im.get("url") or im.get("web_view_url")
            if not url or url in seen:
                continue
            seen.add(url)
            # Catalogue tracks the hero in the separate hero_image field, not via an
            # isHero flag on each image — honour both.
            is_hero = 1 if (im.get("isHero") or (hero_url and url == hero_url)) else 0
            rows.append({
                "image": url,
                "file_name": im.get("fileName") or url.split("/")[-1],
                "alt_text": im.get("fileName") or "",
                "shared_type": im.get("sharedType") or ("variant" if vcode else "inclusive"),
                "variant_code": vcode,
                "is_hero": is_hero,
                "_q": im.get("qualityScore") or 0,
            })
    rows.sort(key=lambda r: (0 if r["is_hero"] else 1, -(r["_q"] or 0)))
    for i, r in enumerate(rows):
        r["rank"] = i
        r.pop("_q", None)
    return rows


def catalogue_ai_metadata_for_grouping(grouping):
    """Return the first non-empty ai_metadata dict under a grouping (image-analysis
    block: description / colorPalette / marketingTags / luxuryFactors)."""
    if not grouping:
        return None
    like = grouping.rstrip("}")
    cats = frappe.get_all(
        "Product Catalogue",
        filters=[["index_key", "like", like + "%"], ["ai_metadata", "is", "set"]],
        fields=["ai_metadata"],
    )
    for c in cats:
        md = _load(c.ai_metadata)
        if isinstance(md, dict) and md:
            return md
    return None


# ----------------------------------------------------------------- completeness
def _wi_status(wi, cat_imgs, cat_md):
    """Per-product field status across L2 (Catalogue) and L3 (Website Item)."""
    spec = frappe.db.count("Item Website Specification", {"parent": wi["name"]})
    gal = frappe.db.count("Website Item Gallery Image", {"parent": wi["name"]})
    l3 = {
        "hero": bool(wi.get("website_image")),
        "gallery": gal,
        "web_long_description": bool(_txt(wi.get("web_long_description"))),
        "short_description": bool(_txt(wi.get("short_description"))),
        "website_content": bool(_txt(wi.get("website_content"))),
        "alt": bool(_txt(wi.get("website_image_alt"))),
        "specs": spec > 0,
    }
    l2 = {
        "images": len(cat_imgs),
        "ai_metadata": bool(cat_md),
        "ai_description": bool(cat_md and cat_md.get("description")),
    }
    actions = []
    if not l3["hero"] and l2["images"]:
        actions.append("forward:hero")
    if l3["gallery"] == 0 and l2["images"]:
        actions.append("forward:gallery")
    if not l3["website_content"] and l2["ai_description"]:
        actions.append("forward:content_draft")
    for f in ("web_long_description", "short_description", "website_content"):
        if not l3[f] and not l2["ai_metadata"]:
            actions.append("ai_draft:%s" % f)
    if not l3["specs"]:
        actions.append("ai_draft:specs" if not l2["ai_metadata"] else "review:specs")
    # backward: curated text on L3 not represented at L2
    if (l3["website_content"] or l3["specs"]) and not l2["ai_metadata"]:
        actions.append("backward:curated->catalogue")
    return l2, l3, sorted(set(actions))


@frappe.whitelist()
def completeness_report(published_only=1):
    """The Ledger: every (published) Website Item with its L2/L3 field status and the
    recommended fill actions. Read-only."""
    filters = {"published": 1} if int(published_only) else {}
    wis = frappe.get_all(
        "Website Item", filters=filters,
        fields=["name", "item_code", "web_item_name", "custom_index_key",
                "custom_grouping_key", "custom_palace_slug", "website_image",
                "website_image_alt", "web_long_description", "short_description",
                "website_content"],
        order_by="custom_palace_slug, custom_grouping_key",
    )
    out, summary = [], {}
    for wi in wis:
        grp = wi.get("custom_grouping_key") or wi.get("custom_index_key")
        cat_imgs = catalogue_images_for_grouping(grp)
        cat_md = catalogue_ai_metadata_for_grouping(grp)
        l2, l3, actions = _wi_status(wi, cat_imgs, cat_md)
        for a in actions:
            summary[a] = summary.get(a, 0) + 1
        out.append({"item_code": wi["item_code"], "title": wi.get("web_item_name"),
                    "grouping": grp, "palace": wi.get("custom_palace_slug"),
                    "l2": l2, "l3": l3, "actions": actions})
    return {"count": len(out), "summary": summary, "items": out}


# ----------------------------------------------------------------- forward fill
@frappe.whitelist()
def forward_fill_media(item_code=None, grouping=None, dry_run=1):
    """Catalogue -> Website Item (MEDIA only; auto-apply). Sets website_image (hero)
    when empty and (re)builds custom_gallery_images from the Drive/Catalogue images.
    Idempotent. website_image is written via db.set_value to avoid webshop on_update."""
    dry_run = int(dry_run)
    wi = frappe.db.get_value(
        "Website Item", {"item_code": item_code} if item_code else {"name": grouping},
        ["name", "custom_grouping_key", "custom_index_key", "website_image"], as_dict=True,
    ) if (item_code or grouping) else None
    if not wi:
        frappe.throw("Website Item not found for %s" % (item_code or grouping))
    grp = grouping or wi.custom_grouping_key or wi.custom_index_key
    imgs = catalogue_images_for_grouping(grp)
    result = {"item_code": item_code, "grouping": grp, "catalogue_images": len(imgs),
              "changes": [], "applied": False}
    if not imgs:
        return result
    # Variant-aware hero so website_image matches the item's own gender.
    hero = _pick_hero(imgs, wi.custom_index_key, grp)
    if not any(i["is_hero"] for i in imgs):
        hero["is_hero"] = 1
    if not wi.website_image:
        result["changes"].append({"field": "website_image", "to": hero["image"]})
    cur_gal = frappe.db.count("Website Item Gallery Image", {"parent": wi.name})
    result["changes"].append({"field": "custom_gallery_images",
                              "from_count": cur_gal, "to_count": len(imgs)})
    if not dry_run:
        if not wi.website_image:
            frappe.db.set_value("Website Item", wi.name, "website_image",
                                hero["image"], update_modified=False)
        from dsi_catalogue.api import save_website_item_gallery
        save_website_item_gallery(website_item=wi.name, gallery=json.dumps(imgs))
        frappe.cache().delete_value("dsi_shop_filters")
        result["applied"] = True
    return result


@frappe.whitelist()
def forward_fill_media_bulk(dry_run=1, palace=None):
    """Run forward_fill_media across all published Website Items that need media."""
    dry_run = int(dry_run)
    filters = {"published": 1}
    if palace:
        filters["custom_palace_slug"] = palace
    wis = frappe.get_all("Website Item", filters=filters, fields=["item_code"])
    done, touched = [], 0
    for w in wis:
        r = forward_fill_media(item_code=w["item_code"], dry_run=dry_run)
        if r.get("changes"):
            touched += 1
            done.append({"item_code": w["item_code"], "imgs": r["catalogue_images"],
                         "changes": [c["field"] for c in r["changes"]]})
    return {"scanned": len(wis), "with_changes": touched, "dry_run": bool(dry_run),
            "items": done}
