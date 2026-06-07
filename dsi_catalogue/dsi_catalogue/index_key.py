"""
Server-side port of the website's lib/erpnext/index-key-decoder.ts.

Single source of truth for the code maps is index_key_maps.json (mirrored to the
website at lib/erpnext/index_key_maps.json; a parity test keeps them identical).
Pure functions — no Frappe import — so they can be unit-tested standalone and
reused by api.py / doc_event hooks to precompute fields on Website Item.
"""
import json
import os
import re

_MAPS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index_key_maps.json")

with open(_MAPS_PATH, "r", encoding="utf-8") as _f:
    _M = json.load(_f)

PALACE_MAP = _M["palace_map"]
RANGE_MAP = _M["range_map"]
GENDER_IDENTITY_CODES = set(_M["gender_identity_codes"])
PRODUCT_TYPE_CODES = set(_M["product_type_codes"])
WATCH_PRODUCT_CODES = set(_M["watch_product_codes"])
SIZE_CODES_FOR_TYPE = set(_M["size_codes_for_type"])

# Merge sub-groups in the exact order the TS file spreads them.
PRODUCT_CODES = {}
for _grp in _M["_merge_order"]["product_codes"]:
    PRODUCT_CODES.update(_M["products"][_grp])

VARIANT_CODES = {}
for _grp in _M["_merge_order"]["variant_codes"]:
    VARIANT_CODES.update(_M["variants"][_grp])

# Sub-group key sets for get_variant_type()
_WATCH_KEYS = set(_M["variants"]["watch"].keys())
_PEN_KEYS = set(_M["variants"]["pen"].keys())
_STYLE_KEYS = set(_M["variants"]["style"].keys())
_GIFT_SET_VARIANT_KEYS = set(_M["variants"]["gift_set"].keys())


# ---------------------------------------------------------------- helpers
def is_watch_product_code(code):
    return code in WATCH_PRODUCT_CODES


def is_strap_product_code(code):
    return code in ("LS", "RS")


def get_accessory_material_code(product_code):
    if product_code == "LS" or product_code.startswith("STRAP-L"):
        return "LEA"
    if product_code == "RS" or product_code.startswith("STRAP-R"):
        return "RUB"
    return None


def is_gender_identity_code(code):
    return code in GENDER_IDENTITY_CODES


def get_product_name(code):
    return PRODUCT_CODES.get(code, code)


def get_variant_name(code):
    return VARIANT_CODES.get(code, code)


def slugify(text):
    text = (text or "").lower()
    text = re.sub(r"['‘’]", "", text)   # remove straight + curly apostrophes
    text = re.sub(r"[^a-z0-9]+", "-", text)        # non-alphanumeric runs -> hyphen
    text = re.sub(r"(^-|-$)", "", text)            # trim leading/trailing hyphen
    return text


# ---------------------------------------------------------------- decode
def decode_index_key(index_key):
    """Decode '{F-CR-AK-AC}' -> dict mirroring DecodedIndexKey, or None."""
    if not index_key:
        return None

    raw = index_key
    clean_key = re.sub(r"[{}]", "", index_key).strip()
    parts = clean_key.split("-")
    if len(parts) < 2:
        return None

    palace_code = parts[0]
    palace = PALACE_MAP.get(palace_code)
    if not palace:
        return None

    rng = None
    product_start = 1
    if len(parts) > 1 and parts[1] in RANGE_MAP:
        rng = RANGE_MAP[parts[1]]
        product_start = 2

    product_code = ""
    product_end = product_start

    # compound code first (e.g. ODA-R, PS-NRE)
    if len(parts) > product_start + 1:
        compound = "{0}-{1}".format(parts[product_start], parts[product_start + 1])
        if compound in PRODUCT_CODES:
            product_code = compound
            product_end = product_start + 2

    # single code
    if not product_code and product_start < len(parts):
        single = parts[product_start]
        if single in PRODUCT_CODES:
            product_code = single
            product_end = product_start + 1

    # raw fallback
    if not product_code and product_start < len(parts):
        product_code = parts[product_start]
        product_end = product_start + 1

    variants = parts[product_end:]
    variant_names = [VARIANT_CODES.get(v, v) for v in variants]

    return {
        "palace": palace,
        "range": rng,
        "productCode": product_code,
        "productName": PRODUCT_CODES.get(product_code, product_code),
        "variants": variants,
        "variantNames": variant_names,
        "isTemplate": len(variants) == 0,
        "raw": raw,
    }


