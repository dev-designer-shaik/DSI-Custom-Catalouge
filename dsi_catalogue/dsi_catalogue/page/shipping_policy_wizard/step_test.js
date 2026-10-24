// Part of the Shipping policy wizard page: shipping_policy_wizard.js stitches it in and says how.
//
// The Test tab's body: three read-only checks of what is live, not of the draft (test_integration): the ERP's
// price API, the carrier, and the website. Test is not a step: its tab sits apart at the end of the row, and
// nothing here reads or changes the draft. It reads top down:
//	- one line that says the checks use the saved policy and the live site;
//	- the sample order: where it goes (the CountryPicker), the city, and up to 10 items (the tape's item search,
//	  each with its quantity). Left empty, each is the server's default; after a run it shows the sample the server
//	  used (its `order`), with the note the server gives when it had to make do;
//	- "Run the tests": one request at a time (a run takes 15 to 45 seconds, sometimes more), with a spinner and the
//	  button held while it runs; a refused order is said in plain words;
//	- the three checks, always in the same order, each with its status in words ("Passed", "Did not pass", "Not
//	  asked", "Rate limited", the last two neutral), its time, its summary, and its details folded away;
//	- under a double rule, the verdict: one sentence, read out when it comes.
// What the tab shows lives in wizard.ui.test, so that a result lasts while the page is open (a new run clears it):
// `order` (the sample as typed), `result`, `running`, `problem`, `note` and `view` (the view drawn last).

const MAX_TEST_ITEMS = 10; // test_integration takes at most this many items

const TEST_CHECKS = [
	{ name: "erp_price_api", label: __("ERP price API"), testid: "test-check-erp-api" },
	{ name: "carrier", label: __("Carrier API"), testid: "test-check-carrier" },
	{ name: "website", label: __("Website"), testid: "test-check-website" },
];

Object.assign(sp, {
	// A check's status in words, and its kind for the look: "passed", "failed", "not_asked" or "limited" (the website
	// allows 10 asks a minute: an HTTP 429 says nothing about the integration), or "missing" when it did not run.
	check_status(check) {
		if (!sp.is_record(check)) return { kind: "missing", text: __("Not run") };
		if (check.ok === true) return { kind: "passed", text: __("Passed") };
		if (check.status === 429) return { kind: "limited", text: __("Rate limited") };
		if (sp.is_record(check.detail) && check.detail.problem === "not_asked") return { kind: "not_asked", text: __("Not asked") };
		return { kind: "failed", text: __("Did not pass") };
	},
});

let test_views = 0; // ids for the labels of each view

class TestView {
	constructor(wizard, panel) {
		test_views += 1;
		const uid = (this.uid = `sp-test-${test_views}`);
		this.wizard = wizard;
		const ui = (this.ui = wizard.ui.test);
		ui.view = this; // the view drawn last: an answer that arrives later goes to it
		ui.order = ui.order || { country: "", city: "", items: [] };

		this.country = new dsi_catalogue.CountryPicker({
			id: `${uid}-country`,
			countries: () => wizard.state.countries,
			placeholder: __("United States, unless you choose"),
			testid: "test-country",
			on_change: (code) => {
				ui.order.country = code;
			},
		});
		this.country.set(ui.order.country);
		this.city = el("input", {
			type: "text",
			id: `${uid}-city`,
			class: "form-control sp-input",
			maxlength: 140,
			autocomplete: "off",
			placeholder: __("New York, unless you type one"),
			"data-testid": "test-city",
		});
		this.city.value = ui.order.city;
		this.city.addEventListener("input", () => {
			ui.order.city = this.city.value;
		});
		const picker = el("div", { class: "sp-item-picker" });
		this.item_control = frappe.ui.form.make_control({
			parent: picker,
			df: {
				fieldtype: "Link",
				options: "Item",
				fieldname: "sp_test_item",
				placeholder: __("Search items by code or name"),
				only_select: 1,
				change: () => this.add_item(),
			},
			only_input: true,
			render_input: true,
		});
		this.item_control.$input.attr({ id: `${uid}-item`, "data-testid": "test-add-item", "aria-describedby": `${uid}-items-hint` });
		this.items = el("ul", { class: "sp-items", "aria-label": __("Items of the sample order"), "data-testid": "test-items" });
		this.items_hint = el("p", { class: "sp-hint", id: `${uid}-items-hint` });
		this.note = el("p", { class: "sp-hint", "data-testid": "test-order-note" });

		this.run_button = el(
			"button",
			{ type: "button", class: "btn btn-primary btn-sm", "data-testid": "test-run", onclick: () => this.run() },
			__("Run the tests")
		);
		// the desk's own spinner (Bootstrap's spinner-border), named for assistive technology
		this.spinner = el("span", {
			class: "spinner-border sp-spinner",
			role: "img",
			"aria-label": __("Running the checks"),
			hidden: true,
		});
		this.status = el("span", { class: "sp-test-status", role: "status" });
		this.problem = el("p", { class: "sp-test-problem", role: "alert", "data-testid": "test-error" });
		this.checks = el("ol", { class: "sp-checks", "aria-label": __("The three checks, in the order they run") });
		this.verdict = el("p", {
			class: "sp-test-verdict",
			"aria-live": "polite",
			"aria-atomic": "true",
			"data-testid": "test-verdict",
		});
		this.results = el(
			"section",
			{ class: "sp-test-results", "aria-label": __("What the checks found"), hidden: true },
			this.checks,
			el("div", { class: "sp-test-total" }, el("p", { class: "sp-test-verdict-label" }, __("Verdict")), this.verdict)
		);

		panel.append(
			el("p", { class: "sp-step-intro" }, __("These checks use the saved policy and the live site, not your draft.")),
			el(
				"fieldset",
				{ class: "sp-test-order" },
				el("legend", { class: "sp-test-legend" }, __("The sample order")),
				el(
					"div",
					{ class: "sp-row sp-test-place" },
					el(
						"div",
						{ class: "sp-field" },
						el("label", { class: "sp-label", for: `${uid}-country` }, __("Ship to")),
						this.country.root
					),
					el(
						"div",
						{ class: "sp-field" },
						el("label", { class: "sp-label", for: `${uid}-city` }, __("City")),
						this.city
					)
				),
				el(
					"div",
					{ class: "sp-field" },
					el("label", { class: "sp-label", for: `${uid}-item` }, __("Items")),
					picker,
					this.items,
					this.items_hint
				),
				this.note
			),
			el("div", { class: "sp-test-run" }, this.run_button, this.spinner, this.status),
			this.problem,
			this.results
		);
		this.render_items();
		this.show();
	}

