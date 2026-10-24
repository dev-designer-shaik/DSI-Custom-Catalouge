// Part of the Shipping policy wizard page: shipping_policy_wizard.js stitches it in and says how.
//
// A zone of the rate card in the Rates step (wizard_rates_card.js keeps one per zone of an open profile): a line
// with its name, its countries, what it charges and its delivery method, and the price on the right (brass, or
// Free in sage), which opens to the zone itself. Then: its name; its countries as chips, added with the Zones
// step's CountryPicker (a country can be in one zone of a profile only: another is refused, naming the zone that
// has it), a code the ERP does not know first and in brick, and only the first 30 others until all are asked
// for; its delivery method and days; and its prices, either one price at any weight or a ledger of weight tiers,
// each a band in pounds and its price in dollars, edited where they stand. A price is kept to the cent and a
// weight to three decimals, as the ERP keeps them (the desk's own flt, as the other steps round); it is never
// below zero; and left blank it is missing, which the draft holds, so the card says so until one is typed.
// What is typed that is not a number stays in its field, out of the draft, and says why (wizard.ui.rates.typed).

const CARD_SHOWN_COUNTRIES = 30; // a zone shows its first countries; the others when they are asked for

// The three numbers of a tier: the decimals the ERP keeps of each, the most it takes, and how a field is named.
const TIER_FIELDS = {
	from: { places: 3, max: CARD_LIMITS.weight, label: __("From, in pounds") },
	to: { places: 3, max: CARD_LIMITS.weight, label: __("Up to, in pounds") },
	price: { places: 2, max: CARD_LIMITS.price, label: __("Price in US dollars") },
};

class RateZoneCard {
	constructor(owner, zone) {
		rate_cards += 1;
		this.uid = `sp-czone-${rate_cards}`;
		this.owner = owner;
		this.sheet = owner.sheet;
		this.wizard = owner.wizard;
		this.ui = owner.ui;
		this.zone = zone;
		this.index = 0;
		this.built = false;
		this.tiers = new Map(); // tier object -> RateTierRow
		this.country_refusal = ""; // why the last country was not added, until the next change
		this.title = el("span", { class: "sp-czone-title" });
		this.about = el("span", { class: "sp-disclosure-about" });
		this.figure = el("span", { class: "sp-czone-figure", "aria-hidden": "true" });
		this.toggle = el(
			"button",
			{
				type: "button",
				class: "sp-disclosure sp-czone-toggle",
				"aria-expanded": "false",
				"aria-controls": `${this.uid}-body`,
				"data-testid": "rates-zone-toggle",
				onclick: () => this.set_open(!this.ui.open.has(zone)),
			},
			el("span", { class: "sp-chevron", "aria-hidden": "true" }),
			el("span", { class: "sp-disclosure-text" }, this.title, this.about),
			this.figure
		);
		this.body = el("div", { class: "sp-czone-body", id: `${this.uid}-body`, hidden: true });
		this.root = el("li", { class: "sp-czone", "data-testid": "rates-zone" }, this.toggle, this.body);
	}

	set_open(open) {
		if (open) this.ui.open.add(this.zone);
		else this.ui.open.delete(this.zone);
		this.update(this.index);
	}

	codes() {
		return Array.isArray(this.zone.countries) ? this.zone.countries.map(String) : [];
	}

	update(index) {
		const zone = this.zone;
		this.index = index;
		const name = String(zone.name ?? "").trim() || __("A zone without a name");
		this.title.textContent = name;
		const count = this.codes().length;
		const where = count === 1 ? __("1 country") : __("{0} countries", [count]);
		const method = String(zone.method ?? "").trim();
		const prices = sp.zone_price_words(zone);
		this.about.textContent = method ? __("{0}. {1}, {2}.", [where, prices, method]) : __("{0}. {1}.", [where, prices]);
		this.show_figure();
		this.root.setAttribute("aria-label", __("Zone {0}", [name]));
		const open = this.ui.open.has(zone);
		this.toggle.setAttribute("aria-expanded", String(open));
		this.body.hidden = !open;
		if (open && !this.built) this.build();
		if (!open && this.built) this.unbuild();
		if (this.built) this.update_body();
	}

