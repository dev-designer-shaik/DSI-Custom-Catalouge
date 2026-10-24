// Part of the Shipping policy wizard page: shipping_policy_wizard.js stitches it in and says how.
//
// The Rates tab's body. Task 16 replaces this stub with the price source, the import of the Shopify
// export, and the rate card's profiles, zones and tiers. Until then it prints the placeholder line.
const step_rates = {
	render(wizard, panel) {
		panel.append(el("p", { class: "sp-placeholder" }, __("This step is built in the next stage.")));
	},
	refresh(wizard, panel) {},
	priced(wizard, panel, answer) {},
};
