# Shipping Policy: delivery pricing set from the ERP

Status: draft for approval, 2026-10-22 (revised after a builder read-through). Written for the catalogue app (`dsi_catalogue`), built and tested on erp2 first, promoted to the live ERP afterwards.

## Why this exists

Today a customer's delivery price is whatever Aramex quotes, summed per carton by the website (`lib/shipping/quote.ts` in the website repo, asking `dsi_shipping.api.get_shipping_rates` once per carton). Nothing in the ERP lets the business decide what a customer pays. The FAQ and the Shipping and Delivery page carry typed prices and country lists that disagree with the checkout (review item CT-11). When the Aramex account was blocked on 1 October, checkout offered no delivery method at all and nobody could buy.

The business wants to set delivery pricing itself, in the ERP, by region, by item, and by weight and volume, see the effect immediately on the site, and have the FAQ and Shipping page read the same source.

## Goals

1. Zones and their country lists are defined in the ERP, picked from the ERP's own Country list. Nothing about regions is hard-coded.
2. Rules decide what a customer pays inside a zone. A rule covers every item or a selected set of items, and can be limited by order value, shipment weight and shipment volume. Rules are checked top to bottom and the first match wins.
3. A rule charges in one of three ways: pass the carrier's price through, free, or a custom formula. Any non-free rule can round the result up to the next whole dollar.
4. What happens when a carrier that normally serves the destination gives no quote (as with the blocked Aramex account) is configurable per zone, with a policy-wide default.
5. Everything is edited in a step-by-step wizard: Zones, then Rules, then "If the carrier can't quote", then Review. A "Try an order" panel beside every step re-prices a sample order live.
6. When the wizard applies a change it takes effect on the storefront immediately. Nothing is cached in the quote path.
7. The FAQ and Shipping page can read zones, countries and a customer-facing line per zone from the same policy.

## Non-goals

- Replacing `dsi_shipping`, the Aramex adapter, or carton packing. They keep producing the carrier's price per service.
- Choosing which carrier services serve a country. That stays with Shipping Service and its countries.
- Delivery estimates. They stay per Shipping Service.
- Currencies other than USD, taxes, duties and cash on delivery.
- Rules per carrier service. A rule applies to every service quoted to the zone.
- Selling to a country no carrier serves. The policy never turns "nobody delivers there" into a price.

## Anti-goals (a failure even if it technically works)

- A half-finished policy changing live prices.
- A formula that can run arbitrary code. The ERP had a compromise through Server Scripts, and they are disabled estate-wide.
- The price shown at checkout and the amount charged at payment disagreeing.
- A mixed order picking up a rule meant for other items.
- A customer charged nothing because a lookup failed. Failures end in the configured fallback, never in zero by accident.
- Taking payment for a destination nobody can deliver to.
- Needing a developer to change a price.

## Constraints (decided)

- New standard doctypes and code inside `dsi_catalogue` only. No Custom Fields, Property Setters, Client Scripts, Server Scripts, new roles or Custom DocPerm rows.
- Editing is for the System Manager role, the same as Shipping Service today. The two read-only guest methods below return only what a customer may see.
- Built and verified on erp2 (`ssh erp2-sandbox`) before promotion. erp2's catalogue copy is old and not a git repo; this work happens in the real repo and is copied over. erp2 has `dsi_shipping` installed but no item weights, so weight and volume are typed into the Try an order panel there.
- The pricing code reads `dsi_shipping`'s Shipping Service and Shipping Service Country records to know whether a carrier serves a country. It is a read only, and if those records do not exist the country counts as served.
- Promotion goes through the catalogue repo. The live host holds commits that are not on GitHub yet (`6a39d3e`, `702a036`); they must be pushed first.
- The repo's conventions apply: hermetic pytest in `tests/hermetic` (no bench), tab indentation, ruff at 110.
- The website changes (Part 2 below) wait until the ERP side is promoted.

## What it is made of

