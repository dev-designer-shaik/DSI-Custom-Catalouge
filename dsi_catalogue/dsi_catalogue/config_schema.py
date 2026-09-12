"""dsi_catalogue's site_config contract, declared once and enforced by dsi_core."""

from dsi_core.config_schema import Key

SCHEMA: tuple[Key, ...] = (
	Key("n8n_webhook_url", "url", description="base URL of the n8n instance holding the publish pipeline"),
	Key("dsi_pipeline_token", "secret", description="X-DSI-Token on the three guest n8n callbacks"),
	Key(
		"website_revalidate_url",
		"url",
		required=False,
		description="set WITH website_revalidate_secret to enable durable storefront cache invalidation",
	),
	Key(
		"website_revalidate_secret", "secret", required=False, description="pair with website_revalidate_url"
	),
	Key(
		"erp_company",
		"doc_link",
		required=False,
		link_doctype="Company",
		description="web-order SO provisioning",
	),
	Key(
		"erp_customer_group",
		"doc_link",
		required=False,
		link_doctype="Customer Group",
		description="web-order customer provisioning",
	),
	Key(
		"tap_mode_of_payment",
		"doc_link",
		required=False,
		link_doctype="Mode of Payment",
		description="set WITH tap_receiving_account to record captured Tap charges as Payment Entries",
	),
	Key(
		"tap_receiving_account",
		"doc_link",
		required=False,
		link_doctype="Account",
		description="pair with tap_mode_of_payment",
	),
)
