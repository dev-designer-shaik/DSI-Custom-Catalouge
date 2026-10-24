// Part of the Shipping policy wizard page: shipping_policy_wizard.js stitches it in and says how.
//
// One rule of the Rules step (step_rules.js) as a card: its head (its place, its name, Use this rule, the up and
// down buttons, Delete), its sentence of chips and what it charges the tape's order, its "Only when" row, what
// is wrong with it, and the editor its open chip shows under the chip's line (the zone, the items with the
// item set editor, the charge with the formula and Round up, the six limits). The card keeps its rule object
// while the rule is in the draft, so a moved rule takes its card, and its focus, along.

let rule_cards = 0; // ids for the labels and editors of each card

class RuleCard {
	constructor(view, rule) {
		rule_cards += 1;
		const uid = (this.uid = `sp-rule-${rule_cards}`);
		this.view = view;
		this.wizard = view.wizard;
		this.rule = rule;
		this.index = 0;
		this.chip = null; // the chip whose editor is open on this card
		this.kept = {}; // limit key -> the number kept when it differs from the one typed, until it is typed again
		this.sync_editor = () => {};
		const words = (text) => el("span", { class: "sp-words" }, text);

		// the sentence: its chips open their editors
		const chip = (name, testid) =>
			el("button", {
				type: "button",
				class: "sp-chip",
				"aria-expanded": "false",
				"aria-controls": `${uid}-editor`,
				"data-testid": testid,
				onclick: () => view.toggle(this, name),
				onkeydown: (event) => {
					if (event.key === "Escape" && this.chip) {
						// the desk's own Escape (on the window) would take the focus away from the chip
						event.preventDefault();
						event.stopPropagation();
						// Escape closes the innermost thing: a set being made is cancelled first
						if (view.ui.set_editor?.rule === this.rule) this.set_done(null);
						else view.close();
					}
				},
			});
		this.chips = {
			zone: chip("zone", "chip-zone"),
			items: chip("items", "chip-items"),
			charge: chip("charge", "chip-charge"),
			limits: chip("limits", "chip-limits"),
		};
		this.chips.items.setAttribute("aria-describedby", "sp-rules-note");
		this.rounded = words(__(", rounded up"));
		this.sentence = el(
			"p",
			{ class: "sp-sentence", "data-testid": "rule-sentence" },
			words(__("In")),
			" ",
			this.chips.zone,
			" ",
			words(__("for")),
			" ",
			this.chips.items,
			" ",
			words(__("charge")),
			" ",
			this.chips.charge,
			this.rounded
		);
		this.price = el("p", { class: "sp-rule-price", "data-testid": "rule-price" });
		this.only_when = el(
			"p",
			{ class: "sp-only-when", "data-testid": "rule-only-when" },
			words(__("Only when")),
			" ",
			this.chips.limits
		);
		this.problems = el("ul", { class: "sp-problems", "data-testid": "rule-problems" });
		this.editor = el("div", { class: "sp-editor", id: `${uid}-editor`, "data-testid": "rule-editor", hidden: true });
		// Escape is seen before a list in a field closes itself: a field whose list is open keeps it. Whatever
		// it closed, Escape goes no further than the editor: the desk's own Escape (on the window) would take
		// the focus away from where it is.
		this.editor.addEventListener("keydown", (event) => this.on_editor_key(event), true);
		this.editor.addEventListener("keydown", (event) => {
			if (event.key === "Escape") event.stopPropagation();
		});

		// the head: its place, its name, on or off, move, delete
		this.grip = el("span", {
			class: "sp-grip",
			"aria-hidden": "true",
			title: __("Drag to move this rule"),
			"data-testid": "rule-grip",
			onpointerdown: (event) => view.drag(this, event),
		});
		this.number = el("span", { class: "sp-rule-number", "data-testid": "rule-number" });
		this.label = el("input", {
			type: "text",
			class: "sp-title-input sp-rule-label",
			maxlength: NAME_LENGTH,
			autocomplete: "off",
			placeholder: __("Name it, for your own reference"),
			"aria-label": __("Name it, for your own reference"),
			"data-testid": "rule-label",
		});
		this.label.addEventListener("input", () =>
			this.wizard.change(() => {
				rule.label = this.label.value;
			})
		);
		this.state = el("span", { class: "sp-state", "data-testid": "rule-state" });
		this.enabled = el("input", { type: "checkbox", id: `${uid}-enabled`, "data-testid": "rule-disable" });
		this.enabled.addEventListener("change", () =>
			this.wizard.change(() => {
				rule.enabled = this.enabled.checked;
			})
		);
		const arrow = (testid, glyph, step) =>
			el("button", { type: "button", class: "sp-icon-button", "data-testid": testid, onclick: () => this.step(step) }, glyph);
		this.up = arrow("rule-up", "↑", -1);
		this.down = arrow("rule-down", "↓", 1);
		this.delete = el(
			"button",
			{ type: "button", class: "sp-link sp-quiet", "data-testid": "rule-delete", onclick: () => view.delete_rule(this) },
			__("Delete")
		);

		this.root = el(
			"li",
			{ class: "sp-rule", "data-testid": "rule-card" },
			el(
				"div",
				{ class: "sp-card-head" },
				el("div", { class: "sp-card-title" }, this.grip, this.number, this.label, this.state),
				el(
					"div",
					{ class: "sp-card-actions" },
					el("label", { class: "sp-check", for: `${uid}-enabled` }, this.enabled, el("span", {}, __("Use this rule"))),
					el("span", { class: "sp-moves" }, this.up, this.down),
					this.delete
				)
			),
			el("div", { class: "sp-rule-line" }, this.sentence, this.price),
			this.only_when,
			this.problems
		);
	}