	// What the zone charges, on the right of its line: one price, or its lowest and highest; Free in sage.
	show_figure() {
		const prices = sp
			.card_tiers(this.zone)
			.map((tier) => tier.price)
			.filter((price) => sp.is_number(price) && price >= 0);
		const money = (amount) => (amount === 0 ? __("Free") : sp.money(amount));
		this.figure.className = "sp-czone-figure";
		if (!prices.length) {
			this.figure.textContent = __("No price yet");
			return;
		}
		const low = Math.min(...prices);
		const high = Math.max(...prices);
		this.figure.textContent = low === high ? money(low) : __("{0} to {1}", [money(low), money(high)]);
		this.figure.classList.add(high === 0 ? "is-free" : "is-paid");
	}

	// ---------------------------------------------------------- the open zone
	build() {
		const uid = this.uid;
		const zone = this.zone;
		const text_field = (testid, label, after) => {
			const input = el("input", {
				type: "text",
				id: `${uid}-${testid}`,
				class: "form-control sp-input",
				autocomplete: "off",
				spellcheck: "false",
				"data-testid": testid,
			});
			return [input, el("div", { class: "sp-field" }, el("label", { class: "sp-label", for: input.id }, label), input, ...after)];
		};
		const [name, name_field] = text_field("rates-zone-name", __("Name"), []);
		this.name = name;
		this.name.maxLength = CARD_LIMITS.text;
		this.name.addEventListener("input", () =>
			this.wizard.change(() => {
				zone.name = this.name.value;
			})
		);
		commit_on_leave(this.name, () => this.commit_text(this.name, "name"));

		// the countries
		this.unknown = el("div", { class: "sp-unknown-countries", "data-testid": "rates-unknown-countries" });
		this.chips = el("div", { class: "sp-token-box" });
		this.shown_countries = null;
		this.more = el("button", {
			type: "button",
			class: "sp-link sp-more-countries",
			"data-testid": "rates-countries-all",
			onclick: () => this.show_all(!this.ui.all_countries.has(zone)),
		});
		this.picker = new dsi_catalogue.CountryPicker({
			id: `${uid}-country`,
			countries: () => this.offered(),
			placeholder: __("Add a country by its name or code"),
			testid: "rates-country-add",
			on_change: (code) => this.add_country(code),
		});
		this.country_problem = el("p", { class: "sp-problem", "aria-live": "polite", "data-testid": "rates-country-problem" });

		// the delivery method and the days it promises
		const [method, method_field] = text_field("rates-zone-method", __("Delivery method, as customers read it"), []);
		this.method = method;
		this.method.maxLength = CARD_LIMITS.text;
		this.method.addEventListener("input", () =>
			this.wizard.change(() => {
				zone.method = this.method.value;
			})
		);
		commit_on_leave(this.method, () => this.commit_text(this.method, "method"));
		this.days_problem = el("p", { class: "sp-problem", id: `${uid}-days-problem`, "aria-live": "polite" });
		const [days, days_field] = text_field("rates-zone-days", __("Days promised"), [this.days_problem]);
		this.days = days;
		this.days.setAttribute("inputmode", "numeric");
		this.days.setAttribute("aria-describedby", `${uid}-days-problem`);
		this.days.classList.add("sp-days");
		this.days.addEventListener("input", () => this.type_days(this.days.value));

		// the prices: one at any weight, or by weight
		this.kinds = ["flat", "weight"].map((kind) => {
			const input = el("input", { type: "radio", name: `${uid}-kind`, value: kind, "data-testid": "rates-zone-prices" });
			input.addEventListener("change", () => {
				if (input.checked) this.set_kind(kind);
			});
			return input;
		});
		this.kind_hint = el("span", { class: "sp-option-note", id: `${uid}-kind-hint` });
		this.rows = el("tbody", {});
		this.table = el(
			"table",
			{ class: "sp-tiers", "data-testid": "rates-tiers" },
			el("caption", { class: "sp-sr-only" }, __("Prices of this zone")),
			el(
				"thead",
				{},
				el(
					"tr",
					{},
					el("th", { scope: "col" }, __("Weight, in pounds")),
					el("th", { scope: "col", class: "sp-tier-price-head" }, __("Price, in US dollars")),
					el("th", { scope: "col" }, el("span", { class: "sp-sr-only" }, __("Remove")))
				)
			),
			this.rows
		);
		this.tier_add = el(
			"button",
			{
				type: "button",
				class: "btn btn-default btn-xs",
				"aria-describedby": `${uid}-tier-hint`,
				"data-testid": "rates-tier-add",
				onclick: () => this.add_tier(),
			},
			__("Add a weight tier")
		);
		this.tier_hint = el("p", { class: "sp-hint", id: `${uid}-tier-hint` });
		this.delete = el(
			"button",
			{ type: "button", class: "sp-link sp-quiet", "data-testid": "rates-zone-delete", onclick: () => this.owner.delete_zone(this) },
			__("Delete this zone")
		);
		this.body.replaceChildren(
			name_field,
			el(
				"div",
				{ class: "sp-field sp-countries" },
				el("label", { class: "sp-label", for: `${uid}-country` }, __("Countries")),
				this.unknown,
				this.chips,
				this.more,
				this.picker.root,
				this.country_problem
			),
			el("div", { class: "sp-czone-method" }, method_field, days_field),
			el(
				"fieldset",
				{ class: "sp-choices sp-czone-kinds" },
				el("legend", { class: "sp-label" }, __("Prices")),
				el(
					"div",
					{ class: "sp-czone-kind-row" },
					el("label", { class: "sp-option" }, this.kinds[0], el("span", { class: "sp-option-text" }, __("One price at any weight"))),
					el("label", { class: "sp-option" }, this.kinds[1], el("span", { class: "sp-option-text" }, __("By weight"))),
					this.kind_hint
				)
			),
			this.table,
			el("div", { class: "sp-add sp-tier-add" }, this.tier_add, this.tier_hint),
			el("div", { class: "sp-profile-foot" }, this.delete)
		);
		this.built = true;
	}

