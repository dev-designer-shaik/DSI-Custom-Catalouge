"""
Storefront aggregation endpoints — collapse the website's N per-page ERP round-trips
into ONE DB-local call. The website keeps its tested assembly logic; these just feed it
a pre-joined bundle.

- get_pdp_bundle(palace, slug | index_key): all Website Items in the product grouping +
  batched price (Item Price) + batched stock (Bin) + ranked gallery + sibling-gender, in
  one response. Replaces getProductWithAllVariants's getWebsiteItem(xN, 2 calls each) +
  per-variant price/stock + gallery fan-out.
- get_shop_listing(palace): the shop grid from precomputed columns + batched price, plus
  the cached facet counts. Replaces the 500-row fetchAllPublishedWebsiteItems pull +
  client-side decode + listShopProductVariants.

All reads, guest-allowed, idempotent.
"""
import frappe
import json
import re

from dsi_catalogue import index_key as ik

PRICE_LIST = "USD - Online"

_WI_FIELDS = [
    "name", "item_code", "item_name", "web_item_name", "website_image", "website_image_alt",
    "short_description", "web_long_description", "item_group", "route", "slideshow",
    "website_content", "custom_index_key", "custom_repository_path",
    "custom_palace_code", "custom_palace_slug", "custom_range_code", "custom_range_slug",
    "custom_product_code", "custom_product_slug", "custom_variant_slug", "custom_grouping_key",
    "custom_selectable_variant_codes", "custom_sibling_gender_slug", "custom_is_template",
]


# ----------------------------------------------------------------- batched lookups
def _prices_for(item_codes):
    if not item_codes:
        return {}
    rows = frappe.get_all(
        "Item Price",
        filters=[["item_code", "in", item_codes], ["selling", "=", 1], ["price_list", "=", PRICE_LIST]],
        fields=["item_code", "price_list_rate", "currency"],
    )
    out = {}
    for r in rows:
        if r.item_code not in out:
            out[r.item_code] = {"price": r.price_list_rate, "currency": r.currency or "USD"}
    return out


def _stock_for(item_codes):
    if not item_codes:
        return {}
    rows = frappe.get_all("Bin", filters=[["item_code", "in", item_codes]],
                          fields=["item_code", "actual_qty"])
    agg = {}
    for r in rows:
        agg[r.item_code] = agg.get(r.item_code, 0) + (r.actual_qty or 0)
    return {ic: {"stock": q, "available": q > 0} for ic, q in agg.items()}


def _specs_for(wi_names):
    out = {n: [] for n in wi_names}
    if wi_names:
        for s in frappe.get_all(
            "Item Website Specification",
            filters=[["parent", "in", wi_names]],
            fields=["parent", "label", "description"], order_by="idx asc",
        ):
            out.setdefault(s.parent, []).append({"label": s.label, "value": s.description})
    return out


# ----------------------------------------------------------------- PDP bundle
@frappe.whitelist(allow_guest=True)
def get_pdp_bundle(palace=None, slug=None, index_key=None):
    """One call: every Website Item in the product grouping, each enriched with price,
    stock, and specifications (all batched), plus the ranked gallery and sibling-gender."""
    grouping = None
    if index_key:
        grouping = ik.get_product_grouping_key(index_key) or ik.get_template_index_key(index_key) or index_key
    elif palace and slug:
        cand = frappe.get_all(
            "Website Item",
            filters={"published": 1, "custom_palace_slug": palace, "custom_product_slug": slug},
            fields=["custom_index_key", "custom_grouping_key"], limit_page_length=1,
        )
        if cand:
            grouping = cand[0].get("custom_grouping_key") or cand[0].get("custom_index_key")
    if not grouping:
        return {"grouping": None, "items": [], "gallery": [], "sibling_gender": None, "price_list": PRICE_LIST}

    like = grouping.rstrip("}")
    items = frappe.get_all(
        "Website Item",
        filters=[["published", "=", 1], ["custom_index_key", "like", like + "%"]],
        fields=_WI_FIELDS, order_by="custom_index_key asc",
    )
    codes = [i.item_code for i in items if i.get("item_code")]
    names = [i.name for i in items]
    prices, stock, specs = _prices_for(codes), _stock_for(codes), _specs_for(names)
    for it in items:
        p = prices.get(it.item_code, {})
        s = stock.get(it.item_code, {})
        it["price"] = p.get("price")
        it["currency"] = p.get("currency", "USD")
        it["stock"] = s.get("stock", 0)
        it["available"] = bool(s.get("available", False))
        it["specifications"] = specs.get(it.name, [])

    from dsi_catalogue.api import get_product_gallery
    gallery = get_product_gallery(index_key=grouping).get("gallery_images", [])
    sib = ik.get_sibling_gender_product(items[0].custom_index_key) if items else None
    return {"grouping": grouping, "items": items, "gallery": gallery,
            "sibling_gender": sib, "price_list": PRICE_LIST}


# ----------------------------------------------------------------- shop listing
@frappe.whitelist(allow_guest=True)
def get_shop_listing(palace=None):
    """The shop grid in one call: one card per published non-refill Website Item, built
    from precomputed columns, with batched price + facet tags + the cached filter counts."""
    filters = [["published", "=", 1]]
    palace_code = None
    if palace:
        filters.append(["custom_palace_slug", "=", palace])
        pp = ik.get_palace_by_slug(palace)
        palace_code = pp["code"] if pp else palace
    items = frappe.get_all(
        "Website Item", filters=filters,
        fields=["item_code", "web_item_name", "website_image", "website_image_alt",
                "custom_index_key", "custom_palace_code", "custom_palace_slug",
                "custom_range_code", "custom_range_slug", "custom_product_slug",
                "custom_grouping_key", "custom_selectable_variant_codes"],
        order_by="custom_palace_slug, custom_grouping_key",
    )
    items = [i for i in items if not re.search(r"-RF[-}]", i.get("custom_index_key") or "")]
    codes = [i.item_code for i in items if i.get("item_code")]
    prices = _prices_for(codes)

    cards = []
    for it in items:
        d = ik.decode_index_key(it.get("custom_index_key") or "")
        p = prices.get(it.item_code, {})
        tags = []
        if d:
            tags.append(d["palace"]["code"])
            if d["range"]:
                tags.append(d["range"]["code"])
            tags.extend(d["variants"])
            mat = ik.get_accessory_material_code(d["productCode"])
            if mat:
                tags.append(mat)
        try:
            vcodes = json.loads(it.get("custom_selectable_variant_codes") or "[]")
        except (ValueError, TypeError):
            vcodes = []
        cards.append({
            "itemCode": it.item_code,
            "name": it.get("web_item_name"),
            "image": it.get("website_image") or "",
            "imageAlt": it.get("website_image_alt") or "",
            "indexKey": it.get("custom_index_key"),
            "palace": it.get("custom_palace_slug"),
            "palaceCode": it.get("custom_palace_code"),
            "range": it.get("custom_range_slug"),
            "rangeCode": it.get("custom_range_code"),
            "slug": it.get("custom_product_slug"),
            "grouping": it.get("custom_grouping_key"),
            "price": p.get("price"),
            "currency": p.get("currency", "USD"),
            "variantCodes": vcodes,
            "filterTags": tags,
        })

    from dsi_catalogue.api import get_shop_filters
    return {"products": cards, "filters": get_shop_filters(palace=palace_code), "count": len(cards)}