**Shipping Zone** (its own doctype, because a zone holds a list of countries and a child table cannot hold another).
- Name, enabled, a table of countries (each a link to the ERP Country list).
- "Not shipped": delivery is not offered to these countries.
- "Everywhere else": the zone that catches any country not in another zone. At most one.
- "Line for customers" (built as the field label; first drafted as "Shown to customers"): one optional plain line meant for the FAQ and Shipping page. The website does not show it yet (it never calls `public_summary`).
- "If delivery isn't offered": an optional message shown at checkout (used by Not shipped, by a declined fallback and, with the rate card, by a country the card does not cover), once the policy is on with the rate card as the price source.
- Fallback when the carrier can't quote: use the policy default, don't offer delivery, charge a fixed amount, or free; plus the amount.
- A country may be in only one enabled zone. Saving a zone that repeats a country names the other zone and is refused.

**Shipping Item Set** (its own doctype, reusable across rules). A name, a table of item groups (an item group includes the groups beneath it) and a table of specific items. An item belongs to the set if it is listed or its group is listed or sits beneath a listed group.

**Shipping Policy** (a single document).
- An active switch, shipped off.
- "Anything not covered": what to do when a country is in no zone and there is no "Everywhere else" zone, or when no rule in the zone holds. Pass the carrier price through, or don't offer delivery.
- "If the carrier can't quote (default)": don't offer delivery, charge a fixed amount, or free, plus the amount. Ships as "don't offer delivery". This is a separate setting from "Anything not covered".
- The table of rules. Version tracking is on, so each change is recorded with who made it.

**Shipping Policy Rule** (rows of the policy, in order).
- Enabled, zone, an optional item set, an optional label.
- Conditions, all optional: order value from and up to, shipment weight from and up to (kg), shipment volume from and up to (litres). "From" includes the number, "up to" does not (from $0 up to $500 means $0 to $499.99).
- How it charges: carrier price, free, or custom formula. Plus "round up to the next whole dollar".

The wording about items sits on the page next to the choice, with an asterisk: **\* A rule for selected items applies only when every item in the order is one of them. A rule for every item applies to every order.** It appears on the wizard's Rules step and on the Shipping Policy form's item set field.

## How an order is priced

The caller gives the ERP: the destination country, the items and quantities, the packed cartons (each with its weight in kg and volume in litres), and the carrier's quote per service: the service, the amount, and how many cartons that service quoted (possibly none at all).

1. **Active?** If the policy is off, every quote is returned unchanged, exactly as today.
2. **Is the destination served?** If no enabled Shipping Service lists the country, or the heaviest carton is over the weight limit of every service that does, delivery is not offered (reason `not_served`). No fallback applies: the policy does not sell to places nobody delivers to.
3. **Whole quotes only.** A service that did not quote every carton is dropped. Its total would be too low. If that leaves nothing for a served country, it counts as "the carrier can't quote".
4. **Zone.** The destination country's enabled zone; otherwise the "Everywhere else" zone; otherwise "Anything not covered". A "Not shipped" zone returns no delivery and its message (reason `not_shipped`).
5. **If the carrier can't quote** (a served country, no usable quote): rules are not used. The zone's fallback decides, using the policy default when the zone says so: don't offer delivery (reason `not_offered`), a fixed amount, or free. The result carries one synthetic offer so checkout still has a delivery method (reason `fallback_offer`).
6. **Rule.** In the zone, the first enabled rule whose conditions all hold. Order value comes from the ERP's price list, not from the caller. A rule with an item set holds only if every item in the order is in the set. If no rule holds, "Anything not covered" decides (reason `not_covered` when that means no delivery).
7. **Price.** Carrier price: each service's quote as is. Free: zero. Formula: evaluated per service with that service's quote. Then, if chosen, rounded up to the next whole dollar (already whole prices stay, zero stays zero). Never negative. Otherwise rounded to cents.
8. A formula that fails to evaluate counts as "can't price" and goes to the fallback in step 5 for that order. It is logged, and never becomes zero.

The result lists, per service: the amount, which rule or fallback produced it, and a one-line reason, which is what the Try an order panel prints. Reasons travel as codes (`not_served`, `not_shipped`, `not_offered`, `not_covered`, `fallback_offer`, `formula_error`) with English text.

