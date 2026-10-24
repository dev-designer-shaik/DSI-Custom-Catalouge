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


---

## Part 1b: interim rate card, price source, integration test (added 2026-10-22)

**Spec:** the section "Part 1b: the interim rate card" of the design. **Execution order of the whole plan:** Task 6 (done first), then 11, 12, 13, 14, 15, then 7, 8, 9, 16, 10. The numbers are labels, not the order.

**Extra Global Constraints for Tasks 11-16**
- The rate card lives on the Shipping Policy single (`rate_card`, JSON text) with `price_source`; no new doctypes, no Custom Fields. The real Shopify export stays outside this public repo (`~/shipping-interim-rates/`); the repo only holds a trimmed sample.
- Units: weights in pounds on the card (2.20462262 pounds to the kilogram, rounded to three decimals before a tier is chosen); prices in USD; an item without a weight counts as 0.35 kg and is flagged.
- Product code = Website Item index key without braces, upper-case. Profile precedence: first matching profile wins; `*` matches everything and must be last.
- Price source default is "Carrier"; a policy that is switched off returns the carrier quotes unchanged whatever the price source says. In rate-card mode the carrier is never called and `served` is not checked; Not shipped zones still refuse.
- New reason code: `too_heavy`. New offer field: `estimated_days` (integer or null). New guest response field: `price_source`.
- The guest response never carries card contents (tiers, zones, patterns); only the priced offer.
- The website change is committed locally; pushing is the owner's decision.

**Extra Review Focus for Tasks 11-16**
1. **Weights.** Unknown, zero, negative or non-numeric weights count as 0.35 kg and are flagged; quantities multiply; a weight exactly on a tier edge, inside a gap, below the first tier and above the last tier each have a pinned result. (Tasks 11, 13)
2. **Mixed carts.** Profile prices add; one profile that cannot be priced makes the whole order not offered, never a partial price. (Task 11)
3. **Hostile or broken cards.** A card with duplicate countries, overlapping tiers, huge lists, NaN prices or text where numbers belong is refused by validation and never crashes pricing. (Tasks 11, 12)
4. **Import files as people send them.** BOM, Windows line endings, quoted commas, accented country names, trailing blanks, a file with the wrong columns, an empty file. (Task 11, 14)
5. **The website fails safe.** ERP method missing, slow, or returning nothing useful falls back to today's behaviour, and the amount charged equals the amount shown. (Task 15)

### Task 11: Rate card core

**Files:**
- Create: `dsi_catalogue/shipping_rate_card.py`, `tests/hermetic/fixtures/shopify_shipping_config_sample.csv`, `tests/hermetic/fixtures/shopify_countries_not_delivered_sample.csv`
- Test: `tests/hermetic/test_shipping_rate_card.py`

**Interfaces:**
- Produces (pure, no `frappe` import):
  - `LB_PER_KG = 2.20462262`, `DEFAULT_ITEM_WEIGHT_KG = 0.35`, `class RateCardError(ValueError)`, `KNOWN_PROFILES` = `{"Dangerous Goods Petite": ["F-*-PT", "F-CR-PC-*"], "Refill Service": ["F-*-RF"], "Dangerous Goods": ["F-*"], "Rest of the World Allowed Items": ["*"]}` in that order.
  - Card shape: `{"version": 1, "currency": "USD", "unit": "lb", "profiles": [{"name", "match": [str], "zones": [{"name", "countries": [ISO2 upper], "method": str, "days": int|None, "tiers": [{"from": float|None, "to": float|None, "price": float}]}]}]}`; a flat zone has exactly one tier with `from` and `to` both None.
  - `normalize_index_key(value) -> str`; `classify_item(card, index_key) -> int | None` (index of the first profile with a matching pattern; `fnmatch.fnmatchcase` on the normalised key and upper-cased pattern).
  - `parse_shopify_export(config_csv: str, not_delivered_csv: str | None = None) -> {"card", "not_delivered": [ISO2], "notes": [str]}`.
  - `validate_card(card) -> list[str]`; `card_summary(card) -> {"profiles","zones","tiers","countries"}`.
  - `price_card(card, order) -> {"priced": bool, "reason": None|"not_covered"|"too_heavy"|"not_offered", "total": float, "name": str, "days": int|None, "lines": [{"profile","zone","weight_lb","price","tier","items","defaulted_weight"}], "detail": str}`; `order` is `{"country_code", "items": [{"index_key", "weight_kg" (unit weight, kg, may be missing), "qty"}]}`.
