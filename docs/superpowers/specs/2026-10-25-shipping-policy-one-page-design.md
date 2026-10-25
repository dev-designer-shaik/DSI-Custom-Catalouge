# Shipping Policy, one page (Part 1c)

Status: draft, 2026-10-25. The owner approved the direction in chat the same day ("all okay, simplify the UI") and added that the Aramex account is not blocked. This file amends `2026-10-22-shipping-policy-design.md`. Where the two disagree, this one wins. The older file stays as the history of what was built first.

Words used here: an **item group** is the ERP's own Item Group (a tree of categories). A **product set** is a Shipping Item Set: a named collection of products. A **row** is a Shipping Policy Rule. A **zone** is a Shipping Zone.

## Why this exists

The owner asked for three things: one page to manage the whole shipping policy, a filter that narrows as you type when you pick the products a price applies to, and a way to choose the forwarding provider and its services, the way checkout on the website lists them.

What exists makes that hard. The wizard has six tabs. Zones, Rates and Rules are three views of one question (who gets which delivery at what price), and the Rates tab is a second pricing system kept beside the first, with its own zones, its own product sets (typed code patterns such as `F-*-PT`) and its own prices. It exists because Shopify's rates overlap (in the export the US sits in four zones, one per product type, and 78 countries sit in two or more of its ten country lists) while a Zone allows a country in only one zone. With the rate card as the price source the engine does not read the rules at all, so there are two pricing systems and one switch. The card's single offer carries no provider, so those orders have to be shipped by hand.

The Aramex account works: on 2026-10-25 the live website returned Aramex quotes for Bahrain and for the US. The carrier's own price is a live choice again, and the Shopify table becomes one way to charge among several, not a mode of the whole policy.

## Goals

1. One page edits the whole policy. No steps and no tabs.
2. The page is a list of **product sets**. A set holds **rows**; a row says where it ships (a zone), which provider services deliver it, and how it is priced.
3. Products are picked with a list that narrows as you type and filters by item group and code pattern. Picking can add everything that matches as a live filter, so products added to the catalogue later join the set by themselves.
4. "Delivered by" lists every Shipping Provider in the ERP with its Shipping Services underneath (today: Aramex BH and its services), shows how many of the zone's countries each service serves, and lets the manager tick the services a row offers.
5. A row charges one of five ways: a weight table, a fixed amount, free, the carrier's own price, or a formula over the carrier's price. Every one of them works on the website, not only in the ERP.
6. The Shopify export is imported as product sets, rows and zones. Prices stay identical to the cent.
7. Checkout lists the offered services with provider, label, days and price, as it does now, and the order carries the provider and service the customer chose. A table, fixed or free row with no service ticked offers one plain "Delivery" method, as the rate card does today.
8. The safety rules of the first design stay: nothing is live until Save, Save is all or nothing, a failure never becomes a price of zero, a country in a Not delivered zone is never delivered to, and nothing in the price path is cached.

## Non-goals

- A second carrier adapter. Only Aramex is integrated; the page lists whatever providers the ERP holds.
- Different prices per service inside one row. For that, add a second row.
- Currencies other than USD, taxes, duties, cash on delivery, carton packing, the Aramex adapter itself.
- Moving the FAQ and Shipping page onto `public_summary` (still Part 2 of the first design).
- Migrating live data. The live ERP has no Shipping Policy yet (HTTP 417 on 2026-10-25); only erp2 holds one, and it is rebuilt by importing the Shopify files again.
- Weight and volume conditions on a row (removed, see "What goes away").

## Anti-goals (a failure even if it technically works)

- The prices customers pay changing from the Shopify export without anyone choosing it.
- A choice on the page that does not take effect at checkout.
- A product priced under another set's rate: a petite perfume priced as a full-size one because its own set has no row for the country.
- A half-finished policy changing live prices.
- A failure, a missing quote or a bad formula turning into a price of zero.
- Taking payment for a destination the policy says nobody delivers to.
- The price shown at checkout and the amount charged at payment disagreeing.
- A service named at checkout that cannot carry the parcel.
- Needing a developer to change a price.

## Constraints (decided)

