// Part of the Shipping policy wizard page: shipping_policy_wizard.js stitches it in and says how.
//
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
		this.quote_epoch = 0; // moves on when a quote is typed: an answer to an earlier Ask is then dropped
		this.ask_tried = false; // "Ask the carrier" was pressed while it could not be used
		// the cartons the ERP packed the items into, and the items they were packed for (JSON of the order's items)
		this.packed = { weight: 0, volume: 0, cartons: 0, key: null };
		this.computed_value = null; // the items' value in the price list, from the last answer
		this.sequence = 0; // the number of the newest request: the answer to an older one is dropped
		this.timer = null;
		this.build();
		this.render_controls();
		this.render_receipt({ blocked: this.request().blocked });
		this.keep_in_view();
	}

	// Beside the steps the tape is never taller than the window below where it starts, and its total is pinned
	// at its foot (the style does both). The style knows the window's height but not where the tape starts
	// (under the page head, the tabs and, at times, the draft banner; higher once the page scrolls), so the
	// room is measured here, as --sp-tape-room, whenever the page scrolls or something above the tape changes.
	// It stops 88 px above the window's foot: the desk's floating buttons sit in that corner, over the total.
	keep_in_view() {
		let frame = null;
		const measure = () => {
			frame = null;
			if (!this.root.isConnected || !this.root.offsetParent) return; // the page is not shown
			const top = Math.max(this.root.getBoundingClientRect().top, 0);
			this.root.style.setProperty("--sp-tape-room", `${Math.max(window.innerHeight - top - 88, 240)}px`);
		};
		const soon = () => {
			frame = frame || requestAnimationFrame(measure);
		};
		window.addEventListener("scroll", soon, { passive: true });
		window.addEventListener("resize", soon);
		new ResizeObserver(soon).observe(this.root.parentElement || this.root);
		soon();
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
				if (key === "quote") {
					// a typed quote replaces what the carrier said, and wins over an answer still on its way
					this.asked = null;
					this.quote_epoch += 1;
				}
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
			countries: () => this.wizard.state.countries,
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

		// The receipt: the lines under a tear line, then the total, then what follows it. They are children of
		// the tape itself (not of one receipt box), so that the total can stay pinned at the tape's foot wherever
		// the tape is scrolled to (a pinned element cannot leave the box it is in).
		this.lines = el("div", { class: "sp-receipt-lines", "data-testid": "tape-lines" });
		this.receipt = el("div", { class: "sp-receipt sp-printed" }, this.lines);
		this.total_label = el("span", { class: "sp-total-label", "data-testid": "tape-total-label" });
		this.total = el("span", { class: "sp-total-amount", "data-testid": "tape-total" });
		// the total, with its label, is the polite live region: a new price is read out as one sentence
		this.total_row = el(
			"p",
			{ class: "sp-total sp-printed", "aria-live": "polite", "aria-atomic": "true" },
			this.total_label,
			this.total
		);
		this.after_total = el("p", { class: "sp-after-total sp-printed", hidden: true });
		this.more = el("div", { class: "sp-more sp-printed", hidden: true });

		this.root.append(
			head,
			el(
				"div",
				{ class: "sp-tape-order" },
				field(__("Ship to"), "sp-tape-country", this.country.root),
				field(__("Items"), "sp-tape-item", picker, this.items_list)
			),
			this.carrier_block,
			this.receipt,
			this.total_row,
			this.after_total,
			this.more
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

	// What to send to try_order: {args, typed_cartons, typed_value, items_key, key, epoch}; or {packing,
	// items_key} when the ERP must first pack the items (one measure typed, the items changed); or {blocked:
	// {empty | invalid}} when there is nothing to price yet or a field does not hold a number.
	request({ ask = false } = {}) {
		const sample = this.sample;
		const block = (reason, text) => ({ blocked: { [reason]: text } });
		// a field that holds no number: a comma in the wrong place gets its own plain sentence
		const refuse = (reading, text) => block("invalid", reading.comma ? __("Use a period for decimals.") : text);
		if (!sample.country) return block("empty", __("Choose where the order ships to."));
		const items = [];
		for (const line of sample.items) {
			const qty = sp.read_number(line.qty, { above_zero: true });
			if (qty.value === null) {
				return refuse(qty, __("Type each quantity as a number above 0, such as 1 or 2."));
			}
			items.push({ item_code: line.item_code, qty: qty.value });
		}
		const draft = this.wizard.store.draft;
		if (this.wizard.price_source() === "rate_card") {
			if (!items.length) {
				return block("empty", __("Add the items in the order. The rate card prices them by weight."));
			}
			const args = { draft, order: { country_code: sample.country, items } };
			return { args, key: this.asked_key(), epoch: this.quote_epoch };
		}
		const value = sp.read_number(sample.order_value);
		const weight = sp.read_number(sample.weight);
		const volume = sp.read_number(sample.volume);
		if (!value.ok) return refuse(value, __("Type the order value as a number, such as 310 or 310.50."));
		if (!weight.ok) return refuse(weight, __("Type the weight in kilograms as a number, such as 1.2."));
		if (!volume.ok) return refuse(volume, __("Type the volume in litres as a number, such as 3."));
		const typed_cartons = weight.value !== null || volume.value !== null;
		const both_typed = weight.value !== null && volume.value !== null;
		const units = items.reduce((sum, item) => sum + item.qty, 0);
		if (!both_typed && units > MAX_PACKED_UNITS) {
			return block(
				"invalid",
				__("Try an order packs at most {0} units. Lower the quantities, or type the weight and volume.", [
					MAX_PACKED_UNITS,
				])
			);
		}
		// null: nothing typed; []: the carrier gives no price; a list: the quotes to price with
		let quotes = null;
		if (sample.unavailable) quotes = [];
		else if (ask) quotes = null;
		else if (this.asked) quotes = this.asked.quotes;
		else {
			const quote = sp.read_number(sample.quote);
			if (!quote.ok) return refuse(quote, __("Type the carrier's quote as a number, such as 38.20."));
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
		// One measure is typed and the items changed since the ERP last packed them: first ask how it packs
		// them (no quote, so the carrier is not asked), so that the measure not typed is the packed one and
		// never one packed for other items. price() then asks again with the fresh figure.
		const items_key = JSON.stringify(items);
		if (typed_cartons && !both_typed && items.length && this.packed.key !== items_key) {
			const order = { country_code: sample.country, items, order_value: value.value, boxes: null, quotes: [] };
			return { packing: { draft, order }, items_key };
		}
		// Typed figures make one carton; a figure not typed is the packed one (no items, no cartons).
		const packed = items.length ? this.packed : { weight: 0, volume: 0 };
		const boxes = typed_cartons
			? [{ weight_kg: weight.value ?? packed.weight, volume_l: volume.value ?? packed.volume }]
			: null;
		const order = { country_code: sample.country, items, order_value: value.value, boxes, quotes };
		const args = { draft, order };
		if (ask) {
			args.ask_carrier = 1;
			args.city = sample.city.trim();
		}
		const typed_value = value.value !== null;
		return { args, typed_cartons, typed_value, items_key, key: this.asked_key(), epoch: this.quote_epoch };
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
		if (request.packing) {
			return this.wizard.call("try_order", request.packing, { quiet: true }).then(
				(answer) => {
					if (sequence !== this.sequence) return;
					this.keep_packed(answer?.order, request.items_key);
					this.render_controls();
					return this.price({ ask });
				},
				(problem) => {
					if (sequence !== this.sequence) return;
					this.set_busy(false);
					this.wizard.set_priced(null);
					this.render_receipt({ problem });
				}
			);
		}
		return this.wizard.call("try_order", request.args, { quiet: true }).then(
			(answer) => {
				if (ask) this.keep_asked(request, answer);
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

	// The carrier's answer becomes the quote, unless the sample changed or a quote was typed while it was asked.
	keep_asked(request, answer) {
		if (request.key !== this.asked_key() || request.epoch !== this.quote_epoch) return;
		const quotes = Array.isArray(answer?.order?.quotes) ? answer.order.quotes : [];
		this.asked = { key: request.key, quotes };
		const cheapest = [...quotes].sort((one, other) => one.amount - other.amount)[0];
		this.sample.quote = cheapest ? cheapest.amount.toFixed(2) : "";
		this.quote_input.value = this.sample.quote;
	}

	// The cartons the ERP packed the items into (the order as try_order priced it, with no carton typed),
	// remembered with the items they were packed for.
	keep_packed(order, items_key) {
		const boxes = Array.isArray(order?.boxes) ? order.boxes : [];
		const sum = (key) => boxes.reduce((total, box) => total + (Number(box?.[key]) || 0), 0);
		this.packed = { weight: sum("weight_kg"), volume: sum("volume_l"), cartons: boxes.length, key: items_key };
	}

	take(answer, request) {
		const order = sp.is_record(answer?.order) ? answer.order : {};
		if (!request.typed_cartons && this.wizard.price_source() === "carrier") {
			this.keep_packed(order, request.items_key);
		}
		if (!request.typed_value) {
			this.computed_value = sp.is_number(order.order_value) ? order.order_value : null;
		}
		this.render_controls();
		this.wizard.set_priced(answer);
		this.render_receipt({ answer });
	}

	// While a price is on its way the printed part dims, and the total waits to be read out until it is there.
	set_busy(busy) {
		this.status.hidden = !busy;
		this.root.classList.toggle("is-pricing", busy);
		this.receipt.setAttribute("aria-busy", String(busy));
		this.total_row.setAttribute("aria-busy", String(busy));
	}

	// ---------------------------------------------------------- the receipt
	// Prints an answer of try_order, a reason why nothing is priced yet (blocked), or a problem. The tape prices
	// the draft as if it were switched on, so that it can be tried before it goes live; whether it is on is
	// read from the draft. While it is off, the receipt says so first, and its total is what customers would
	// pay once it is on.
	render_receipt({ answer = null, blocked = null, problem = null } = {}) {
		const note = (text, kind) => el("p", { class: `sp-${kind}` }, text);
		const off = !this.wizard.store.draft.active;
		this.total_label.textContent = off ? __("Customer would pay") : __("Customer pays");
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
		if (off) {
			const today = __(
				"The policy is switched off, so today customers pay the carrier's price. This receipt shows what they would pay once you turn the policy on in the Review step."
			);
			this.lines.append(note(today, "note"));
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
		// The workings first, then how they came about, just above the total: the receipt reads top down.
		const [first, ...others] = offers;
		this.lines.append(...this.offer_lines(first, offers.length > 1));
		const said = this.statement(result, first);
		if (said) this.lines.append(note(said, "statement"));
		this.set_total(this.amount_of(first));
		const days = first.estimated_days;
		if (Number.isInteger(days) && days > 0) {
			this.after_total.textContent = days === 1 ? __("About 1 day") : __("About {0} days", [days]);
			this.after_total.hidden = false;
		}
		// The other delivery methods, a quiet row each (its name, what it costs), so that the total above stays
		// the one loud figure.
		if (others.length) {
			const row = (offer) => {
				const amount = offer.amount;
				const price = amount === 0 ? __("Free") : sp.is_number(amount) ? sp.money(amount) : "";
				return el(
					"li",
					{ class: "sp-more-row" },
					el("span", { class: "sp-more-name" }, String(offer.name || offer.service_id || "")),
					el("span", { class: amount === 0 ? "sp-money is-free" : "sp-money" }, price)
				);
			};
			this.more.append(
				el("h3", { class: "sp-more-title" }, __("Other delivery methods")),
				el("ul", { class: "sp-more-list", "data-testid": "tape-more" }, others.map(row))
			);
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