- Consumes: nothing from earlier tasks.

- [ ] **Step 1: Write the sample files** (trimmed from the real export, real prices): Dangerous Goods USA (first four tiers and the last), Europe (2 tiers, a few countries including accented names such as `CI - Côte d'Ivoire` in a Tier 2 zone and `AX - Åland Islands`), Bahrain free flat, GCC-4 flat 35, Allowed worldwide (3 tiers), Allowed GCC free, Refill USA (2 tiers), Petite GCC flat 20 and Petite International (2 tiers, including the odd `4.1` start); not-delivered sample with 3 countries.
- [ ] **Step 2: Write the failing tests** in `test_shipping_rate_card.py`: `test_parse_reads_profiles_zones_tiers_and_flat_rows`; `test_parse_puts_the_catch_all_last_and_gives_known_profiles_their_patterns`; `test_parse_notes_what_it_cannot_use` (a bad country token, a row with only one weight, an unknown profile name which gets no patterns and a note, a bad price); `test_parse_not_delivered_countries`; `test_parse_refuses_a_file_without_the_expected_columns_or_rows` (empty text, wrong header, only blank lines, BOM and CRLF accepted); `test_validate_card_accepts_the_parsed_sample` and `test_validate_card_reports_each_problem` (parametrised: duplicate country in a profile, overlapping tiers, from above to, negative or non-finite price, flat zone with two tiers, zone with no countries, unknown country code, empty profile name, duplicate profile name, a catch-all profile that is not last, a profile with no patterns, oversized lists); `test_classify_item_by_product_code` (`{F-CR-OSA-M-PT}` and `{F-CR-PC-W}` are petite, `{F-CR-OSA-M-100-RF}` is refill, `{F-CR-CSE-W-40}` is dangerous goods, `{P-AA-AT1-IG}` and an empty key are allowed items; braces, case and spaces do not matter); `test_price_card_examples_from_the_export` (USA dangerous goods: 0.5 lb is 35, 0.65 lb in the gap is 60, exactly 2 lb is 60, 2.004 lb is 80, 0.9 kg is 60 and 1 kg is 80; Bahrain dangerous goods 0 named "Free Shipping (1-2 days)"; GCC-4 flat 35 at any weight; petite GCC flat 20; allowed items GCC 0); `test_price_card_adds_the_profiles_in_a_cart` (dangerous goods 1.5 lb plus allowed items 1.0 lb to the USA is 60 + 40 = 100, named after the slower profile, days the longest); `test_price_card_refuses_what_it_cannot_price` (8 lb dangerous goods to Europe is `too_heavy`; a refill to AE is `not_covered`; a country in no zone is `not_covered`; an empty cart is `not_offered`; one unpriceable profile makes the whole order not priced); `test_price_card_weights` (unit weight times quantity, several items of a profile add up, a missing, zero, negative, NaN or boolean weight counts as 0.35 kg per unit and sets `defaulted_weight`, quantity 0 is ignored); `test_price_card_tier_edges_and_float_noise` (0.6000000001 lb rounds to 0.600 and stays in the first tier; below the first `from` pays the first tier); `test_nothing_raises_on_odd_input` (sweep over a junk card, junk order, junk items, junk country).
- [ ] **Step 3: Run to verify failure:** `python3 -m pytest tests/hermetic/test_shipping_rate_card.py -q` fails with an import error.
- [ ] **Step 4: Implement the module** per the Interfaces. CSV with the standard `csv` module (`utf-8-sig`); header names matched case-insensitively with spaces collapsed; money and weights parsed from text (`float`, reject non-finite); `days` is the last whole number inside the method's brackets; country tokens split on `;`, code is the text before ` - ` (two ASCII letters, upper-cased); profile order is the `KNOWN_PROFILES` order, unknown profiles after the known non-catch-all ones, the catch-all profile last; prices to cents with `Decimal(str(x))` half up.
- [ ] **Step 5: Run to verify they pass,** the whole hermetic suite and ruff on the two Python files.
- [ ] **Step 6: Commit** `feat: interim rate card core`.

