// Part of the Shipping policy wizard page: shipping_policy_wizard.js stitches it in and says how.
//
// The Review tab's body. Task 9 replaces this stub with what will change (preview_changes), the policy in
// plain words, the Active switch and Apply. Until then it prints the placeholder line.
const step_review = {
	render(wizard, panel) {
		panel.append(el("p", { class: "sp-placeholder" }, __("This step is built in the next stage.")));
	},
	refresh(wizard, panel) {},
	priced(wizard, panel, answer) {},
};
