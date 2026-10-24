// Shipping policy wizard: the desk page, opened from the Shipping Policy form, that edits the policy as a
// draft kept in this browser and prices a sample order with that draft after every change. Nothing is written
// to the ERP until Apply, in the Review step.
//
// What is in this file (Tasks 8, 9 and 16 add the step bodies here, in this structure):
//
//	frappe.pages["shipping-policy-wizard"]    on_page_load builds the desk page and starts the wizard;
//	                                          on_page_show reloads the saved policy while the draft is clean
//	dsi_catalogue.ShippingPolicyWizard        the store, the regions of the page and the server calls
//	dsi_catalogue.ShippingPolicyWizard.STEPS  the six tabs in order, each with the function that draws its body
//	dsi_catalogue.ShippingPolicyTape          "Try an order": the sample order's controls and the receipt
//	dsi_catalogue.CountryPicker               a field that finds a country by its name or its code
//	dsi_catalogue.sp                          small helpers: el (elements from text, never HTML), clone, money
//
// The store, wizard.store = {original, draft, version, step, dirty}:
//	original  the saved policy: a deep copy of wizard_state().policy, never edited
//	draft     the policy being edited, in the same shape (shipping_engine.py documents it); steps change it
//	version   wizard_state().version; Apply sends it back, so that a change someone made meanwhile is noticed
//	step      "zones" | "rates" | "rules" | "fallback" | "review" | "test"
//	dirty     whether the draft differs from the saved policy; worked out after every change, never set
// While the draft is dirty it is kept in localStorage under "dsi_catalogue.sp_draft" as {version, draft,
// step}, written 300 ms after the last change and removed once the draft equals the saved policy again. On
// load a stored draft is restored only while its version is still the server's; otherwise it is dropped.
// The draft banner says which of the two happened.
//
// One render function per region:
//	wizard.render_banner()  the draft banner (draft-banner); its lines come from wizard.banner_lines()
//	wizard.render_tabs()    the tab row: which tab is selected and which one the keyboard lands on
//	wizard.render_step()    the selected step's body: STEPS[i].render(wizard, panel), into an emptied panel
//	wizard.tape             the tape: it draws its controls once and its receipt after every answer
//
// How a step body plugs in: replace the placeholder `render` of the step's entry in STEPS, which removes the
// placeholder line with it. An entry may also have, called only while its step is shown:
//	refresh(wizard, panel)         after any change of the draft (the body is not rebuilt, so focus stays put)
//	priced(wizard, panel, answer)  after each answer of try_order ({result, previews, order}), or null when
//	                               there is nothing to price
// A step changes the draft only through wizard.change((draft) => { ...edit it in place... }). The wizard then
// works out dirty, stores the draft, redraws the banner and the tabs, calls the step's refresh, and has the
// tape price the order again 250 ms later. wizard.priced holds the latest answer (null while nothing is
// priced); wizard.state the rest of wizard_state(): countries, served_codes, price_list, carrier_available.
// A step that needs its body rebuilt calls wizard.render_step() itself.
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

	// ------------------------------------------------------------ helpers
	const sp = (dsi_catalogue.sp = {
		// An element from a tag, attributes and children. A child that is a string becomes a text node, so
		// nothing passed here is ever read as HTML. Attribute keys: "class", "text" (the text content),
		// "on<event>" with a function (a listener); any other key is set as an attribute (false, null and
		// undefined leave it out, true sets it empty).
		el(tag, attributes = {}, ...children) {
			const element = document.createElement(tag);
			for (const [key, value] of Object.entries(attributes || {})) {
				if (value === false || value === null || value === undefined) continue;
				if (key === "class") element.className = value;
				else if (key === "text") element.textContent = value;
				else if (key.startsWith("on") && typeof value === "function") {
					element.addEventListener(key.slice(2), value);
				} else element.setAttribute(key, value === true ? "" : String(value));
			}
			const absent = (child) => child === null || child === undefined || child === false;
			element.append(...children.flat(Infinity).filter((child) => !absent(child)));
			return element;
		},

		clone(value) {
			return JSON.parse(JSON.stringify(value));
		},

		same(one, other) {
			return JSON.stringify(one) === JSON.stringify(other);
		},

		is_number(value) {
			return typeof value === "number" && Number.isFinite(value);
		},

		// An amount as the tape prints it: a dollar sign and two decimals.
		money(amount) {
			return `$${amount.toLocaleString("en-US", {
				minimumFractionDigits: 2,
				maximumFractionDigits: 2,
			})}`;
		},

		// A weight or a volume as a hint shows it: at most three decimals.
		measure(value) {
			return value.toLocaleString("en-US", { maximumFractionDigits: 3 });
		},

		// What a person typed as a number: {value, ok}. Blank is {value: null, ok: true}. A leading dollar sign
		// and thousands commas are allowed; a negative number, text, or more than the server takes is not ok.
		read_number(text, { above_zero = false } = {}) {
			const typed = String(text ?? "")
				.trim()
				.replace(/^\$/, "")
				.replace(/,/g, "");
			if (!typed) return { value: null, ok: true };
			if (!/^(\d+(\.\d*)?|\.\d+)$/.test(typed)) return { value: null, ok: false };
			const value = Number(typed);
			const ok = value <= MAX_NUMBER && (above_zero ? value > 0 : value >= 0);
			return { value: ok ? value : null, ok };
		},

		is_record(value) {
			return value !== null && typeof value === "object" && !Array.isArray(value);
		},
	});
	const el = sp.el;

	// The sentences of an error that came through Frappe's message path, as plain text.
	function problem_text(response) {
		let messages = [];
		try {
			messages = JSON.parse(response?._server_messages || "[]").map((entry) => {
				const message = JSON.parse(entry);
				return typeof message === "string" ? message : message?.message;
			});
		} catch (error) {
			messages = [];
		}
		const text = messages
			.filter(Boolean)
			.map((message) => frappe.utils.html2text(String(message)).trim())
			.filter(Boolean)
			.join(" ");
		return text || __("The server did not answer. Check the connection and try again.");
	}

	// The stored draft, or null. Storage can be switched off or full: the draft then lives in this tab only.
	function read_stored() {
		try {
			const text = window.localStorage.getItem(DRAFT_KEY);
			if (!text) return null;
			const stored = JSON.parse(text);
			if (sp.is_record(stored)) return stored;
		} catch (error) {
			// not readable: dropped below
		}
		forget_stored();
		return null;
	}

	function forget_stored() {
		try {
			window.localStorage.removeItem(DRAFT_KEY);
		} catch (error) {
			// nothing to do: storage is not available
		}
	}

	// ------------------------------------------------------------ a country, found by name or code
	// A combobox over wizard_state().countries: type to narrow the list (by name, accents ignored, or by the
	// two-letter code), arrow keys to move, Enter or Tab to choose, Escape to close. on_change(code) is called
	// with the chosen code ("" when the field is cleared).
	dsi_catalogue.CountryPicker = class CountryPicker {
		constructor({ id, countries, placeholder, testid, on_change }) {
			this.countries = Array.isArray(countries) ? countries : [];
			this.on_change = on_change;
			this.code = "";
			this.matches = [];
			this.active = -1;
			this.list_id = `${id}-list`;
			this.input = el("input", {
				id,
				type: "text",
				class: "form-control sp-input",
				role: "combobox",
				"aria-autocomplete": "list",
				"aria-expanded": "false",
				"aria-controls": this.list_id,
				autocomplete: "off",
				spellcheck: "false",
				placeholder,
				"data-testid": testid,
			});
			this.list = el("ul", {
				id: this.list_id,
				class: "sp-combo-list",
				role: "listbox",
				"aria-label": __("Countries"),
				hidden: true,
			});
			this.root = el("div", { class: "sp-combo" }, this.input, this.list);
			this.input.addEventListener("input", () => this.open(this.input.value));
			this.input.addEventListener("click", () => {
				if (this.list.hidden) this.open(this.input.value === this.name_of(this.code) ? "" : this.input.value);
			});
			this.input.addEventListener("keydown", (event) => this.on_key(event));
			this.input.addEventListener("blur", () => this.settle());
			// mousedown, not click: the field keeps the focus, so it does not settle before the choice
			this.list.addEventListener("mousedown", (event) => {
				event.preventDefault();
				const option = event.target.closest("[role='option']");
				if (option) this.choose(Number(option.dataset.index));
			});
		}

		name_of(code) {
			return this.countries.find((country) => country.code === code)?.name || "";
		}

		set(code) {
			this.code = code || "";
			this.input.value = this.name_of(this.code);
		}

		static fold(text) {
			return String(text)
				.normalize("NFD")
				.replace(/\p{M}/gu, "")
				.toLowerCase()
				.trim();
		}

		find(text) {
			const query = CountryPicker.fold(text);
			if (!query) return this.countries;
			const rank = (country) => {
				if (country.code.toLowerCase() === query) return 0;
				return CountryPicker.fold(country.name).startsWith(query) ? 1 : 2;
			};
			return this.countries
				.filter(
					(country) =>
						country.code.toLowerCase() === query || CountryPicker.fold(country.name).includes(query)
				)
				.sort((one, other) => rank(one) - rank(other));
		}

		open(text) {
			this.matches = this.find(text);
			this.active = this.matches.length ? 0 : -1;
			this.list.replaceChildren(
				...this.matches.map((country, index) =>
					el(
						"li",
						{
							id: `${this.list_id}-${index}`,
							class: "sp-combo-option",
							role: "option",
							"aria-selected": "false",
							"data-index": index,
						},
						country.name
					)
				)
			);
			if (!this.matches.length) {
				this.list.append(
					el("li", { class: "sp-combo-none", role: "presentation" }, __("No country has that name."))
				);
			}
			this.list.hidden = false;
			this.input.setAttribute("aria-expanded", "true");
			this.mark();
		}

		close() {
			this.list.hidden = true;
			this.input.setAttribute("aria-expanded", "false");
			this.input.removeAttribute("aria-activedescendant");
		}

		mark() {
			for (const option of this.list.querySelectorAll("[role='option']")) {
				const active = Number(option.dataset.index) === this.active;
				option.setAttribute("aria-selected", String(active));
				if (active) {
					this.input.setAttribute("aria-activedescendant", option.id);
					this.show(option);
				}
			}
			if (this.active < 0) this.input.removeAttribute("aria-activedescendant");
		}

		// Scrolls the list, and only the list, so that the option is in view (scrollIntoView would move the
		// page as well when the list reaches below the window).
		show(option) {
			const { list } = this;
			const top = option.offsetTop;
			const bottom = top + option.offsetHeight;
			if (top < list.scrollTop) list.scrollTop = top;
			else if (bottom > list.scrollTop + list.clientHeight) list.scrollTop = bottom - list.clientHeight;
		}

		choose(index) {
			const country = this.matches[index];
			if (!country) return;
			this.close();
			this.pick(country.code);
		}

		pick(code) {
			const changed = code !== this.code;
			this.set(code);
			if (changed) this.on_change(code);
		}

		on_key(event) {
			const open = !this.list.hidden;
			if (event.key === "ArrowDown" || event.key === "ArrowUp") {
				event.preventDefault();
				if (!open) {
					this.open(this.input.value === this.name_of(this.code) ? "" : this.input.value);
					return;
				}
				const step = event.key === "ArrowDown" ? 1 : -1;
				this.active = Math.min(Math.max(this.active + step, 0), this.matches.length - 1);
				this.mark();
			} else if (event.key === "Enter" && open) {
				event.preventDefault();
				this.choose(this.active);
			} else if (event.key === "Tab" && open && this.active >= 0) {
				this.choose(this.active);
			} else if (event.key === "Escape" && open) {
				event.preventDefault();
				this.close();
				this.input.value = this.name_of(this.code);
			}
		}

		// The field lost the focus: what it says is either a country (by name or code), nothing, or put back.
		settle() {
			this.close();
			const typed = CountryPicker.fold(this.input.value);
			if (!typed) {
				this.pick("");
				return;
			}
			const found = this.countries.find(
				(country) => CountryPicker.fold(country.name) === typed || country.code.toLowerCase() === typed
			);
			if (found) this.pick(found.code);
			else this.input.value = this.name_of(this.code);
		}
	};

	// ------------------------------------------------------------ the tape: Try an order
	// The sample order, as typed, lives in tape.sample; the answer of the last try_order in wizard.priced.
	// The controls are drawn once (build); render_controls() updates what they show and allow, and
	// render_receipt() prints an answer. The carrier's controls (city, order value, weight, volume, the quote,
	// asking the carrier, "the carrier can't quote") are one block, this.carrier_block, so that the rate-card
	// mode can hide them with one change (Task 16): the country and the items stay.
	dsi_catalogue.ShippingPolicyTape = class ShippingPolicyTape {
		constructor(wizard, root) {
			this.wizard = wizard;
			this.root = root;
			this.sample = {
				country: "",
				city: "",
				items: [], // [{item_code, qty (as typed), name}]
				order_value: "",
				weight: "",
				volume: "",
				quote: "",
				unavailable: false,
			};
			this.asked = null; // {key, quotes}: what the carrier quoted when it was last asked
			this.asking = false;
			this.ask_tried = false; // "Ask the carrier" was pressed while it could not be used
			this.packed = { weight: 0, volume: 0, cartons: 0 }; // the cartons the ERP packed the items into
			this.computed_value = null; // the items' value in the price list, from the last answer
			this.sequence = 0; // the number of the newest request: the answer to an older one is dropped
			this.timer = null;
			this.build();
			this.render_controls();
			this.render_receipt({ blocked: this.request().blocked });
		}

		// ---------------------------------------------------------- the controls
		build() {
			const field = (label, id, ...controls) =>
				el("div", { class: "sp-field" }, el("label", { class: "sp-label", for: id }, label), ...controls);
			const number = (id, testid, key, hint_id) => {
				const input = el("input", {
					id,
					type: "text",
					class: "form-control sp-input",
					inputmode: "decimal",
					autocomplete: "off",
					"data-testid": testid,
					"aria-describedby": hint_id,
				});
				input.addEventListener("input", () => {
					this.sample[key] = input.value;
					if (key === "quote") this.asked = null; // a typed quote replaces what the carrier said
					this.changed();
				});
				return input;
			};

			this.status = el("span", { class: "sp-tape-status", "aria-hidden": "true", hidden: true }, __("Pricing…"));
			const head = el(
				"div",
				{ class: "sp-tape-head" },
				el("h2", { class: "sp-tape-title", id: "sp-tape-title" }, __("Try an order")),
				this.status
			);

			this.country = new dsi_catalogue.CountryPicker({
				id: "sp-tape-country",
				countries: this.wizard.state.countries,
				placeholder: __("Choose a country"),
				testid: "tape-country",
				on_change: (code) => {
					this.sample.country = code;
					this.changed();
				},
			});

			const picker = el("div", { class: "sp-item-picker" });
			this.item_control = frappe.ui.form.make_control({
				parent: picker,
				df: {
					fieldtype: "Link",
					options: "Item",
					fieldname: "sp_tape_item",
					placeholder: __("Search items by code or name"),
					only_select: 1,
					change: () => this.add_picked_item(),
				},
				only_input: true,
				render_input: true,
			});
			this.item_control.$input.attr({ id: "sp-tape-item", "data-testid": "tape-add-item" });
			this.items_list = el("ul", {
				class: "sp-items",
				"data-testid": "tape-items",
				"aria-label": __("Items in the order"),
			});

			this.value_hint = el("p", { class: "sp-hint", id: "sp-tape-value-hint" });
			this.weight_hint = el("p", { class: "sp-hint", id: "sp-tape-weight-hint" });
			this.volume_hint = el("p", { class: "sp-hint", id: "sp-tape-volume-hint" });
			this.value_input = number("sp-tape-value", "tape-order-value", "order_value", "sp-tape-value-hint");
			this.weight_input = number("sp-tape-weight", "tape-weight", "weight", "sp-tape-weight-hint");
			this.volume_input = number("sp-tape-volume", "tape-volume", "volume", "sp-tape-volume-hint");
			this.quote_input = number("sp-tape-quote", "tape-quote", "quote", "sp-tape-services");

			this.city_input = el("input", {
				id: "sp-tape-city",
				type: "text",
				class: "form-control sp-input",
				autocomplete: "off",
				"data-testid": "tape-city",
				"aria-describedby": "sp-tape-ask-hint",
			});
			this.city_input.addEventListener("input", () => {
				this.sample.city = this.city_input.value;
				this.changed();
			});
			this.ask_button = el(
				"button",
				{
					type: "button",
					class: "btn btn-default btn-sm sp-ask",
					"data-testid": "tape-ask-carrier",
					"aria-describedby": "sp-tape-ask-hint",
					onclick: () => this.ask(),
				},
				__("Ask the carrier")
			);
			this.ask_hint = el("p", { class: "sp-hint", id: "sp-tape-ask-hint" });
			this.services = el("ul", {
				class: "sp-services",
				id: "sp-tape-services",
				"data-testid": "tape-services",
				"aria-label": __("What the carrier quoted"),
				hidden: true,
			});
			this.unavailable_input = el("input", {
				type: "checkbox",
				id: "sp-tape-unavailable",
				"data-testid": "tape-unavailable",
			});
			this.unavailable_input.addEventListener("change", () => {
				this.sample.unavailable = this.unavailable_input.checked;
				this.changed();
			});

			this.carrier_block = el(
				"div",
				{ class: "sp-tape-carrier", "data-block": "carrier" },
				el(
					"div",
					{ class: "sp-row sp-row-3" },
					field(__("Order value ($)"), "sp-tape-value", this.value_input, this.value_hint),
					field(__("Weight (kg)"), "sp-tape-weight", this.weight_input, this.weight_hint),
					field(__("Volume (litres)"), "sp-tape-volume", this.volume_input, this.volume_hint)
				),
				field(__("Carrier quote ($)"), "sp-tape-quote", this.quote_input, this.services),
				el(
					"div",
					{ class: "sp-row sp-row-ask" },
					field(__("City"), "sp-tape-city", this.city_input),
					this.ask_button
				),
				this.ask_hint,
				el(
					"label",
					{ class: "sp-check", for: "sp-tape-unavailable" },
					this.unavailable_input,
					el("span", {}, __("The carrier can't quote"))
				)
			);

			this.lines = el("div", { class: "sp-receipt-lines", "data-testid": "tape-lines" });
			this.total = el("span", { class: "sp-total-amount", "data-testid": "tape-total" });
			this.after_total = el("p", { class: "sp-after-total", hidden: true });
			this.more = el("div", { class: "sp-more", hidden: true });
			this.receipt = el(
				"div",
				{ class: "sp-receipt" },
				this.lines,
				// the total, with its label, is the polite live region: a new price is read out as one sentence
				el(
					"p",
					{ class: "sp-total", "aria-live": "polite", "aria-atomic": "true" },
					el("span", { class: "sp-total-label" }, __("Customer pays")),
					this.total
				),
				this.after_total,
				this.more
			);

			this.root.append(
				head,
				el(
					"div",
					{ class: "sp-tape-order" },
					field(__("Ship to"), "sp-tape-country", this.country.root),
					field(__("Items"), "sp-tape-item", picker, this.items_list)
				),
				this.carrier_block,
				this.receipt
			);
		}

		// Any change of the sample: forget a carrier answer the change makes stale, redraw the controls, and
		// price the order again shortly.
		changed() {
			if (this.asked && this.asked.key !== this.asked_key()) {
				this.asked = null;
				this.sample.quote = "";
				this.quote_input.value = "";
			}
			this.render_controls();
			this.schedule();
		}

		// The draft changed (the wizard calls this): price the order again shortly.
		draft_changed() {
			this.render_controls();
			this.schedule();
		}

		add_picked_item() {
			const code = this.item_control.get_value();
			if (!code) return;
			this.item_control.set_value("");
			const line = this.sample.items.find((entry) => entry.item_code === code);
			if (line) {
				const qty = sp.read_number(line.qty, { above_zero: true });
				line.qty = String((qty.value || 0) + 1);
			} else {
				const added = { item_code: code, qty: "1", name: "" };
				this.sample.items.push(added);
				this.name_item(added);
			}
			this.render_items();
			this.changed();
		}

		// The item's name, for its line; the code stays until it arrives.
		name_item(line) {
			frappe.db.get_value("Item", line.item_code, "item_name").then(
				(response) => {
					const name = response?.message?.item_name;
					if (!name || !this.sample.items.includes(line)) return;
					line.name = String(name);
					this.render_items();
				},
				() => {}
			);
		}

		render_items() {
			const focused = document.activeElement;
			const keep = focused && this.items_list.contains(focused) ? focused.dataset.testid : null;
			const at = keep ? [...this.items_list.children].findIndex((li) => li.contains(focused)) : -1;
			this.items_list.replaceChildren(
				...this.sample.items.map((line, index) => {
					const label = line.name || line.item_code;
					const qty = el("input", {
						type: "text",
						class: "form-control sp-input sp-qty",
						inputmode: "decimal",
						autocomplete: "off",
						value: line.qty,
						"aria-label": __("Quantity of {0}", [label]),
						"data-testid": "tape-item-qty",
					});
					qty.addEventListener("input", () => {
						line.qty = qty.value;
						this.changed();
					});
					const remove = el(
						"button",
						{
							type: "button",
							class: "sp-link sp-remove",
							"aria-label": __("Remove {0}", [label]),
							"data-testid": "tape-item-remove",
							onclick: () => this.remove_item(index),
						},
						__("Remove")
					);
					return el(
						"li",
						{ class: "sp-item", "data-testid": "tape-item" },
						el("span", { class: "sp-item-name", title: line.item_code }, label),
						qty,
						remove
					);
				})
			);
			// a line drawn again keeps the focus where it was (the name of an item can arrive while typing)
			if (at >= 0 && this.items_list.children[at]) {
				const again = this.items_list.children[at].querySelector(`[data-testid="${keep}"]`);
				if (again) {
					again.focus();
					if (again.tagName === "INPUT") again.setSelectionRange(again.value.length, again.value.length);
				}
			}
		}

		remove_item(index) {
			this.sample.items.splice(index, 1);
			this.render_items();
			const next = this.items_list.children[Math.min(index, this.items_list.children.length - 1)];
			(next?.querySelector(".sp-remove") || this.item_control.$input.get(0)).focus();
			this.changed();
		}

		// What the controls show and allow, from the sample, the last answer and the draft.
		render_controls() {
			const sample = this.sample;
			const typed = (text) => String(text ?? "").trim() !== "";
			const has_items = sample.items.length > 0;
			this.value_hint.textContent =
				has_items && !typed(sample.order_value) && this.computed_value !== null
					? __("Items: {0}", [sp.money(this.computed_value)])
					: "";
			const packed = (value, unit) => {
				if (!has_items) return "";
				return this.packed.cartons
					? __("Packed: {0} {1}", [sp.measure(value), unit])
					: __("Not packed");
			};
			this.weight_hint.textContent = typed(sample.weight) ? "" : packed(this.packed.weight, __("kg"));
			this.volume_hint.textContent = typed(sample.volume) ? "" : packed(this.packed.volume, __("litres"));
			this.quote_input.disabled = sample.unavailable;
			this.render_services();
			this.render_ask();
		}

		render_services() {
			const shown = Boolean(this.asked) && !this.sample.unavailable;
			this.services.hidden = !shown;
			if (!shown) {
				this.services.replaceChildren();
				return;
			}
			const quotes = [...this.asked.quotes].sort((one, other) => one.amount - other.amount);
			this.services.replaceChildren(
				...(quotes.length
					? quotes.map((quote) =>
							el(
								"li",
								{ class: "sp-service" },
								el("span", { class: "sp-service-name" }, String(quote.name || quote.service_id)),
								el("span", { class: "sp-money" }, sp.money(quote.amount))
							)
					  )
					: [el("li", { class: "sp-service" }, __("The carrier did not answer."))])
			);
		}

		// Why the carrier cannot be asked yet, or "" when it can.
		ask_problem() {
			const sample = this.sample;
			if (!this.wizard.state.carrier_available) {
				return __("No carrier service is switched on, so there is no carrier to ask.");
			}
			if (sample.unavailable) return __("Untick “The carrier can't quote” to ask the carrier.");
			if (!sample.country) return __("Choose a country to ask the carrier.");
			if (!sample.items.length) return __("Add the items to ask the carrier.");
			const weight = sp.read_number(sample.weight);
			const carton = weight.value !== null ? weight.value : this.packed.weight;
			if (!(carton > 0)) return __("Type the weight to ask the carrier.");
			if (!sample.city.trim()) return __("Type the city to ask the carrier.");
			return "";
		}

		// The button stays in the tab order while it cannot be used (aria-disabled), and its hint says why. The
		// hint is always its description; it shows once someone turns to the carrier: a city typed, or a press.
		render_ask() {
			const problem = this.asking ? "" : this.ask_problem();
			const blocked = this.asking || Boolean(problem);
			this.ask_button.setAttribute("aria-disabled", String(blocked));
			this.ask_button.classList.toggle("disabled", blocked);
			this.ask_button.textContent = this.asking ? __("Asking the carrier…") : __("Ask the carrier");
			this.ask_hint.textContent = problem;
			const wanted =
				this.ask_tried || this.sample.city.trim() !== "" || !this.wizard.state.carrier_available;
			this.ask_hint.hidden = !problem || !wanted;
		}

		// The key of what the carrier was asked about: its answer is kept only while this stays the same.
		asked_key() {
			const sample = this.sample;
			return JSON.stringify([
				sample.country,
				sample.city.trim(),
				sample.items.map((line) => [line.item_code, line.qty]),
				sample.weight,
				sample.volume,
			]);
		}

		ask() {
			if (this.asking) return;
			if (this.ask_problem()) {
				this.ask_tried = true;
				this.render_ask();
				return;
			}
			this.asked = null;
			this.sample.quote = "";
			this.quote_input.value = "";
			this.asking = true;
			this.render_controls();
			this.price({ ask: true }).finally(() => {
				this.asking = false;
				this.render_controls();
			});
		}

		// ---------------------------------------------------------- pricing
		// Price again 250 ms after the last change. The change itself already outdates any answer on its way
		// (the sequence moves on), so the receipt never shows an answer to a sample that is no longer there.
		schedule() {
			this.sequence += 1;
			clearTimeout(this.timer);
			this.timer = setTimeout(() => this.price(), PRICE_DELAY);
		}

		// What to send to try_order: {args, typed_cartons, typed_value, key}, or {blocked: {empty | invalid}}
		// when there is nothing to price yet or a field does not hold a number.
		request({ ask = false } = {}) {
			const sample = this.sample;
			const block = (reason, text) => ({ blocked: { [reason]: text } });
			if (!sample.country) return block("empty", __("Choose where the order ships to."));
			const items = [];
			for (const line of sample.items) {
				const qty = sp.read_number(line.qty, { above_zero: true });
				if (qty.value === null) {
					return block("invalid", __("Type each quantity as a number above 0, such as 1 or 2."));
				}
				items.push({ item_code: line.item_code, qty: qty.value });
			}
			const draft = this.wizard.store.draft;
			if (this.wizard.price_source() === "rate_card") {
				if (!items.length) {
					return block("empty", __("Add the items in the order. The rate card prices them by weight."));
				}
				return { args: { draft, order: { country_code: sample.country, items } }, key: this.asked_key() };
			}
			const value = sp.read_number(sample.order_value);
			const weight = sp.read_number(sample.weight);
			const volume = sp.read_number(sample.volume);
			if (!value.ok) return block("invalid", __("Type the order value as a number, such as 310 or 310.50."));
			if (!weight.ok) return block("invalid", __("Type the weight in kilograms as a number, such as 1.2."));
			if (!volume.ok) return block("invalid", __("Type the volume in litres as a number, such as 3."));
			const typed_cartons = weight.value !== null || volume.value !== null;
			const units = items.reduce((sum, item) => sum + item.qty, 0);
			if (!typed_cartons && units > MAX_PACKED_UNITS) {
				return block(
					"invalid",
					__("Try an order packs at most {0} units. Lower the quantities, or type the weight and volume.", [
						MAX_PACKED_UNITS,
					])
				);
			}
			// Typed figures make one carton; what is not typed comes from the cartons the ERP packed.
			const boxes = typed_cartons
				? [
						{
							weight_kg: weight.value !== null ? weight.value : this.packed.weight,
							volume_l: volume.value !== null ? volume.value : this.packed.volume,
						},
				  ]
				: null;
			// null: nothing typed; []: the carrier gives no price; a list: the quotes to price with
			let quotes = null;
			if (sample.unavailable) quotes = [];
			else if (ask) quotes = null;
			else if (this.asked) quotes = this.asked.quotes;
			else {
				const quote = sp.read_number(sample.quote);
				if (!quote.ok) return block("invalid", __("Type the carrier's quote as a number, such as 38.20."));
				if (quote.value !== null) {
					quotes = [
						{
							service_id: TYPED_QUOTE,
							name: __("Carrier quote"),
							amount: quote.value,
							boxes_quoted: WHOLE_SHIPMENT,
						},
					];
				}
			}
			if (quotes === null && !ask) {
				return block(
					"empty",
					__("Type the carrier's quote, ask the carrier, or tick “The carrier can't quote”.")
				);
			}
			const order = { country_code: sample.country, items, order_value: value.value, boxes, quotes };
			const args = { draft, order };
			if (ask) {
				args.ask_carrier = 1;
				args.city = sample.city.trim();
			}
			return { args, typed_cartons, typed_value: value.value !== null, key: this.asked_key() };
		}

		// Price the sample with the draft now. Every call gets a number; an answer that is not to the newest
		// call is dropped, so an old answer never overwrites a newer one.
		price({ ask = false } = {}) {
			clearTimeout(this.timer);
			const sequence = ++this.sequence;
			const request = this.request({ ask });
			if (request.blocked) {
				this.set_busy(false);
				this.wizard.set_priced(null);
				this.render_receipt({ blocked: request.blocked });
				return Promise.resolve();
			}
			this.set_busy(true);
			return this.wizard.call("try_order", request.args, { quiet: true }).then(
				(answer) => {
					if (ask) this.keep_asked(request.key, answer);
					if (sequence !== this.sequence) {
						if (ask && this.asked) this.schedule(); // price the newer sample with the carrier's quotes
						return;
					}
					this.set_busy(false);
					this.take(answer, request);
				},
				(problem) => {
					if (sequence !== this.sequence) return;
					this.set_busy(false);
					this.wizard.set_priced(null);
					this.render_receipt({ problem });
				}
			);
		}

		keep_asked(key, answer) {
			if (key !== this.asked_key()) return; // the sample changed while the carrier was asked
			const quotes = Array.isArray(answer?.order?.quotes) ? answer.order.quotes : [];
			this.asked = { key, quotes };
			const cheapest = [...quotes].sort((one, other) => one.amount - other.amount)[0];
			this.sample.quote = cheapest ? cheapest.amount.toFixed(2) : "";
			this.quote_input.value = this.sample.quote;
		}

		take(answer, request) {
			const order = sp.is_record(answer?.order) ? answer.order : {};
			if (!request.typed_cartons && this.wizard.price_source() === "carrier") {
				const boxes = Array.isArray(order.boxes) ? order.boxes : [];
				const sum = (key) => boxes.reduce((total, box) => total + (Number(box?.[key]) || 0), 0);
				this.packed = { weight: sum("weight_kg"), volume: sum("volume_l"), cartons: boxes.length };
			}
			if (!request.typed_value) {
				this.computed_value = sp.is_number(order.order_value) ? order.order_value : null;
			}
			this.render_controls();
			this.wizard.set_priced(answer);
			this.render_receipt({ answer });
		}

		set_busy(busy) {
			this.status.hidden = !busy;
			this.receipt.setAttribute("aria-busy", String(busy));
		}

		// ---------------------------------------------------------- the receipt
		// Prints an answer of try_order, a reason why nothing is priced yet (blocked), or a problem.
		render_receipt({ answer = null, blocked = null, problem = null } = {}) {
			const note = (text, kind) => el("p", { class: `sp-${kind}` }, text);
			this.lines.replaceChildren();
			this.more.replaceChildren();
			this.more.hidden = true;
			this.after_total.hidden = true;
			if (blocked || problem) {
				this.lines.append(
					blocked?.empty
						? note(blocked.empty, "empty")
						: note(blocked?.invalid || __("This order could not be priced: {0}", [problem]), "problem")
				);
				this.set_total("pending");
				return;
			}
			const result = sp.is_record(answer?.result) ? answer.result : {};
			// The tape prices the draft as if it were switched on; whether it is on is read from the draft.
			if (!this.wizard.store.draft.active) {
				this.lines.append(
					note(
						__(
							"The policy is switched off, so customers would pay the carrier's price. Turn it on in the Review step."
						),
						"note"
					)
				);
			}
			const offers = (Array.isArray(result.offers) ? result.offers : [])
				.filter(sp.is_record)
				.sort((one, other) => this.amount_of(one) - this.amount_of(other));
			if (!result.offered || !offers.length) {
				this.lines.append(note(__("Delivery is not offered."), "headline"));
				if (result.message) this.lines.append(note(String(result.message), "message"));
				const why = this.why_not_offered(result);
				if (why) this.lines.append(note(why, "hint"));
				this.set_total("not_offered");
				return;
			}
			const [first, ...others] = offers;
			const said = this.statement(result, first);
			if (said) this.lines.append(note(said, "statement"));
			this.lines.append(...this.offer_lines(first, offers.length > 1));
			this.set_total(this.amount_of(first));
			const days = first.estimated_days;
			if (Number.isInteger(days) && days > 0) {
				this.after_total.textContent = days === 1 ? __("About 1 day") : __("About {0} days", [days]);
				this.after_total.hidden = false;
			}
			if (others.length) {
				this.more.append(el("h3", { class: "sp-more-title" }, __("Other delivery methods")));
				for (const offer of others) {
					const amount = this.amount_of(offer);
					this.more.append(
						el(
							"div",
							{ class: "sp-offer" },
							this.offer_lines(offer, true),
							el(
								"p",
								{ class: "sp-offer-total" },
								el("span", {}, __("Customer pays")),
								el(
									"span",
									{ class: amount === 0 ? "sp-money is-free" : "sp-money" },
									amount === 0 ? __("Free") : sp.money(amount)
								)
							)
						)
					);
				}
				this.more.hidden = false;
			}
		}

		amount_of(offer) {
			return sp.is_number(offer.amount) ? offer.amount : Infinity;
		}

		// The engine's trace of one offer, a line per step: its text, and its amount when it has one. The step
		// that says what the customer pays is the total, printed below, so it is not repeated here. Returns the
		// elements to print: the delivery method's name when it is named, then the list of lines.
		offer_lines(offer, named) {
			const lines = el("ol", { class: "sp-lines" });
			for (const step of Array.isArray(offer.trace) ? offer.trace : []) {
				if (!sp.is_record(step) || step.kind === "result") continue;
				const amount = sp.is_number(step.amount)
					? `${step.kind === "round" ? "+" : ""}${sp.money(step.amount)}`
					: "";
				lines.append(
					el(
						"li",
						{ class: "sp-line", "data-kind": String(step.kind ?? "") },
						el("span", { class: "sp-line-text" }, String(step.text ?? "")),
						amount ? el("span", { class: "sp-line-amount" }, amount) : null
					)
				);
			}
			// A delivery method is named when there are several, or when it is not the quote typed here.
			const name =
				offer.service_id === TYPED_QUOTE && !named ? "" : String(offer.name || offer.service_id || "");
			return name ? [el("p", { class: "sp-offer-name" }, name), lines] : [lines];
		}

		// A plain sentence for how the first offer came about, when the trace alone does not say it.
		statement(result, offer) {
			if (result.reason === "fallback_offer") {
				return __("The carrier could not quote, so the fallback price applies.");
			}
			if (result.reason === "formula_error") {
				return __("A rule's formula could not be worked out for this order, so the fallback price applies.");
			}
			if (offer.source === "carrier") {
				return result.zone === null || result.zone === undefined
					? __("No zone covers this country, so the carrier's price is passed through.")
					: __("No rule fits this order, so the carrier's price is passed through.");
			}
			return "";
		}

		why_not_offered(result) {
			if (result.reason === "not_served" && result.price_source !== "rate_card") {
				return __("No carrier service delivers to this country.");
			}
			if (result.reason === "not_shipped") return __("This country is in a zone that is not shipped to.");
			return "";
		}

		set_total(amount) {
			const total = this.total;
			total.className = "sp-total-amount";
			if (amount === "pending") {
				total.textContent = __("Not priced yet");
				total.classList.add("is-pending");
			} else if (amount === "not_offered") {
				total.textContent = __("Not offered");
				total.classList.add("is-not-offered");
			} else if (amount === 0) {
				total.textContent = __("Free");
				total.classList.add("is-free");
			} else if (sp.is_number(amount)) {
				total.textContent = sp.money(amount);
				total.classList.add("is-paid");
			} else {
				total.textContent = __("Not priced yet");
				total.classList.add("is-pending");
			}
		}
	};

	// ------------------------------------------------------------ the wizard
	function not_built_yet(wizard, panel) {
		panel.append(el("p", { class: "sp-placeholder" }, __("This step is built in the next stage.")));
	}

	dsi_catalogue.ShippingPolicyWizard = class ShippingPolicyWizard {
		constructor(page, wrapper) {
			this.page = page;
			this.wrapper = wrapper;
			this.store = { original: null, draft: null, version: null, step: STEPS[0].id, dirty: false };
			this.state = null;
			this.priced = null;
			this.notice = null; // "restored" or "discarded": what happened to a stored draft on load
			this.save_timer = null;
			this.loading = true;
			this.root = el("div", { class: "sp-wizard" });
			this.page.main.empty().append(this.root);
			this.root.append(el("p", { class: "sp-loading" }, __("Loading the saved policy…")));
			// a change made in the last 300 ms is stored before the page goes away
			window.addEventListener("beforeunload", () => {
				if (this.save_timer) this.save_draft();
			});
			this.load();
		}

		// ---------------------------------------------------------- the server
		// A wizard method, as a promise of its answer. With quiet, a refusal is not shown in Frappe's dialog: the
		// promise is rejected with its sentences as plain text, for the page to show where it belongs.
		call(method, args = {}, { quiet = false } = {}) {
			return new Promise((resolve, reject) => {
				frappe.call({
					method: METHOD + method,
					args,
					silent: quiet,
					callback: (response) => resolve(response.message),
					error: (response) => reject(quiet ? problem_text(response) : null),
				});
			});
		}

		load() {
			return this.call("wizard_state").then(
				(state) => {
					this.loading = false;
					this.take_state(state, { restore: true });
					this.build();
					this.render();
				},
				() => {
					this.loading = false;
					this.root.replaceChildren(
						el(
							"p",
							{ class: "sp-problem" },
							__("The saved policy could not be loaded. Reload the page to try again.")
						)
					);
				}
			);
		}

		take_state(state, { restore = false } = {}) {
			const { policy, version, ...rest } = state;
			this.state = rest;
			this.store.original = sp.clone(policy);
			this.store.draft = sp.clone(policy);
			this.store.version = version;
			if (restore) this.restore_draft();
			this.store.dirty = !sp.same(this.store.draft, this.store.original);
		}

		restore_draft() {
			const stored = read_stored();
			if (!stored) return;
			if (!sp.is_record(stored.draft) || sp.same(stored.draft, this.store.original)) {
				forget_stored(); // unreadable, or nothing in it that is not saved already
				return;
			}
			if (stored.version !== this.store.version) {
				forget_stored();
				this.notice = "discarded";
				return;
			}
			this.store.draft = stored.draft;
			if (STEPS.some((step) => step.id === stored.step)) this.store.step = stored.step;
			this.notice = "restored";
		}

		// The page came back into view: when the draft is clean, take the saved policy again, so that a change
		// made on the form meanwhile is what the wizard shows.
		on_show() {
			if (this.loading || !this.tape || this.store.dirty) return;
			this.call("wizard_state").then(
				(state) => {
					if (this.store.dirty || state.version === this.store.version) return;
					this.take_state(state);
					this.render();
					this.tape.draft_changed();
				},
				() => {}
			);
		}

		// ---------------------------------------------------------- the page
		build() {
			this.banner = el("div", {
				class: "sp-banner",
				role: "status",
				"data-testid": "draft-banner",
				hidden: true,
			});
			this.tabs = el("div", {
				class: "sp-tabs",
				role: "tablist",
				"aria-label": __("Shipping policy steps"),
				onkeydown: (event) => this.on_tab_key(event),
			});
			this.panels = {};
			for (const step of STEPS) {
				this.tabs.append(
					el(
						"button",
						{
							type: "button",
							class: step.apart ? "sp-tab sp-tab-apart" : "sp-tab",
							role: "tab",
							id: `sp-tab-${step.id}`,
							"aria-controls": `sp-panel-${step.id}`,
							"aria-label": step.name || null,
							"data-label": step.label,
							"data-testid": `step-tab-${step.id}`,
							onclick: () => this.set_step(step.id),
						},
						step.label
					)
				);
				this.panels[step.id] = el("div", {
					class: "sp-panel",
					role: "tabpanel",
					id: `sp-panel-${step.id}`,
					"aria-labelledby": `sp-tab-${step.id}`,
					tabindex: "0",
					hidden: true,
				});
			}
			const tape = el("section", { class: "sp-tape", "aria-labelledby": "sp-tape-title" });
			this.root.replaceChildren(
				this.banner,
				this.tabs,
				el("div", { class: "sp-step" }, Object.values(this.panels)),
				tape
			);
			this.tape = new dsi_catalogue.ShippingPolicyTape(this, tape);
		}

		render() {
			this.render_banner();
			this.render_tabs();
			this.render_step();
		}

		// What the banner says, a line each: {text, action: {label, testid, run}}. Task 9 adds its line here.
		banner_lines() {
			const lines = [];
			if (this.notice === "restored") {
				lines.push({
					text: __("Restored your unsaved draft. Nothing is live until you apply it."),
					action: {
						label: __("Discard the draft"),
						testid: "draft-discard",
						run: () => this.confirm_discard(),
					},
				});
			} else if (this.notice === "discarded") {
				lines.push({
					text: __("Someone changed the policy after you started, so your old draft was discarded."),
					action: { label: __("Dismiss"), testid: "draft-dismiss", run: () => this.dismiss_notice() },
				});
			}
			return lines;
		}

		render_banner() {
			const lines = this.banner_lines();
			this.banner.replaceChildren(
				...lines.map((line) =>
					el(
						"p",
						{ class: "sp-banner-line" },
						el("span", {}, line.text),
						line.action
							? el(
									"button",
									{
										type: "button",
										class: "sp-link",
										"data-testid": line.action.testid,
										onclick: line.action.run,
									},
									line.action.label
							  )
							: null
					)
				)
			);
			this.banner.hidden = !lines.length;
		}

		render_tabs() {
			for (const tab of this.tabs.children) {
				const selected = tab.id === `sp-tab-${this.store.step}`;
				tab.setAttribute("aria-selected", String(selected));
				tab.tabIndex = selected ? 0 : -1;
			}
		}

		render_step() {
			for (const step of STEPS) this.panels[step.id].hidden = step.id !== this.store.step;
			const step = this.current_step();
			const panel = this.panels[step.id];
			panel.replaceChildren();
			step.render(this, panel);
		}

		current_step() {
			return STEPS.find((step) => step.id === this.store.step) || STEPS[0];
		}

		set_step(id, { focus = false } = {}) {
			if (!STEPS.some((step) => step.id === id)) return;
			if (id !== this.store.step) {
				this.store.step = id;
				this.render_tabs();
				this.render_step();
				const step = this.current_step();
				step.priced?.(this, this.panels[step.id], this.priced);
				if (this.store.dirty) this.save_draft_soon();
			}
			if (focus) document.getElementById(`sp-tab-${id}`)?.focus();
		}

		// The tab row as a tablist: the arrow keys move between tabs (and select them), Home and End jump.
		on_tab_key(event) {
			const ids = STEPS.map((step) => step.id);
			const at = ids.indexOf(this.store.step);
			const back = getComputedStyle(this.tabs).direction === "rtl" ? "ArrowRight" : "ArrowLeft";
			const ahead = back === "ArrowLeft" ? "ArrowRight" : "ArrowLeft";
			const to = { [ahead]: at + 1, [back]: at - 1, Home: 0, End: ids.length - 1 }[event.key];
			if (to === undefined) return;
			event.preventDefault();
			this.set_step(ids[(to + ids.length) % ids.length], { focus: true });
		}

		// ---------------------------------------------------------- the draft
		change(mutate) {
			mutate(this.store.draft);
			this.after_change();
		}

		after_change() {
			this.store.dirty = !sp.same(this.store.draft, this.store.original);
			if (!this.store.dirty && this.notice === "restored") this.notice = null;
			this.save_draft_soon();
			this.render_banner();
			this.render_tabs();
			const step = this.current_step();
			step.refresh?.(this, this.panels[step.id]);
			this.tape.draft_changed();
		}

		price_source() {
			return this.store.draft?.price_source === "rate_card" ? "rate_card" : "carrier";
		}

		set_priced(answer) {
			this.priced = answer;
			const step = this.current_step();
			step.priced?.(this, this.panels[step.id], answer);
		}

		save_draft_soon() {
			clearTimeout(this.save_timer);
			this.save_timer = setTimeout(() => this.save_draft(), SAVE_DELAY);
		}

		save_draft() {
			clearTimeout(this.save_timer);
			this.save_timer = null;
			if (!this.store.draft) return;
			try {
				if (this.store.dirty) {
					const { version, draft, step } = this.store;
					window.localStorage.setItem(DRAFT_KEY, JSON.stringify({ version, draft, step }));
				} else {
					window.localStorage.removeItem(DRAFT_KEY);
				}
			} catch (error) {
				// storage is full or switched off: the draft then lives in this tab only
			}
		}

		confirm_discard() {
			frappe.confirm(__("Discard your unsaved draft and start again from the saved policy?"), () =>
				this.discard_draft()
			);
		}

		discard_draft() {
			this.store.draft = sp.clone(this.store.original);
			this.notice = null;
			this.render_step();
			this.after_change();
		}

		dismiss_notice() {
			this.notice = null;
			this.render_banner();
		}
	};

	// The tabs, in order. Test is not a step: it checks what is live, so it sits apart at the end of the row.
	const STEPS = (dsi_catalogue.ShippingPolicyWizard.STEPS = [
		{ id: "zones", label: __("Zones"), render: not_built_yet },
		{ id: "rates", label: __("Rates"), render: not_built_yet },
		{ id: "rules", label: __("Rules"), render: not_built_yet },
		{ id: "fallback", label: __("If the carrier can't quote"), render: not_built_yet },
		{ id: "review", label: __("Review"), render: not_built_yet },
		{ id: "test", label: __("Test"), name: __("Test the integration"), apart: true, render: not_built_yet },
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