### Task 12: Price source in the engine and the data module

**Files:**
- Modify: `dsi_catalogue/shipping_engine.py`, `dsi_catalogue/shipping_policy_data.py`, `tests/hermetic/fixtures/shipping_policy_example.json`
- Test: `tests/hermetic/test_shipping_engine.py`, `tests/hermetic/test_shipping_policy_data.py` (extend; keep every existing test passing)

**Interfaces:**
- Consumes: Task 11 (`price_card`, `validate_card`).
- Produces:
  - Policy shape gains `"price_source": "carrier"|"rate_card"` and `"rate_card": card|None`. Order items gain optional `"index_key"` and `"weight_kg"` (unit weight). Every offer gains `"estimated_days": int|None`; trace entries may have `kind` `"rate_card"`.
  - `LABELS["price_source"] = {"Carrier": "carrier", "Rate card": "rate_card"}`; `LABELS["zone_fallback"]` becomes `Use the policy default, Don't offer delivery, Charge a fixed amount, Free, Use the rate card`; `LABELS["default_fallback"]` becomes `Don't offer delivery, Charge a fixed amount, Free, Use the rate card` (codes `rate_card`).
  - `REASONS` gains `too_heavy`; `DEFAULT_MESSAGES["too_heavy"]` ("This order is heavier than our delivery rates cover.").
  - `resolve`: policy off returns quotes unchanged (as before). Otherwise a bad country is `not_served`. **Rate card mode**: the destination's zone with `not_shipped` refuses (`not_shipped`); quotes and `served` are ignored; `price_card` prices the order; success is one offer `{"service_id": "rate-card", "name": card name, "amount": total, "source": "rate_card", "reason": None, "estimated_days": days, "trace": [one `rate_card` entry per profile line, then the result]}`; failure is not offered with the card's reason (`not_covered`, `too_heavy`, `not_offered`), the zone's `not_offered_message` replacing the default text only for `not_covered`; no rate card at all is not offered with `not_covered`. **Carrier mode**: as before, and a fallback method `rate_card` (zone or policy default) prices the order from the card when the carrier cannot quote (offer `source` `rate_card`, `reason` `fallback_offer`; the card failing means not offered with the card's reason).
  - `validate_policy` also reports: an unknown `price_source`; `price_source` `rate_card` or any fallback `rate_card` with no card; every `validate_card` problem prefixed `Rate card: `.
  - Data module: `policy_from_rows` / `rows_from_policy` carry `price_source` (label) and `rate_card` (JSON text, `""` when none; invalid text reads as None); `response_from` adds `estimated_days` to each offer and `price_source` to the result; `diff_policy` adds `price_source` and `rate_card` to `settings` when they differ and a `rate_card` entry `{"added": [...], "removed": [...], "changed": [...]}` by profile name; `snapshot_version` changes when the card changes.

