# Shipping Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Zones, item sets, rules, a safe formula language, fallbacks and a four-step wizard in the catalogue app, built and verified on erp2 with the policy shipped switched off.

**Architecture:** Three pure Python modules (formula, engine, data shaping) carry all pricing logic and are tested hermetically. A thin Frappe layer adds seven standard doctypes, two guest read methods, five System-Manager wizard methods and one desk Page. The wizard keeps a draft in the browser, prices it live through `try_order`, and writes everything in one transaction on Apply.

**Tech Stack:** Python 3.10+ (CI 3.12, erp2 container 3.14), Frappe 16.18 on erp2, pytest, vanilla JS in the ERP desk, puppeteer for browser checks.

**Spec:** `docs/superpowers/specs/2026-10-22-shipping-policy-design.md`. Work happens in `~/dsi-catalogue-work`, branch `shipping-policy`. Nothing is pushed.

## Global Constraints

- New standard doctypes and code inside `dsi_catalogue` only. No Custom Fields, Property Setters, Client Scripts, Server Scripts, new roles or Custom DocPerm rows. `hooks.py` is not touched.
- Editing is for the System Manager role only. The two guest methods are read-only and return only what a customer may see. Wizard methods call `frappe.only_for("System Manager")`.
- Built and verified on erp2 (`ssh erp2-sandbox`, site `erp.shaik.net`, also public at `https://erp2.shaik.net`). erp2's catalogue app is an old copy and not a git repo: files reach it only through `tools/erp2_push.sh`, and only the new paths it lists.
- Pricing code reads `dsi_shipping`'s Shipping Service and Shipping Service Country read-only; if those records do not exist the country counts as served.
- USD only. The price list used for order value is `"USD - Online"`.
- Rounding: "round up to the next whole dollar" = round to cents (half up) then ceil; whole prices stay, zero stays zero. Never negative. Otherwise round to cents.
- Bands: "from" includes the number, "up to" excludes it. An empty or `0` limit means no limit.
- A country is in at most one enabled zone; at most one enabled "Everywhere else" zone.
- The policy ships off (`active = 0`); off means every quote is returned unchanged (partial quotes kept).
- Reason codes: `not_served`, `not_shipped`, `not_offered`, `not_covered`, `fallback_offer`, `formula_error`. The fallback offer is `service_id = "policy-fallback"`, name `"Standard delivery"`.
- Formula language: names `carrier_price`, `order_value`, `weight_kg`, `volume_l`, `boxes`, `item_count`; functions `min max round ceil floor abs`; arithmetic, comparisons, `and or not`, `a if c else b`; numbers only; whitelist over a parsed tree, never `eval`; 300-character and node-count limits; no power operator.
- The wizard writes nothing until Apply. The asterisk sentence must appear verbatim on the wizard's Rules step and on the Shipping Policy form: `* A rule for selected items applies only when every item in the order is one of them. A rule for every item applies to every order.`
- Repo conventions: hermetic pytest in `tests/hermetic` (no bench, no frappe import), tab indentation, ruff line length 110. Commit messages end with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`; commit with `git -c user.name='dev-designer-shaik' -c user.email=mdad@designershaik.com commit`.
- Out of scope: the website changes (Part 2 of the spec), pushing to GitHub, anything on the live ERP.

## Review Focus

1. **Unknown, unpriced or groupless items.** Order value counts them as 0 and the order still prices; an item with no group matches a selected set only if listed by code. (Task 5)
2. **Empty or zero cartons.** An order with no weights still prices; weight and volume bands compare against 0; with no cartons any quote counts as complete. (Tasks 2, 5)
3. **Country codes as people send them.** `" ae "`, `"AE"`, `""`, `"UAE"`: normalised to two upper-case letters; anything else is `not_served`, never a crash. (Task 2)
4. **Money edges.** 38.999999999 rounds up to 39; a three-decimal quote 107.610 becomes 107.61; a formula that goes negative prices at 0; a non-finite result is `formula_error` and goes to the fallback. (Task 2)
5. **Apply under pressure.** Dropping a zone or item set that rules still use, a stale wizard (someone applied since), duplicate zone names in a draft, a draft that fails halfway: refused or rolled back whole. (Task 6)

## File Structure

```
dsi_catalogue/shipping_formula.py          pure: parse and evaluate formulas
dsi_catalogue/shipping_engine.py           pure: zones, rules, pricing, fallbacks, validate_policy, label constants
dsi_catalogue/shipping_policy_data.py      pure: input cleaning, rows <-> policy, summary, response shaping, diff, version
dsi_catalogue/shipping_policy.py           frappe: load_policy, guest methods, item/price/served lookups
dsi_catalogue/shipping_policy_wizard.py    frappe: wizard_state, try_order, check_formula, apply_policy
dsi_catalogue/dsi_catalogue/doctype/{shipping_zone, shipping_zone_country, shipping_item_set,
    shipping_item_set_group, shipping_item_set_item, shipping_policy, shipping_policy_rule}/
dsi_catalogue/dsi_catalogue/page/shipping_policy_wizard/   {.json, .py, .js, .css}
tests/hermetic/{test_shipping_formula, test_shipping_engine, test_shipping_policy_data,
    test_shipping_doctypes}.py  and  fixtures/shipping_policy_example.json