	unbuild() {
		this.body.replaceChildren();
		this.tiers.clear();
		this.built = false;
		this.name = null;
	}

	typed() {
		let typed = this.ui.typed.get(this.zone);
		if (!typed) {
			typed = {};
			this.ui.typed.set(this.zone, typed);
		}
		return typed;
	}

	// A text of the zone (its name, its method) is kept without the spaces around it once it is committed.
	commit_text(input, key) {
		const trimmed = input.value.trim();
		if (trimmed === input.value) return;
		input.value = trimmed;
		this.wizard.change(() => {
			this.zone[key] = trimmed;
		});
	}

	// Days: a whole number from 0 to 60, or blank for none; anything else stays in its field, out of the draft.
	type_days(text) {
		const typed = this.typed();
		const value = String(text).trim();
		if (!value) {
			delete typed.days;
			this.wizard.change(() => {
				this.zone.days = null;
			});
			return;
		}
		if (!/^\d+$/.test(value) || Number(value) > CARD_LIMITS.days) {
			typed.days = text;
			this.update_body();
			return;
		}
		delete typed.days;
		this.wizard.change(() => {
			this.zone.days = Number(value);
		});
	}

	set_kind(kind) {
		const tiers = sp.card_tiers(this.zone);
		if (kind === "flat") {
			if (tiers.length > 1) return; // the radio is not offered then: the other tiers go first
			this.wizard.change(() => {
				if (!tiers.length) this.zone.tiers = [sp.new_card_tier(null, null, null)];
				else Object.assign(tiers[0], { from: null, to: null });
			});
			return;
		}
		// one price at any weight becomes the first weight tier, from 0 up to a weight still to be typed
		this.wizard.change(() => {
			if (!tiers.length) this.zone.tiers = [sp.new_card_tier(0, null, null)];
			else if (sp.is_flat_zone(this.zone)) tiers[0].from = 0;
		});
		[...this.tiers.values()][0]?.to?.focus();
	}

	add_tier() {
		const tiers = Array.isArray(this.zone.tiers) ? this.zone.tiers : [];
		if (tiers.length >= CARD_LIMITS.tiers || sp.is_flat_zone(this.zone)) return;
		// it starts a hundredth of a pound above where the last tier ends, as the Shopify tiers do
		const last = sp.card_tiers(this.zone).at(-1);
		const edge = [last?.to, last?.from].find((weight) => sp.is_number(weight));
		const from = edge === undefined ? 0 : flt(edge + 0.01, 3);
		const tier = sp.new_card_tier(from, null, null);
		this.wizard.change(() => {
			if (!Array.isArray(this.zone.tiers)) this.zone.tiers = [];
			this.zone.tiers.push(tier);
		});
		this.tiers.get(tier)?.to?.focus();
		this.sheet.live.say(__("Weight tier added, from {0} lb.", [sp.pounds(from)]));
	}