**Formulas.** A small expression language, not Python. Names: `carrier_price`, `order_value`, `weight_kg`, `volume_l`, `boxes`, `item_count`. Functions: `min`, `max`, `round`, `ceil`, `floor`, `abs`. Arithmetic, comparisons, `and`, `or`, `not`, and `a if condition else b`. Numbers only. Evaluated by a whitelist over a parsed expression, never `eval`, with a length limit, a size limit and no power operator. Saving a formula runs it against sample values and refuses it with a plain message naming the problem. Examples: `carrier_price * 1.1`, `max(carrier_price * 1.1, 45)`, `carrier_price - 10 if order_value >= 500 else carrier_price`, `weight_kg * 6 + 8`.

## The wizard (the "Steps" layout was chosen)

One page in the app, opened from the Shipping Policy form. Four steps across the top; the Try an order tape is always on the right.

1. **Zones.** A card per zone: name, country chips (add from the ERP Country list, with search), the Not shipped and Everywhere else switches, the customer line. Add and remove zones. A warning lists any zone country that no carrier service serves.
2. **Rules.** Each rule is a sentence of clickable chips: "In [zone] for [every item / a set] charge [carrier price / free / formula], rounded up" with the price this rule gives the sample order on the right. Clicking a chip edits it in place. An "Only when" row adds order value, weight and volume limits. Drag to reorder; add, disable and delete. Item sets are created from the item chip. The asterisk note sits under the list.
3. **If the carrier can't quote.** Each zone picks its fallback; below, the policy default and "Anything not covered". The tape can simulate "the carrier can't quote".
4. **Review.** A diff of what will change, then the whole policy in plain language, zone by zone, and the Active switch with its state. **Apply** writes everything at once.

**Nothing is written until Apply.** The wizard works on a draft kept in the browser (and restored after a reload). Try an order prices the draft, so you see the effect before anything is live. Apply sends the whole draft in one request that validates it as a whole and writes zones, item sets and rules in one transaction, so a half-finished policy can never go live. Apply refuses if someone else changed the policy since the wizard loaded it. When the policy is active, a banner says edits go live on Apply. The three doctypes can also be edited directly by a System Manager; those saves are live per save with the same validation, and new rule rows start disabled.

**What a direct save checks** (amended 2026-10-24, rulings P66, P83, P85 and P101). That validation applies while the policy is on. While it is off, nothing in it is validated on save (only a value that is not a number at all is refused), so a half-finished policy never blocks the carrier path or a save of the form, and switching it on is checked. A zone saved enabled must list only countries that have a two-letter country code. A zone saved disabled is outside the live policy and always saves, whatever its countries' codes. A direct save of a zone checks that zone's own rules (its countries, one Everywhere else, its numbers), not the whole policy, so a zone whose fallback is "Use the rate card" saves while no rate card is stored, and the engine then declines the orders that reach that fallback (it fails closed).

**Try an order.** Ship to (a country), items and quantities (or a typed order value), weight and volume (filled from the items when they have them, otherwise typed), and the carrier's quote (typed, or asked from the carrier when the items have weights and dimensions). Output is a receipt: carrier quote, the rule that matched, any rounding, and "Customer pays". When no rule matched, the destination isn't served, or the fallback applied, it says so.

**Look.** Native to the ERP desk in light and dark, using the desk's own colour variables. One accent: brass for what the customer pays, green for free, red for not shipped. Sentence case, no all-caps labels, no mono labels. The tape is the one memorable element; everything around it stays quiet. Keyboard-reachable, visible focus, reduced motion respected, works at narrow widths.

## Interfaces the website will use

Both are in the catalogue app, whitelisted, and read-only.

- `dsi_catalogue.shipping_policy.price_delivery` (guest). Input as described above, plus an optional language. Output: whether the policy is on, the zone, per-service offers with amount, source and reason code, whether delivery is offered, and a customer-facing message. Never cached.
- `dsi_catalogue.shipping_policy.public_summary` (guest). While the policy is active: zones, their country codes and names, and each zone's customer line. Nothing else (no formulas, fallbacks or thresholds). While it is off, nothing.

