app_name = "dsi_catalogue"
app_title = "DSI Product Catalogue"
app_publisher = "DSI"
app_description = "Manage product catalogue sync and website publishing"
app_email = "dev@designershaik.com"
app_license = "MIT"

# Include JS/CSS globally
app_include_js = "/assets/dsi_catalogue/js/item_publish.js"
app_include_css = "/assets/dsi_catalogue/css/publish_modal.css"

# DocType JS - This is important for form scripts
doctype_js = {
    "Item": "public/js/item_publish.js"
}

# Whitelisted methods for API
# ---------------------------

# Document Events
# ---------------
doc_events = {
    "Website Item": {
        # Precompute decoded index-key fields (palace/range/product/slugs/grouping/
        # sibling) so the website reads columns instead of decoding 500 items per page.
        "validate": "dsi_catalogue.api.website_item_precompute",
    }
}

# Scheduled Tasks
# ---------------

# Fixtures
# --------
fixtures = [
    # Precompute custom fields on Website Item (decoded index-key columns).
    # Scoped to exactly our fields so re-export never touches custom_index_key etc.
    {
        "dt": "Custom Field",
        "filters": [["name", "in", [
            "Website Item-custom_decoded_section",
            "Website Item-custom_palace_code",
            "Website Item-custom_palace_slug",
            "Website Item-custom_range_code",
            "Website Item-custom_range_slug",
            "Website Item-custom_cb_decoded",
            "Website Item-custom_product_code",
            "Website Item-custom_product_slug",
            "Website Item-custom_variant_slug",
            "Website Item-custom_grouping_key",
            "Website Item-custom_selectable_variant_codes",
            "Website Item-custom_sibling_gender_slug",
            "Website Item-custom_is_template",
            "Website Item-custom_gallery_section",
            "Website Item-custom_gallery_images",
        ]]],
    }
]