	remove_tier(row) {
		const tiers = this.zone.tiers;
		const at = tiers.indexOf(row.tier);
		if (at < 0 || sp.card_tiers(this.zone).length < 2) return;
		this.wizard.change(() => tiers.splice(at, 1));
		const rows = [...this.rows.children];
		const next = rows[Math.min(at, rows.length - 1)];
		(next?.querySelector('[data-testid="rates-tier-remove"]') || this.tier_add).focus();
		this.sheet.live.say(__("Weight tier removed."));
	}

	// The countries the picker offers: those the zone does not list yet.
	offered() {
		const listed = this.codes();
		return this.wizard.state.countries.filter((country) => !listed.includes(country.code));
	}

	add_country(code) {
		this.picker.set("");
		if (!code) return;
		const name = this.sheet.view.country_name(code);
		const owner = this.owner.owner_of(code, this.zone);
		if (owner) {
			const where = String(owner.name ?? "").trim();
			this.country_refusal = where
				? __("{0} is already in the zone {1} of this profile.", [name, where])
				: __("{0} is already in another zone of this profile.", [name]);
			this.update_body();
			return;
		}
		if (this.codes().length >= CARD_LIMITS.countries) {
			this.country_refusal = __("A zone holds at most {0} countries.", [CARD_LIMITS.countries]);
			this.update_body();
			return;
		}
		this.wizard.change(() => {
			if (!Array.isArray(this.zone.countries)) this.zone.countries = [];
			if (!this.zone.countries.includes(code)) this.zone.countries.push(code);
		});
		this.sheet.live.say(__("{0} added.", [name]));
	}

	// `box` holds the chip that was pressed: after the removal the focus goes to the chip that took its place.
	remove_country(code, index, box) {
		const name = this.sheet.view.country_name(code);
		this.wizard.change(() => {
			const at = this.zone.countries.indexOf(code);
			if (at >= 0) this.zone.countries.splice(at, 1);
		});
		focus_token(box, index, this.picker.input);
		this.sheet.live.say(__("{0} removed.", [name]));
	}

	show_all(all) {
		if (all) this.ui.all_countries.add(this.zone);
		else this.ui.all_countries.delete(this.zone);
		this.update_body();
	}

	// The countries as removable chips: first the codes the ERP does not know (the card cannot be applied with them),
	// then the others, the first 30 of them until all are asked for. Drawn again only when what they show changed.
	show_countries() {
		const codes = this.codes();
		const known = new Set(this.wizard.state.countries.map((country) => country.code));
		const unknown = codes.filter((code) => !known.has(code));
		const listed = codes.filter((code) => known.has(code));
		const all = this.ui.all_countries.has(this.zone) || listed.length <= CARD_SHOWN_COUNTRIES;
		const shown = all ? listed : listed.slice(0, CARD_SHOWN_COUNTRIES);
		const key = JSON.stringify([unknown, shown, listed.length]);
		if (key === this.shown_countries) return;
		this.shown_countries = key;
		const tokens = (list) => list.map((code) => ({ key: code, text: this.sheet.view.country_name(code) }));
		this.unknown.replaceChildren(
			...(unknown.length
				? [
						el(
							"p",
							{ class: "sp-unknown-note" },
							unknown.length === 1
								? __("Not a country in the ERP, so the card cannot be applied with it:")
								: __("Not countries in the ERP, so the card cannot be applied with them:")
						),
						token_list({
							tokens: tokens(unknown),
							label: __("Codes of this zone that are not countries in the ERP"),
							testid: "rates-country-chip",
							remove_testid: "rates-country-remove",
							remove: (code, at) => this.remove_country(code, at, this.unknown),
						}),
				  ]
				: [])
		);
		this.chips.replaceChildren(
			...(shown.length
				? [
						token_list({
							tokens: tokens(shown),
							label: __("Countries of this zone"),
							testid: "rates-country-chip",
							remove_testid: "rates-country-remove",
							remove: (code, at) => this.remove_country(code, at, this.chips),
						}),
				  ]
				: [])
		);
		this.more.hidden = listed.length <= CARD_SHOWN_COUNTRIES;
		this.more.textContent = this.ui.all_countries.has(this.zone)
			? __("Show the first {0} countries only", [CARD_SHOWN_COUNTRIES])
			: __("Show all {0} countries", [listed.length]);
	}

