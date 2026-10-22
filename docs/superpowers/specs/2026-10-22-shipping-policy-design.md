# Shipping Policy: delivery pricing set from the ERP

Status: draft for approval, 2026-10-22. Written for the catalogue app (`dsi_catalogue`), built and tested on erp2 first, promoted to the live ERP afterwards.

## Why this exists

Today a customer's delivery price is whatever Aramex quotes, summed per carton by the website (`lib/shipping/quote.ts` in the website repo, asking `dsi_shipping.api.get_shipping_rates` once per carton). Nothing in the ERP lets the business decide what a customer pays. The FAQ and the Shipping and Delivery page carry typed prices and country lists that disagree with the checkout (review item CT-11). When the Aramex account was blocked on 1 October, checkout offered no delivery method at all and nobody could buy.

The business wants to set delivery pricing itself, in the ERP, by region, by item, and by weight and volume, see the effect immediately on the site, and have the FAQ and Shipping page read the same source.

## Goals

1. Zones and their country lists are defined in the ERP, picked from the ERP's own Country list. Nothing about regions is hard-coded.
2. Rules decide what a customer pays inside a zone. A rule covers every item or a selected set of items, and can be limited by order value, shipment weight and shipment volume. Rules are checked top to bottom and the first match wins.
3. A rule charges in one of three ways: pass the carrier's price through, free, or a custom formula. Any non-free rule can round the result up to the next whole dollar.
4. What happens when the carrier gives no quote (as with the blocked Aramex account) is configurable per zone, with a policy-wide default.
5. Everything is edited in a step-by-step wizard: Zones, then Rules, then "If the carrier can't quote", then Review. A "Try an order" panel beside every step re-prices a sample order live.
6. A change takes effect on the storefront immediately. Nothing is cached in the quote path.
7. The FAQ and Shipping page can read zones, countries and a customer-facing line per zone from the same policy.

## Non-goals

- Replacing `dsi_shipping`, the Aramex adapter, or carton packing. They keep producing the carrier's price per service.
- Choosing which carrier services serve a country. That stays with Shipping Service and its countries.
- Delivery estimates. They stay per Shipping Service.
- Currencies other than USD, taxes, duties and cash on delivery.
- Rules per carrier service. A rule applies to every service quoted to the zone.

## Anti-goals (a failure even if it technically works)

- A half-finished policy changing live prices.
- A formula that can run arbitrary code. The ERP had a compromise through Server Scripts, and they are disabled estate-wide.
- The price shown at checkout and the amount charged at payment disagreeing.
- A mixed order picking up a rule meant for other items.
- A customer charged nothing because a lookup failed. Failures end in the configured fallback, never in zero by accident.
- Needing a developer to change a price.

## Constraints (decided)

- New standard doctypes and code inside `dsi_catalogue` only. No Custom Fields, Property Setters, Client Scripts, Server Scripts, new roles or Custom DocPerm rows.
- Editing is for the System Manager role, the same as Shipping Service today. The two read-only guest methods below return only what a customer may see.
- Built and verified on erp2 (`ssh erp2-sandbox`) before promotion. erp2's catalogue copy is old and not a git repo; this work happens in the real repo and is copied over. erp2 has no item weights, so weight and volume are typed into the Try an order panel there.
- Promotion goes through the catalogue repo. The live host holds commits that are not on GitHub yet (`6a39d3e`, `702a036`); they must be pushed first.
- The repo's conventions apply: hermetic pytest in `tests/hermetic` (no bench), tab indentation, ruff at 110.
- The website changes (Part 2 below) wait until the ERP side is promoted.

## What it is made of

**Shipping Zone** (its own doctype, because a zone holds a list of countries and a child table cannot hold another).
- Name, enabled, a table of countries (each a link to the ERP Country list).
- "Not shipped": delivery is not offered to these countries.
- "Everywhere else": the zone that catches any country not in another zone. At most one.
- "Shown to customers": one optional plain line for the FAQ and Shipping page.
- Fallback when the carrier can't quote: use the policy default, don't offer delivery, charge a fixed amount, or free; plus the amount.
- A country may be in only one enabled zone. Saving a zone that repeats a country names the other zone and is refused.

**Shipping Item Set** (its own doctype, reusable across rules). A name, a table of item groups (an item group includes the groups beneath it) and a table of specific items. An item belongs to the set if it is listed or its group is listed or sits beneath a listed group.

