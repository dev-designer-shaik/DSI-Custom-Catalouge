// Shipping policy wizard: the desk page, opened from the Shipping Policy form, that edits the policy as a
// draft kept in this browser and prices a sample order with that draft after every change. Nothing is written
// to the ERP until Apply, in the Review step.
//
// The page is this file and the part files of its folder. When the desk loads the page, Frappe puts each part
// where its include line stands (render_include: one directive per line, the path from the app's folder), so
// every part shares the scope of the function below: the constants, and the other parts. A part is plain
// JavaScript; this file alone is not, because of its include lines. The style is the one file
// shipping_policy_wizard.css, which the desk reads as it is.
//
//	wizard_helpers.js         dsi_catalogue.sp: el (elements from text, never HTML), clone, money, read_number
//	wizard_country_picker.js  dsi_catalogue.CountryPicker: a field that finds a country by its name or code
//	wizard_tape.js            dsi_catalogue.ShippingPolicyTape: "Try an order", its controls and the receipt
//	wizard_core.js            dsi_catalogue.ShippingPolicyWizard: the store, the regions, the server calls
//	step_<id>.js              the body of each tab, one file each, step_<id> = {render, refresh, priced}:
//	                          Tasks 8, 9 and 16 replace these stubs, each task its own files
//	this file                 the constants, the parts in order, STEPS (the six tabs) and the page's events:
//	                          on_page_load builds the page and starts the wizard; on_page_show reloads the
//	                          saved policy while the draft is clean
//
// The store, wizard.store = {original, draft, version, step, dirty}:
//	original  the saved policy: a deep copy of wizard_state().policy, never edited
//	draft     the policy being edited, in the same shape (shipping_engine.py documents it); steps change it
//	version   wizard_state().version; Apply sends it back, so that a change someone made meanwhile is noticed
//	step      "zones" | "rates" | "rules" | "fallback" | "review" | "test"
//	dirty     whether the draft differs from the saved policy; worked out after every change, never set
// While the draft is dirty it is kept in localStorage under "dsi_catalogue.sp_draft" as {user, version,
// draft, step}, written 300 ms after the last change and removed once the draft equals the saved policy
// again. On load a stored draft is restored only while its version is still the server's; otherwise it is
// dropped. The draft banner says which of the two happened. A draft stored by someone else signed in on
// this browser is not there for this person: not restored, no notice, not removed (wizard_core.js).
//
// One render function per region:
//	wizard.render_banner()  the draft banner (draft-banner); its lines come from wizard.banner_lines()
//	wizard.render_tabs()    the tab row: which tab is selected and which one the keyboard lands on
//	wizard.render_step()    the selected step's body: STEPS[i].render(wizard, panel), into an emptied panel
//	wizard.tape             the tape: it draws its controls once and its receipt after every answer
//
// How a step body plugs in: its file, step_<id>.js, defines step_<id> = {render, refresh, priced}, which
// STEPS spreads into the step's entry. The stub's render prints the placeholder line; replacing the stub
// removes it. refresh and priced are called only while the step is shown:
//	refresh(wizard, panel)         after any change of the draft (the body is not rebuilt, so focus stays put)
//	priced(wizard, panel, answer)  each time the body is drawn (its first show and every tab switch), and
//	                               after each answer of try_order: {result, previews, order}, or null when
//	                               there is nothing to price
// A step changes the draft only through wizard.change((draft) => { ...edit it in place... }). The wizard then
// works out dirty, stores the draft, redraws the banner and the tabs, calls the step's refresh, and has the
// tape price the order again 250 ms later. wizard.priced holds the latest answer (null while nothing is
// priced); wizard.state the rest of wizard_state(): countries, served_codes, price_list, carrier_available.
// A step that needs its body rebuilt calls wizard.render_step() itself.
//
// Notes for the step bodies:
//	- render_step empties the panel and draws the body again on every tab switch, so an open editor or a
//	  scroll position cannot live in the panel: keep it in wizard.ui[<step id>], an object per step that
//	  lasts for the session and is not stored with the draft.
//	- A dsi_catalogue.CountryPicker gets an id of its own when it is given none (give one only for a label's
//	  `for`), and reads the countries when it uses them: pass countries: () => wizard.state.countries.
//	- dirty compares the draft's JSON with the saved policy's, key order included: build new entries with
//	  dsi_catalogue.sp.new_zone(name), new_rule(zone) and new_item_set(name), which follow the server's
//	  order, so that an addition taken back leaves the draft clean.
//
// Text: everything the server returns and everything people type reaches the page as text (sp.el and
// textContent), never as HTML. Errors raised through Frappe's message path arrive as HTML that the server
// escaped; they are shown in Frappe's own dialog, or read with frappe.utils.html2text.