- [ ] **Step 1: Write the failing tests:** engine: `test_rate_card_mode_prices_the_example_orders` (the Task 11 sample card inside a policy: US dangerous goods 0.9 kg is 60 and 1 kg is 80; a cart of dangerous goods plus allowed items adds; Bahrain is free and still offered at 0.00); `test_rate_card_mode_ignores_carrier_quotes_and_served`; `test_rate_card_mode_still_honours_not_shipped_zones`; `test_rate_card_mode_refusals` (`too_heavy`, `not_covered`, empty cart, no card, junk card never raises); `test_carrier_fallback_can_use_the_rate_card` (no quote, zone fallback `rate_card`: offer `rate-card`, source `rate_card`, reason `fallback_offer`; with the policy default `rate_card` through `policy_default`); `test_policy_off_returns_quotes_unchanged_in_every_price_source`; `test_offers_carry_estimated_days_and_carrier_offers_carry_none`; `test_validate_policy_rate_card_problems`; `test_labels_keep_their_order_and_gain_the_new_entries`; data: `test_rate_card_round_trips_through_rows_and_the_fixture_still_matches`; `test_diff_reports_rate_card_profile_changes_and_price_source`; `test_guest_response_has_estimated_days_and_price_source_but_no_card_contents`; `test_snapshot_version_changes_with_the_card`; `test_broken_card_text_reads_as_no_card`. Update the example fixture with `"price_source": "carrier"` and `"rate_card": null`.
- [ ] **Step 2: Run to verify failure** (the new tests fail, the existing ones still pass).
- [ ] **Step 3: Implement** the changes in both modules; keep the pure modules free of `frappe`.
- [ ] **Step 4: Run to verify they pass:** `python3 -m pytest tests/hermetic -q` all green; ruff clean on the edited files.
- [ ] **Step 5: Commit** `feat: price source and rate card fallback in the engine`.

### Task 13: Price source in the Frappe layer and the guest method

**Files:**
- Modify: `dsi_catalogue/dsi_catalogue/doctype/shipping_policy/shipping_policy.json`, `.../shipping_policy/shipping_policy.py`, `.../shipping_zone/shipping_zone.json`, `dsi_catalogue/shipping_policy.py`, `tests/hermetic/test_shipping_doctypes.py`, the native tests in `.../shipping_policy/test_shipping_policy.py`

**Interfaces:**
- Consumes: Tasks 11, 12 and Task 5's `build_order`, `price_delivery`.
- Produces:
  - Shipping Policy fields `price_source` (Select, options as `LABELS["price_source"]`, default `Carrier`) and `rate_card` (Code, options `JSON`, read only on the form with a description pointing to the wizard); `Use the rate card` added to the zone's `fallback_method` and the policy's `default_fallback_method` options; `ShippingPolicy.validate` parses `rate_card` (invalid JSON is refused in plain words) and `validate_policy` covers the rest.
  - `load_policy` reads both fields.
  - `build_order(country_code, items, boxes, quotes, order_value=None, item_facts=False)`: with `item_facts` each item gets `index_key` (Website Item `custom_index_key` by item code, any publish state, `""` when none) and `weight_kg` (Item `weight_per_unit`, 0 when none) in at most two queries.
  - `price_delivery`: after loading the policy, in rate-card mode only the items are cleaned and used (boxes and quotes are not read; no `served` lookup), `item_facts` on; in carrier mode with any `rate_card` fallback configured, `item_facts` on; otherwise unchanged. The response carries `price_source`, and each offer `estimated_days`.
- [ ] **Step 1: Write the failing tests:** hermetic: `test_policy_has_price_source_and_rate_card_fields_with_safe_defaults`, `test_every_select_still_matches_the_engine_labels`, `test_the_data_module_reads_the_new_fields`; native: `test_rate_card_mode_prices_a_guest_order_end_to_end` (seed a policy with the sample card and `price_source` Rate card, active; call as Guest with items only), `test_rate_card_mode_needs_no_boxes_or_quotes_and_never_calls_the_carrier`, `test_carrier_mode_fallback_uses_the_rate_card_when_no_quote`, `test_policy_off_is_unchanged_in_rate_card_mode`, `test_unknown_items_and_missing_weights_use_the_default_weight`, `test_item_facts_use_at_most_two_queries` (`assertQueryCount`), `test_policy_refuses_invalid_rate_card_json_and_card_problems_in_plain_words`, `test_guest_response_never_contains_card_contents`.
- [ ] **Step 2: Run to verify failure** on erp2 (push, then `bench ... run-tests`).
- [ ] **Step 3: Implement**; push with `tools/erp2_push.sh --restart`; reload the doctypes (the script does it).
- [ ] **Step 4: Run to verify they pass** on erp2; over the wire, `curl` as a guest against `https://erp2.shaik.net` with a seeded active rate-card policy returns the priced offer with `estimated_days`; restore the policy to empty and off.
- [ ] **Step 5: Commit** `feat: price source and rate card in the guest price method`.