	update_body() {
		const zone = this.zone;
		const typed = this.typed();
		const name = String(zone.name ?? "");
		if (document.activeElement !== this.name && this.name.value !== name) this.name.value = name;
		this.show_countries();
		this.country_problem.textContent = this.country_refusal;
		const method = String(zone.method ?? "");
		if (document.activeElement !== this.method && this.method.value !== method) this.method.value = method;
		if (document.activeElement !== this.days) {
			this.days.value = typed.days ?? (Number.isInteger(zone.days) ? String(zone.days) : "");
		}
		this.days_problem.textContent =
			typed.days !== undefined ? __("Type a whole number of days from 0 to {0}, or leave it empty.", [CARD_LIMITS.days]) : "";

		const tiers = sp.card_tiers(zone);
		const flat = sp.is_flat_zone(zone);
		this.kinds[0].checked = flat;
		this.kinds[1].checked = !flat;
		const can_flatten = flat || tiers.length <= 1;
		this.kinds[0].disabled = !can_flatten;
		this.kind_hint.textContent = can_flatten ? "" : __("Remove the other weight tiers first.");
		if (can_flatten) this.kinds[0].removeAttribute("aria-describedby");
		else this.kinds[0].setAttribute("aria-describedby", this.kind_hint.id);
		const all = Array.isArray(zone.tiers) ? zone.tiers : [];
		keep_cards({
			entries: all,
			cards: this.tiers,
			list: this.rows,
			make: (tier) => new RateTierRow(this, tier),
			each: (row, at) => row.update(at, flat, tiers.length),
		});
		const full = all.length >= CARD_LIMITS.tiers;
		this.tier_add.hidden = flat;
		this.tier_add.setAttribute("aria-disabled", String(full));
		this.tier_hint.textContent = full ? __("A zone holds at most {0} weight tiers.", [CARD_LIMITS.tiers]) : "";
		this.delete.setAttribute("aria-label", __("Delete the zone {0}", [name.trim() || String(this.index + 1)]));
	}
}

// One tier of a zone, as a row of its prices table: the band (from and up to, in pounds; "Any weight" for one
// price) and the price, each edited where it stands, and a button that removes it.
class RateTierRow {
	constructor(zone_card, tier) {
		rate_cards += 1;
		this.uid = `sp-tier-${rate_cards}`;
		this.zone_card = zone_card;
		this.wizard = zone_card.wizard;
		this.ui = zone_card.ui;
		this.tier = tier;
		this.kept = {}; // field -> the number kept when it differs from the one typed, until it is typed again
		const number = (field, testid) => {
			const input = el("input", {
				type: "text",
				class: "form-control sp-input sp-tier-input",
				inputmode: "decimal",
				autocomplete: "off",
				"data-testid": testid,
			});
			input.addEventListener("input", () => this.type_value(field, input.value));
			return input;
		};
		this.from = number("from", "rates-tier-from");
		this.to = number("to", "rates-tier-to");
		this.price = number("price", "rates-tier-price");
		this.any = el("span", { class: "sp-tier-any" }, __("Any weight"));
		this.band = el("span", { class: "sp-tier-band" }, this.from, el("span", { class: "sp-words" }, __("to")), this.to);
		this.weight_problem = el("p", { class: "sp-problem", id: `${this.uid}-weight`, "aria-live": "polite" });
		this.price_problem = el("p", { class: "sp-problem", id: `${this.uid}-price`, "aria-live": "polite" });
		this.from.setAttribute("aria-describedby", this.weight_problem.id);
		this.to.setAttribute("aria-describedby", this.weight_problem.id);
		this.price.setAttribute("aria-describedby", this.price_problem.id);
		this.remove = el(
			"button",
			{ type: "button", class: "sp-link sp-quiet", "data-testid": "rates-tier-remove", onclick: () => zone_card.remove_tier(this) },
			__("Remove")
		);
		this.root = el(
			"tr",
			{ class: "sp-tier", "data-testid": "rates-tier" },
			el("td", { class: "sp-tier-weights" }, this.any, this.band, this.weight_problem),
			el(
				"td",
				{ class: "sp-tier-price" },
				el("span", { class: "sp-amount-box" }, el("span", { class: "sp-amount-sign", "aria-hidden": "true" }, "$"), this.price),
				this.price_problem
			),
			el("td", { class: "sp-tier-remove" }, this.remove)
		);
	}

