// Part of the Shipping policy wizard page: shipping_policy_wizard.js stitches it in and says how.
//
// A combobox over wizard_state().countries: type to narrow the list (by name, accents ignored, or by the
// two-letter code), arrow keys to move, Enter or Tab to choose, Escape to close. on_change(code) is called
// with the chosen code ("" when the field is cleared).
//
// Options: `countries` is a function that returns the list when it is called, normally
// () => wizard.state.countries, so that a picker always uses the list the wizard holds now (a plain list is
// taken too). `id` is the input's id, for a label's `for`; leave it out and each picker gets its own, so that
// many pickers (one per zone card, say) never share one. `placeholder` and `testid` are the input's.
// The picker works the same over any list of {code, name} (the item groups of an item set use it, the
// group's name as both): `list_label` names the list and `none` is what it says when nothing matches.
let country_pickers = 0;
dsi_catalogue.CountryPicker = class CountryPicker {
	constructor({
		id,
		countries,
		placeholder,
		testid,
		on_change,
		list_label = __("Countries"),
		none = __("No country has that name."),
	}) {
		country_pickers += 1;
		id = id || `sp-country-${country_pickers}`;
		this.id = id;
		this.source = countries;
		this.on_change = on_change;
		this.none = none;
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
			"aria-label": list_label,
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

	// The countries as the wizard holds them now: [{code, name}].
	get countries() {
		const list = typeof this.source === "function" ? this.source() : this.source;
		return Array.isArray(list) ? list : [];
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
			this.list.append(el("li", { class: "sp-combo-none", role: "presentation" }, this.none));
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
			// the list's Escape: the desk's own Escape (on the window) would take the focus away from the field
			event.preventDefault();
			event.stopPropagation();
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