	step(by) {
		const to = this.index + by;
		if (to < 0 || to >= this.view.rules.length) return; // at the top or the bottom already
		this.view.move(this, to);
	}

	// What the card says, from its rule and the rest of the draft. Fields being typed into are left alone.
	update(index, total) {
		const rule = this.rule;
		this.index = index;
		const place = index + 1;
		this.number.textContent = __("Rule {0}", [place]);
		const label = String(rule.label ?? "");
		if (document.activeElement !== this.label && this.label.value !== label) this.label.value = label;
		this.root.setAttribute("aria-label", label.trim() ? __("Rule {0}: {1}", [place, label]) : __("Rule {0}", [place]));
		this.enabled.checked = Boolean(rule.enabled);
		this.state.textContent = rule.enabled ? "" : __("Off");
		this.root.classList.toggle("is-off", !rule.enabled);
		this.up.setAttribute("aria-disabled", String(index === 0));
		this.down.setAttribute("aria-disabled", String(index >= total - 1));
		this.up.setAttribute("aria-label", __("Move rule {0} up", [place]));
		this.down.setAttribute("aria-label", __("Move rule {0} down", [place]));
		this.delete.setAttribute("aria-label", __("Delete rule {0}", [place]));

		// the sentence's words are sp.rule_words's (wizard_rule_words.js), which the Review step prints too
		const words = sp.rule_words(rule);
		this.chips.zone.textContent = words.zone;
		this.chips.zone.setAttribute("aria-label", __("Zone: {0}", [words.zone]));
		this.chips.items.replaceChildren(words.items, el("span", { class: "sp-asterisk", "aria-hidden": "true" }, "*"));
		this.chips.items.setAttribute("aria-label", __("Items: {0}", [words.items]));
		this.chips.charge.textContent = words.charge;
		this.chips.charge.setAttribute(
			"aria-label",
			words.rounded ? __("Charge: {0}, rounded up", [words.charge]) : __("Charge: {0}", [words.charge])
		);
		this.rounded.hidden = !words.rounded;
		const limits = words.limits || __("no limits");
		this.chips.limits.textContent = limits;
		this.chips.limits.setAttribute("aria-label", __("Only when: {0}", [limits]));

		this.problems.replaceChildren(...this.view.problems_of(this).map((text) => el("li", {}, text)));
		if (this.chip) this.sync_editor();
	}

