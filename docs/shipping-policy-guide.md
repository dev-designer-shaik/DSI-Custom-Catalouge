# Shipping policy

Updated 25 October 2026. Configure the sandbox at https://erp2.shaik.net/desk/shipping-policy-wizard. System Manager or Website Manager access is required.

## Start with your own policy

This is one page. A product group contains delivery rows: **Country group**, **Delivered by**, **Customer pays**. There is no separate rate card or price-source switch. The optional Shopify import converts the old CSV into ordinary editable groups and rows. Nothing in the price engine depends on Shopify profiles or a particular provider.

The sandbox policy is off. The live website and live ERP have not been changed by this build. Website integration changes are local and must be deployed with the ERP before the live checkout can use the new policy.

1. Add a product group and choose products. Search updates while you type; narrow by ERP item group, code pattern, or published products. Tick individual results, or select the results shown.
2. To include future products automatically, choose one item group or one code pattern and click **Follow this filter**. Combined filters and text searches produce a fixed selection. A group's explicit products, item groups and patterns are combined with OR.
3. Add a country group row. Country groups are reusable lists. **Everywhere else** covers destinations without a specific row in that product group.
4. Choose forwarding services and the price customers pay. All providers and services come from ERP records. Disabled services are shown as disabled.
5. Try an order using actual ERP products and a destination. City and, where required, postcode are used for provider quotes. The preview uses the current draft even while the saved policy is off.
6. **Review and save** stores the whole policy atomically. The save bar only appears when there are edits. An old browser draft can be continued or discarded. Another user's save requires reloading before saving yours.

## Customer pays

| Choice | What happens |
|---|---|
| Free | An explicit zero customer charge. |
| One price | Your fixed USD charge for this product group's shipment. |
| By weight | Your USD price tiers, using item weights and quantities. Bounds are in pounds. A blank final bound means “and above.” Above a bounded final tier, delivery is refused. |
| Carrier's price | Every eligible service keeps its own API quote. With no services selected, every available API service is offered. |
| Formula | The expression runs separately over each eligible service's API quote. Every successful service remains an option with its own price. |

For example, `carrier_price * 1.1 + 5` adds 10% and USD 5 to each service's quote. This is an example, not an installed pricing rule. Expressions also support `order_value`, `weight_kg`, `volume_l`, `boxes`, `item_count`, and the functions `min`, `max`, `round`, `ceil`, `floor`, `abs`. Expressions are validated by a restricted evaluator; arbitrary Python cannot run. Formula rows request provider quotes, including when the expression does not mention `carrier_price`.

For free, fixed and weight prices, select services to carry your price while retaining the provider needed for booking. Leaving the services empty means a plain delivery arranged manually. For carrier/formula rows, an empty service selection means **all eligible API services**; selecting some restricts the offers to those services.

A manual row whose selected services cannot serve the shipment becomes a plain delivery, with a warning in the preview. Plain deliveries carry no provider and require manual fulfilment. If automatic forwarding is required, check that the preview names the intended service for every destination.

## Matching and delivery boundaries

Product groups claim products in the order shown. A group with no delivery rows still owns its products and refuses their delivery; they cannot fall through into a later group's prices. An empty named group owns no products. The optional **Everything else** card handles unclaimed products.

Each group forms its own shipment. A mixed cart adds the shipment prices. This can mean more than one carrier quote when products are in separate groups. Group together products intended to travel in the same shipment.

A country may appear in different product groups, but only once within one group's specific delivery rows. Not-delivered country groups always win. A product with no matching delivery row, a blocked country, an unsupported carrier destination, or an order above the last weight tier is refused. These cases never use the carrier-failure fallback.

The single fallback covers a provider or formula that gives no usable price. It can refuse delivery or charge your chosen flat amount (zero deliberately means free). It prices only failed shipments; successfully priced shipments keep their charges. A fallback order is a plain delivery that needs manual fulfilment.

For a combined order the customer chooses a service usable by every shipment. If no common service exists, carrier-priced orders are refused; entirely manual-priced orders can be offered as one plain delivery. Partial carton quotes are never used as complete shipment prices.

## Provider API and checkout

The website first asks ERP to resolve the policy. Manual prices can finish immediately. For carrier/formula rows, ERP returns only the shipments needing quotes and an opaque plan token. The website packs and quotes those shipments, then asks ERP to calculate the final offers. A changed policy/cart/directory makes the plan stale and triggers one fresh round.

Checkout receives every resulting offer, its provider, booking code, service, price, and estimated days. Quote-based offers retain the API's collection/delivery dates when supplied. The payment route runs the same calculation again. Unknown/missing booking codes are not replaced with an Aramex constant.

Policy off or ERP unavailable uses the existing carrier path. There is no cache in the price path. Nothing is booked, paid, or shipped by the preview or integration checks.

## Sandbox notes and checks

Erp2 currently has carrier simulation enabled in its existing configuration. The preview identifies simulated prices. A separate read-only check with simulation disabled for that process returned real carrier quotes; it did not change the site configuration. Provider/service availability and item facts differ between erp2 and live, so their offers need not be identical.

**Check it works** uses the products and address in Try an order. It checks the ERP endpoint, the carrier API and the website configured in Storefront Settings. A website pointing at a different ERP is not proof that the sandbox policy is deployed there.

Missing item weights use the existing 0.35 kg fallback and are flagged in the preview. Prices come from the ERP's USD online price list. Fix product weights and package dimensions for accurate carrier pricing.

Shopify imports report countries missing from ERP's Country list. Resolve those countries explicitly before saving; the importer does not silently remove them. The original rates and source files remain private, outside the public repository.