	// ---------------------------------------------------------- the sample order
	add_item() {
		const code = this.item_control.get_value();
		if (!code) return;
		this.item_control.set_value("");
		const items = this.ui.order.items;
		const line = items.find((entry) => entry.item_code === code);
		if (line) {
			const qty = sp.read_number(line.qty, { above_zero: true });
			line.qty = String((qty.value || 0) + 1);
		} else if (items.length < MAX_TEST_ITEMS) {
			const added = { item_code: String(code), qty: "1", name: "" };
			items.push(added);
			this.name_item(added);
		}
		this.render_items();
	}

	// The item's name, for its line; the code stays until it arrives.
	name_item(line) {
		frappe.db.get_value("Item", line.item_code, "item_name").then(
			(response) => {
				const name = response?.message?.item_name;
				if (!name || !this.ui.order.items.includes(line)) return;
				line.name = String(name);
				this.ui.view?.render_items();
			},
			() => {}
		);
	}

	render_items() {
		const items = this.ui.order.items;
		const focused = document.activeElement;
		const keep = focused && this.items.contains(focused) ? focused.dataset.testid : null;
		const at = keep ? [...this.items.children].findIndex((li) => li.contains(focused)) : -1;
		this.items.replaceChildren(
			...items.map((line, index) => {
				const label = line.name || line.item_code;
				const qty = el("input", {
					type: "text",
					class: "form-control sp-input sp-qty",
					inputmode: "decimal",
					autocomplete: "off",
					value: line.qty,
					"aria-label": __("Quantity of {0}", [label]),
					"data-testid": "test-item-qty",
				});
				qty.addEventListener("input", () => {
					line.qty = qty.value;
				});
				return el(
					"li",
					{ class: "sp-item", "data-testid": "test-item" },
					el("span", { class: "sp-item-name", title: line.item_code }, label),
					qty,
					el(
						"button",
						{
							type: "button",
							class: "sp-link sp-remove",
							"aria-label": __("Remove {0}", [label]),
							"data-testid": "test-item-remove",
							onclick: () => this.remove_item(index),
						},
						__("Remove")
					)
				);
			})
		);
		if (at >= 0 && this.items.children[at]) this.items.children[at].querySelector(`[data-testid="${keep}"]`)?.focus();
		const full = items.length >= MAX_TEST_ITEMS;
		this.items_hint.textContent = full
			? __("The sample holds at most {0} items.", [MAX_TEST_ITEMS])
			: items.length
			? ""
			: __("Left empty, the sample is a published full-size fragrance and a published watch.");
	}

	remove_item(index) {
		this.ui.order.items.splice(index, 1);
		this.render_items();
		const next = this.items.children[Math.min(index, this.items.children.length - 1)];
		(next?.querySelector(".sp-remove") || this.item_control.$input.get(0)).focus();
	}

	// The order test_integration is sent: what is typed, left out where it is empty (the server's default), or a
	// sentence saying what cannot be sent.
	order_args() {
		const order = this.ui.order;
		const args = {};
		if (order.country) args.country_code = order.country;
		if (order.city.trim()) args.city = order.city.trim();
		const items = [];
		for (const line of order.items) {
			const qty = sp.read_number(line.qty, { above_zero: true });
			if (qty.value === null) return { refusal: __("Type each quantity as a number above 0, such as 1 or 2.") };
			items.push({ item_code: line.item_code, qty: qty.value });
		}
		if (items.length) args.items = items;
		return { args };
	}