	// What the rule charges the order in the tape, from the latest answer (`answer.result.rule` is the rule
	// that priced it; an earlier rule that holds pre-empts a later one, whose price is then shown muted).
	show_price(answer) {
		this.price.classList.remove("is-stale");
		const previews = Array.isArray(answer?.previews) ? answer.previews : null;
		const preview = previews && sp.is_record(previews[this.index]) ? previews[this.index] : null;
		if (!preview) {
			this.price.replaceChildren();
			return;
		}
		const result = sp.is_record(answer.result) ? answer.result : {};
		let amount = null;
		let reason = "";
		let paid = false;
		if (!this.rule.enabled) reason = __("Off");
		else if (!preview.in_zone) reason = __("Not for this country");
		else if (!preview.matches) reason = __("Conditions not met");
		else if (!sp.is_number(preview.amount)) reason = __("No price");
		else {
			amount = preview.amount;
			paid = result.rule === this.index;
			if (Number.isInteger(result.rule) && result.rule < this.index) {
				reason = __("Rule {0} comes first", [result.rule + 1]);
			}
		}
		let shown = "–";
		let kind = "is-none";
		if (amount !== null) {
			shown = amount === 0 ? __("Free") : sp.money(amount);
			kind = !paid ? "is-passed" : amount === 0 ? "is-free" : "is-paid";
		}
		const amount_line = el("span", { class: `sp-rule-amount ${kind}` }, shown);
		this.price.replaceChildren(
			...(reason ? [amount_line, el("span", { class: "sp-rule-reason" }, reason)] : [amount_line])
		);
	}

	// ---------------------------------------------------------- the editor under the line of its chip
	open_editor(chip, { focus = true } = {}) {
		this.chip = chip;
		for (const [name, element] of Object.entries(this.chips)) {
			element.setAttribute("aria-expanded", String(name === chip));
		}
		this.root.insertBefore(this.editor, chip === "limits" ? this.problems : this.only_when);
		this.editor.hidden = false;
		this.root.classList.add("is-editing");
		this.build_editor();
		this.update(this.index, this.view.rules.length);
		if (focus) this.focus_editor();
	}

	close_editor() {
		this.chip = null;
		this.sync_editor = () => {};
		this.editor.hidden = true;
		this.editor.replaceChildren();
		this.root.classList.remove("is-editing");
		for (const element of Object.values(this.chips)) element.setAttribute("aria-expanded", "false");
		if (this.view.ui.set_editor?.rule === this.rule) this.view.ui.set_editor = null;
		this.view.ui.typed.delete(this.rule); // limits typed that were not numbers were never in the draft
		this.kept = {};
		this.update(this.index, this.view.rules.length);
	}

	build_editor() {
		const build = {
			zone: () => this.zone_editor(),
			items: () => this.items_editor(),
			charge: () => this.charge_editor(),
			limits: () => this.limits_editor(),
		}[this.chip];
		this.sync_editor = () => {};
		this.editor.replaceChildren(...build());
		this.sync_editor();
	}

	focus_editor() {
		const target =
			this.editor.querySelector("input[type='radio']:checked") ||
			this.editor.querySelector("input:not([disabled]), button");
		target?.focus();
	}

	on_editor_key(event) {
		if (event.key !== "Escape" || event.target.getAttribute?.("aria-expanded") === "true") return;
		event.preventDefault();
		event.stopPropagation();
		if (this.view.ui.set_editor?.rule === this.rule) this.set_done(null);
		else this.view.close();
	}

	actions(...more) {
		return el(
			"div",
			{ class: "sp-editor-actions" },
			...more,
			el(
				"button",
				{ type: "button", class: "btn btn-default btn-xs", "data-testid": "rule-done", onclick: () => this.view.close() },
				__("Done")
			)
		);
	}

