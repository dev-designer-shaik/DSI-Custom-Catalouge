# One-page Shipping Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the six-tab Shipping Policy wizard and its separate rate card with one page of product sets and rows (zone, services, price), a live product picker, provider and service selection, and a website that applies every kind of row.

**Architecture:** A pure engine (`shipping_engine.py`) takes a policy dict and an order dict and returns offers, or the list of shipments that still need carrier quotes. The Frappe layer loads the policy and the Shipping Service directory and exposes `price_delivery` (two steps at most) and the wizard's methods. The page is plain JavaScript parts stitched together by Frappe. The website asks the ERP, quotes the carrier only for the shipments the ERP names, and asks again.

**Tech Stack:** Python 3.12 and Frappe 16 (erp2 16.18.3, live 16.52), hermetic pytest (`python3 -m pytest tests/hermetic -q`, 2242 tests pass before any change), Frappe-native tests on erp2, plain ES2020 JavaScript included with Frappe's `{% include %}`, headless Chrome (puppeteer) for the page; the website is Next.js 16 and TypeScript (`npm test`, `npx tsc --noEmit`).

**Spec:** `docs/superpowers/specs/2026-10-25-shipping-policy-one-page-design.md` (amends `2026-10-22-shipping-policy-design.md`). Words: item group = the ERP's Item Group; product set = Shipping Item Set; row = Shipping Policy Rule; zone = Shipping Zone.

## Global Constraints