	typed() {
		let typed = this.ui.typed.get(this.tier);
		if (!typed) {
			typed = {};
			this.ui.typed.set(this.tier, typed);
		}
		return typed;
	}

	// A number typed: one the ERP can keep goes into the draft at once, rounded as the ERP rounds it (blank is a
	// missing number, which the card then says it lacks); anything else stays in its field and says why.
	type_value(field, text) {
		const spec = TIER_FIELDS[field];
		const typed = this.typed();
		const reading = sp.read_number(text);
		this.kept[field] = null;
		if (!reading.ok || (reading.value !== null && reading.value > spec.max)) {
			typed[field] = text;
			this.zone_card.update_body();
			return;
		}
		delete typed[field];
		const value = reading.value === null ? null : flt(reading.value, spec.places);
		if (reading.value !== null && value !== reading.value) this.kept[field] = value;
		this.wizard.change(() => {
			this.tier[field] = value;
		});
	}

	// What a field says when it cannot be used as it is: "" when it can.
	problem_of(field) {
		const spec = TIER_FIELDS[field];
		const typed = this.typed()[field];
		const price = field === "price";
		if (typed !== undefined) {
			const reading = sp.read_number(typed);
			if (reading.comma) return __("Use a period for decimals.");
			if (reading.ok) {
				return price
					? __("A price is at most {0}.", [sp.money(spec.max)])
					: __("A weight is at most {0} lb.", [spec.max.toLocaleString("en-US")]);
			}
			return price ? __("Type a price of 0 or more, such as 35.") : __("Type a weight in pounds, such as 2.5.");
		}
		if (!sp.is_number(this.tier[field])) {
			return price ? __("Type the price, such as 35.") : __("Type the weight in pounds, such as 2.5.");
		}
		if (this.kept[field] !== null && this.kept[field] !== undefined) {
			return price
				? __("The ERP keeps cents, so this is {0}.", [sp.money(this.kept[field])])
				: __("The ERP keeps 3 decimals of a weight, so this is {0} lb.", [sp.pounds(this.kept[field])]);
		}
		return "";
	}

	update(at, flat, count) {
		const tier = this.tier;
		const typed = this.typed();
		const shown = (field, places) => {
			const input = this[field];
			if (document.activeElement === input) return;
			const value = tier[field];
			input.value = typed[field] ?? (sp.is_number(value) ? (places === 2 ? value.toFixed(2) : sp.pounds(value)) : "");
		};
		shown("price", 2);
		shown("from", 3);
		shown("to", 3);
		this.any.hidden = !flat;
		this.band.hidden = flat;
		const band = flat
			? __("any weight")
			: __("{0} to {1} lb", [sp.is_number(tier.from) ? sp.pounds(tier.from) : "?", sp.is_number(tier.to) ? sp.pounds(tier.to) : "?"]);
		this.from.setAttribute("aria-label", __("Tier {0}: {1}", [at + 1, TIER_FIELDS.from.label]));
		this.to.setAttribute("aria-label", __("Tier {0}: {1}", [at + 1, TIER_FIELDS.to.label]));
		this.price.setAttribute("aria-label", flat ? TIER_FIELDS.price.label : __("Tier {0}: {1}", [at + 1, TIER_FIELDS.price.label]));
		this.remove.hidden = flat || count < 2;
		this.remove.setAttribute("aria-label", __("Remove the tier {0}", [band]));
		this.weight_problem.textContent = flat ? "" : this.problem_of("from") || this.problem_of("to");
		this.price_problem.textContent = this.problem_of("price");
	}
}