The wizard's own methods (state, apply, try an order) require the System Manager role.

**Languages.** Zone lines and messages are one text field each, written in English. Other languages use the ERP's built-in Translation records: the two methods return the translated text when a translation exists for the requested language, and English otherwise. Standard messages come back as the reason codes above, and the website localises those itself. Country names are localised by the website from the codes. The delivery method created by the fallback is called "Standard delivery" and carries the reason code `fallback_offer`.

## Part 2, after promotion: the website

Not built until the ERP side is verified on erp2 and promoted. `lib/shipping/quote.ts` calls `price_delivery` after the per-service quotes are collected, passing the items, the packed cartons and, per service, how many cartons it quoted; the payment route's re-quote uses the same call and charges the amount it returns. When carriers return nothing, the site shows the synthetic fallback offer. The FAQ and Shipping page read `public_summary` and drop their typed prices and country lists. Policy applies ask the website to refresh those pages through the existing revalidation outbox, where it exists.

The same file already has a small fault of its own: it adds up each service's price over the cartons that service quoted, so a service that skipped one carton shows a partial, too-low total. With the policy on, step 3 above removes such services. With it off, that fault remains as it is today.

## How we will know it works

- Hermetic pytest for the pure core: the formula evaluator (accepts the examples, rejects everything else, including names, calls, attributes, strings, power, huge input), zone selection, duplicate countries, rule matching including mixed orders, band edges, rounding, partial quotes, unserved countries, the two fallbacks and "anything not covered".
- On erp2: the doctypes install without custom anything; a guest can call both read methods and read nothing else; a non-System-Manager cannot edit or call the wizard methods; Apply is all-or-nothing (a draft with one bad rule writes nothing); the wizard builds the example policy from scratch in a headless browser, with screenshots in light and dark.
- A parity check that `price_delivery` and the Try an order panel give identical answers for the same input.
- Promotion proof: with the policy off, a quote is byte-for-byte what it is today.

## Left to the builder

Field and method names beyond those above, validation wording, the structure of the page's script, how chips are edited (popover or inline), test data, the order of work, and how the files reach erp2.

## Calls I made (change any of them)

- "Round to the nearest top dollar" means round up to the next whole dollar.
- Bands include the lower number and exclude the upper.
- Item groups include the groups beneath them.
- The policy ships switched off, so promoting it changes nothing until someone turns it on.
- The wizard writes nothing until Apply (a draft kept in the browser), rather than saving each step as you go.
- The fallback replaces the rules when there is no carrier quote, even for a free rule, and applies only where a carrier serves the country. A country nobody serves gets no delivery.
- A service that didn't quote every carton is dropped when the policy is on.
- There are two separate defaults: "Anything not covered" and "If the carrier can't quote".
- Zone text is English, with other languages through the ERP's Translation records; standard messages are reason codes the website translates.
- Volume is in litres, the policy is a single document, and the delivery estimate stays on the Shipping Service.
- One country, one enabled zone.


## Part 1b: the interim rate card (added 2026-10-22, approved by the owner)

### Why

The Aramex account has been blocked since 1 October and the shipping partner is not final. The new website goes live on the rates the Shopify store charges today: weight tiers by zone and product profile, in pounds, in USD (the export sent by the Head of IT: 4 profiles, about 10 zones, 49 rates, plus 12 countries that get no delivery). Those rates must be the price customers pay, ahead of any carrier price, until the owner decides to show the carrier integration again. When a partner is confirmed, the card is replaced by their rate card.

### What it is

A **rate card** is kept on the Shipping Policy: profiles, each with the zones it serves and a weight tier table per zone. It is imported from the two Shopify CSV files, shown and edited in the wizard (a new Rates step), and applied with everything else in one Apply.

