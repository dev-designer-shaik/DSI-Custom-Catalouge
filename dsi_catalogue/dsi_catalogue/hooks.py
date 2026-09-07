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
        # On-demand storefront cache invalidation (no-op unless website_revalidate_url
        # + website_revalidate_secret are set in site_config).
        "on_update": "dsi_catalogue.api.notify_revalidate",
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
        # Lead custom fields for the web-leads sync (abandoned carts +
        # inquiries land as Leads; see dsi_catalogue/leads.py).
        "dt": "Custom Field",
        "filters": [["name", "in", [
            "Lead-data_source",
            "Lead-cart_value",
            "Lead-cart_items_json",
            "Lead-cart_item_count",
            "Lead-cart_first_added",
            "Lead-cart_last_activity",
            "Lead-typed_address_json",
            "Lead-website_user_id",
            "Lead-guest_session_id",
            "Lead-recovery_token",
            "Lead-recovered",
            "Lead-recovered_order",
            "Lead-linked_customer",
            "Lead-marketing_consent",
            "Lead-inquiry_message",
        ]]],
    },
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
            "Sales Order-custom_idempotency_key",
        ]]],
    }
]