	// After a run, the fields show the sample the server used.
	take_order(order) {
		if (!sp.is_record(order)) return;
		const ui = this.ui;
		const before = new Map(ui.order.items.map((line) => [line.item_code, line.name]));
		ui.order = {
			country: typeof order.country_code === "string" ? order.country_code : ui.order.country,
			city: typeof order.city === "string" ? order.city : ui.order.city,
			items: (Array.isArray(order.items) ? order.items : []).filter(sp.is_record).map((item) => ({
				item_code: String(item.item_code ?? ""),
				qty: String(item.qty ?? ""),
				name: before.get(String(item.item_code ?? "")) || "",
			})),
		};
		ui.note = typeof order.note === "string" ? order.note : "";
		for (const line of ui.order.items) if (!line.name) this.name_item(line);
	}

	// ---------------------------------------------------------- the run
	run() {
		const ui = this.ui;
		if (ui.running) return;
		const order = this.order_args();
		if (order.refusal) {
			ui.problem = order.refusal;
			this.show();
			return;
		}
		ui.running = true;
		ui.result = null;
		ui.problem = "";
		this.show();
		this.wizard.call("test_integration", { order: order.args }, { quiet: true }).then(
			(answer) => {
				ui.running = false;
				ui.result = sp.is_record(answer) ? answer : { checks: [], verdict: "" };
				const view = ui.view;
				view?.take_order(ui.result.order);
				if (view) {
					view.country.set(ui.order.country);
					if (document.activeElement !== view.city) view.city.value = ui.order.city;
					view.render_items();
				}
				view?.show();
			},
			(problem) => {
				ui.running = false;
				ui.problem = __("The checks could not run: {0}", [String(problem)]);
				ui.view?.show();
			}
		);
	}

	// ---------------------------------------------------------- what the tab shows
	show() {
		const ui = this.ui;
		this.run_button.setAttribute("aria-disabled", String(Boolean(ui.running)));
		this.run_button.classList.toggle("disabled", Boolean(ui.running));
		this.spinner.hidden = !ui.running;
		this.status.textContent = ui.running ? __("Running the checks. A run takes 15 to 45 seconds, sometimes more.") : "";
		this.problem.textContent = ui.problem || "";
		this.note.textContent = ui.note ? __("The server says of the sample: {0}", [ui.note]) : "";
		const result = ui.result;
		this.results.hidden = !result;
		const said = JSON.stringify(result);
		if (said === this.result_said) return;
		this.result_said = said;
		if (!result) {
			this.checks.replaceChildren();
			this.verdict.textContent = "";
			return;
		}
		const checks = Array.isArray(result.checks) ? result.checks.filter(sp.is_record) : [];
		this.checks.replaceChildren(
			...TEST_CHECKS.map((spec) => this.check_card(spec, checks.find((check) => check.name === spec.name)))
		);
		this.verdict.textContent = String(result.verdict ?? "");
	}

	// One check as a receipt line: its status in words, its name, its time, its summary, and its details folded.
	check_card(spec, check) {
		const status = sp.check_status(check);
		const detail = sp.is_record(check?.detail) ? check.detail : {};
		const ms = sp.is_number(check?.ms) ? check.ms : null;
		const toggle_text = el("span", {}, __("Show details"));
		const details = el(
			"details",
			{ class: "sp-check-detail" },
			el("summary", { "data-testid": "test-check-details" }, toggle_text),
			el("pre", { class: "sp-check-json" }, JSON.stringify(detail, null, 2))
		);
		details.addEventListener("toggle", () => {
			toggle_text.textContent = details.open ? __("Hide details") : __("Show details");
		});
		return el(
			"li",
			{ class: `sp-check is-${status.kind}`, "data-testid": spec.testid, "data-status": status.kind },
			el(
				"div",
				{ class: "sp-check-head" },
				el("span", { class: "sp-check-status", "data-testid": "test-check-status" }, status.text),
				el("h3", { class: "sp-check-name" }, spec.label),
				ms === null ? null : el("span", { class: "sp-check-time" }, __("{0} ms", [ms.toLocaleString("en-US")]))
			),
			el(
				"p",
				{ class: "sp-check-summary", "data-testid": "test-check-summary" },
				check ? String(check.summary ?? "") : __("This check did not run.")
			),
			status.kind === "limited"
				? el("p", { class: "sp-hint" }, __("Rate limited: the site allows 10 asks a minute. Run the tests again in a minute."))
				: null,
			check ? details : null
		);
	}
}

const step_test = {
	render(wizard, panel) {
		wizard.ui.test.view = new TestView(wizard, panel);
	},
	refresh(wizard, panel) {},
	priced(wizard, panel, answer) {},
};
