# dsi_catalogue

The product content and commerce bridge between ERPNext and the DSI website:
Drive/ERP catalogue sync, storefront aggregation, web orders, web leads,
authored translations, and delivery pricing (the Shipping Policy). Live on erp1
(`erp.shaik.net` in-container, served as `erp1.shaik.net`) — sole deployment
since 2026-09-11.

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
| `shipping_policy.py` + `shipping_policy_wizard.py`, on the pure `shipping_engine.py`, `shipping_formula.py`, `shipping_rate_card.py`, `shipping_policy_data.py`, `shipping_integration_check.py` | the Shipping Policy: what customers pay for delivery (zones, item sets, rules or the interim rate card, fallbacks), its two guest methods and the wizard's seven (see [Shipping Policy](#shipping-policy)) |

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

The Shipping Policy adds no key (its Test tab reaches the site's own API at
Frappe's standard `host_name`).

## Public API surface (guest)
Read-only, ungated: `get_published_website_items`, `get_website_item_by_index_key`,
`get_product_gallery`, `get_shop_filters`, `storefront.get_pdp_bundle`,
`storefront.get_shop_listing`, `shipping_policy.price_delivery`,
`shipping_policy.public_summary`.
Token-gated (`X-DSI-Token`): `sync_product_catalogue`, `receive_publish_callback`,
`receive_generation_callback`.
System Manager only: the seven `shipping_policy_wizard` methods (`wizard_state`,
`try_order`, `check_formula`, `preview_changes`, `apply_policy`, `import_rate_card`,
`test_integration`; `apply_policy`, `try_order` and `test_integration` take POST
only).
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

## Shipping Policy
What customers pay for delivery, set in the ERP (spec and plan in
`docs/superpowers/`). Its doctypes, System Manager only and version-tracked:
Shipping Zone (countries from the ERP's Country list, Not shipped, Everywhere
else, a line for customers, what to do when the carrier can't quote), Shipping
Item Set (item groups, with the groups beneath them, and items), and the single
Shipping Policy (Active, the price source, the rate card, Anything not covered,
the default fallback, and the rules: the first that holds wins, and a rule for an
item set holds only when every item in the order is in the set). The policy ships
**off**; while it is off, `price_delivery` returns the carrier's quotes exactly as
they were posted.

**The wizard.** The **Open the wizard** button on the Shipping Policy form opens
the page `/desk/shipping-policy-wizard` (System Manager only). Six tabs: Zones,
Rates, Rules, If the carrier can't quote, Review, and Test. It edits a draft kept
in the browser and writes nothing until **Apply** on Review, which writes the whole
draft in one transaction or nothing, and refuses when someone changed the policy
after the wizard loaded it. A Try an order tape beside every step prices a sample
order with the draft. Rates holds the price source: Carrier (the default: the
carrier's price, shaped by the rules; for now the website applies the policy only
with the rate card as the price source, so with Carrier customers still pay the
carrier's own prices, on or off, and the rules only show in Try an order) or Rate
card (the interim Shopify weight-tier card: customers pay from it and the carrier
is not asked, until the carrier integration is shown again). The card is imported
from the Shopify CSV export (the rates file and the countries not delivered,
chosen as files or pasted), shown with what it holds and what stops it, edited
there, and applied with the rest. Test ("Test the integration") runs three
read-only checks of what is live: the ERP's own `price_delivery` over HTTP, the
carrier, and the website named in Storefront Settings.

**Formulas.** A rule can charge a formula over `carrier_price`, `order_value`,
`weight_kg`, `volume_l`, `boxes` and `item_count`, with `min`, `max`, `round`,
`ceil`, `floor`, `abs`, arithmetic, comparisons, `and`/`or`/`not` and
`a if condition else b` (no power operator; parsed and whitelisted, never `eval`).
round() rounds halves away from zero (2.5 is 3, -2.5 is -3), like the cents
rounding. A formula that fails for an order sends it to the fallback, never to
0, and is noted in the Error Log as "Shipping policy formula error", at most once
per rule in 10 minutes.

**Guest methods**, read-only, read the policy afresh on every call (nothing is
cached): `price_delivery(country_code, items, boxes, quotes, language)` says
whether the policy is on, its price source, the zone, whether delivery is
offered, a reason code and a customer-facing message, and one offer per delivery
method (amount, source, reason, estimated days); `public_summary(language)` lists
the enabled zones with their countries and customer lines while the policy is on,
and nothing while it is off. Neither returns a rule, a formula, a trace or a line
of the rate card.

### Promotion
Push the live host's unpushed catalogue commits first (`6a39d3e` review approval
fix and `702a036` workspace rename; this branch starts from them), then
`bench migrate`: it adds the doctypes and the page, and the policy arrives **off
and empty**. This app, with `price_delivery`, goes on the live ERP **before** the
website change is pushed (the website falls back to the carrier path while the
method is missing). No new `site_config` keys, no new roles, no Custom DocPerm and
no customisation (Custom Field, Property Setter, Client or Server Script); the new
doctypes and the page carry their own System Manager permissions, and `hooks.py`
is unchanged. Then:
- [ ] Packing and the outbox, on a site that has `dsi_shipping.packing` and
  `dsi_core` (erp2 has neither; the native tests use stand-ins): Try an order with
  real items, and one Apply, which leaves an outbox row "website-revalidate"
  tagged "shipping-policy".
- [ ] The wizard's native tests on a replica with the live app set (erp1, the
  nightly replica): another app's `doc_events` committing inside `save` would
  break Apply's all-or-nothing. Record the live MariaDB version and
  `innodb_snapshot_isolation` (erp2: MariaDB 11.8.8, snapshot isolation on).
- [ ] On the live ERP, look for the `employee_self_service` catch-all hook (on
  erp2 it runs on every Error Log insert and can recurse) and nginx's body limit
  (erp2: 50 MB; the real export and a draft holding its card are well under
  100 KB).
- [ ] Before a policy goes on where rules are already stored, re-check every
  stored formula under the stricter checks (open the policy in the wizard and
  read Review). Neither erp2 nor the live ERP stores one today.
- [ ] Until the website is updated to apply rules over the carrier's quotes, only
  the Rate card price source changes what customers pay: with Carrier the website
  charges the carrier's own prices whether the policy is on or off (the wizard's
  Rates, Review and Test say so).
- [ ] After the website deploy, run the Test tab once on the live ERP with the
  rate card as the price source: its verdict should say the site uses the policy
  (until then it says the site still asks the carrier).

## Tests
Hermetic (no bench): `python3 -m pytest tests/hermetic -q`
Frappe-native: `bench --site <site> run-tests --app dsi_catalogue`
The Shipping Policy's five native modules (`--module
dsi_catalogue.dsi_catalogue.doctype.<module>`: `shipping_zone.test_shipping_zone`,
`shipping_item_set.test_shipping_item_set`, `shipping_policy.test_shipping_policy`,
`shipping_policy.test_shipping_policy_ratecard`,
`shipping_policy.test_shipping_policy_wizard`) reset the policy, and refuse to run
on a site that holds one (zones, item sets, rules, or the policy on): run them on
erp2, one at a time, after `tools/erp2_push.sh --restart`. The promotion proof
(policy off: the posted quotes come back unchanged, a partial quote among them) is
`TestGuestMethods.test_policy_off_returns_the_quotes_unchanged`.
Browser, erp2 only: `tests/browser/shipping_policy_wizard.mjs` (the command is in
its header; it needs a System Manager user on erp2).