- New fields and child tables on the existing standard doctypes of `dsi_catalogue` only. No Custom Fields, Property Setters, Client Scripts, Server Scripts, new roles or Custom DocPerm rows. The doctypes are edited in place: nothing is live, so there is no migration, and erp2's policy is reset and refilled.
- The page keeps its route (`/desk/shipping-policy-wizard`) and its roles: System Manager or Website Manager.
- Built and verified on erp2 first. Nothing goes to the live ERP until the owner runs the promote script, which is re-pinned to the final commit. The guide and the helper documents are regenerated afterwards.
- The website changes are committed locally in the private website repository and pushed only when the owner says so.
- The catalogue repository is public: no real Shopify prices, no secrets, in code, tests, fixtures or documents. Tests use made-up prices.
- Conventions of the repository: hermetic pytest in `tests/hermetic` for everything pure, tab indentation, ruff at 110. The page stays plain JavaScript included by Frappe, with no build step.
- Every call between the ERP and the website keeps a limit on each list, a time-out, and a defined behaviour on failure (unchanged from the first design).

## What the owner sees

One desk page, from top to bottom:

```
Shipping policy                    Active [ on ]      [Test live]
                                   Unsaved changes: [Review and save] [Discard]   (only when there are any)

PRODUCTS AND DELIVERY        the first set that lists a product prices it      [Import Shopify rates]
+- Petite fragrances   F-*-PT, F-CR-PC-*    [Change products]          [up] [down] --------------+
|  Ship to              Delivered by              Price                                          |
|  Bahrain              Any service               Free                                      [..] |
|  GCC (5)              Any service               Fixed  $..                                [..] |
|  International (72)   Aramex Intl PPX           Table  up to 2 lb $..  up to 4 lb $..  ... [..] |
|  [+ Add destination]                                                                           |
+------------------------------------------------------------------------------------------------+
+- Everything else     all other products                                                        |
|  Everywhere           Any service               Carrier's price                           [..] |
+------------------------------------------------------------------------------------------------+
[+ Add product set]

ZONES   Bahrain (1)  GCC (5)  USA (1)  Europe (43)  Not delivered (12)  [+ Add zone]

WHEN WE CAN'T PRICE AN ORDER    ( ) Don't offer delivery   ( ) Charge a fixed $[..]   ( ) Free

                                                   Try an order: country, products, quantities
                                                   -> each set's share, services, what the customer pays
```

