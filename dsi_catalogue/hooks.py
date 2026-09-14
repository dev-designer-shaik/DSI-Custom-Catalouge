app_name = "dsi_catalogue"
app_title = "DSI Product Catalogue"
app_publisher = "DSI"
app_description = "Manage product catalogue sync and website publishing"
app_email = "dev@designershaik.com"
app_license = "MIT"

# Shared plumbing (outbox, config contract, patrol, health).
required_apps = ["dsi_core"]

# dsi_core validates this schema at migrate + hourly + first read.
dsi_core_config_schema = "dsi_catalogue.config_schema.SCHEMA"

# Include JS/CSS globally
app_include_js = "/assets/dsi_catalogue/js/item_publish.js"
app_include_css = "/assets/dsi_catalogue/css/publish_modal.css"

# DocType JS - This is important for form scripts
doctype_js = {
	"Storefront Review": "public/js/storefront_review.js",
	"Item": "public/js/item_publish.js",
	# In-place language switching on the Website Item form. Lives in app code
	# (not a Client Script record) so it survives a rebuild, matching the
	# doctype it drives.
	"Website Item": "public/js/website_item_i18n.js",
}

# Whitelisted methods for API
# ---------------------------

# Document Events
# ---------------
doc_events = {
	"*": {"before_validate": "dsi_catalogue.website_item_review.guard_child", "on_trash": "dsi_catalogue.website_item_review.guard_child"},
	"Storefront Settings": {"before_validate": "dsi_catalogue.website_item_review.guard", "validate": "dsi_catalogue.storefront_review.validate_settings", "on_update": "dsi_catalogue.storefront_review.invalidate"},
	"Storefront Review": {"before_validate": "dsi_catalogue.website_item_review.guard", "on_trash": "dsi_catalogue.website_item_review.guard", "validate": "dsi_catalogue.storefront_review.validate_review", "on_update": "dsi_catalogue.storefront_review.review_updated"},
	"Website Item": {
		"on_trash": "dsi_catalogue.website_item_review.guard",
		# Authored translations (2026-08-29). before_validate is the PRIMARY seat:
		# run_before_save_methods runs before_validate BEFORE the
		# flags.ignore_validate early return, so validate/before_save are both
		# skippable with one flag and this is not. Keeps canonical English English.
		"before_validate": ["dsi_catalogue.website_item_review.guard", "dsi_catalogue.website_item_events.i18n_guard", "dsi_catalogue.website_item_review.stage"],
		# Precompute decoded index-key fields (palace/range/product/slugs/grouping/
		# sibling) so the website reads columns instead of decoding 500 items per page.
		"validate": "dsi_catalogue.api.website_item_precompute",
		# On-demand storefront cache invalidation (no-op unless website_revalidate_url
		# + website_revalidate_secret are set in site_config).
		"on_update": [
			"dsi_catalogue.api.notify_revalidate",
			# Ported from Server Script "Website Item Variant Content Sync" (After Save), 2026-08-06.
			# Shared edits require individual Website Item approval.
			# LAST: must observe the sibling saves variant_content_sync just made.
			"dsi_catalogue.website_item_events.i18n_mark_stale",
		],
		# Ported from Server Scripts (Before Save), 2026-08-06 - see website_item_events.py.
		"before_save": [
			# FIRST: catches anything in the validate stage that touched a
			# protected field. Must precede publish_gate so the publish decision
			# is taken against the restored doc, and precede gallery_gender_stamp
			# because that hook swallows its own exceptions.
			"dsi_catalogue.website_item_events.i18n_guard_assert",
			"dsi_catalogue.website_item_events.publish_gate",
			"dsi_catalogue.website_item_events.gallery_gender_stamp",
		],
	}
}

# Scheduled Tasks
# ---------------

# Fixtures
# --------
fixtures = [
	{
		# Lead custom fields for the web-leads sync (abandoned carts +
		# inquiries land as Leads; see dsi_catalogue/leads.py).
		"dt": "Custom Field",
		"filters": [
			[
				"name",
				"in",
				[
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
				],
			]
		],
	},
	# Precompute custom fields on Website Item (decoded index-key columns).
	# Scoped to exactly our fields so re-export never touches custom_index_key etc.
	{
		"dt": "Custom Field",
		"filters": [
			[
				"name",
				"in",
				[
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
				],
			]
		],
	},
]

has_permission = {dt: "dsi_catalogue.website_item_review.has_permission" for dt in ("Website Item", "Storefront Review", "Storefront Settings")}

override_doctype_class = {"Website Item": "dsi_catalogue.website_item_controller.ReviewedWebsiteItem"}
