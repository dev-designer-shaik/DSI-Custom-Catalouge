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
| `shipping_policy.py` + `shipping_policy_wizard.py`, on the pure `shipping_engine.py`, `shipping_formula.py`, `shipping_shopify_import.py`, `shipping_policy_data.py`, `shipping_integration_check.py` | the Shipping Policy: what customers pay for delivery (one page of product groups with delivery rows, provider services, weight tables, formulas, one fallback), its three guest methods and the page's nine (see [Shipping Policy](#shipping-policy)) |

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

The Shipping Policy adds no key (its Check it works reaches the site's own API at
Frappe's standard `host_name`).

## Public API surface (guest)
Read-only, ungated: `get_published_website_items`, `get_website_item_by_index_key`,
`get_product_gallery`, `get_shop_filters`, `storefront.get_pdp_bundle`,
`storefront.get_shop_listing`, `shipping_policy.price_delivery`,
`shipping_policy.public_summary`, `shipping_policy.delivery_services`.
Token-gated (`X-DSI-Token`): `sync_product_catalogue`, `receive_publish_callback`,
`receive_generation_callback`.
System Manager or Website Manager only: the nine `shipping_policy_wizard` methods (`wizard_state`,
`search_products`, `count_products`, `check_formula`, `preview_changes`, `apply_policy`,
`import_shopify`, `try_order`, `test_integration`; `apply_policy`, `try_order` and
`test_integration` take POST only).
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
What customers pay for delivery, set in the ERP on ONE page, `/desk/shipping-policy-wizard`
(System Manager or Website Manager only). User guide: `docs/shipping-policy-guide.md`;
deployment record and rollback: `docs/shipping-policy-deployment.md`; design history in
`docs/superpowers/` (the one-page spec and plan are background, the guide and the code win).

**Model.** Records, version-tracked: Shipping Zone (a reusable country group, including
the not-delivered group), Shipping Item Set (a product group: explicit items, item
groups, code patterns that follow new products) and the single Shipping Policy
(Active, the fallback, and the delivery rows: product group, country group, services,
customer price). The policy ships **off**; while it is off, `price_delivery` returns the
carrier's quotes as posted. The customer price of a row is Free, One price, By weight
(tiers in pounds), the carrier's price, or a Formula run over each service's quote.
There is no price-source switch and no rate-card module; the Shopify CSV is only an
input (`shipping_shopify_import.py`) that the ERP Helper or `import_shopify` turns into
ordinary editable groups and rows. Provider services come from the ERP's Shipping
Provider and Shipping Service records, never from constants in the engine.

**The page.** It edits a draft kept in the browser and writes nothing until **Review and
save**, which writes the whole draft in one transaction or nothing, and refuses when
someone changed the policy after the page loaded. Try an order prices a real cart (ERP
items, destination, city/postcode) with the draft, even while the saved policy is off.
Check it works runs the read-only checks of the ERP endpoint, the carrier and the
website named in Storefront Settings.

**Formulas.** A formula can use `carrier_price`, `order_value`, `weight_kg`, `volume_l`,
`boxes` and `item_count`, with `min`, `max`, `round`, `ceil`, `floor`, `abs`,
arithmetic, comparisons, `and`/`or`/`not` and `a if condition else b` (no power
operator; parsed and whitelisted, never `eval`). `round()` rounds halves away from
zero. A formula that fails for a service drops that service's offer; if none is left
the fallback applies, never a zero price.

**Guest methods**, read-only, read the policy afresh on every call (nothing is
cached): `price_delivery(country_code, items, shipment_quotes, plan_token, language)`
is two-step for carrier-priced rows: the first answer may be `needs_quotes` with the
shipments to quote and a `plan_token`; the website returns the carton quotes with the
token and gets the offers (provider, booking code, service, price, days). A changed
policy, cart or provider directory makes the token stale. `delivery_services()` returns
only the public booking identity (name, provider, carrier code) of each service.
`public_summary(language)` lists the enabled zones with their countries and customer
lines while the policy is on. None returns a rule, a formula or a trace.

**Refusals and the fallback.** `not_covered`, `too_heavy`, `not_served` and `not_shipped`
never reach the fallback; the single fallback (refuse, or charge a flat amount) covers
only a provider or formula that gives no usable price.

### Promotion
`~/erp-live-fix/shipping-policy/promote.sh prod` (check) then `--apply`: additive first
installation of the listed files (`tools/shipping_policy_files.txt`: 46 app files and
one safe fixture; a hermetic test keeps the list equal to the tree), with a database
backup, exact-path copies, children-first reload, no migrate, no restart, no new key,
role, Custom DocPerm or customisation. The policy arrives **off and empty**. It is not
a conversion of an existing active legacy policy. Deploy the matching website change
after the ERP part. On a site whose shipping app lacks the carton packer or the
postcode-aware rate method, install that support first (see the deployment record).
- [ ] Native tests on a replica with the live app set: another app's `doc_events`
  committing inside `save` would break Apply's all-or-nothing. Record the MariaDB
  version and `innodb_snapshot_isolation`.
- [ ] Look for the `employee_self_service` catch-all hook and nginx's body limit.
- [ ] Resolve countries missing from the live Country list when importing the file.
- [ ] After the website deploy, build the policy, run Check it works with the policy on,
  and try a real checkout. Emergency switch-off: untick Active and save.

## Tests
Hermetic (no bench): `python3 -m pytest tests/hermetic -q`
Frappe-native: `bench --site <site> run-tests --app dsi_catalogue`
The Shipping Policy's native modules (`--module
dsi_catalogue.dsi_catalogue.doctype.<module>`; list them with `ls
dsi_catalogue/dsi_catalogue/doctype/shipping_*/test_*.py`) roll back their writes and
refuse to run on a site that holds a policy: run them on erp2, one at a time, after
`tools/erp2_push.sh --restart`.
Browser, erp2 only: `tests/browser/shipping_policy_wizard.mjs` (the command is in
its header; it needs a System Manager user on erp2).