### Task 14: Wizard methods for the rate card, and the integration test

**Files:**
- Modify: `dsi_catalogue/shipping_policy_wizard.py`
- Create: `dsi_catalogue/shipping_integration_check.py` (pure helpers: sample order choice, response summarisers, verdict), `tests/hermetic/test_shipping_integration_check.py`
- Test: `dsi_catalogue/dsi_catalogue/doctype/shipping_policy/test_shipping_policy_wizard.py` (extend)

**Interfaces:**
- Consumes: Tasks 6, 11-13.
- Produces (System Manager only):
  - `import_rate_card(config_csv, not_delivered_csv=None) -> {"card", "not_delivered": [ISO2], "not_delivered_missing": [ISO2], "summary", "notes": [str], "problems": [str]}`: parses and validates only (files up to 1 MB each, text), marks country codes not in the ERP's Country list; writes nothing.
  - `try_order` prices a draft in rate-card mode (`draft.price_source`) with item facts; `wizard_state`, `preview_changes`, `apply_policy` carry `price_source` and `rate_card` through the existing draft (the card is validated before anything is written).
  - `test_integration(order=None) -> {"checks": [{"name": "erp_price_api"|"carrier"|"website", "ok": bool, "status": int|None, "ms": int, "summary": str, "detail": dict}], "verdict": str, "order": dict}`: default sample order = one published full-size fragrance and one published watch Website Item, quantity 1, destination `US`, city `New York`; check 1 POSTs `<site url>/api/method/dsi_catalogue.shipping_policy.price_delivery` (items, plus boxes and quotes only in carrier mode) and compares the offer amount with the amount `price_delivery` gives in-process for the saved policy; check 2 calls `dsi_shipping.api.get_shipping_rates` in-process for one sample parcel (ok means at least one rate; a carrier error is reported in plain words, today "account blocked"); check 3 POSTs `<website>/api/webshop/shipping/rates` where `<website>` is Storefront Settings `active_domain` (http or https only, no redirects followed, 15 s timeout, responses cut to 4 KB in `detail`) and reports the methods and amounts and whether they equal check 1. Nothing is written; no secrets or tokens in any output.
- [ ] **Step 1: Write the failing tests:** hermetic for the pure helpers (`pick_sample_order`, `summarize_policy_response`, `summarize_carrier_response`, `summarize_website_response`, `verdict_for`, URL guard); native: `test_import_rate_card_previews_without_writing`, `test_import_refuses_oversized_or_malformed_files_in_plain_words`, `test_import_marks_countries_missing_from_the_erp`, `test_try_order_in_rate_card_mode_prices_a_draft`, `test_apply_writes_price_source_and_the_card_atomically_and_refuses_a_broken_card`, `test_test_integration_runs_all_three_checks_with_mocked_http`, `test_test_integration_only_calls_the_configured_website`, `test_test_integration_survives_timeouts_and_garbage`, `test_non_system_managers_are_refused`.
- [ ] **Step 2: Run to verify failure,** **Step 3: Implement,** **Step 4: Run to verify they pass** on erp2 (the website check uses a mocked response in tests; one manual run against the real site is recorded in the report).
- [ ] **Step 5: Commit** `feat: wizard rate card import and integration test`.

### Task 15: The website asks the ERP for the price

**Files (website repo `/home/mdad/Website/DSI WEB N`, branch main, commit locally, do not push):**
- Create: `lib/shipping/policy-quote.ts`, `tests/shipping-policy-quote.test.mjs`
- Modify: `lib/shipping/quote.ts`

