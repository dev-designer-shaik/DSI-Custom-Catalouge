# Shipping Policy: how delivery prices are set

Document type: standard operating procedure for the ERP assistant and the team. Folder: Website.

The Shipping Policy decides what a customer pays for delivery at checkout. It is managed on ONE page in the ERP desk: `/desk/shipping-policy-wizard`. Every user with the Sales User, Website Manager or System Manager role can open and edit it; nobody else can. To give someone else access, open their User record and tick Sales User (or Website Manager) under Roles; they must be a System User.

## The pieces

- **Product group** (record: Shipping Item Set). The products that travel together as one shipment: explicit products, item groups, and code patterns that follow new products automatically.
- **Country group** (record: Shipping Zone). A reusable list of countries from the ERP's Country list. One country group holds the countries that are not delivered to.
- **Delivery row.** Inside a product group: Country group, Delivered by, Customer pays.
- **Delivered by.** The forwarding services. Every provider and service comes from the ERP's Shipping Provider and Shipping Service records; disabled services are shown as disabled.
- **The policy** (single record: Shipping Policy). Active, the one fallback, and all the rows.

## How do I change the shipping policy?

1. Open `/desk/shipping-policy-wizard`.
2. **To add or change a product group:** add a group and choose its products. Search updates while you type; narrow by ERP item group, code pattern, or published products. Tick individual results or select all shown. To include future products automatically, choose one item group or one code pattern and click **Follow this filter**. Combined filters and text searches make a fixed selection. A group's explicit products, item groups and patterns are combined with OR.
3. **To add or change a delivery row:** pick the Country group (or **Everywhere else**, which covers every destination without a specific row in that group), choose the forwarding services, and set Customer pays (below).
4. **To change a country group:** edit the list of countries. A country may appear in different product groups but only once within one group's specific rows. The not-delivered group always wins.
5. **Try an order.** Choose real ERP products and a destination (city and, where required, postcode). The preview uses your current draft, even when the saved policy is off, and says which group and row priced each shipment and why.
6. **Review and save.** The save bar appears when there are edits. Saving writes the whole policy in one step or nothing. If another user saved first, reload and redo your edit. An unsaved browser draft can be continued or discarded.
7. **Switch it on or off.** Tick **Active** and save to put the policy into use. Untick it and save to return to the carrier's own price immediately. While the policy is off, or if the ERP does not answer within 4 seconds, customers pay the carrier's own price.

## Customer pays

| Choice | What the customer is charged |
|---|---|
| Free | An explicit zero. |
| One price | Your fixed USD charge for the group's shipment. |
| By weight | Your USD price tiers from item weights and quantities. Bounds are in pounds; a blank final bound means "and above". Above a bounded final tier, delivery is refused. |
| Carrier's price | Each eligible service keeps its own live quote. With no services selected, every available service is offered. |
| Formula | An expression run separately over each eligible service's quote. Every successful service stays an option with its own price. |

Formula example: `carrier_price * 1.1 + 5` adds 10% and USD 5 to each service's quote. Variables: `carrier_price`, `order_value`, `weight_kg`, `volume_l`, `boxes`, `item_count`. Functions: `min`, `max`, `round`, `ceil`, `floor`, `abs`. Also arithmetic, comparisons, `and`, `or`, `not` and `a if condition else b`. The page validates the formula as you type; arbitrary code cannot run. Formula rows always request carrier quotes.

For Free, One price and By weight, select services to attach your price to a provider, so the shipment books through it. With no service selected the delivery is plain and staff arrange it by hand. For Carrier's price and Formula, an empty selection means all eligible services and a selection restricts the offers. If the selected services cannot serve a shipment, it becomes a plain delivery and the preview warns you.

## How a price is decided

