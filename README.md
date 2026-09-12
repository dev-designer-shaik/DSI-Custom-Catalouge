# dsi_catalogue

The product content and commerce bridge between ERPNext and the DSI website:
Drive/ERP catalogue sync, storefront aggregation, web orders, web leads, and
authored translations. Live on erp1 (`erp.shaik.net` in-container, served as
`erp1.shaik.net`) — sole deployment since 2026-09-11.

## Modules
| module | what it owns |
|---|---|
| `api.py` | catalogue sync + publish pipeline (n8n request-response), precompute, gallery, shop filters |
| `storefront.py` | guest read endpoints: PDP bundle + shop listing (one DB-local call per page) |
| `orders.py` | `create_order_atomic` — transactional idempotent SO + customer/address provisioning + Payment Entry |
| `leads.py` | `capture_web_lead` — abandoned carts + inquiries → Leads (one per identity) |
| `index_key.py` | the `{P-XX-YYY-VV}` index-key decoder (pure; parity-tested against the website decoder) |
| `i18n.py` + `website_item_events.py` | authored translations, variant content sync, publish gate, gallery gender stamp |
| `data_completion.py` | L2↔L3 content fill engine (dry-run by default) |
| `maintenance.py` | bench-executable one-offs (`backfill_precompute_fields`) |

## Install
```bash
bench get-app https://github.com/dev-designer-shaik/DSI-Custom-Catalouge dsi_catalogue
bench --site <site> install-app dsi_catalogue
bench --site <site> migrate   # fixtures: precompute + Lead custom fields
```

## site_config keys
| key | required | gates |
|---|---|---|
| `n8n_webhook_url` | yes (no default) | base URL for publish/generation trigger posts |
| `dsi_pipeline_token` | yes | `X-DSI-Token` on the three guest n8n callbacks |
| `website_revalidate_url` + `website_revalidate_secret` | revalidate pair | storefront cache invalidation on WI update |
| `erp_company` / `erp_customer_group` | order path | SO provisioning |
| `tap_mode_of_payment` / `tap_receiving_account` | payment pair | Payment Entry on captured Tap charges |

## Public API surface (guest)
Read-only, ungated: `get_published_website_items`, `get_website_item_by_index_key`,
`get_product_gallery`, `get_shop_filters`, `storefront.get_pdp_bundle`,
`storefront.get_shop_listing`.
Token-gated (`X-DSI-Token`): `sync_product_catalogue`, `receive_publish_callback`,
`receive_generation_callback`.
Session-required: everything else (publish flow, `create_order_atomic`,
`capture_web_lead`, gallery write, data_completion).

## Document events (Website Item)
`before_validate` i18n_guard → `validate` precompute → `before_save`
[i18n_guard_assert, publish_gate, gallery_gender_stamp] → `on_update`
[notify_revalidate, variant_content_sync, i18n_mark_stale].
Order matters; comments in `hooks.py` say why.

## Grouping rule (load-bearing)
Group membership is `custom_grouping_key` EQUALITY, never an index-key prefix
(`{P-AQ-AD2-DS}` and `{P-AQ-AD2-DSS}` are two products). Every collection path
must go through `index_key.get_product_grouping_key`.

## Tests
Hermetic (no bench): `python3 -m pytest tests/hermetic -q`
Frappe-native: `bench --site <site> run-tests --app dsi_catalogue`
