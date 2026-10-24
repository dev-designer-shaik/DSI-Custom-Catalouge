// Part of the Shipping policy wizard page: shipping_policy_wizard.js stitches it in and says how.
//
// The Rules tab's body. Task 8 replaces this stub with each rule as a sentence of chips, the price it
// gives the sample order (the previews of try_order), and the asterisk sentence under the list. Until
// then it prints the placeholder line.
const step_rules = {
	render(wizard, panel) {
		panel.append(el("p", { class: "sp-placeholder" }, __("This step is built in the next stage.")));
	},
	refresh(wizard, panel) {},
	priced(wizard, panel, answer) {},
};