**Interfaces:**
- Consumes: the guest method `dsi_catalogue.shipping_policy.price_delivery` (items only in this task) through `erpMethod`.
- Produces: `askPolicyForOffers({ countryCode, cartItems }) -> Promise<PolicyOffers | null>` where `PolicyOffers` is `{ policyActive, priceSource, offered, reason, message, offers: [{ serviceId, name, amount, source, reason, estimatedDays }] }`, and `offersToShippingMethods(offers, currency) -> ShippingMethod[]`; `getShippingQuotes` calls the first before any carrier work: when `policyActive` and `priceSource === 'rate_card'`, a returned offer list becomes the methods (id `rate-card`, provider `DSI`, service name from the offer, `estimatedDays`, `shippingAmount` the offer amount, `scope` `International`, no dates), the carrier is not called, and a not-offered answer throws `ShippingQuoteError` with the ERP's message; anything else (policy off, carrier mode, ERP error, bad shape, timeout) leaves the current carrier path untouched. The packing plan and `package` in the result are still computed from the cart as today.
- [ ] **Step 1: Write the failing tests** (`node --test` style used by `tests/*.test.mjs`, stub `erpMethod` through `loadModule`): `rate card answer replaces the carrier path`, `carrier is never called in rate card mode`, `policy off keeps the carrier path`, `carrier mode keeps the carrier path`, `ERP error or timeout falls back to the carrier path`, `a malformed answer falls back`, `not offered throws the ERP message`, `the same function serves the payment re-quote (resolveShippingCost picks the offered method and its amount)`, `offers map to methods exactly (id, name, days, amount, no dates)`, `no weights or boxes are needed from the client`.
- [ ] **Step 2: Run to verify failure:** `npm test` (the new file fails, the 191 existing tests pass). **Step 3: Implement.** **Step 4:** `npm test` and `npx tsc --noEmit` clean. **Step 5: Commit** `Ask the ERP shipping policy for the delivery price first` (website repo commit rules: author `dev-designer-shaik`, same trailer, no push).

### Task 16: Rates step and Test tab

**Files:**
- Modify: `shipping_policy_wizard.js`, `shipping_policy_wizard.css`, `tests/browser/shipping_policy_wizard.mjs`

**Interfaces:**
- Consumes: Task 7's shell and store, Task 14's `import_rate_card` and `test_integration`, Task 9's Review/Apply.
- Produces hooks: `step-tab-rates`, `step-tab-test`, `price-source`, `rates-import`, `rates-file-config`, `rates-file-notdelivered`, `rates-import-preview`, `rates-import-apply`, `rates-summary`, `rates-profile`, `rates-zone`, `rates-tier-price`, `rates-problems`, `rates-not-used-note`, `test-run`, `test-check-erp-api`, `test-check-carrier`, `test-check-website`, `test-verdict`.
- [ ] **Step 1: Extend the browser script (red):** import the real export (from `$RATES_DIR`) through the Rates step, check the preview counts (4 profiles, the zones, 49 tiers, 12 not-delivered countries) and that countries missing from the ERP are listed; keep the import in the draft; set price source to Rate card; assert the tape prices a USA order from the card; open Review and Apply; run the Test tab and assert three result rows and a verdict.
- [ ] **Step 2: Build the Rates step:** price source switch with a plain sentence saying what it means; import panel (two file choosers plus paste boxes, preview, "Use these rates" puts the card and the Not shipped zone into the draft); a read view of each profile, zone, countries (chips), method text and tier prices; tier prices editable inline (money step 0.01), patterns editable per profile, countries editable through the same chips as Zones; problems from `validate_card` shown in plain words; a quiet note on the Rules step while the price source is the rate card ("Rules are not used while the price source is the rate card").
- [ ] **Step 3: Build the Test tab:** "Run the tests" button, three result cards (status, time, summary, details collapsed), the verdict line, the sample order editable (country, city, items), keyboard operable, errors in plain words.
- [ ] **Step 4: Run the script:** passes; light, dark and 760 px screenshots of both. **Step 5: Commit** `feat: wizard rates step and integration test tab`.