frappe.provide("dsi_catalogue");

(() => {
	const METHOD = "dsi_catalogue.shipping_policy_wizard.";
	const DRAFT_KEY = "dsi_catalogue.sp_draft";
	const SAVE_DELAY = 300; // ms after the last change before the draft is stored
	const PRICE_DELAY = 250; // ms after the last change before the sample order is priced again
	const MAX_NUMBER = 1e9; // the most the server takes for an amount, a weight, a volume or a quantity
	const MAX_PACKED_UNITS = 1000; // the most units Try an order packs into cartons on the server
	const WHOLE_SHIPMENT = 50; // a typed quote is for every carton: the most cartons the server takes
	const TYPED_QUOTE = "typed-quote"; // the service id of the quote typed into the tape

	// The parts. Each runs where it stands, so a part may use at once only what an earlier part defines; its
	// functions may use anything, since they run after the page has loaded.
	{% include "dsi_catalogue/dsi_catalogue/page/shipping_policy_wizard/wizard_helpers.js" %}
	{% include "dsi_catalogue/dsi_catalogue/page/shipping_policy_wizard/wizard_country_picker.js" %}
	{% include "dsi_catalogue/dsi_catalogue/page/shipping_policy_wizard/wizard_tape.js" %}
	{% include "dsi_catalogue/dsi_catalogue/page/shipping_policy_wizard/wizard_core.js" %}
	{% include "dsi_catalogue/dsi_catalogue/page/shipping_policy_wizard/step_zones.js" %}
	{% include "dsi_catalogue/dsi_catalogue/page/shipping_policy_wizard/step_rates.js" %}
	{% include "dsi_catalogue/dsi_catalogue/page/shipping_policy_wizard/step_rules.js" %}
	{% include "dsi_catalogue/dsi_catalogue/page/shipping_policy_wizard/step_fallback.js" %}
	{% include "dsi_catalogue/dsi_catalogue/page/shipping_policy_wizard/step_review.js" %}
	{% include "dsi_catalogue/dsi_catalogue/page/shipping_policy_wizard/step_test.js" %}

	// The tabs, in order. Test is not a step: it checks what is live, so it sits apart at the end of the row.
	const STEPS = (dsi_catalogue.ShippingPolicyWizard.STEPS = [
		{ id: "zones", label: __("Zones"), ...step_zones },
		{ id: "rates", label: __("Rates"), ...step_rates },
		{ id: "rules", label: __("Rules"), ...step_rules },
		{ id: "fallback", label: __("If the carrier can't quote"), ...step_fallback },
		{ id: "review", label: __("Review"), ...step_review },
		{ id: "test", label: __("Test"), name: __("Test the integration"), apart: true, ...step_test },
	]);

	frappe.pages["shipping-policy-wizard"].on_page_load = (wrapper) => {
		const page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("Shipping policy wizard"),
			single_column: true,
		});
		wrapper.wizard = new dsi_catalogue.ShippingPolicyWizard(page, wrapper);
	};

	frappe.pages["shipping-policy-wizard"].on_page_show = (wrapper) => wrapper.wizard?.on_show();
})();