- **Profile**: a name, a list of product code patterns that decide which items belong to it, and its zones. The first profile whose pattern matches an item takes it; a pattern of `*` catches everything left. The four Shopify profiles map to: Dangerous Goods Petite (`F-*-PT`, `F-CR-PC-*`), Refill Service (`F-*-RF`), Dangerous Goods (`F-*`, the fragrances), Rest of the World Allowed Items (`*`).
- **Product code** here means the Website Item index key with the braces removed and upper-cased, for example `F-CR-OSA-M-PT` (the same normalisation the website uses). Patterns use `*` as a wildcard. Petite and refill bottles share item groups with the full-size fragrances, which is why item groups are not used.
- **Zone**: a name, a list of countries (two-letter codes), the delivery method text from Shopify (for example "International Express (4-7 days)") and the days promised (the upper number), and either weight tiers or one flat price. A country sits in at most one zone of a profile.
- **Tier**: weight from and up to, in pounds, and a price in USD.

### Price source and fallback

The policy gets a setting **Price source**: "Rate card" or "Carrier" (the default, so promoting changes nothing).

- **Rate card**: customers are charged from the card. The carrier is not asked, its quotes are ignored, and the "is this country served by a carrier" check is skipped. A country in a Not shipped zone still gets no delivery. If the card cannot price the order, delivery is not offered.
- **Carrier**: as designed in Part 1. Zones and the policy default gain one more fallback choice, **Use the rate card**: when the carrier cannot quote, the order is priced from the card instead of a fixed amount.

A policy that is switched off still returns the carrier quotes unchanged, whatever the price source says.

### How an order is priced from the card

1. The destination must not be in a Not shipped zone, and must be two letters.
2. Each item goes to the first profile whose pattern matches its product code. An item with no Website Item counts as unmatched and goes to the catch-all profile.
3. A profile's weight is the sum of its items' unit weight times quantity, in kilograms, turned into pounds (2.20462262 pounds to the kilogram) and rounded to three decimals. An item with no weight counts as 0.35 kg and is flagged.
4. The profile's zone is the one that lists the country. No zone means the card does not serve that country for that profile, and the whole order is not offered.
5. A flat zone charges its one price whatever the weight. A tiered zone charges the first tier whose upper weight is at least the profile's weight, so a weight in a gap between tiers pays the next tier up, and a weight below the first tier pays the first. Heavier than the top tier: the whole order is not offered (reason `too_heavy`).
6. The order's price is the sum of the profile prices (Shopify adds the rates of every profile in the cart). It never goes below zero.
7. One delivery method is offered, service id `rate-card`, named after the profiles' method text (if they differ, the one that takes the most days), with `estimated_days` set to the longest promise. When the total is zero it is still offered, at 0.00.

### Import

The import reads the two CSV files exactly as exported (columns Profile, Zone (recommended), Country count, Countries (ISO-2 - Name), Shipping method, Weight from (lb), Weight to (lb), Price (USD); and ISO-2, Country). It shows what it found and what it could not use before anything is kept: profiles, zones, tiers, countries, rows it skipped, countries that are not in the ERP's Country list, unknown profile names (which get no product patterns until someone adds them), tier overlaps, and the 12 not-delivered countries as a Not shipped zone. Nothing is written until Apply.

### Test the integration

The wizard has a Test tab with three read-only checks that run against what is live, not the draft:

1. **ERP price API**: calls the public `price_delivery` endpoint over HTTP exactly as the website does, with a sample order, and shows status, time, the answer and whether the amount matches the policy's own price for the saved policy.
2. **Carrier API**: asks the carrier for one sample parcel through the existing shipping module and shows what comes back (today: account blocked).
3. **Website**: posts the same sample cart to the website's shipping rates route and shows the methods and prices the site returns, with a verdict on whether the site is using the policy.

Only the website address stored in Storefront Settings is ever called. Nothing is written.

### The website

The website asks the ERP for the delivery price first. When the policy is active and its price source is the rate card, the website uses the offer it gets and never calls the carrier; otherwise it behaves as before. The payment route's server-side re-quote goes through the same function, so the amount charged is the amount shown. If the ERP method is missing or fails, the website falls back to its current behaviour. The website change is committed locally and is pushed only when the owner says so.

### Anti-goals added

- Interim prices that differ from the Shopify export without anyone choosing to change them.
- An order priced on a profile's rate that belongs to another profile.
- A card that cannot price an order silently charging nothing.
- A heavy order, or a country the card does not serve, being sold.