	// A choice of radio buttons: [{value, text, note, after}]; choose(value) is called when one is chosen.
	choices(question, testid, options, choose) {
		const group = `${this.uid}-${testid}`;
		return el(
			"fieldset",
			{ class: "sp-choices" },
			el("legend", { class: "sp-editor-title" }, question),
			options.map((option) => {
				const input = el("input", { type: "radio", name: group, value: option.value, "data-testid": testid });
				input.disabled = Boolean(option.disabled);
				input.addEventListener("change", () => {
					if (input.checked) choose(option.value);
				});
				return el(
					"div",
					{ class: "sp-option-row" },
					el(
						"label",
						{ class: "sp-option" },
						input,
						el("span", { class: "sp-option-text" }, option.text),
						option.note ? el("span", { class: "sp-option-note" }, option.note) : null
					),
					...(option.after || [])
				);
			})
		);
	}

	check_choices(testid, value) {
		for (const input of this.editor.querySelectorAll(`[data-testid="${testid}"]`)) input.checked = input.value === value;
	}

	zone_editor() {
		const rule = this.rule;
		const zones = this.view.zones;
		this.sync_editor = () => this.check_choices("rule-zone-option", String(rule.zone ?? ""));
		if (!zones.length) {
			return [el("p", { class: "sp-hint" }, __("There are no zones yet. Add them in the Zones step.")), this.actions()];
		}
		const options = zones.map((zone) => {
			const name = String(zone.name ?? "");
			const notes = [!zone.enabled && __("Off"), zone.not_shipped && __("Not shipped"), zone.everywhere_else && __("Everywhere else")];
			return {
				value: name,
				text: name || __("A zone without a name"),
				note: notes.filter(Boolean).join(", "),
				disabled: !name.trim(),
			};
		});
		const choose = (name) =>
			this.wizard.change(() => {
				rule.zone = name;
			});
		return [
			this.choices(__("Which zone is this rule for?"), "rule-zone-option", options, choose),
			el("p", { class: "sp-hint" }, __("Zones and their countries are set in the Zones step.")),
			this.actions(),
		];
	}

	items_editor() {
		const state = this.view.ui.set_editor;
		if (state && state.rule === this.rule) {
			const editor = new dsi_catalogue.ItemSetEditor({ wizard: this.wizard, state, done: (name) => this.set_done(name) });
			return [editor.root];
		}
		const rule = this.rule;
		const link = (testid, text, label, run) =>
			el("button", { type: "button", class: "sp-link sp-quiet", "aria-label": label, "data-testid": testid, onclick: run }, text);
		const options = [{ value: "", text: __("Every item") }];
		for (const set of this.view.item_sets) {
			const name = String(set.name ?? "");
			options.push({
				value: name,
				text: name || __("A set without a name"),
				note: sp.set_summary(set),
				after: [
					link("itemset-edit", __("Edit"), __("Edit the item set {0}", [name]), () => this.edit_set(set)),
					link("itemset-delete", __("Delete"), __("Delete the item set {0}", [name]), () => this.delete_set(set)),
				],
			});
		}
		this.items_problem = el("p", { class: "sp-problem", "aria-live": "polite", "data-testid": "rule-items-problem" });
		this.sync_editor = () => this.check_choices("rule-items-option", String(rule.item_set ?? ""));
		const choose = (name) =>
			this.wizard.change(() => {
				rule.item_set = name || null;
			});
		const make = el(
			"button",
			{ type: "button", class: "btn btn-default btn-xs", "data-testid": "itemset-new", onclick: () => this.edit_set(null) },
			__("New item set")
		);
		return [
			this.choices(__("Which items is this rule for?"), "rule-items-option", options, choose),
			this.items_problem,
			this.actions(make),
		];
	}

	// Opens the item set editor in place of the list of sets: `set` is the draft's set, or null for a new one.
	edit_set(set) {
		this.view.ui.set_editor = {
			rule: this.rule,
			set,
			name: set ? String(set.name ?? "") : "",
			item_groups: set && Array.isArray(set.item_groups) ? set.item_groups.map(String) : [],
			items: set && Array.isArray(set.items) ? set.items.map(String) : [],
			problem: "",
		};
		this.build_editor();
		this.editor.querySelector('[data-testid="itemset-name"]')?.focus();
	}

