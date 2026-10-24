// Part of the Shipping policy wizard page: shipping_policy_wizard.js stitches it in and says how.
//
// The Zones tab's body. Task 8 replaces this stub with a card per zone: its name, its countries (a
// dsi_catalogue.CountryPicker), the Not shipped and Everywhere else switches, the customer line, and a
// warning for the countries no carrier serves. Until then it prints the placeholder line.
const step_zones = {
	render(wizard, panel) {
		panel.append(el("p", { class: "sp-placeholder" }, __("This step is built in the next stage.")));
	},
	refresh(wizard, panel) {},
	priced(wizard, panel, answer) {},
};