- Catalogue repo `/home/mdad/dsi-catalogue-work`, branch `shipping-policy-one-page` (from `d2364e8`); it is PUBLIC: no real Shopify prices, no secrets, no customer data in code, tests, fixtures or docs. Tests and fixtures use made-up prices.
- Website repo `/home/mdad/Website/DSI WEB N` is PRIVATE; work in a worktree `/home/mdad/Website/dsi-ship-onepage` on branch `shipping-policy-one-page` cut from `origin/main` (fetch with `git -c credential.helper= -c credential.helper='!gh auth git-credential' fetch`). Commit locally; NEVER push either repo, and never touch the other worktrees (`dsi-translations`).
- Only erp2 may be touched remotely (`ssh erp2-sandbox`, docker container `frappe_docker-backend-1`, site `erp.shaik.net`, `tools/erp2_push.sh [--restart]`). Never touch the live ERP or the live site's data. Read-only GET/POST quote requests to the live website are fine.
- No Custom Fields, Property Setters, Client Scripts, Server Scripts, new roles or Custom DocPerm rows. Edit the existing doctypes' JSON in place (nothing is live), and bump each edited doctype/page JSON's `modified` stamp so `reload_doc` reloads it.
- Roles stay `MANAGER_ROLES = ("System Manager", "Website Manager")`, enforced with `frappe.only_for(MANAGER_ROLES)` first in every wizard method; the page route stays `shipping-policy-wizard`.
- Conventions: tab indentation, ruff at 110, plain text only into the DOM (`sp.el`, `textContent`), no build step, no `eval`.
- Prices: USD, rounded to cents; weights: the table is in pounds (kg x 2.20462262, rounded to three decimals), items weigh `weight_per_unit` kg (an item with no usable weight counts 0.35 kg per unit and the answer says so).
- Limits on every guest list: items 200, shipments 10, cartons 50 per shipment, quotes 20 per shipment, numbers at most 10^9, texts 140 characters (the first design's limits).
- Nothing in the price path is cached. A failure is never a price of zero. A country in an enabled Not delivered zone is never delivered to.
- Retired behaviour is deleted with its tests, not kept alive: price source, rate card pricing, "Anything not covered", policy and zone fallbacks, weight and volume conditions, one-country-one-zone, the "every item" rule, the six-tab wizard.

## Review Focus

Failure modes the spec implies that no single task owns; each has a named test in the task that owns the code.

1. A mixed cart where one product's set has no row for the country: refused (`not_covered`), never priced under another set. (Task 1: `test_set_is_decided_by_the_product_alone`)
2. A table or fixed row with ticked services none of which serves the destination: a plain delivery, never a service name that cannot carry it. (Task 1: `test_ticked_services_that_do_not_serve_give_a_plain_offer`)
3. The policy saved between the website's two calls: refused as stale and asked again once, never priced from a mismatched plan. (Task 4: `test_stale_plan_is_refused`; Task 7: `test_stale_plan_asks_again_once`)
4. Overlapping zones where one is Not delivered: no delivery. (Task 1: `test_not_delivered_wins_over_overlapping_zones`)
5. A carrier-priced row with partial, missing or non-numeric quotes: the fallback, never zero. (Task 1: `test_partial_and_junk_quotes_never_price_zero`)
6. An item with no Website Item, no index key or no weight: priced as the catch-all would price it, flagged in the trace. (Task 1: `test_item_without_facts`)

## Contracts shared by the tasks

Every task reads this section; names here are exact.

**Policy dict** (the engine's input, the wizard's draft, `policy_from_rows` output):

```
{"active": bool,
 "fallback": {"method": "no_delivery"|"fixed"|"free", "amount": float},
 "zones": [{"name", "enabled", "countries": [ISO2 upper], "not_shipped": bool, "everywhere_else": bool,
            "customer_line", "not_offered_message"}],
 "item_sets": [{"name", "item_groups": [str], "items": [str], "patterns": [str]}],
 "rules": [{"name"?, "enabled", "label", "zone", "item_set" ("" = every product), "services": [Shipping Service names],
            "method": "carrier_price"|"free"|"fixed"|"table"|"formula", "amount": float,
            "table": [{"up_to_lb": float, "price": float}], "formula": str, "round_up": bool, "days": int,
            "min_order_value": float, "max_order_value": float}]}
```

Select strings stored by the doctypes: method `Carrier price | Free | Fixed amount | Weight table | Custom formula`; fallback `Don't offer delivery | Charge a fixed amount | Free`. A row stores `services` and `price_table` as JSON text (Code fields); the policy dict has them as lists. Tiers are kept sorted by `up_to_lb`.

**Order dict** (engine input):

```
{"country_code": str, "order_value": float,
 "items": [{"item_code", "qty", "groups": [item group and every ancestor], "index_key", "weight_kg"}],
 "directory": None | [{"name", "provider", "carrier_code", "scope", "enabled": bool, "estimated_days": int,
                       "max_weight_kg": float (0 = no limit), "countries": [ISO2]}],
 "shipment_quotes": None | {"<index>": {"boxes": [{"weight_kg", "volume_l"}],
                                         "quotes": [{"service_id", "name", "amount", "boxes_quoted"}]}}}
```

`directory` None means the ERP has no Shipping Service records: every country counts as served and every ticked service as serving.

**Engine result** (`resolve`): `{"policy_active", "offered", "reason", "message", "zone", "needs_quotes", "shipments": [{"index", "set", "row", "items": [{"item_code","qty"}], "services": [str]|None, "weight_kg", "weight_lb", "charge", "quote": bool}], "offers": [{"service_id", "name", "amount", "source", "reason", "estimated_days", "provider", "carrier_code", "scope", "trace": [{"kind","text","amount"}]}], "trace": [{"kind","text","amount"}]}`. Reasons: `not_served`, `not_shipped`, `not_offered`, `not_covered`, `fallback_offer`, `formula_error`, `too_heavy`. A plain offer has service id `rate-card`, empty provider, carrier code and scope. `needs_quotes` is true only when `offered` would depend on quotes not given; then `offers` is empty and the shipments with `quote: true` are the ones to quote.

**Guest answer** (`dsi_catalogue.shipping_policy.price_delivery(country_code=None, items=None, shipment_quotes=None, plan_token=None, language=None)`, guest, JSON text or lists accepted):

```
{"policy_active", "offered", "reason", "message", "needs_quotes", "plan_token": str|None,
 "shipments": [{"index", "items": [{"item_code","qty"}], "services": [str]|None}]   (only when needs_quotes),
 "offers": [{"service_id", "name", "amount", "source", "reason", "estimated_days", "provider", "carrier_code", "scope"}]}
```

A policy that is off answers `{"policy_active": false, "offered": false, "offers": [], "needs_quotes": false, ...}`. A plan token that no longer matches answers `offered: false, reason: "stale_plan"` (an answer, not an exception). No row number, trace, formula or tier is ever returned to a guest.

**Wizard methods** (module `dsi_catalogue.shipping_policy_wizard`, all `frappe.only_for(MANAGER_ROLES)`): `wizard_state()`, `try_order(draft, order, ask_carrier=False, city=None)`, `check_formula(source)`, `preview_changes(draft)`, `apply_policy(draft, expected_version)`, `import_shopify(config_csv, not_delivered_csv=None)`, `search_products(text="", item_group="", pattern="", published_only=1, sets=None, start=0, limit=50)`, `count_products(item_group="", pattern="", published_only=1)`, `test_integration(order=None)`. `wizard_state()` returns `{"policy", "version", "countries", "providers", "price_list", "carrier_available"}` with `providers = [{"name", "carrier_code", "enabled", "services": [{"name", "scope", "enabled", "estimated_days", "max_weight_kg", "countries": [ISO2]}]}]` (empty list when `dsi_shipping` is not installed).

---

### Task 1: The engine prices product sets and rows

**Files:**
- Modify: `dsi_catalogue/shipping_engine.py` (new policy and order shapes, `resolve`, validators; the module docstring is rewritten to describe the new rules)
- Modify: `tests/hermetic/test_shipping_engine.py` (rewrite; delete every test that pins retired behaviour)
- Modify: `tests/hermetic/fixtures/shipping_policy_example.json` (a new example policy in the new shape, made-up prices, covering every charge)
- Unchanged: `dsi_catalogue/shipping_formula.py` and its tests

**Interfaces:**
- Consumes: `shipping_formula.evaluate`, `check_formula`, `FormulaError` (unchanged).
- Produces: `LABELS` (the Select strings and codes above, plus `"zone_kind"` if useful), `resolve(policy, order) -> dict`, `product_code(index_key) -> str`, `pattern_matches(pattern, code) -> bool`, `item_in_set(item, item_set) -> bool`, `tier_price(table, weight_lb) -> float | None` (None above the top tier), `validate_policy(policy) -> list[str]` (plain sentences; empty list = valid), `shipments_of(policy, order) -> list[dict]` (the grouping of steps 3 to 5, reused by the guest method for plan tokens).

- [ ] **Step 1: Write the failing tests** in `tests/hermetic/test_shipping_engine.py`, each with made-up prices, asserting the spec's values:
  - `test_set_is_decided_by_the_product_alone`: sets Petite (`F-*-PT`) and Perfumes (`F-*`), rows Petite to Bahrain only, Perfumes to Bahrain and to the US. A petite perfume to the US is `offered False, reason "not_covered"` although `F-*` also lists it; a full-size perfume to the US is priced by its table.
  - `test_mixed_cart_sums_the_shipments`: a perfume and a pen (no-set last row) to the same country: amount = perfume table price + pen table price; one plain offer, name from the row with the most days, `estimated_days` = the longest; `shipments` has two entries.
  - `test_table_edges`: tiers up to 1, 2 and 4 lb: 0.5 lb pays tier 1; exactly 1.0 pays tier 1; 1.01 pays tier 2 (gap pays the next tier up); 4.0 pays tier 3; 4.01 is `offered False, reason "too_heavy"` and stays `too_heavy` with the fallback set to Free.
  - `test_fixed_free_and_round_up`: fixed amount, free (0.0, not missing), a formula with `round_up` (4.20 becomes 5.00, 5.00 stays, 0 stays).
  - `test_ticked_services_that_do_not_serve_give_a_plain_offer`: table row with `services=["Svc A"]`, directory where Svc A lists only BH, destination US: one plain offer (`service_id "rate-card"`, provider ""), and a trace line saying why; with the directory None the same row offers Svc A.
  - `test_serving_services_are_offered_with_their_facts`: ticked, enabled, serving service gives an offer with `provider`, `carrier_code`, `scope`, `estimated_days` from the directory and the table price; a disabled ticked service is not offered.
  - `test_services_must_be_usable_by_every_shipment`: two shipments with ticked lists {A, B} and {B, C}: only B is offered, at the sum; lists {A} and {C} with table rows: one plain offer at the sum; the same with one carrier-priced shipment: `not_covered` then the fallback.
  - `test_carrier_rows_ask_for_quotes_then_price`: a carrier-priced row: first `resolve` gives `needs_quotes True`, `offers == []` and the shipment with `quote True`; with `shipment_quotes` the offers are the carrier's per service; a service that quoted fewer cartons than the shipment has is dropped; the formula row is evaluated per service with `carrier_price`, `weight_kg`, `volume_l`, `boxes`, `item_count` of the shipment.
  - `test_partial_and_junk_quotes_never_price_zero`: quotes missing, `amount` NaN, negative, a string, `boxes_quoted` short: the result is the fallback, never an offer of 0.0 unless a Free row or fallback says so.
  - `test_not_served_for_carrier_rows_skips_the_fallback`: no enabled service lists the country, or the heaviest carton is over every serving service's `max_weight_kg`: `not_served` with the fallback set to Free.
  - `test_fallback_modes`: no_delivery keeps the failure's reason (`not_covered`, `formula_error`, `not_offered`); fixed 9.5 and free give one plain offer named "Standard delivery", reason `fallback_offer`, amounts 9.5 and 0.0; `not_served`, `not_shipped` and `too_heavy` never reach it.
  - `test_not_delivered_wins_over_overlapping_zones`: US in zone USA (delivery) and in zone Nowhere (Not delivered): `not_shipped` with Nowhere's message; with Nowhere disabled the US is priced.
  - `test_everywhere_zone_holds_every_country`, `test_disabled_rows_are_ignored` (a disabled first row neither claims products nor prices), `test_order_value_condition` (from inclusive, up to exclusive, value from the order dict), `test_policy_off_answers_inactive`, `test_country_code_must_be_two_letters`.
  - `test_item_without_facts`: an item with empty `index_key` or `weight_kg` 0 is claimed by the first row with no products listed (or by groups/items it does belong to), weighs 0.35 kg per unit, and the trace says "weight estimated".
  - `test_patterns`: `pattern_matches("F-*-PT", "F-CR-OSA-M-PT")` true, `"F-*-PT"` vs `"F-CR-OSA-M-RF"` false, `?` and `[AB]` work, matching is on the upper-cased code, a pattern longer than 80 characters or with more than 12 stars never matches (no catastrophic backtracking: a 10,000-character code with 12 stars returns within 50 ms).
  - `test_validate_policy`: one assertion per problem class in the spec: a row at an unknown zone or set, a row at a Not delivered zone, a table that is empty or has a non-positive `up_to_lb` or a duplicate bound, a fixed row with no amount, a formula that fails `check_formula`, a set that lists no products that is not last, Active with no enabled row, two zones or two sets whose names differ only in capitals or accents, a country that is not two letters, a negative price.
  - `test_resolve_never_raises_on_junk`: policy and order built from `None`, strings, nested junk, huge numbers: always a result dict with `offered False` or valid offers.
- [ ] **Step 2: Run** `python3 -m pytest tests/hermetic/test_shipping_engine.py -q`; expect the new tests to fail.
- [ ] **Step 3: Implement** the contract above in `shipping_engine.py`. Read the current module first and reuse what still holds (rounding helpers, `_totals`, `_formula` evaluation, `_offer`, reason messages, cents arithmetic with `Decimal`). Step numbers of the spec's "How an order is priced" map to functions: `_destination`, `shipments_of` (steps 3 and 4), `_price_shipment` (5), `_usable_services` (6), `_offers` (7), `_fallback` (8). Remove the rate-card imports and every use of `price_source`, `rate_card`, `not_covered`, zone fallbacks, bands on weight and volume.
- [ ] **Step 4: Run** the engine tests and the whole hermetic suite for this module; expect PASS. (The other modules' tests fail until their tasks; that is expected, note the count.)
- [ ] **Step 5: Commit** `git commit -m "engine: product sets, rows, services and weight tables"`.

### Task 2: The Shopify import becomes sets, rows and zones, and parity is proven

**Files:**
- Rename: `dsi_catalogue/shipping_rate_card.py` to `dsi_catalogue/shipping_shopify_import.py` (`git mv`)
- Modify: `dsi_catalogue/shipping_shopify_import.py` (keep the CSV parsing and `KNOWN_PROFILES`; delete card pricing and card validation: `price_card`, `validate_card`, `lb_text` if unused, and their constants; add `policy_from_export`)
- Rename and modify: `tests/hermetic/test_shipping_rate_card.py` to `tests/hermetic/test_shipping_shopify_import.py` (keep every parser test that still describes the parser; delete card-pricing tests)
- Modify: `tests/hermetic/fixtures/shopify_shipping_config_sample.csv`, `shopify_countries_not_delivered_sample.csv` only if a test needs a case they lack (made-up prices only)
- Create: `tools/parity_check.py`

**Interfaces:**
- Consumes: Task 1's `resolve`, `LABELS`, the policy and order contracts.
- Produces: `policy_from_export(config_csv, not_delivered_csv=None) -> {"zones": [...], "item_sets": [...], "rules": [...], "notes": [str], "summary": {"sets", "rows", "zones", "tiers", "countries", "not_delivered"}}`, each list in the policy-dict shape; nothing is merged here, the wizard merges it into a draft. `parse_shopify_export` keeps its signature and notes.

- [ ] **Step 1: Write the failing tests** in `test_shipping_shopify_import.py`: `test_zones_with_the_same_countries_are_shared` (the sample's USA zone of two profiles is one Shipping Zone), `test_same_name_different_countries_is_told_apart_by_profile`, `test_sets_follow_the_profile_order_with_the_known_patterns` (Petite `F-*-PT` and `F-CR-PC-*`, Refill `F-*-RF`, Dangerous Goods `F-*`; the Rest of the World profile has `item_set ""` and comes last), `test_unknown_profile_becomes_an_empty_set_with_a_note`, `test_flat_zone_is_a_fixed_row_and_tiers_are_a_table`, `test_weight_from_is_not_stored`, `test_label_and_days_come_from_the_method_text` ("International Express (4-7 days)" gives label "International Express", days 7), `test_not_delivered_countries_become_a_not_delivered_zone`, `test_notes_and_summary_counts`.
- [ ] **Step 2: Run, see them fail. Step 3: Implement** `policy_from_export` on top of the parsed card; remove card pricing code. Update imports elsewhere with `grep -rn shipping_rate_card` (the wizard and data modules change in their own tasks, so leave those importers failing and list them in the report).
- [ ] **Step 4: Write `tools/parity_check.py`**: it loads the old engine and old card pricing from git (`git show d2364e8:dsi_catalogue/shipping_engine.py` and `shipping_rate_card.py`, `shipping_formula.py` written to a temp directory under a unique package name, never into the repo), reads a Shopify config CSV given on the command line (default: the sample fixture), builds the old policy (rate card as price source) and the new policy (`policy_from_export`, then a policy dict with `active` True and fallback no_delivery), and compares them over a generated battery: product kinds (full-size, petite, refill, pen-like with no `F-` prefix) x quantities 1 to 4 x unit weights (0.1, 0.35, 0.9, 1.0, 2.5 kg) x every country in the card plus ten the card does not know, plus the user guide's worked examples when `--examples <json>` is given. It prints each mismatch and exits non-zero if any total or any `offered` differs. Allowed differences: the plain offer's name and days text. It must not print or write real prices anywhere except stdout when run on a real CSV.
- [ ] **Step 5: Run** `python3 tools/parity_check.py` on the sample and record the output in the task report; run the module tests; commit `git commit -m "import: Shopify export becomes product sets, rows and zones; parity check against the first engine"`.

### Task 3: The doctypes and the data shaping

**Files:**
- Modify: `dsi_catalogue/dsi_catalogue/doctype/shipping_policy/shipping_policy.json` and `.py` and `.js` (remove `price_source`, `rate_card`, `not_covered`, `default_fallback_method`, `default_fallback_amount`; add `fallback_method` Select `Don't offer delivery | Charge a fixed amount | Free` and `fallback_amount`; the rules note is rewritten: "Each product is priced by the first set that lists it, by the first row of that set that ships to the destination.")
- Modify: `dsi_catalogue/dsi_catalogue/doctype/shipping_policy_rule/shipping_policy_rule.json` (remove the weight and volume conditions; `method` options become `Carrier price | Free | Fixed amount | Weight table | Custom formula`; add `amount` (Currency, USD), `price_table` (Code, JSON), `services` (Code, JSON), `days` (Int); keep `enabled`, `label`, `zone`, `item_set`, `formula`, `round_up`, `min_order_value`, `max_order_value`)
- Modify: `dsi_catalogue/dsi_catalogue/doctype/shipping_zone/shipping_zone.json` and `.py` (remove `fallback_method`, `fallback_amount`; the duplicate-country validation goes; keep one-Everywhere-zone validation only if the spec's "Everywhere" still needs it, otherwise drop it and say so)
- Create: `dsi_catalogue/dsi_catalogue/doctype/shipping_item_set_pattern/` (`__init__.py`, `.json`, `.py`: a child table, field `pattern` Data) and add its table `patterns` to `shipping_item_set.json`
- Modify: `dsi_catalogue/shipping_policy_data.py` (`policy_from_rows`, `rows_from_policy`, `diff_policy`, `snapshot_version`, `response_from`, `clean_*` for the new shapes; new `clean_shipment_quotes`)
- Modify: `tests/hermetic/test_shipping_doctypes.py`, `tests/hermetic/test_shipping_policy_data.py` (rewrite for the new shapes), `tools/shipping_policy_files.txt` (add the new files, rename the import module, drop any deleted file)
- Modify: the native tests under the doctype folders that pin removed fields (`test_shipping_zone.py`, `test_shipping_item_set.py`; `test_shipping_policy_ratecard.py` is deleted)

**Interfaces:**
- Consumes: Task 1's `LABELS` and shapes; Task 2's module name.
- Produces: `policy_from_rows(...)`/`rows_from_policy(...)` converting the doctypes' rows to and from the policy dict (JSON fields parsed to lists, unreadable JSON read as empty and reported by `validate_policy`), `snapshot_version(policy) -> str` over the new shape, `diff_policy(before, after) -> list[str]` (plain lines such as `Set "Petite": 2 products added`), `clean_items`, `clean_shipment_quotes(value) -> dict[int, {"boxes", "quotes"}]` (limits above), `response_from(result, translate) -> guest answer` per the contract.

- [ ] **Step 1: Write failing tests**: `test_every_new_field_is_in_the_json`, `test_removed_fields_are_gone`, `test_files_list_matches_the_tree` (the existing check), `test_policy_round_trips_through_rows` (including a table with three tiers, services, patterns), `test_unreadable_json_fields_read_as_empty`, `test_diff_names_added_changed_removed_sets_rows_zones`, `test_snapshot_version_changes_with_any_field`, `test_response_has_only_the_contract_fields` (an engine result with trace, row numbers and formulas yields none of them), `test_clean_shipment_quotes_limits_and_junk`.
- [ ] **Step 2: Run, fail. Step 3: Implement.** Edit the JSON files by hand, keep field order sensible for the form, bump `modified`. The form of the policy stays usable by a System Manager. Native tests that need a site are updated but run in Task 8.
- [ ] **Step 4: Run** the hermetic tests of these modules; **Step 5: commit** `git commit -m "doctypes: rows carry services, weight tables and fixed amounts; sets carry patterns"`.

### Task 4: The loaders and the guest methods

**Files:**
- Modify: `dsi_catalogue/shipping_policy.py` (`load_policy`, `services_directory`, `build_order`, `price_delivery`, `public_summary` unchanged in behaviour, `item_facts_for`, `order_value_for`, `item_groups_for` kept)
- Modify: `dsi_catalogue/dsi_catalogue/doctype/shipping_policy/test_shipping_policy.py` (native, rewrite for the new answer); delete `test_shipping_policy_ratecard.py`
- Test: hermetic tests for everything that can be tested without a database (token, shipments, directory shaping) in `tests/hermetic/test_shipping_policy_data.py` or a new `tests/hermetic/test_shipping_policy_methods.py` with a fake `frappe`

**Interfaces:**
- Consumes: Task 1 `resolve` and `shipments_of`; Task 3 `policy_from_rows`, `snapshot_version`, `response_from`, `clean_*`.
- Produces: `services_directory() -> list | None` (reads Shipping Provider, Shipping Service and Shipping Service Country; None when the doctypes do not exist or hold no services), `plan_token(policy, country, shipments) -> str` (a hash of `snapshot_version(policy)`, the country and each shipment's index, row and items; any change in the policy or the cart changes it), `price_delivery(country_code=None, items=None, shipment_quotes=None, plan_token=None, language=None)` per the guest-answer contract.

- [ ] **Step 1: Write failing tests**: `test_off_policy_answers_inactive_without_touching_the_database`, `test_all_table_policy_answers_final_in_one_call`, `test_carrier_row_answers_needs_quotes_with_shipments_and_a_token`, `test_second_call_with_quotes_answers_final`, `test_stale_plan_is_refused` (policy changed between the calls: `offered False, reason "stale_plan"`), `test_plan_with_other_items_is_refused`, `test_unknown_shipment_index_is_refused`, `test_directory_is_none_without_shipping_service_records`, `test_guest_answer_has_no_internal_fields`, `test_bad_input_is_a_plain_validation_error` (negative quantity, 201 items, junk quotes), `test_language_translates_messages`.
- [ ] **Step 2: Run, fail. Step 3: Implement.** Keep the formula-error logging (`_log_formula_error`) adapted to rows. A guest posting `shipment_quotes` while the policy needs none gets the same final answer (the quotes are ignored).
- [ ] **Step 4: Run** the hermetic suite; **Step 5: commit** `git commit -m "guest methods: price_delivery asks for carrier quotes only for the shipments that need them"`.

### Task 5: The wizard's server methods

**Files:**
- Modify: `dsi_catalogue/shipping_policy_wizard.py` (new draft shapes in `_SHAPES`; `wizard_state` with `providers`; `search_products`, `count_products`, `import_shopify` replacing `import_rate_card`; `try_order` running the two steps with the carrier asked live for carrier rows; `preview_changes`, `apply_policy` writing sets, rows and zones in one transaction with the existing lock, savepoint and version check)
- Modify: `dsi_catalogue/shipping_integration_check.py` (the three checks of `test_integration` against the new answer: the ERP price API over HTTP including a second call when `needs_quotes`, the carrier for one sample parcel, the website's rates route)
- Modify: `dsi_catalogue/dsi_catalogue/doctype/shipping_policy/test_shipping_policy_wizard.py` (native), `tests/hermetic/test_shipping_integration_check.py`
- Create: `tests/hermetic/test_shipping_wizard_methods.py` for what a fake `frappe` can test (draft shape checking, `search_products` query building, `import_shopify` merging, `preview_changes` lines)

**Interfaces:**
- Consumes: Tasks 1 to 4.
- Produces: the wizard methods of the contracts section. `search_products(...)` returns `{"rows": [{"item_code", "item_name", "index_key", "item_group", "published": bool, "listed_by": str}], "more": bool}`; the query filters Website Items and Items on the database (text on name, code and index key; item group including descendants; pattern with `*`, `?` translated to SQL `LIKE` and refined in Python with `pattern_matches`; published first) and caps `limit` at 100; `listed_by` is the first set of `sets` (the draft's sets, posted by the page) that lists the item. `count_products(...)` returns `{"count": int}` for the live-filter button. `import_shopify(...)` returns Task 2's `policy_from_export` result plus `problems_for_countries` (codes missing from the ERP's Country list). `apply_policy` refuses a draft whose services name a Shipping Service the ERP does not hold, whose countries are not in the Country list, or that fails `validate_policy`, with plain sentences; an unknown service in a saved row is allowed to remain only if the directory is None.

- [ ] **Step 1: Write failing tests** for each method with a fake `frappe` (hermetic) and list the native ones for Task 8: `test_wizard_methods_refuse_a_non_manager`, `test_state_lists_providers_and_services`, `test_search_products_filters_and_marks_listed_by`, `test_search_limit_is_capped`, `test_count_products_matches_search`, `test_import_merges_nothing_and_reports_unknown_countries`, `test_preview_changes_lines`, `test_apply_is_all_or_nothing` (one bad row writes nothing), `test_apply_refuses_stale_version`, `test_apply_refuses_unknown_service`, `test_try_order_runs_two_steps` (fake carrier), `test_try_order_without_carrier_marks_carrier_rows_unpriced`.
- [ ] **Step 2: Run, fail. Step 3: Implement.** Reuse the lock, savepoint, version and upsert helpers of the current module; delete everything about the card.
- [ ] **Step 4: Run** the hermetic suite. Expected at this point: every hermetic test passes except none (all earlier modules have been migrated); record the count. **Step 5: commit** `git commit -m "wizard methods: providers, product search, Shopify import into rows, two-step try order"`.

### Task 6: The page

**Files** (all under `dsi_catalogue/dsi_catalogue/page/shipping_policy_wizard/`):
- Rewrite: `shipping_policy_wizard.js` (page load, store, includes), `shipping_policy_wizard.css` (new, small), `wizard_core.js` (store, draft in localStorage, server calls, `change()`; keep what still fits), `wizard_helpers.js`, `wizard_country_picker.js` (kept)
- Create: `wizard_sets.js` (the list of product sets and their rows, move/disable/delete), `wizard_row_editor.js` (one row: zone, delivered by, price, more), `wizard_service_picker.js` (providers and services with ticks and coverage), `wizard_product_picker.js` (the live-filter dialog), `wizard_zones.js` (the zone chips and dialog), `wizard_review.js` (Review and save dialog), `wizard_import.js` (Shopify import dialog), `wizard_try_order.js` (the panel), `wizard_test_live.js` (the Test live dialog), `wizard_words.js` (sentences for rows, shared by the review and the panel)
- Delete: `step_*.js`, `wizard_rates_*.js`, `wizard_rate_card.js`, `wizard_rule_card.js`, `wizard_rule_words.js`, `wizard_policy_words.js`, `wizard_itemset_editor.js`, `wizard_chips.js`, `wizard_tape.js` once nothing includes them
- Modify: `tools/shipping_policy_files.txt` to list the final files; `shipping_policy_wizard.json` (`modified` stamp); `tests/browser/shipping_policy_wizard.mjs` (rewrite, small)

**Interfaces:**
- Consumes: the wizard methods and the policy dict (contracts).
- Produces: the page described in the spec's "What the owner sees": the store `wizard.store = {original, draft, version, dirty}`; every edit goes through `wizard.change((draft) => {...})`; `sp.new_zone`, `sp.new_item_set`, `sp.new_row` build entries in the server's key order (so an addition taken back leaves the draft clean); the page keeps each set's rows together and in order, shows `Everything else` last, and saves refuse a no-products set that is not last.

- [ ] **Step 1: Write the browser test first** (`tests/browser/shipping_policy_wizard.mjs`, puppeteer, erp2 only, headless; its header carries the command): it logs in as a System Manager, then asserts: the page opens with one heading and no tabs; an empty policy shows the two starter buttons; "Start with the carrier's own prices" creates one set and one row and shows Unsaved changes; adding a set via the product dialog narrows its list as text is typed (type "amber", the list shrinks, an item count appears), "Add everything that matches as a live filter" stores a pattern chip; ticking a service shows the provider heading and the service's coverage text; choosing Weight table shows tier rows and rejects a non-ascending bound with a visible problem; the Review and save dialog lists changes, blocks on a problem and saves on none; reloading shows the saved rows; a stale version shows the conflict message; the Test live dialog shows three check results; keyboard: every control reachable by Tab with a visible focus ring; light and dark screenshots saved under a path given by an environment variable.
- [ ] **Step 2: Implement** the parts. Plain DOM through `sp.el`; every server string reaches the page as text. The product picker queries `search_products` 250 ms after the last keystroke, ignores answers that arrive out of order, shows "N shown, more" with a Load more button, and marks products another set lists. The service picker reads `wizard.state.providers` and counts, per service, how many of the row's zone countries it serves. The Try an order panel posts the draft and the sample to `try_order` 250 ms after the last change and renders each shipment's set, row, tier and amount, the offers, the plain-offer note ("shipped by hand") and the warnings (weight estimated, unknown product).
- [ ] **Step 3: Run** the node syntax check on every part (`node --check`), then the browser test in Task 8 once the server side is on erp2. **Step 4: commit** `git commit -m "page: one page of product sets and rows"`.

### Task 7: The website asks, quotes only what is needed, and asks again

**Repo:** website worktree `/home/mdad/Website/dsi-ship-onepage`, branch `shipping-policy-one-page`. Run `npm ci` or link `node_modules` from the primary checkout; baseline `npm test` and `npx tsc --noEmit` first and record the counts.

**Files:**
- Modify: `lib/shipping/policy-quote.ts` (new answer type `PolicyAnswer` with `needsQuotes`, `planToken`, `shipments`, offers with `provider`, `carrierCode`, `scope`; remove `priceSource` and the rate-card-only check; `askPolicy({countryCode, cartItems, shipmentQuotes?, planToken?})` never throws, returns null for anything it cannot stand behind; `offersToShippingMethods` fills provider, providerCode, scope; a plain offer keeps service id `rate-card` and no provider)
- Modify: `lib/shipping/quote.ts` (`getShippingQuotes`: ask; final answer: methods; `needsQuotes`: `quoteShipment` for each listed shipment then ask again with `shipmentQuotes`; `stale_plan`: ask again once; null or policy off: the carrier path exactly as before; extract the carrier quoting into `quoteShipment(items, country, city, postcode, currency, allowedServices?)` that returns the aggregated methods and the cartons of the best packing plan)
- Modify: `lib/shipping/constants.ts` only if needed; `lib/payments/settle-charge.ts` and `lib/erpnext/orders.ts` only if a test shows a service offer from the policy fails their checks (a policy service offer carries `providerCode` from `carrier_code`)
- Tests: `tests/` (the existing runner and `loadModule` stubs; follow the style of the existing shipping tests)

**Interfaces:**
- Consumes: the guest-answer contract; `erpMethod` stub.
- Produces: `getShippingQuotes` unchanged in signature; `ShippingMethod` unchanged in shape, with provider fields filled for service offers; `resolveShippingCost` unchanged (it calls `getShippingQuotes`, so the amount charged is the amount shown).

- [ ] **Step 1: Write failing tests**: `test_final_answer_needs_no_carrier_call`, `test_needs_quotes_quotes_each_listed_shipment_then_asks_again`, `test_only_allowed_services_are_kept_in_a_shipment_quote`, `test_service_offers_carry_provider_code_and_scope`, `test_plain_offer_keeps_the_rate_card_service_id`, `test_stale_plan_asks_again_once`, `test_second_stale_plan_uses_the_carrier_path`, `test_policy_off_or_error_or_timeout_uses_the_carrier_path_as_before`, `test_payment_requote_matches_the_checkout_amount` (the same stubs through `resolveShippingCost`), `test_answer_of_the_wrong_shape_is_ignored`, plus the existing shipping tests still passing.
- [ ] **Step 2: Run, fail. Step 3: Implement.** The ERP call keeps its 4 second limit per call. **Step 4: Run** `npm test` and `npx tsc --noEmit`; **Step 5: commit** (website repo) `git commit -m "shipping: ask the ERP policy, quote only the shipments it names, ask again"`.

### Task 8: erp2 end to end

**Files:** `tests/browser/shipping_policy_wizard.mjs` (finish), `tools/erp2_push.sh` (only if it needs the new file list handling), no product code unless a defect is found (fix it in the owning task's files and say so).

- [ ] **Step 1: Reset the policy on erp2** with a script run through `ssh erp2-sandbox` (stdin script, never inline quoting): empty the Shipping Policy rows, delete the zones and sets the old fill script made, then `tools/erp2_push.sh --restart`; confirm the doctypes reloaded (the new fields exist, the removed ones are gone).
- [ ] **Step 2: Run the native tests** one module at a time on erp2 (`bench --site erp.shaik.net run-tests --app dsi_catalogue --module ...`), fixing defects.
- [ ] **Step 3: Import the real Shopify files** (`~/shipping-interim-rates/*.csv`, not committed) through `import_shopify` and Apply with Active off, then `python3 tools/parity_check.py ~/shipping-interim-rates/shopify_shipping_config.csv ...` and keep the output local. Zero mismatches is the bar.
- [ ] **Step 4: Run the browser test** against erp2 and keep screenshots in `~/Downloads/shipping-policy-one-page/screenshots/`.
- [ ] **Step 5: Website against erp2:** run the website worktree locally with `ERP` env pointing at erp2 (read `.env.local` conventions from the memory notes; never print secrets), the carrier stubbed or asked live as available, and check headlessly: Bahrain, the US, a mixed cart, a service offer with a ticked Aramex service, a carrier-priced row, and that the order payload carries provider and service. Write results to the report.
- [ ] **Step 6: Commit** any fixes with their own messages.

### Task 9: Documents, promote script, guide and helper files

**Files:** `README.md` ("Shipping Policy" and "Promotion" sections), `CHANGELOG.md`, `docs/superpowers/specs/2026-10-25-shipping-policy-one-page-design.md` (an "As built" note listing every deviation), `~/erp-live-fix/shipping-policy/promote.sh` (re-pin `COMMIT`, `BASE`, `EXPECTED_FILES` from the final file list; keep check-by-default), `~/erp-live-fix/shipping-policy/erp2/fill_shopify.py` (rewrite to use `import_shopify` and `apply_policy`), `~/Downloads/shipping-policy-erp2/user-guide/` (screenshots and pages for the new page; the real prices keep it local), `~/Downloads/helper-drive-upload/Website/SHIPPING_POLICY.md` and `WEBSITE_ERP_INTEGRATION.md` (regenerated), memory notes.

- [ ] **Step 1:** Update README and CHANGELOG from the final state; add the "As built" note to the spec. **Step 2:** Re-pin `promote.sh` and rehearse it on erp2 (`promote.sh erp2` check, then the scratch rehearsal). **Step 3:** Rebuild the guide with `build/make.sh` after new captures (agents build; I verify page count and a text-layer check). **Step 4:** Regenerate the two helper markdown files with `make_helper_md.py`/`build_website_erp_md.py` adapted to the new page. **Step 5:** Final full runs: `python3 -m pytest tests/hermetic -q`, ruff, the website's `npm test`, `npx tsc --noEmit`. **Step 6:** Commit; report the counts; do not push.
