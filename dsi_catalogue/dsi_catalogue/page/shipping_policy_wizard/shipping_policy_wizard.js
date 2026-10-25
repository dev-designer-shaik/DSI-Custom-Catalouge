frappe.provide("dsi_catalogue");
(() => {
 const METHOD="dsi_catalogue.shipping_policy_wizard.";
 const MAX_NUMBER=1e9;
 const DRAFT_KEY="dsi_catalogue.shipping_policy.draft.v2";
 {% include "dsi_catalogue/dsi_catalogue/page/shipping_policy_wizard/wizard_helpers.js" %}
 {% include "dsi_catalogue/dsi_catalogue/page/shipping_policy_wizard/wizard_country_picker.js" %}
 {% include "dsi_catalogue/dsi_catalogue/page/shipping_policy_wizard/wizard_core.js" %}
 {% include "dsi_catalogue/dsi_catalogue/page/shipping_policy_wizard/wizard_products.js" %}
 {% include "dsi_catalogue/dsi_catalogue/page/shipping_policy_wizard/wizard_editors.js" %}
 frappe.pages["shipping-policy-wizard"].on_page_load = wrapper => {
  const page=frappe.ui.make_app_page({parent:wrapper,title:__("Shipping policy"),single_column:true});
  wrapper.wizard=new dsi_catalogue.ShippingPolicyWizard(page,wrapper);
 };
 frappe.pages["shipping-policy-wizard"].on_page_show = wrapper => wrapper.wizard?.onShow();
})();
