frappe.ui.form.on("Shipping Policy", {refresh(frm) {
 frm.add_custom_button(__("Open shipping policy"), () => frappe.set_route("shipping-policy-wizard"));
}});