1. The not-delivered country group is checked first; those countries get no delivery.
2. Each product belongs to the first product group that claims it, in the order shown on the page. A group with no delivery rows still owns its products and refuses their delivery; they never fall into a later group. An optional **Everything else** group handles unclaimed products.
3. Each product group is one shipment with its own price. A mixed cart adds the shipment prices. Put products that ship together in the same group.
4. In each group the row for the destination applies, else **Everywhere else**.
5. Free, One price and By weight finish inside the ERP. Carrier's price and Formula need the carrier: the ERP names the shipments to quote, the website packs and quotes them, and the ERP finishes from the quotes. If the policy, cart or provider list changes meanwhile, the website asks again once.
6. The customer picks a service every shipment can use. The payment step prices the order again by the same rules. The offer keeps the provider, booking code, service, price and estimated days; quote-based offers keep the carrier's collection and delivery dates.
7. If no service is common to all shipments, carrier-priced orders are refused; orders priced entirely by rows are offered as one plain delivery.

## Refusals and the fallback

These refuse delivery and never use the fallback:
- no product group claims the product;
- the group has no row for the destination;
- the destination is in the not-delivered group;
- the carrier does not serve the destination;
- the weight is above the last tier.

The single **fallback** covers only a provider or formula that returns no usable price. Choose to refuse delivery or to charge a flat amount (zero means free). It prices only the failed shipments; the others keep their prices. A fallback order is a plain delivery fulfilled by hand.

## How do I build a policy from the Shopify shipping file?

Attach the Shopify shipping CSV (or a single-sheet XLSX with the same columns) to the ERP Helper and ask: **Build a shipping policy from this file.** Attach the not-delivered countries file too if you have one. The Helper reads the whole file and opens an unsaved proposal on the policy page as ordinary product groups and delivery rows. Review the product filters, countries, prices and services, edit what you need, then Review and save. The proposal arrives with the policy off; switch it on yourself. Existing not-delivered countries are kept when you supply no replacement file.

Countries in the file that the ERP's Country list lacks are reported. Resolve each one before saving; none are dropped silently. If you already have unsaved edits, the page asks before replacing them.

An empty policy has no rows and offers no delivery. Start with your own rows, or use **Start with the carrier's own prices**.

## Weights, prices and quotes

- Weights: the item's weight per unit is read as kilograms; bounds in By weight rows are pounds (1 kg = 2.20462 lb). An item with no weight counts as 0.35 kg per unit and is flagged in the preview. Keep weights and package dimensions correct for accurate carrier quotes.
- Prices come from the ERP's USD online price list.
- A quote that arrives incomplete, missing or unusable never becomes a free or underpriced delivery.
- **Check it works** tests the ERP endpoint, the carrier API and the website named in Storefront Settings with the products and address in Try an order. A Storefront Settings website that reads a different ERP says nothing about this ERP's policy.

## Product forwarding restrictions

A Shipping Service can be limited to an ERP Item Group and its descendants, with individual products excluded. **Require this service for these products** makes matching orders use that service, including mixed orders. For example, a perfume group can require DGG or DGX while other products keep their normal options. Multiple required services for the same group are alternatives; the destination and live carrier response determine which are available. These settings live on the Shipping Service form and apply independently of the pricing policy. **Require for: International only** exempts domestic deliveries within the provider account country. For Bahrain-origin perfume orders, an eligible dangerous-goods service is required internationally while Bahrain deliveries use the normal domestic services. An unsupported international destination still refuses delivery; it is not treated as a domestic exception.

Manual, free, weight, carrier and formula prices still work. A required forwarding service cannot be replaced by an ordinary service or manual fallback. If the required service is disabled, does not cover the destination, or cannot provide a required quote, delivery is refused. The policy page identifies these restrictions beside the service choices.

**Always use live rates** on a service bypasses sandbox rate simulation for that service. **Contents description for carrier** supplies its configured goods description to the rate request.

## What the website receives

At checkout the website asks the ERP for the price. The ERP answers either with final offers or with the shipments that need carrier quotes and an opaque plan token. The website returns the carton quotes with the token. Every resulting offer carries its provider, booking code, service, price and estimated days. Nothing is cached in the price path. Nothing is booked, paid or shipped by the preview.
