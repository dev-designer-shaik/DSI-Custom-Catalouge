# Changelog

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