**Shipping Policy** (a single document). An active switch, "Anything not covered" (pass the carrier price through, or don't offer delivery) and a table of rules. Version tracking is on, so each change is recorded with who made it.

**Shipping Policy Rule** (rows of the policy, in order).
- Enabled, zone, an optional item set, an optional label.
- Conditions, all optional: order value from and up to, shipment weight from and up to (kg), shipment volume from and up to (litres). "From" includes the number, "up to" does not (from $0 up to $500 means $0 to $499.99).
- How it charges: carrier price, free, or custom formula. Plus "round up to the next whole dollar".

The wording about items sits on the page next to the choice, with an asterisk: **\* A rule for selected items applies only when every item in the order is one of them. A rule for every item applies to every order.** It appears on the wizard's Rules step and on the Shipping Policy form's item set field.

## How an order is priced

The caller gives the ERP: destination country, the items and quantities, the shipment's total weight (kg) and volume (litres) as packed, and the carrier's quote for each service it quoted (possibly none).

1. **Active?** If the policy is off, every quote is returned unchanged, exactly as today.
2. **Zone.** The destination country's enabled zone; otherwise the "Everywhere else" zone; otherwise "Anything not covered". A "Not shipped" zone returns no delivery and its message.
3. **Rule.** In the zone, the first enabled rule whose conditions all hold. Order value comes from the ERP's price list, not from the caller. A rule with an item set holds only if every item in the order is in the set. If no rule holds, "Anything not covered" decides.
4. **Price.** Carrier price: each service's quote as is. Free: zero. Formula: evaluated per service with that service's quote. Then, if chosen, rounded up to the next whole dollar (already whole prices stay, zero stays zero). Never negative. Otherwise rounded to cents.
5. **No quote.** If the carrier gave no quote at all, rules are not used. The zone's fallback decides: don't offer delivery, a fixed amount, or free. The result carries one synthetic offer so checkout still has a delivery method.
6. A formula that fails to evaluate counts as "can't price" and goes to the fallback. It is logged, and never becomes zero.

The result lists, per service: the amount, which rule or fallback produced it, and a one-line reason, which is what the Try an order panel prints.

**Formulas.** A small expression language, not Python. Names: `carrier_price`, `order_value`, `weight_kg`, `volume_l`, `boxes`, `item_count`. Functions: `min`, `max`, `round`, `ceil`, `floor`, `abs`. Arithmetic, comparisons, `and`, `or`, `not`, and `a if condition else b`. Numbers only. Evaluated by a whitelist over a parsed expression, never `eval`, with a length limit, a size limit and no power operator. Saving a formula runs it against sample values and refuses it with a plain message naming the problem. Examples: `carrier_price * 1.1`, `max(carrier_price * 1.1, 45)`, `carrier_price - 10 if order_value >= 500 else carrier_price`, `weight_kg * 6 + 8`.

## The wizard (the "Steps" layout was chosen)

One page in the app, opened from the Shipping Policy form. Four steps across the top; the Try an order tape is always on the right.

1. **Zones.** A card per zone: name, country chips (add from the ERP Country list, with search), the Not shipped and Everywhere else switches, the customer line. Add and remove zones.
2. **Rules.** Each rule is a sentence of clickable chips: "In [zone] for [every item / a set] charge [carrier price / free / formula], rounded up" with the price this rule gives the sample order on the right. Clicking a chip edits it in place. An "Only when" row adds order value, weight and volume limits. Drag to reorder; add, disable and delete. Item sets are created from the item chip. The asterisk note sits under the list.
3. **If the carrier can't quote.** Each zone picks its fallback; below, the "Anything not covered" default. The tape can simulate "the carrier can't quote".
4. **Review.** The whole policy in plain language, zone by zone, and the Active switch with its state. Turning it on says it takes effect immediately.

Saving happens as you go. If the policy is active, the wizard says so at the top and every change is live.

**Try an order.** Ship to (a country), items and quantities (or a typed order value), weight and volume (filled from the items when they have them, otherwise typed), and the carrier's quote (typed, or asked from the carrier when the items have weights and dimensions). Output is a receipt: carrier quote, the rule that matched, any rounding, and "Customer pays". When no rule matched or the fallback applied, it says so.

**Look.** Native to the ERP desk in light and dark, using the desk's own colour variables. One accent: brass for what the customer pays, green for free, red for not shipped. Sentence case, no all-caps labels, no mono labels. The tape is the one memorable element; everything around it stays quiet. Keyboard-reachable, visible focus, reduced motion respected, works at narrow widths.

## Interfaces the website will use

Both are in the catalogue app, whitelisted, and read-only.

- `dsi_catalogue.shipping_policy.price_delivery` (guest). Input as described above; output: whether the policy is on, the zone, per-service offers with amount and source, blocked or not, and a customer-facing message. Never cached.
- `dsi_catalogue.shipping_policy.public_summary` (guest). Zones, their country names, and each zone's customer line. Nothing else (no formulas, fallbacks or thresholds).

The wizard's own methods (state, save, try an order) require the System Manager role.

## Part 2, after promotion: the website

Not built until the ERP side is verified on erp2 and promoted. `lib/shipping/quote.ts` calls `price_delivery` after summing the carrier's price per service, passing the items, packed weight and volume; the payment route's re-quote uses the same call and charges the amount it returns. When carriers return nothing, the site shows the synthetic fallback offer. The FAQ and Shipping page read `public_summary` and drop their typed prices and country lists. Policy saves ask the website to refresh those pages through the existing revalidation outbox, where it exists.

## How we will know it works

- Hermetic pytest for the pure core: the formula evaluator (accepts the examples, rejects everything else, including names, calls, attributes, strings, power, huge input), zone selection, duplicate countries, rule matching including mixed orders, band edges, rounding, fallbacks and "anything not covered".
- On erp2: the doctypes install without custom anything; a guest can call both read methods and read nothing else; a non-System-Manager cannot edit or call the wizard methods; the wizard builds the example policy from scratch in a headless browser, with screenshots in light and dark.
- A parity check that `price_delivery` and the Try an order panel give identical answers for the same input.
- Promotion proof: with the policy off, a quote is byte-for-byte what it is today.

## Left to the builder

Field and method names beyond those above, validation wording, the structure of the page's script, how chips are edited (popover or inline), test data, the order of work, and how the files reach erp2.

## Calls I made (change any of them)

- "Round to the nearest top dollar" means round up to the next whole dollar.
- Bands include the lower number and exclude the upper.
- Item groups include the groups beneath them.
- The policy ships switched off, so promoting it changes nothing until someone turns it on.
- The fallback replaces the rules when there is no carrier quote, even for a free rule.
- Volume is in litres, the policy is a single document, and the delivery estimate stays on the Shipping Service.
- One country, one enabled zone.