- **A set** is a card: its name, what it holds, and its rows in order. Sets are in priority order and can be moved; the page keeps each set's rows together, and the order of the rows in the policy is the order shown. "Add destination" puts a new row after the set's last row. A set that lists no products holds every product no earlier set lists; it must be the last set (saving refuses otherwise, because the sets after it could never be used).
- **A row** edits in place. Ship to: a zone. Delivered by: providers and services as ticks, "Any service" while nothing is ticked; each service shows its scope, its days and how many of the row's zone countries it serves, and the control says so plainly where `dsi_shipping` is not installed. Price: Weight table (rows "up to N lb: $P"), Fixed amount, Free, Carrier's price, Formula. Behind "More": the name customers see for a plain delivery, the days promised, "only when the order value is at least / at most $N", round up to the next whole dollar, disable, delete, move.
- **Change products** opens a dialog with a search box that narrows the list as you type (code or name, published products first), an item-group choice, a code-pattern field with quick picks for the petite and refill patterns, and a result list with ticks. Two buttons add: "Add the N ticked" (items) and "Add everything that matches as a live filter" (one item group, or one code pattern). The live filter is stored as an item group or a pattern, so it follows the catalogue; text searches and combinations are added as items. Each result shows its code, its item group and which set already lists it.
- **Zones** edit in a dialog: name, countries with search, a type (delivery zone, Not delivered, Everywhere), the line for customers and the message shown when delivery is not offered. Zones may overlap; a country may be in as many zones as the owner likes.
- **Review and save** is a dialog: what changed in plain lines, the problems that block saving, Save. It replaces the Review tab, the plain-language summary and the draft banner. Edits are kept in the browser as a draft until saved (as now); one line offers to continue or discard an old draft. A version check still refuses a save over someone else's change.
- **Test live** is a dialog with the three read-only checks of the first design (the ERP price API over HTTP, the carrier for one sample parcel, the website's rates route), adapted to the new answer.
- **Try an order** prices the draft as if it were on, with the carrier asked live for carrier-priced rows and the items' weights from the ERP. Typed order values, typed cartons and typed quotes go away.

An empty policy shows two buttons, "Import Shopify rates" and "Start with the carrier's own prices" (one set, Everything else, one row: Everywhere, any service, carrier's price: switching on changes nothing for customers).

## The model

**Shipping Zone** (as built, with two changes). Name, enabled, Not delivered, Everywhere, countries, the line for customers, the not-offered message. Changes: zones may overlap (the one-country-one-zone rule goes), and the per-zone fallback goes. A country that is in any enabled Not delivered zone is never delivered to, whatever other zones say. An Everywhere zone holds every country. A row cannot point at a Not delivered zone.

**Shipping Item Set** (a product set). A name, item groups (an item group includes the groups beneath it), items, and new: **code patterns**. A pattern is matched against an item's product code, which is its Website Item index key without the braces, upper-cased (`{F-CR-OSA-M-PT}` is `F-CR-OSA-M-PT`). `*` is any run of characters, `?` one character, `[AB]` one of the characters listed. A product belongs to the set if it is listed, or its item group is listed or sits beneath a listed group, or its code matches a pattern. Petite and refill bottles share item groups with full-size perfumes, which is why patterns exist.

**Shipping Policy Rule** (a row; the rows of the policy, in order):
- enabled, a label, a zone, a product set (empty: every product);
- `services`: a list of Shipping Service names, empty meaning any;
- the charge: Carrier price, Free, Fixed amount, Weight table, Custom formula; the amount of a fixed charge; the table (a list of tiers, each an upper weight in pounds and a price in USD); the formula; round up;
- `days`: an optional number of days promised, used by a plain delivery;
- one condition: order value from and up to (USD). Weight and volume conditions are removed.

A disabled row is ignored everywhere, as if it were not there.

**Shipping Policy** (single). Active; the rows; **one** fallback, "When we can't price an order": don't offer delivery (default), charge a fixed amount, or free. It replaces "Anything not covered", the policy default, every zone's fallback and "Use the rate card". The price source, the rate card and everything that read them are removed. A policy cannot be switched on without at least one enabled row.

## How an order is priced

The inputs: the destination country, the items with their quantities, and, for carrier-priced shipments only, the carrier's quotes (see the conversation below). The ERP knows each item's item groups, price, code and unit weight. A weight is the unit weight in kilograms times the quantity; an item with no usable weight counts as 0.35 kg per unit and the answer says so. A weight in pounds is kilograms times 2.20462262, rounded to three decimals. The order value comes from the ERP's price list, never from the caller.

1. **Active?** If the policy is off, every quote is returned unchanged, exactly as before.
2. **Destination.** Anything that is not two letters is `not_served`. A country in an enabled Not delivered zone is `not_shipped`, with that zone's message.
3. **Set per product.** Scan the enabled rows in order. The first row whose product set lists the item (a row with no set lists every product) decides the item's set. The set is decided by the product alone, not by the destination. The items of one set form one shipment.
4. **Row per set.** Among the enabled rows of the set, in order, take the first whose zone holds the destination (an Everywhere zone holds every country) and whose order-value condition holds. If a set that has items has no such row, the order cannot be priced and its reason is `not_covered`: go to step 8.
5. **Price per shipment.** By the row:
   - Free: 0. Fixed: the amount.
   - Weight table: the shipment's weight in pounds against the tiers. The first tier whose upper weight is at least the weight applies, so a weight in a gap between tiers pays the next tier up and a weight below the first tier pays the first. A weight above the top tier is `too_heavy`: delivery is not offered and the fallback does not apply (a heavy order is a refusal, not a pricing failure).
   - Carrier's price: the carrier's quote for the shipment's cartons, per service. If no enabled service serves the destination, or the heaviest carton is over the weight limit of every service that does (a service with no limit has none), the answer is `not_served`, with no fallback: the policy does not sell to places nobody delivers to. A service that did not quote every carton of the shipment is dropped, because its total would be too low. If the ERP has no Shipping Service records at all, every country counts as served.
   - Formula: evaluated per service on that service's quote, with the names of the first design (`carrier_price`, `order_value`, `weight_kg`, `volume_l`, `boxes`, `item_count`), where weight, volume, boxes and item count are the shipment's. A formula that fails makes the order unpriceable (step 8).
   - Round up, if chosen, rounds up to the next whole dollar (already whole prices stay, zero stays zero). Prices never go below zero and are rounded to cents.
6. **Usable services of a shipment.** For a carrier-priced row: the services that quoted, limited to the ticked ones when any are ticked. For any other row: with nothing ticked, any service; otherwise the ticked services that are enabled and serve the destination according to the ERP's Shipping Service country lists (a service whose records are missing counts as serving), and if none of them does, any service, with a line in the trace saying the row falls back to a plain delivery. The zone, not the carrier, decides where such a row sells (the rule of the interim card is kept), so a service that does not serve the destination is never named at checkout.
7. **Offers.** The customer chooses one delivery for the whole order. Let S be the services usable by every shipment, where "any" accepts any service.
   - S is a non-empty list: one offer per service in S, priced as the sum over the shipments of that shipment's price for that service (a table, fixed or free price does not depend on the service; a carrier price does). Each offer carries the service's provider, carrier code, scope and estimated days.
   - Every shipment says "any" and none is carrier-priced: one plain offer, priced as the sum, named after the label of the row that promises the most days (or "Delivery"), with those days, no provider and no service. It is shipped by hand, as the interim card is today.
   - Otherwise (the shipments' ticked services share nothing): when no shipment is carrier-priced, the same plain offer; when one is, the order cannot be priced, reason `not_covered`: step 8.
8. **Fallback.** The policy's single setting decides. Don't offer delivery: no offer, and the reason is the one of the failure (`not_covered`, `formula_error`, or `not_offered` when the carrier gave no usable price). A fixed amount or free: one plain offer "Standard delivery", reason `fallback_offer`. The reasons `not_served`, `not_shipped` and `too_heavy` never reach the fallback.
9. A failure is never zero. A missing price, a quote that is not a price and a formula that fails are all "cannot be priced".

The guest answer lists, per offer: the service id, name, amount, provider, carrier code, scope, estimated days, source (`table`, `fixed`, `free`, `carrier`, `formula`, `fallback`) and the reason code when one applies. It carries no row number, trace, formula or tier. The wizard's Try an order gets the same answer plus a trace per shipment (which row, which tier, how much).

## The conversation with the website

The website asks the ERP twice at most.

1. `price_delivery(country_code, items)` (guest, as now). If no shipment is carrier-priced the answer is final: `offers` is all that is needed and the carrier is never asked (the website's behaviour with the rate card today). If some shipment needs the carrier, the answer says `needs_quotes` and lists the **shipments** to quote: for each, its index, the items and quantities in it, and the services allowed (or any). A plan token identifies the policy the plan was made from.
2. The website packs each listed shipment's items into cartons, asks the carrier per carton as it does now (keeping only the allowed services), adds up each service over the shipment's cartons, and calls `price_delivery(country_code, items, group_quotes, plan_token)` with, per shipment, its cartons and its quotes (service id, name, amount, cartons quoted). The ERP forms the same shipments again from the same items, checks them against what it was sent, and returns the final offers. A plan that no longer matches (the policy was saved in between, an index does not exist) is refused with a reason the website answers by asking again from step 1, once. A mismatch is never priced silently.
3. The payment route's server-side re-quote runs the same two steps, so the amount charged is the amount shown.
4. When the ERP is missing, slow (4 seconds) or answers nonsense, the website prices delivery from the carrier as it did before the policy existed, and logs why (unchanged).
5. The methods list gains the provider, carrier code and scope from the offers, so the checkout shows logo, label, days and price for the offered services as it does for the carrier today, and the order's shipping quote carries provider, service id, service name, scope and days. A plain offer has no provider, as the rate card's has now.

## Shopify import

The two CSV files are read as they are today (columns, tolerance, notes, skipped rows). The result is no longer a card but:

- **Zones.** One per distinct country list. Profiles that use the same zone name with the same countries share the zone; a name that carries different countries in two profiles is told apart by adding the profile's name. The countries missing from the ERP's Country list are reported as now.
- **Product sets.** One per Shopify profile, in the profile order of the file, with the patterns the owner's four profiles carry today (Dangerous Goods Petite `F-*-PT` and `F-CR-PC-*`, Refill Service `F-*-RF`, Dangerous Goods `F-*`; Rest of the World Allowed Items lists no products, so it is the last set). An unknown profile name becomes a set with no products, flagged.
- **Rows.** One per profile and zone: that zone, that set, no service ticked, the charge Weight table (several tiers) or Fixed amount (one flat price), the label from the method text of the file and the days from its upper number. The `weight from` column is not stored: the first tier whose upper weight is at least the weight is the whole rule.
- **Not delivered.** The twelve-country file becomes a Not delivered zone.
- The import shows what it found and could not use (the notes of today's preview) and a short summary; "Replace my sets" or "Add to my sets"; nothing is kept until Save.

## What goes away

The Zones, Rates, Rules, "If the carrier can't quote", Review and Test tabs and the step navigation; the price source, the rate card and its separate editor, preview and summary; "Anything not covered"; the policy default fallback, the per-zone fallbacks and "Use the rate card"; weight and volume conditions; the rule that a country is in one zone only; the rule that a row for chosen items applies only when every item of the order is one of them (replaced by "each product is priced by its own set, and the shipments are added up", as the Shopify card does); the sentence-of-chips rule editor; typed carrier quotes, cartons and order values in Try an order. Tests that pin any of these are retired, not kept alive.

## How we will know it works

- **Hermetic engine tests** for: the set of a product, the first open row, sums over shipments, every table edge (a gap, below the first tier, above the top), fixed, free, formula, carrier price, services (ticked, any, not serving, none usable, no common service), the plain offer, the fallback, overlapping zones with a Not delivered zone, the order-value condition, `not_served` for carrier rows, and "a failure is never zero".
- **A parity harness.** The first engine (this repository at commit `d2364e8`) and the new one run the same imported card (a made-up card with the same structure as the real one, plus the real one run locally and never committed) on the same orders: every worked example of the user guide, then a generated battery over product kinds, quantities, weights and every country the card knows. Totals and "not offered" must match to the cent. The only differences allowed are the ones this document lists (the method's name and days come from the row or the ticked service).
- **On erp2:** the doctypes install without custom anything; the import fills the policy; the page runs in a headless browser (build a policy from scratch and by import, pick products with the live filter, tick services, save, conflict on a stale version, non-manager refused), with screenshots in light and dark; a guest can call the two read methods and nothing else; Save is all or nothing.
- **Website:** unit tests of the two-step conversation (final at once, `needs_quotes`, stale plan, ERP down, mixed carts, plain and service offers), then a local site wired to erp2 with the carrier stubbed, checked headlessly for Bahrain, the US, a mixed cart and a carrier-priced set; the order payload carries the provider and service.
- **After the owner promotes:** the read-only live check of the first design, adapted (the ERP method answers, the site agrees with the ERP).
- The user guide, the helper documents and the promote script are rebuilt from the final state.

## Left to the builder

Field and method names beyond those above, validation wording, how the plan token is made, the shape of the previews, the structure of the page's script and style, how the picker queries the catalogue (the page must stay fast with a few thousand Website Items), the order of work, and how the files reach erp2.

## Calls I made (change any of them)

- A product's set is decided by the first row that lists it, not by the first row that is open for the destination. That keeps the Shopify rule that a petite perfume to a country its own set does not cover is refused rather than priced as a full-size perfume.
- A row with no service ticked is a plain delivery for table, fixed and free charges (as the interim card is) and "any service" for the carrier's price.
- Weight and volume conditions are removed because they depend on packing, which is not known when the shipments are formed; the weight table does what a weight condition was for.
- One fallback instead of four settings; "pass the carrier's price through" becomes an ordinary visible row.
- Zone type Everywhere holds every country, and the order of the rows decides.
- The weight table's tiers keep an upper weight and a price only.
- When the ERP fails, customers see the carrier's own prices, as in the first design.
- The page's words say "products", "ship to", "delivered by" and "price"; the doctypes keep their names.