# ---------------------------------------------------------------- slugs / keys
def generate_product_slug(decoded):
    slug = slugify(decoded["productName"])
    variants = decoded["variants"]
    if variants:
        first = variants[0]
        if first in GENDER_IDENTITY_CODES:
            slug += "-" + first.lower()
        elif is_watch_product_code(decoded["productCode"]):
            colour_slug = slugify(" ".join(decoded["variantNames"]))
            if colour_slug:
                slug += "-" + colour_slug
    return slug


def generate_variant_slug(variants):
    if not variants:
        return ""
    names = [VARIANT_CODES.get(v, v) for v in variants]
    return slugify(" ".join(names))


def build_index_key(palace_code, range_code, product_code, variants=None):
    parts = [palace_code]
    if range_code:
        parts.append(range_code)
    parts.append(product_code)
    parts.extend(variants or [])
    return "{" + "-".join(parts) + "}"


def get_template_index_key(index_key):
    d = decode_index_key(index_key)
    if not d:
        return None
    return build_index_key(d["palace"]["code"], (d["range"] or {}).get("code"), d["productCode"], [])


def get_template_index_key_for_product(index_key):
    d = decode_index_key(index_key)
    if not d:
        return None
    template_variants = [v for v in d["variants"] if v in GENDER_IDENTITY_CODES]
    return build_index_key(d["palace"]["code"], (d["range"] or {}).get("code"), d["productCode"], template_variants)


def get_product_grouping_key(index_key):
    d = decode_index_key(index_key)
    if not d:
        return None
    identity = d["variants"] if is_watch_product_code(d["productCode"]) else []
    return build_index_key(d["palace"]["code"], (d["range"] or {}).get("code"), d["productCode"], identity)


def get_non_gender_variant_codes(variants):
    return [v for v in variants if not is_gender_identity_code(v)]


def get_selectable_variant_codes(decoded):
    if is_watch_product_code(decoded["productCode"]):
        return []
    return [v for v in decoded["variants"] if not is_gender_identity_code(v)]


def get_sibling_gender_product(index_key):
    d = decode_index_key(index_key)
    if not d:
        return None
    gender_index = next((i for i, v in enumerate(d["variants"]) if v in GENDER_IDENTITY_CODES), -1)
    if gender_index == -1:
        return None
    current = d["variants"][gender_index]
    other = "W" if current == "M" else "M"
    sibling_variants = list(d["variants"])
    sibling_variants[gender_index] = other
    sibling = dict(d)
    sibling["variants"] = sibling_variants
    return {
        "code": other,
        "gender": "Men" if other == "M" else "Women",
        "slug": generate_product_slug(sibling),
    }


def get_variant_type(code):
    if code in GENDER_IDENTITY_CODES:
        return "gender"
    if code in PRODUCT_TYPE_CODES:
        return "product_subtype"
    if code in SIZE_CODES_FOR_TYPE:
        return "size"
    if code in _WATCH_KEYS or code in _PEN_KEYS:
        return "material"
    if code in _STYLE_KEYS:
        return "style"
    if code in _GIFT_SET_VARIANT_KEYS:
        return "gift_set"
    return "unknown"


def get_palace_by_slug(slug):
    for p in PALACE_MAP.values():
        if p["slug"] == slug:
            return p
    return None


def get_range_by_slug(slug):
    for r in RANGE_MAP.values():
        if r["slug"] == slug:
            return r
    return None


def decode_for_website_item(index_key):
    """Convenience: decoded -> flat dict of the precomputed Website Item fields."""
    d = decode_index_key(index_key)
    if not d:
        return {}
    sibling = get_sibling_gender_product(index_key)
    return {
        "custom_palace_code": d["palace"]["code"],
        "custom_palace_slug": d["palace"]["slug"],
        "custom_range_code": (d["range"] or {}).get("code") or "",
        "custom_range_slug": (d["range"] or {}).get("slug") or "",
        "custom_product_code": d["productCode"],
        "custom_product_slug": generate_product_slug(d),
        "custom_variant_slug": generate_variant_slug(get_selectable_variant_codes(d)),
        "custom_grouping_key": get_product_grouping_key(index_key) or "",
        "custom_selectable_variant_codes": json.dumps(get_selectable_variant_codes(d)),
        "custom_sibling_gender_slug": (sibling or {}).get("slug") or "",
        "custom_is_template": 1 if d["isTemplate"] else 0,
    }