	// Back from the item set editor to the list of sets: the saved set (`name`) has the focus, or New item set.
	set_done(name) {
		this.view.ui.set_editor = null;
		this.build_editor();
		const saved = [...this.editor.querySelectorAll('[data-testid="rule-items-option"]')].find(
			(input) => name !== null && input.value === name
		);
		(saved || this.editor.querySelector('[data-testid="itemset-new"]'))?.focus();
	}

	// A set that rules use is not deleted: they would name a set that is not there.
	delete_set(set) {
		const name = String(set.name ?? "");
		const users = this.view.rules.filter((rule) => sp.is_record(rule) && rule.item_set && rule.item_set === set.name);
		if (users.length) {
			this.items_problem.textContent =
				users.length === 1
					? __("1 rule uses this set. Change that rule first.")
					: __("{0} rules use this set. Change those rules first.", [users.length]);
			return;
		}
		this.wizard.change((draft) => {
			const at = draft.item_sets.indexOf(set);
			if (at >= 0) draft.item_sets.splice(at, 1);
		});
		this.build_editor();
		this.editor.querySelector('[data-testid="itemset-new"]')?.focus();
		this.view.live.say(__("Item set deleted: {0}.", [name]));
	}

	charge_editor() {
		const rule = this.rule;
		const id = `${this.uid}-formula`;
		this.formula_input = el("input", {
			type: "text",
			id,
			class: "form-control sp-input sp-formula",
			autocomplete: "off",
			spellcheck: "false",
			placeholder: "max(carrier_price * 1.1, 45)",
			"aria-describedby": `${id}-names ${id}-error`,
			"data-testid": "rule-formula-input",
		});
		this.formula_input.addEventListener("input", () => {
			this.wizard.change(() => {
				rule.formula = this.formula_input.value;
			});
			this.view.check_formula(this, { soon: true });
		});
		this.formula_error = el("p", {
			class: "sp-formula-error",
			id: `${id}-error`,
			role: "alert",
			"data-testid": "rule-formula-error",
		});
		const names = __("You can use {0}, and the functions {1}.", [
			sp.and_list(FORMULA_VARIABLES),
			sp.and_list(FORMULA_FUNCTIONS),
		]);
		this.formula_field = el(
			"div",
			{ class: "sp-field sp-formula-field" },
			el("label", { class: "sp-label", for: id }, __("Formula")),
			this.formula_input,
			el("p", { class: "sp-hint", id: `${id}-names`, "data-testid": "rule-formula-names" }, names),
			this.formula_error
		);
		this.round_up = el("input", { type: "checkbox", id: `${this.uid}-round-up`, "data-testid": "rule-round-up" });
		this.round_up.addEventListener("change", () =>
			this.wizard.change(() => {
				rule.round_up = this.round_up.checked;
			})
		);
		this.round_up_row = el(
			"label",
			{ class: "sp-check", for: `${this.uid}-round-up` },
			this.round_up,
			el("span", {}, __("Round up to the next whole dollar"))
		);
		this.sync_editor = () => {
			this.check_choices("rule-charge-option", rule.method || "carrier_price");
			this.formula_field.hidden = rule.method !== "formula";
			if (document.activeElement !== this.formula_input) this.formula_input.value = String(rule.formula ?? "");
			this.round_up.checked = Boolean(rule.round_up);
			this.round_up_row.hidden = rule.method === "free";
		};
		// A charge switched away from keeps what it had, as a remembered choice (ruling P88c): Round up stays on
		// when Free is chosen, and the formula stays when the carrier's price is. The server accepts both, and the
		// sentence (sp.rule_words), like the Review step, says only what applies; switching back brings them back.
		const choose = (method) => {
			this.wizard.change(() => {
				rule.method = method;
			});
			if (method === "formula") this.view.check_formula(this);
		};
		if (rule.method === "formula") this.view.check_formula(this);
		return [
			this.choices(
				__("What does the customer pay?"),
				"rule-charge-option",
				CHARGES.map(({ method, label }) => ({ value: method, text: label })),
				choose
			),
			this.formula_field,
			this.round_up_row,
			this.actions(),
		];
	}

