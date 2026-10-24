// Part of the Shipping policy wizard page: shipping_policy_wizard.js stitches it in and says how.
//
// The Test tab's body. Task 16 replaces this stub with the three checks of what is live
// (test_integration). Test is not a step: its tab sits apart at the end of the row. Until then it prints
// the placeholder line.
const step_test = {
	render(wizard, panel) {
		panel.append(el("p", { class: "sp-placeholder" }, __("This step is built in the next stage.")));
	},
	refresh(wizard, panel) {},
	priced(wizard, panel, answer) {},
};
