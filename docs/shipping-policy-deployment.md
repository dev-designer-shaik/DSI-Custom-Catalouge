# One-page Shipping Policy deployment record

25 October 2026. User requested the build on ERP2 and then clarified that manual policy, provider API forwarding, every API option, and formulas over each provider quote are required. The current guide supersedes the old six-tab design and the handoff's instruction to remove formulas.

## Delivered

The `shipping-policy-one-page` branches contain the catalogue redesign and the website integration. Neither branch was pushed. Only ERP2 was updated. Live ERP and the hosted website were inspected through read-only quote requests and were not changed.

The page is `/desk/shipping-policy-wizard`. Its product groups, countries, prices, formulas, provider restrictions and fallback are records edited by the manager. The Shopify importer alone contains its legacy profile mapping. There are no provider identities or product-profile price rules in the engine, page or runtime API.

The website asks only for carrier-dependent shipments, then sends their complete per-service carton quotes back to ERP. It preserves provider identity, booking code and quote delivery dates. Its carrier path reads missing booking codes from the public service identity directory instead of supplying an Aramex constant. Missing/partial/junk quotes do not become free or underpriced delivery.

## Verification

- Catalogue: 1,135 hermetic tests pass. The retired six-tab and rate-card behavior tests were replaced, not preserved as dead compatibility requirements.
- ERP2: 13 native tests pass, including atomic rollback, stale saves, group ordering, guest denial, dynamic product search, formula offers and stale plans. Each test rolls back its database writes.
- Browser: actual product filtering, selection, live pattern, manual row creation, review/save, draft order preview, formula validation, all service options and responsive light/dark rendering. Temporary test policies are restored; the saved policy is off.
- Website: 340 tests pass; TypeScript passes. Direct HTTP integration of the real website quote module with ERP2 passes for carrier, formula, manual forwarding and mixed carts. No carrier rate stubs were used in that integration; ERP2's own configured simulation remains enabled.
- Real provider verification: temporarily disabled simulation only in an isolated Python process, requested rates, received three Bahrain service quotes, then exited without changing the site setting. No shipment booking, order, payment or email was created.
- Optional real Shopify CSV: 28,080 generated comparisons against the prior engine at `d2364e8`, zero price/refusal mismatches. The CSV remains outside this repository.
- Live comparison for one Bahrain parcel: the live API and live website both returned one service. ERP2 has different service configuration; its API returned three. The new integration retains every eligible API offer.

## ERP2 installation and rollback

Catalogue files are deployed with `tools/erp2_push.sh --restart`, using the exact paths in `tools/shipping_policy_files.txt` (46 app files and one safe fixture). New child doctype Shipping Item Set Pattern loads before its parent. No global migration is needed.

The old catalogue source and saved policy were backed up under `/home/ubuntu/shipping-policy-backups/20261025-103633/` on ERP2 before deployment. Removed six-tab files and the retired rate-card module were also removed from the sandbox after the backup. The source remains in Git history for rollback.

ERP2's older shipping app lacked the carton packer and postcode-aware rate method. The local operational script `~/erp-live-fix/shipping-policy/erp2/update_shipping_support.py` installs the existing `packing.py`/`packing_core.py` from `dsi_shipping_blueprint`, replaces only the rate/address methods, and adds the provider's actual booking code to rate responses. It does not replace hooks, booking code, fixtures or provider records. Its originals are under `/home/frappe/frappe-bench/sites/private-shipping-backups/20261025-104548/` inside the ERP2 backend. These shipping-app support changes are in that sandbox container; preserve them when rebuilding its image. The shipping app used by another target must already supply equivalent packing/rate support.

## Remaining deployment boundary

The production promotion script is prepared locally and remains guarded by its published-commit check. It is an additive first installation, not an automatic conversion of an existing active legacy policy. It was not run against production. The website branch must be deployed with the matching ERP contract; the existing hosted website does not yet consume this build.

ERP2 was left with no product rules, its existing not-delivered country group, and the policy off. The manager can create a policy or import the old CSV. Import validation reports `AC`, `AN`, `TA`, `XK` as missing from ERP2's Country list; no country was silently removed and no new policy was activated.

The updated two-page PDF/HTML guide and helper Markdown are local under `~/Downloads/shipping-policy-erp2/`. The prior guide is archived as `user-guide-before-one-page`. The helper source was regenerated but not uploaded to Drive or synced to another service.

## Production integration, 25 October 2026

The production rollout merges this work with the current catalogue main and the website's refill changes. The shipping app incorporates configurable product forwarding restrictions on top of the production collection calendar and delivery-date code. DGG and DGX are alternative configured international perfume services; the provider account country remains domestic. Existing domestic enablement is preserved. Services and destination lists are database configuration, never repository fixtures.

The rollout leaves the new pricing policy off and empty for the manager to configure. Matching website code must be live before product restrictions are enabled. Deployment backs up the production database and changed source files, reloads only the new/extended shipping and policy doctypes, preserves unrelated production changes, and verifies every deployed file across backend and workers. Rate and browser checks create no bookings or payments.