	// The answer of check_formula for this card's formula ("" when it can be used), while its editor is open.
	show_formula_error(error) {
		if (this.chip === "charge" && this.formula_error) this.formula_error.textContent = error;
		this.update(this.index, this.view.rules.length);
	}

	limits_editor() {
		const rule = this.rule;
		const typed = this.view.typed_of(rule);
		const inputs = [];
		const field = (key, word) => {
			const id = `${this.uid}-${key}`;
			const input = el("input", {
				type: "text",
				id,
				class: "form-control sp-input sp-limit",
				inputmode: "decimal",
				autocomplete: "off",
				placeholder: __("no limit"),
				"data-testid": "rule-limit",
				"data-key": key,
			});
			input.addEventListener("input", () => this.type_limit(key, input.value));
			inputs.push(input);
			return [el("label", { class: "sp-limit-word", for: id }, word), input];
		};
		// a labelled group per measure (a legend cannot sit in a row that wraps)
		const rows = LIMITS.map((band) =>
			el(
				"div",
				{ class: "sp-limit-row", role: "group", "aria-labelledby": `${this.uid}-${band.low}-measure` },
				el("span", { class: "sp-limit-label", id: `${this.uid}-${band.low}-measure` }, band.label),
				...field(band.low, __("from")),
				...field(band.high, __("up to"))
			)
		);
		const problems = el("ul", { class: "sp-problems", "aria-live": "polite", "data-testid": "rule-limits-problem" });
		this.sync_editor = () => {
			for (const input of inputs) {
				if (document.activeElement === input) continue;
				const key = input.dataset.key;
				input.value = key in typed ? typed[key] : sp.limit_of(rule[key]) ? String(rule[key]) : "";
			}
			problems.replaceChildren(...this.limit_problems().map((text) => el("li", {}, text)));
		};
		return [
			el("div", { class: "sp-limits" }, rows),
			el(
				"p",
				{ class: "sp-hint" },
				__("“From” includes the number and “up to” does not: from 0 up to 500 means up to 499.99. Leave a field empty for no limit.")
			),
			problems,
			this.actions(),
		];
	}

	// A limit typed: a number (or nothing) goes into the draft at once, kept to the decimals the ERP keeps and
	// rounded as the ERP rounds it (the desk's own flt follows the site's rounding setting, as the server's flt
	// does), so the draft holds what will be stored and the chip says it; when that changes the number, a note
	// says what is kept. Anything else stays in its field, is not in the draft, and says so.
	type_limit(key, text) {
		const typed = this.view.typed_of(this.rule);
		const reading = sp.read_number(text);
		delete this.kept[key];
		if (!reading.ok) {
			typed[key] = text;
			this.sync_editor();
			return;
		}
		delete typed[key];
		const band = LIMITS.find((entry) => entry.low === key || entry.high === key);
		const kept = reading.value === null ? 0 : flt(reading.value, band.places);
		if (reading.value !== null && kept !== reading.value) this.kept[key] = kept;
		this.wizard.change(() => {
			this.rule[key] = kept;
		});
	}

	limit_problems() {
		const typed = this.view.typed_of(this.rule);
		const said = [];
		for (const band of LIMITS) {
			for (const [key, word] of [
				[band.low, __("from")],
				[band.high, __("up to")],
			]) {
				if (key in this.kept) {
					const kept = this.kept[key];
					said.push(
						__("{0}, {1}: the ERP keeps {2} decimals, so this is {3}.", [
							band.label,
							word,
							band.places,
							kept ? band.show(kept) : __("no limit"),
						])
					);
				}
				if (!(key in typed)) continue;
				said.push(
					sp.read_number(typed[key]).comma
						? __("{0}, {1}: use a period for decimals.", [band.label, word])
						: __("{0}, {1}: type a number such as 500, or leave it empty for no limit.", [band.label, word])
				);
			}
		}
		return said;
	}
}
