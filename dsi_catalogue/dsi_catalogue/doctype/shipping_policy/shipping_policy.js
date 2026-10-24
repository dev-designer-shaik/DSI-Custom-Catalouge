// Form script for the Shipping Policy: the button that opens the wizard, and one quiet line that says what
// the wizard is for. The wizard edits the whole policy as a draft and writes it on Apply; this form saves
// at once.

frappe.ui.form.on("Shipping Policy", {
	refresh(frm) {
		frm.add_custom_button(__("Open the wizard"), () => frappe.set_route("shipping-policy-wizard"))
			.removeClass("btn-default")
			.addClass("btn-primary");
		const hint = __(
			"The wizard edits this policy step by step and tries orders on it before anything goes live."
		);
		frm.set_intro(`<span class="text-muted">${frappe.utils.escape_html(hint)}</span>`, "white");
	},
});
