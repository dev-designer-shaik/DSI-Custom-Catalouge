// Part of the Shipping policy wizard page: shipping_policy_wizard.js stitches it in and says how.
//
// The "If the carrier can't quote" tab's body. Task 9 replaces this stub with each zone's fallback, the
// policy default and "Anything not covered". Until then it prints the placeholder line.
const step_fallback = {
	render(wizard, panel) {
		panel.append(el("p", { class: "sp-placeholder" }, __("This step is built in the next stage.")));
	},
	refresh(wizard, panel) {},
	priced(wizard, panel, answer) {},
};