tests/browser/shipping_policy_wizard.mjs   puppeteer run against erp2
tools/erp2_push.sh, tools/shipping_policy_files.txt
```

---

### Task 1: Formula evaluator

**Files:**
- Create: `dsi_catalogue/shipping_formula.py`
- Test: `tests/hermetic/test_shipping_formula.py`

**Interfaces:**
- Produces:
  - `class FormulaError(ValueError)`; its message is plain language for an ERP user.
  - `VARIABLES: tuple[str, ...]` (the six names above, in that order) and `SAMPLE: dict[str, float]` = `{"carrier_price": 38.2, "order_value": 310.0, "weight_kg": 2.4, "volume_l": 6.0, "boxes": 1, "item_count": 2}`.
  - `compile_formula(source: str) -> ast.Expression` (validates, raises `FormulaError`).
  - `evaluate(source: str, variables: dict[str, float]) -> float`.
  - `check_formula(source: str) -> None` (compile, then evaluate against `SAMPLE`).

- [ ] **Step 1: Write the failing tests** in `test_shipping_formula.py`:
  - `test_the_four_documented_examples_evaluate` with `SAMPLE` values: `carrier_price * 1.1` is approximately 42.02; `max(carrier_price * 1.1, 45)` is 45; `carrier_price - 10 if order_value >= 500 else carrier_price` is 38.2 on `SAMPLE` and approximately 28.2 with `order_value=600`; `weight_kg * 6 + 8` is approximately 22.4.
  - `test_functions_and_boolean_logic`: `min(ceil(carrier_price), floor(order_value / 10))` is 31; `round(carrier_price, 1)` is 38.2; `1 if carrier_price > 10 and not order_value < 5 else 0` is 1; `boxes * 5 + item_count` is 7.
  - `test_every_unsafe_or_unsupported_source_is_refused` (parametrised, each raises `FormulaError` from `check_formula`): `__import__('os').system('x')`, `carrier_price.real`, `carrier_price[0]`, `'text'`, `[1, 2]`, `{1: 2}`, `lambda: 1`, `carrier_price ** 2`, `unknown_name`, `open('x')`, `max(carrier_price, key=abs)`, `(x for x in [1])`, `carrier_price if`, `""`, `"   "`, `"a" * 301`, `True`, `None`, `carrier_price; 1`.
  - `test_a_formula_that_is_not_a_number_is_refused_when_evaluated`: `evaluate("carrier_price > 5", SAMPLE)` raises `FormulaError`.
  - `test_node_limit`: `"carrier_price + " * 100 + "1"` is refused with a message containing "too long or complex".
  - `test_runtime_problems_are_formula_errors`: `carrier_price / 0` raises with a message containing "divide"; `carrier_price * 1e308 * 10` (infinite) raises; a missing variable raises.
  - `test_unknown_name_message_lists_what_is_allowed`: the message for `cost` equals `Unknown name 'cost'. You can use: carrier_price, order_value, weight_kg, volume_l, boxes, item_count.`
- [ ] **Step 2: Run to verify failure:** `python -m pytest tests/hermetic/test_shipping_formula.py -q` fails with an import error.
- [ ] **Step 3: Implement the module.** Walk the `ast` tree allowing only `Expression, BinOp (+ - * / % //), UnaryOp (+ - not), BoolOp, Compare, IfExp, Call (to the six function names, positional arguments only), Name (the six variables), Constant (int or float only, bool excluded)`; count nodes against a cap of 80. `evaluate` converts `ceil`/`floor` results to float and rejects non-finite or non-numeric results.
- [ ] **Step 4: Run to verify they pass:** same command, all pass; `ruff check dsi_catalogue/shipping_formula.py` is clean.
- [ ] **Step 5: Commit** `feat: safe formula evaluator for shipping rules`.

---

### Task 2: Pricing engine

**Files:**
- Create: `dsi_catalogue/shipping_engine.py`, `tests/hermetic/fixtures/shipping_policy_example.json`
- Test: `tests/hermetic/test_shipping_engine.py`

**Interfaces:**
- Consumes: `FormulaError`, `evaluate`, `check_formula` from Task 1.
- Produces, with these exact shapes (all later tasks use them):
  ```
  policy = {"active": bool, "not_covered": "pass_through"|"no_delivery",
    "default_fallback": {"method": "no_delivery"|"fixed"|"free", "amount": float},
    "zones": [{"name", "enabled", "countries": [ISO2 upper], "not_shipped", "everywhere_else",
               "customer_line", "not_offered_message",
               "fallback": {"method": "policy_default"|"no_delivery"|"fixed"|"free", "amount"}}],
    "item_sets": [{"name", "item_groups": [str], "items": [str]}],
    "rules": [{"name": str|None, "enabled", "label", "zone", "item_set": str|None,
               "min_order_value","max_order_value","min_weight_kg","max_weight_kg",
               "min_volume_l","max_volume_l",            # 0 = no limit
               "method": "carrier_price"|"free"|"formula", "formula", "round_up"}]}
  order = {"country_code", "items": [{"item_code","qty","groups": [str]}], "order_value",
           "boxes": [{"weight_kg","volume_l"}],
           "quotes": [{"service_id","name","amount","boxes_quoted"}], "served": bool}
  result = {"policy_active", "zone": str|None, "rule": int|None, "offered": bool,
            "reason": str|None, "message": str,
            "offers": [{"service_id","name","amount","source": "carrier"|"rule"|"fallback",
                        "reason": str|None, "trace": [{"kind": "carrier"|"rule"|"formula"|"round"|"fallback"|"result",
                                                       "text": str, "amount": float|None}]}]}
  ```
  - `LABELS`: the Select strings the doctypes store, mapped to codes, one dict each for `method` (`Carrier price`, `Free`, `Custom formula`), `zone_fallback` (`Use the policy default`, `Don't offer delivery`, `Charge a fixed amount`, `Free`), `default_fallback` (`Don't offer delivery`, `Charge a fixed amount`, `Free`), `not_covered` (`Pass the carrier price through`, `Don't offer delivery`).
  - `REASONS`, `FALLBACK_SERVICE_ID`, `FALLBACK_SERVICE_NAME`, `DEFAULT_MESSAGES` (English text per reason).
  - `normalize_country(code) -> str` ("" unless two letters after trim and upper-casing).
  - `select_zone(policy, country_code) -> dict | None`.
  - `rule_matches(rule, order, item_sets) -> bool`.
  - `round_up_dollar(amount: float) -> float`, `to_cents(amount: float) -> float`.
  - `resolve(policy, order) -> result`.
  - `rule_previews(policy, order) -> list[{"index","in_zone","matches","amount"}]` (the price each rule would give the first usable quote; `None` when not in the order's zone, not matching, or unpriceable).
  - `validate_policy(policy) -> list[str]` (plain sentences; empty list means valid).

- [ ] **Step 1: Write `shipping_policy_example.json`** (policy shape): zones `Bahrain` (BH), `GCC` (AE SA KW OM QA, fallback fixed 35), `Rest of world` (everywhere_else), `Not shipped` (RU BY, not_shipped, message "We don't deliver to this country."); item set `Watches` (group `Watches`); rules in order: Bahrain/every item/free; GCC/`Watches`/free; GCC/every item/carrier price/round up; Rest of world/every item/formula `max(carrier_price * 1.1, 45)`/round up; `active` true, `not_covered` pass_through, default fallback no_delivery.
- [ ] **Step 2: Write the failing tests** in `test_shipping_engine.py` against that fixture (helper builds an order: one perfume in group `Fragrances`, value 310, one box 2.4 kg / 6 L, one quote `("svc", 38.20, boxes_quoted=1)`, served true):
  - `test_gcc_perfume_order_rounds_the_carrier_price_up`: amount 39.00, zone `GCC`, rule index 2, source `rule`, trace holds the carrier 38.20, a round step of 0.80 and a result 39.00.
  - `test_selected_items_rule_needs_every_item_in_the_set`: a GCC order of one watch (group `Watches`) prices 0.00 (rule 1); a watch plus a perfume prices 39.00 (rule 2).
  - `test_item_without_groups_matches_a_set_only_by_code`.
  - `test_bahrain_is_free_for_any_order`: quote 12.5 gives 0.00.
  - `test_rest_of_world_formula_with_minimum_and_round_up`: US quote 38.20 gives 45.00; quote 60.00 gives 66.00.
  - `test_not_shipped_zone_returns_its_message`: RU gives `offered False`, reason `not_shipped`, the zone's message.
  - `test_country_in_no_zone_goes_to_everywhere_else_then_not_covered`: JP uses Rest of world; with that zone removed `pass_through` returns the quote unchanged (source `carrier`) and `no_delivery` gives reason `not_covered`.
  - `test_inactive_policy_returns_quotes_unchanged`: `active False` returns each quote as an offer with the same id, name and amount, `policy_active False`, and keeps a partial quote.
  - `test_unserved_country_gets_no_delivery_and_no_fallback`: `served False` in GCC (fixed fallback 35) gives `offered False`, reason `not_served`.
  - `test_carrier_unavailable_uses_the_zone_fallback`: GCC with no quotes gives one offer `policy-fallback` / `Standard delivery` / 35.00 / source `fallback` / reason `fallback_offer`; Bahrain (`policy_default`) with default `no_delivery` gives `offered False`, reason `not_offered`; default switched to fixed 12 gives 12.00; to free gives 0.00.
  - `test_partial_quotes_are_dropped_when_active`: two boxes; service A quoted 1, B quoted 2 gives only B; only A gives the fallback.
  - `test_bands_include_the_lower_edge_and_exclude_the_upper`: a rule with order value from 500 up to 1000: 499.99 no match, 500 and 999.99 match, 1000 no match.
  - `test_weight_and_volume_bands` (same edges, kg and litres) and `test_zero_means_no_limit`.
  - `test_first_matching_rule_wins_disabled_rules_and_other_zones_are_skipped`.
  - `test_a_failing_formula_goes_to_the_fallback_never_to_zero`: formula `carrier_price / (order_value - 310)` at order value 310 gives reason `formula_error` and the zone's fallback amount.
  - `test_money_edges`: 38.999999999 with round up gives 39.00; 39.0 stays 39.0; a 0 price stays 0; formula `carrier_price - 100` prices 0.00; a pass-through quote 107.610 gives 107.61.
  - `test_country_code_forms`: `" ae "` is GCC; `"UAE"` and `""` give `not_served`.
  - `test_empty_boxes_still_price`: no boxes, quote with `boxes_quoted 0` prices normally.
  - `test_rule_previews_give_each_rule_its_price_for_the_sample_order`: for the GCC perfume order, rule 0 is `in_zone False`; rule 1 `matches False`; rule 2 amount 39.00.
  - `test_validate_policy_reports_each_problem`: one case each for a repeated country across enabled zones, two Everywhere-else zones, a rule naming a missing zone, a rule naming a missing item set, a bad formula, a formula rule with no formula, and an inverted band (from >= up to when both set); each returns a sentence naming the zone, rule number or country. The example fixture returns `[]`.
- [ ] **Step 3: Run to verify failure:** `python -m pytest tests/hermetic/test_shipping_engine.py -q`.
- [ ] **Step 4: Implement the module** per the Interfaces. Rules are evaluated per quote; the fallback replaces rules when the order has no usable quote (served but nothing quoted, or every quote dropped as partial); `served False` stops before any fallback. Use `decimal.Decimal` for the cents and ceil steps. Messages come from `DEFAULT_MESSAGES`, overridden by the zone's `not_offered_message` for `not_shipped`, `not_offered` and `not_covered`.
- [ ] **Step 5: Run to verify they pass** and run the whole hermetic suite (`python -m pytest tests/hermetic -q`); ruff clean.
- [ ] **Step 6: Commit** `feat: shipping pricing engine`.

---

### Task 3: Data shaping

**Files:**
- Create: `dsi_catalogue/shipping_policy_data.py`
- Test: `tests/hermetic/test_shipping_policy_data.py`

**Interfaces:**
- Consumes: `LABELS` and the policy/order shapes from Task 2.
- Produces:
  - `MAX_ITEMS = 200`, `MAX_BOXES = 50`, `MAX_QUOTES = 20`; `class InputError(ValueError)`.
  - `parse_json_arg(value, default)`: a JSON string or an already parsed value.
  - `clean_items(items) -> list[{"item_code","qty"}]`, `clean_boxes(boxes) -> list[{"weight_kg","volume_l"}]`, `clean_quotes(quotes) -> list[{"service_id","name","amount","boxes_quoted"}]`; each raises `InputError` on oversize lists, non-numeric, negative or non-finite values, missing `service_id`, `qty <= 0`.
  - `policy_from_rows(policy_row: dict, zone_rows: list[dict], set_rows: list[dict], country_codes: dict[str, str]) -> policy`. Rows use the doctype field names from Task 4 (`active`, `not_covered`, `default_fallback_method`, `default_fallback_amount`, `rules`; zone `zone_name`, `enabled`, `not_shipped`, `everywhere_else`, `countries` as `[{"country": name}]`, `customer_line`, `not_offered_message`, `fallback_method`, `fallback_amount`; set `set_name`, `item_groups` as `[{"item_group"}]`, `items` as `[{"item"}]`). `country_codes` maps Country name to its ISO code; the result is upper-case.
  - `rows_from_policy(policy: dict, country_names: dict[str, str]) -> {"policy": dict, "zones": list[dict], "item_sets": list[dict]}`, the exact inverse (`country_names` maps code to Country name).
  - `public_summary_from(policy: dict, country_names: dict[str, str], translate) -> dict`: `{"active": False, "zones": []}` when off, otherwise `{"active": True, "zones": [{"name","countries":[{"code","name"}],"customer_line","delivers"}]}` with the line passed through `translate(text)`. No rules, formulas, fallbacks or amounts.
  - `response_from(result: dict, translate) -> dict`: the guest response (`policy_active`, `zone`, `offered`, `reason`, `message` translated, `offers` as `{service_id, name, amount, source, reason}`); never `trace`.
  - `snapshot_version(policy: dict) -> str` (stable hash, independent of key order).
  - `diff_policy(current: dict, draft: dict) -> {"zones": {"added","removed","changed"}, "item_sets": {...same}, "rules": {"added","removed","changed","order_changed"}, "settings": [keys]}`; rules are identified by `name`, a rule without a `name` counts as added.

- [ ] **Step 1: Write the failing tests:** `test_clean_inputs_accept_json_strings_and_lists`; `test_clean_inputs_refuse_oversize_negative_and_non_finite_values` (201 items, 51 boxes, 21 quotes, `qty 0`, `amount -1`, `float("nan")`, `float("inf")`, missing `service_id`, `boxes_quoted -1`); `test_policy_round_trips_through_rows` (`policy_from_rows(*rows_from_policy(example))` equals the example fixture, with Select labels and Country names mapped both ways); `test_public_summary_hides_everything_but_zones_while_active` and `..._is_empty_when_off`; `test_guest_response_has_no_trace_or_formula_text`; `test_snapshot_version_is_stable_and_sensitive` (key order does not change it; one changed amount does); `test_diff_reports_added_removed_changed_zones_and_rule_order`.
- [ ] **Step 2: Run to verify failure:** `python -m pytest tests/hermetic/test_shipping_policy_data.py -q`.
- [ ] **Step 3: Implement the module** (no `frappe` import).
- [ ] **Step 4: Run to verify they pass** and the full hermetic suite; ruff clean.
- [ ] **Step 5: Commit** `feat: shipping policy data shaping`.

---

### Task 4: Doctypes, controllers, loader, and the first erp2 install

**Files:**
- Create: the seven doctype folders (each with `__init__.py`, `<name>.json`, `<name>.py`; the three non-child ones also `test_<name>.py`), `dsi_catalogue/shipping_policy.py` (loader only for now), `dsi_catalogue/dsi_catalogue/doctype/shipping_policy/shipping_policy.js` (empty stub, filled in Task 7), `tools/erp2_push.sh`, `tools/shipping_policy_files.txt`
- Test: `tests/hermetic/test_shipping_doctypes.py`; Frappe-native tests in the three `test_*.py` files

**Interfaces:**
- Consumes: Tasks 2 and 3.
- Produces (exact field names; the Select strings are those in `LABELS`):
  - **Shipping Zone** (autoname `field:zone_name`): `zone_name` Data reqd, `enabled` Check default 1, `not_shipped` Check, `everywhere_else` Check, `countries` Table of **Shipping Zone Country** (`country` Link Country reqd), `customer_line` Small Text, `not_offered_message` Small Text, `fallback_method` Select default `Use the policy default`, `fallback_amount` Float (shown only for `Charge a fixed amount`).
  - **Shipping Item Set** (autoname `field:set_name`): `set_name`, `item_groups` Table of **Shipping Item Set Group** (`item_group` Link Item Group), `items` Table of **Shipping Item Set Item** (`item` Link Item).
  - **Shipping Policy** (single): `active` Check default 0, `not_covered` Select default `Pass the carrier price through`, `default_fallback_method` Select default `Don't offer delivery`, `default_fallback_amount` Float, `rules_note` HTML holding the asterisk sentence, `rules` Table of **Shipping Policy Rule**; track changes on.
  - **Shipping Policy Rule** (child): `enabled` Check default 0, `label` Data, `zone` Link Shipping Zone reqd, `item_set` Link Shipping Item Set (description is the asterisk sentence), `min_order_value`, `max_order_value`, `min_weight_kg`, `max_weight_kg`, `min_volume_l`, `max_volume_l` Float, `method` Select default `Carrier price`, `formula` Small Text (mandatory when `Custom formula`), `round_up` Check.
  - All top-level doctypes: module `dsi_catalogue`, permissions only `System Manager` (read, write, create, delete); child doctypes have none.
  - `load_policy(overrides: dict | None = None) -> policy` in `shipping_policy.py`: reads the single, all zones and item sets, and `Country.code`, then calls `policy_from_rows`; `overrides` replaces the policy row's fields (used by controllers to validate an unsaved document).
  - Controllers: `ShippingZone.validate` (a country in another enabled zone is refused naming that zone; a second enabled Everywhere-else zone is refused; an Everywhere-else zone with countries is refused), `ShippingItemSet.validate` (needs a group or an item), `ShippingPolicy.validate` (builds the policy with the unsaved rules and returns `validate_policy` messages through `frappe.throw`).
  - `tools/erp2_push.sh [--restart]`: reads `tools/shipping_policy_files.txt` (one repo-relative path per line), refuses any path outside `dsi_catalogue/shipping_*`, `dsi_catalogue/dsi_catalogue/doctype/shipping_*` and `dsi_catalogue/dsi_catalogue/page/shipping_policy_wizard/`, copies each to `erp2-sandbox:~/dsi_catalogue_dev/<path>`, runs `bench --site erp.shaik.net reload-doc dsi_catalogue doctype <name>` for every doctype folder listed (child doctypes first, then zone, item set, rule, policy) and `reload-doc ... page shipping_policy_wizard` when listed, then `clear-cache`; `--restart` also restarts `frappe_docker-backend-1`. Running it twice changes nothing.

- [ ] **Step 1: Write the failing hermetic tests** in `test_shipping_doctypes.py`: `test_every_doctype_is_standard_and_in_the_module` (module `dsi_catalogue`, no `custom` key); `test_only_system_manager_has_permissions`; `test_select_options_match_the_engine_labels` (each Select's options equal the keys of the matching `LABELS` dict, in that order); `test_new_rule_rows_start_disabled`; `test_policy_defaults_ship_the_policy_off`; `test_asterisk_sentence_is_on_the_form_and_the_item_set_field` (the exact sentence from Global Constraints); `test_every_field_the_data_module_reads_exists` (field names used by `policy_from_rows` are present in the JSON).
- [ ] **Step 2: Run to verify failure:** `python -m pytest tests/hermetic/test_shipping_doctypes.py -q`.
- [ ] **Step 3: Create the doctype JSON files and controllers** following `storefront_settings.json` (single) and `website_item_gallery_image.json` (child) for structure; copy `field_order`, `track_changes`, `sort_field` conventions. List every new file in `tools/shipping_policy_files.txt`.
- [ ] **Step 4: Run hermetic tests** (suite green, ruff clean).
- [ ] **Step 5: Write the Frappe-native tests** (base class `from frappe.tests.classes.integration_test_case import IntegrationTestCase`): `test_zone_refuses_a_country_in_another_enabled_zone` (message names the other zone), `test_disabled_zone_may_repeat_a_country`, `test_only_one_everywhere_else_zone`, `test_item_set_needs_a_group_or_an_item`, `test_policy_refuses_an_inverted_band_and_a_bad_formula_with_plain_messages`, `test_loader_returns_the_example_policy_after_saving_it` (seed the example fixture through `rows_from_policy`, assert `load_policy()` equals it).
- [ ] **Step 6: Install on erp2:** `tools/erp2_push.sh`; then `ssh erp2-sandbox 'docker exec frappe_docker-backend-1 bash -c "cd /home/frappe/frappe-bench && bench --site erp.shaik.net run-tests --module dsi_catalogue.dsi_catalogue.doctype.shipping_zone.test_shipping_zone"'` for each of the three test modules. Expected: all pass. Also confirm on erp2 that the Custom Field, Property Setter and Custom DocPerm row counts are the same as before the install (record them first).
- [ ] **Step 7: Commit** `feat: shipping policy doctypes, controllers and erp2 push script`.

---

### Task 5: Guest methods

**Files:**
- Modify: `dsi_catalogue/shipping_policy.py`
- Test: Frappe-native `dsi_catalogue/dsi_catalogue/doctype/shipping_policy/test_shipping_policy.py`

**Interfaces:**
- Consumes: Tasks 2–4.
- Produces:
  - `@frappe.whitelist(allow_guest=True) price_delivery(country_code, items, boxes, quotes, language=None) -> dict`: parses and cleans inputs (`InputError` becomes a `frappe.ValidationError` with a plain message), builds the order (groups, order value, served), calls `resolve`, returns `response_from(result, translate)`. Not cached.
  - `@frappe.whitelist(allow_guest=True) public_summary(language=None) -> dict`.
  - `build_order(country_code, items, boxes, quotes) -> order`: the one place an order dict is assembled (cleaned inputs, groups, order value, served flag). `price_delivery` and the wizard's `try_order` both call it, so they cannot drift apart.
  - `item_groups_for(item_codes) -> dict[str, list[str]]`: each item's group plus all its ancestor groups (Item Group `lft`/`rgt`); unknown items map to `[]`.
  - `order_value_for(items) -> float`: sum of `Item Price` (price list `"USD - Online"`, selling) times quantity; unknown or unpriced items count as 0.
  - `country_is_served(country_code, heaviest_box_kg) -> bool`: some enabled Shipping Service lists the country and (its `max_weight_kg` is 0 or at least the heaviest carton); true when the shipping doctypes are absent.
  - `translate(text, language)`: `frappe._(text, lang=language)`, English when no translation exists.

- [ ] **Step 1: Write the failing native tests:** `test_guest_can_price_and_read_the_summary_but_not_the_tables` (as Guest: both methods return; `frappe.get_list("Shipping Zone")` raises `PermissionError`); `test_policy_off_returns_the_quotes_unchanged`; `test_example_policy_prices_a_gcc_perfume_order_at_39` (seed the example through `rows_from_policy` with `active` true; one quote 38.20); `test_two_calls_around_a_policy_change_see_the_change` (no caching: change the GCC rule, call again, new price); `test_unknown_and_unpriced_items_count_as_zero_and_a_groupless_item_matches_only_by_code`; `test_order_with_no_weights_or_boxes_still_prices` (no cartons, quote with `boxes_quoted 0`, weight and volume bands compared against 0); `test_translated_zone_line_and_message_follow_the_language` (create a Translation for the zone line in `ar`); `test_oversized_or_negative_input_is_refused`; `test_unserved_country_is_not_offered_even_with_a_fallback` (skip with a message when erp2 lacks Shipping Service records, otherwise create one service for `AE` and check `FR` is refused).
- [ ] **Step 2: Run to verify failure** on erp2 (push, then the module's `run-tests`).
- [ ] **Step 3: Implement the methods** in `shipping_policy.py` (add the file to the push list).
- [ ] **Step 4: Run to verify they pass** on erp2; also `curl -s -X POST https://erp2.shaik.net/api/method/dsi_catalogue.shipping_policy.price_delivery -H 'Content-Type: application/json' -d '{...}'` as a guest returns the expected JSON, and a guest `GET /api/resource/Shipping%20Zone` returns 403.
- [ ] **Step 5: Commit** `feat: guest price_delivery and public_summary`.

---

### Task 6: Wizard methods

**Files:**
- Create: `dsi_catalogue/shipping_policy_wizard.py`
- Test: Frappe-native tests in the same `test_shipping_policy.py`

**Interfaces:**
- Consumes: Tasks 2–5.
- Produces (all call `frappe.only_for("System Manager")` first):
  - `wizard_state() -> {"policy": policy, "version": str, "countries": [{"code","name"}], "served_codes": [str], "price_list": str, "carrier_available": bool}`.
  - `try_order(draft, order, ask_carrier=False, city=None) -> {"result": result, "previews": list, "order": dict}`: prices the draft (not the saved policy); `order` is `{country_code, items:[{item_code, qty}], order_value|None, boxes|None, quotes|None}`; missing value, boxes and groups are filled from the ERP (weight per unit and package dimensions); with `ask_carrier` the quotes come from `dsi_shipping.api.get_shipping_rates` per box, summed per service with `boxes_quoted` counted, and any carrier error yields no quotes. Returns the full result with `trace`.
  - `check_formula(source) -> {"ok": bool, "error": str|None}`.
  - `preview_changes(draft) -> {"changes": diff, "problems": [str]}`: `diff_policy(saved, draft)` plus the `validate_policy` sentences; the Review step shows it and keeps Apply disabled while `problems` is not empty.
  - `apply_policy(draft, expected_version) -> {"version": str, "changes": dict}`: refuses a stale `expected_version`; validates the draft with `validate_policy`; in one transaction upserts item sets and zones, rewrites the policy settings and rules (reusing rule rows by `name`), then deletes zones and item sets absent from the draft; on any failure nothing is written. Afterwards enqueues a website refresh through `dsi_core.outbox.enqueue("website-revalidate", ...)` when `dsi_core` is importable (skip silently when it is not).

- [ ] **Step 1: Write the failing native tests:** `test_non_system_managers_are_refused` (Guest and a user with only a Sales User role); `test_apply_writes_zones_sets_and_rules_in_one_go` (round trip through `wizard_state`); `test_apply_with_one_bad_rule_writes_nothing` (zone and set counts unchanged); `test_apply_refuses_a_stale_version`; `test_apply_drops_a_zone_that_rules_no_longer_use_in_the_right_order`; `test_apply_refuses_duplicate_zone_names_and_a_country_in_two_zones`; `test_try_order_prices_the_draft_not_the_saved_policy`; `test_try_order_carrier_error_means_no_quotes` (patch the carrier call to raise); `test_check_formula_reports_a_plain_error`; `test_preview_changes_reports_the_diff_and_the_problems`.
- [ ] **Step 2: Run to verify failure** on erp2.
- [ ] **Step 3: Implement the module** (add to the push list; restart the backend when the push script asks).
- [ ] **Step 4: Run to verify they pass** on erp2.
- [ ] **Step 5: Commit** `feat: wizard methods with atomic apply`.

---

### Task 7: Wizard page shell, price tape and the form button

**Files:**
- Create: `dsi_catalogue/dsi_catalogue/page/shipping_policy_wizard/{__init__.py, shipping_policy_wizard.json, shipping_policy_wizard.py, shipping_policy_wizard.js, shipping_policy_wizard.css}`, `tests/browser/shipping_policy_wizard.mjs`
- Modify: `dsi_catalogue/dsi_catalogue/doctype/shipping_policy/shipping_policy.js`, `tools/shipping_policy_files.txt`

**Interfaces:**
- Consumes: `wizard_state`, `try_order`, `check_formula` from Task 6.
- Produces:
  - Page `shipping-policy-wizard`, module `dsi_catalogue`, role System Manager only, class `dsi_catalogue.ShippingPolicyWizard` with a store `{original, draft, version, step, dirty}`; the draft is kept in `localStorage` under `dsi_catalogue.sp_draft` with the server `version` and restored only when that version still matches (otherwise discarded with a notice).
  - A Shipping Policy form button "Open the wizard".
  - `data-testid` hooks that Tasks 8 and 9 and the browser script use: `step-tab-zones`, `step-tab-rules`, `step-tab-fallback`, `step-tab-review`, `draft-banner`, `tape-country`, `tape-city`, `tape-add-item`, `tape-order-value`, `tape-weight`, `tape-volume`, `tape-quote`, `tape-ask-carrier`, `tape-unavailable`, `tape-total`, `tape-lines`.
  - Look, from the spec: surfaces use the desk's own variables; accents `--sp-brass` (light `#9A7116`, dark `#D8B259`), `--sp-sage` (`#2B7A57` / `#74C9A0`), `--sp-brick` (`#A8473A` / `#E5907F`) switched on the desk's dark theme attribute; sentence case; no all-caps or mono labels; the tape is the one memorable element.
  - Tape behaviour: ship to, items with quantity (or a typed order value), weight and volume (filled from the items when the ERP has them, otherwise typed), the carrier quote (typed, or asked from the carrier); re-prices through `try_order` 250 ms after any change; prints carrier quote, matched rule, rounding and "Customer pays"; says plainly when the policy is off, delivery is not offered, or the fallback applied.
  - Keyboard-reachable controls with visible focus, `prefers-reduced-motion` respected, layout stacks below 900 px.

- [ ] **Step 1: Write the browser script scaffold** `tests/browser/shipping_policy_wizard.mjs`: loads puppeteer from `$PUPPETEER` (see `~/.cache/browser-scripts/env.sh`), logs in at `https://erp2.shaik.net/login` with `$ERP_USER` and `$ERP_PASSWORD`, helpers `open()`, `shot(name)`, `expectText(testid, text)`, writes screenshots to `$OUT_DIR`. First check (red until the page exists): the four step tabs and `tape-total` are present.
- [ ] **Step 2: Create the sandbox test user** on erp2 with a random password saved to `~/.cache/shipping-policy/erp2-test-password` (mode 600, never committed): a System Manager user `wizard-test@dsi.local`, via `bench execute` on erp2. Run the script: it fails.
- [ ] **Step 3: Build the page** (JSON and Python per `dsi_shipping`'s `shipping_dashboard` page as the template, JS and CSS as described above); push with `tools/erp2_push.sh`.
- [ ] **Step 4: Run the script:** the four tabs and the tape render; with the empty policy the tape says the policy is off and "Customer pays" equals the typed carrier quote; screenshots in light and dark and at 760 px width are attached to the task report; no console errors.
- [ ] **Step 5: Commit** `feat: shipping policy wizard shell and price tape`.

---

### Task 8: Zones and Rules steps

**Files:**
- Modify: `shipping_policy_wizard.js`, `shipping_policy_wizard.css`, `tests/browser/shipping_policy_wizard.mjs`

**Interfaces:**
- Consumes: the store and hooks from Task 7; `check_formula`.
- Produces hooks: `zone-card`, `zone-add`, `zone-name`, `zone-country-add`, `zone-country-chip`, `zone-not-shipped`, `zone-everywhere-else`, `zone-line`, `zone-not-offered-message`, `zone-unserved-warning`, `rule-card`, `rule-add`, `rule-up`, `rule-down`, `rule-disable`, `rule-delete`, `chip-zone`, `chip-items`, `chip-charge`, `rule-round-up`, `rule-formula-input`, `rule-formula-error`, `rule-only-when`, `rule-price`, `itemset-new`, `rules-note`.

- [ ] **Step 1: Extend the browser script (red):** build the example policy from the fixture through the UI. Zones: add Bahrain (BH), GCC (AE SA KW OM QA), Rest of world (Everywhere else), Not shipped (RU BY, message). Item set `Watches` (item group `Watches`). Four rules by clicking chips. Then assert on `tape-total`: GCC Dubai with a typed quote 38.20 reads $39.00; a watch-only order reads Free; a US order with quote 38.20 reads $45.00; RU shows delivery not offered with the zone message; `rules-note` contains the asterisk sentence; `rule-price` on the GCC every-item rule reads $39.
- [ ] **Step 2: Build the Zones step:** cards with country chips added from `wizard_state.countries` (search by name); a country already in another enabled zone is refused inline naming that zone; the two switches (Everywhere-else clears the chips and disables adding); customer line and not-offered message fields; a warning listing zone countries missing from `served_codes`.
- [ ] **Step 3: Build the Rules step:** each rule is a sentence of chips (zone, items, charge); chips open an inline editor; the items chip offers "every item" or a saved set and a "new set" editor (name, item groups, items); the charge chip offers carrier price, free, custom formula (input checked through `check_formula` on a 300 ms debounce, error shown in plain words) and the round-up switch (hidden for free); an "Only when" row with the six limits (placeholder "no limit"); drag to reorder with up/down buttons for the keyboard; add, disable, delete; each card's `rule-price` comes from `previews`; the asterisk sentence sits under the list, and the items chip carries an asterisk.
- [ ] **Step 4: Run the script:** all assertions pass; screenshots of Zones and Rules in light and dark; no console errors.
- [ ] **Step 5: Commit** `feat: wizard zones and rules steps`.

---

### Task 9: Fallback and Review steps, and Apply

**Files:**
- Modify: `shipping_policy_wizard.js`, `shipping_policy_wizard.css`, `tests/browser/shipping_policy_wizard.mjs`

**Interfaces:**
- Consumes: Tasks 7 and 8; `preview_changes` and `apply_policy` from Task 6.
- Produces hooks: `fallback-zone` (one per zone), `fallback-method`, `fallback-amount`, `fallback-policy-default`, `not-covered`, `review-summary`, `review-diff`, `policy-active`, `apply-button`, `apply-error`.

- [ ] **Step 1: Extend the browser script (red):** set GCC fallback to a fixed 35, the policy default to don't offer, "Anything not covered" to pass through; tick `tape-unavailable` for a GCC order and assert the tape shows Standard delivery at $35.00 and, for Bahrain, delivery not offered; open Review and assert `review-diff` lists the added zones and rules and `review-summary` reads each zone in plain language; click `policy-active` then `apply-button`; reload the page and assert the policy persisted and `draft-banner` is gone; edit one rule while active and assert the banner says edits go live on Apply.
- [ ] **Step 2: Build the Fallback step:** per zone choose use the policy default, don't offer delivery, fixed amount, free; below, the policy default and "Anything not covered"; the tape's "carrier can't quote" simulation (`tape-unavailable`) works from here and from every other step.
- [ ] **Step 3: Build the Review step:** the diff and problems from `preview_changes(draft)`, the plain-language summary, the Active switch with its state, and Apply (disabled until the draft validates; server messages from `apply_policy` shown in `apply-error`, a stale version offers "Reload"). Apply clears the stored draft.
- [ ] **Step 4: Run the script end to end:** passes; light, dark and 760 px screenshots of all four steps.
- [ ] **Step 5: Commit** `feat: wizard fallback, review and apply`.

---

### Task 10: End to end on erp2, docs and cleanup

**Files:**
- Modify: `README.md`, `CHANGELOG.md`

- [ ] **Step 1: Hermetic suite:** `python -m pytest tests/hermetic -q` and `ruff check dsi_catalogue tests` are green (the same commands CI runs).
- [ ] **Step 2: Native suite on erp2:** `tools/erp2_push.sh --restart`, then run the three native test modules; all pass.
- [ ] **Step 3: Guest matrix with curl against `https://erp2.shaik.net`:** `price_delivery` and `public_summary` answer as Guest; `GET /api/resource/Shipping%20Zone`, `/Shipping%20Policy` and `/Shipping%20Item%20Set` return 403; a call to each wizard method as Guest returns 403; as the test user with the Sales User role only, 403.
- [ ] **Step 4: Parity:** with the policy off, `price_delivery` returns the posted quotes unchanged (compare JSON), including a partial quote; a hermetic or native test names this "promotion proof". With the policy on, run the pricing cases of Task 2's tests (at least ten) through both `price_delivery` (as Guest, with curl) and `try_order` (as the test user): the amounts are identical.
- [ ] **Step 5: Final browser run and screenshots** of the full flow, collected in `~/Downloads/shipping-policy-erp2/`.
- [ ] **Step 6: Docs:** README gains the Shipping Policy row in the modules table, the two guest methods under the public API surface (read-only, ungated), "no new site_config keys", and a short promotion note: push the live host's unpushed commits first, `bench migrate`, the policy arrives switched off. CHANGELOG gets an entry.
- [ ] **Step 7: Cleanup on erp2:** delete the test user, the test zones, item sets, rules and Translation records the tests created, and set the policy back to off and empty; confirm the Custom Field, Property Setter and Custom DocPerm counts equal the Task 4 baseline.
- [ ] **Step 8: Commit** `docs: shipping policy README and changelog`, then report the branch, the commit list and the erp2 evidence. Do not push.
