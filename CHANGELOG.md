# Changelog

## 2026-10-25 — Every Sales User may manage the Shipping Policy
- The wizard page, its nine methods and the three record types (Shipping Zone,
  Shipping Item Set, Shipping Policy) are open to System Manager, Website Manager
  **and Sales User** (previously the first two). One list, `MANAGER_ROLES` in
  `shipping_policy_wizard.py`; the page and doctype JSON name the same roles and a
  hermetic test fails when they stop agreeing. Guest and every other role stay
  refused. Needs `reload-doc` of the three doctypes and the page on a site that
  already has the policy, and a worker reload for the Python change; the four JSON
  files carry a new `modified` stamp so reload-doc does not skip them.

## 2026-10-25 — Website Manager may manage the Shipping Policy
- The wizard page, its seven methods and the three record types (Shipping Zone,
  Shipping Item Set, Shipping Policy) are open to System Manager **and Website
  Manager** (was System Manager only). One list, `MANAGER_ROLES` in
  `shipping_policy_wizard.py`; the page and doctype JSON name the same roles and
  a hermetic test fails when they stop agreeing. No new role, Custom DocPerm or
  customisation; Guest and every other role stay refused. Needs `reload-doc` of
  the doctypes and the page on a site that already has the policy; the four JSON
  files carry a new `modified` stamp for that (reload-doc skips a page whose stamp
  is unchanged, so edited roles alone would never reach it).

## 2026-10-24 — Shipping Policy (branch `shipping-policy`, not yet promoted)
- Delivery pricing set in the ERP: doctypes Shipping Zone (+ Country), Shipping
  Item Set (+ Group, Item) and the single Shipping Policy (+ Rule), and the
  wizard page `shipping-policy-wizard` (six tabs; Apply writes the whole draft or
  nothing). System Manager only.
- Price source Carrier (rules over the carrier's quotes: carrier price, free or a
  formula, rounded up if asked; fallbacks when the carrier can't quote) or Rate
  card (the interim Shopify weight-tier card, imported from the Shopify CSV export).
- Guest, read-only, never cached: `shipping_policy.price_delivery` and
  `shipping_policy.public_summary`.
- Ships OFF: while off, `price_delivery` returns the posted quotes unchanged (the
  promotion proof, `test_policy_off_returns_the_quotes_unchanged`).
- No new `site_config` keys, roles, Custom DocPerm, Custom Field, Property Setter
  or scripts; `hooks.py` unchanged.
- Verified end to end on erp2: hermetic 2240, native 14 / 5 / 50 / 27 / 93,
  browser 105 of 105 with the real export, the guest and role permission matrix,
  and `price_delivery` equal to `try_order` on 37 orders.
- Carries the live host's `6a39d3e` (review approval publishes only what the
  draft changes) and `702a036` (review workspace renamed): push them before
  promoting (README, Promotion).

## 2026-09-12 — erp1-live capture + Phase 1 hygiene
- Tag `erp1-live-2026-09-12` = repo state + the erp1-live hotfixes adopted.
- Adopted from erp1's backend container (docker-cp'd, never committed): the
  authored-translations system — `i18n.py`, `website_item_events.py`,
  `website_item_i18n.js`, `Website Item Translated Copy` doctype, hooks wiring.
- `dec_option_a.py` → `tools/`; `delete_redundant_fields.py` deleted (would
  drop the 15 precompute fixture fields); `backfill_precompute_fields` →
  `maintenance.py`.
- Untracked .pyc/egg-info/dated backups removed; flit `pyproject.toml`;
  ruff check + format clean. n8n generation trigger now checks its HTTP
  response and fails the task loudly.

## 2026-09-09 — 3e90507 (deployed live)
- orders: one Address document per physical place; email stops being an identity.

## 2026-09-07 — 853a895
- Web leads: `capture_web_lead` — abandoned carts + inquiries land as Leads.

## 2026-09-06 — f4e26f8 / b723980 / caf1109 / b7def79
- `@frappe.whitelist()` restored on `create_order_atomic`.
- Payment Entry for captured Tap charges (savepoint-isolated).
- Pipeline-token gate adopted from the live server.
- PDP grouping: `custom_grouping_key` equality, never index-key prefix.
