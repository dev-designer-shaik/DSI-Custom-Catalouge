// Part of the Shipping policy wizard page: shipping_policy_wizard.js stitches it in and says how.
//
// The Zones tab's body: a card per zone of the draft, in its order. A card holds the zone's name, "Use this
// zone", its countries as removable tokens (added with a dsi_catalogue.CountryPicker), the Not shipped and
// Everywhere else switches, what customers read about it, and the countries no carrier serves. A country
// that is already in another zone that is on is refused, naming that zone. Renaming a zone takes its rules
// along (they name their zone); a zone that rules use cannot be deleted. The zone's fallback is the
// "If the carrier can't quote" step's and is never touched here.
//
// Each zone keeps its card while it is in the draft (the cards are found by the zone object itself), so a
// change redraws what changed and the focus stays where it was.

let zone_cards = 0; // ids for the labels of each card

class ZonesView {
	constructor(wizard, panel) {
		this.wizard = wizard;
		this.cards = new Map(); // zone object -> ZoneCard
		// zone object -> {name, rules}: the rules a rename of the zone takes along (see ZoneCard.begin_rename);
		// kept with the wizard, so a rename left half done is picked up again after a tab switch
		this.renames = wizard.ui.zones.renames = wizard.ui.zones.renames || new WeakMap();
		this.list = el("div", { class: "sp-zones" });
		this.empty = el(
			"p",
			{ class: "sp-empty-state" },
			__("No zones yet. Add one for each group of countries you price the same way.")
		);
		this.add_button = el(
			"button",
			{ type: "button", class: "btn btn-default btn-sm", "data-testid": "zone-add", onclick: () => this.add() },
			__("Add a zone")
		);
		this.live = new Announcer("zones-live");
		panel.append(
			el(
				"p",
				{ class: "sp-step-intro" },
				__("A zone is a group of countries priced alike. A country can be in only one zone that is on.")
			),
			this.list,
			this.empty,
			el("div", { class: "sp-add" }, this.add_button),
			this.live.root
		);
		this.refresh();
	}

	get zones() {
		const zones = this.wizard.store.draft.zones;
		return Array.isArray(zones) ? zones : [];
	}

	get rules() {
		const rules = this.wizard.store.draft.rules;
		return Array.isArray(rules) ? rules.filter(sp.is_record) : [];
	}

	// The cards follow the draft: one per zone, in its order (keep_cards). A card's notes (a refused delete or
	// country, a character taken out of the name) last until the next change.
	refresh() {
		const count = keep_cards({
			entries: this.zones,
			cards: this.cards,
			list: this.list,
			make: (zone) => new ZoneCard(this, zone),
			each: (card, index) => {
				card.refusal = "";
				card.country_refusal = "";
				card.name_note = "";
				card.update(index);
			},
		});
		this.empty.hidden = count > 0;
	}

	add() {
		const name = sp.unique_name(
			__("New zone"),
			this.zones.filter(sp.is_record).map((zone) => String(zone.name ?? ""))
		);
		const zone = sp.new_zone(name);
		this.wizard.change((draft) => {
			if (!Array.isArray(draft.zones)) draft.zones = [];
			draft.zones.push(zone);
		});
		const card = this.cards.get(zone);
		if (card) {
			card.name.focus();
			card.name.select();
		}
		this.live.say(__("Zone added: {0}.", [name]));
	}

	// A zone that rules use is not deleted: they would name a zone that is not there.
	remove(card) {
		const zone = card.zone;
		const users = this.users_of(zone).length;
		if (users) {
			card.refusal =
				users === 1
					? __("1 rule uses this zone. Change or delete that rule first.")
					: __("{0} rules use this zone. Change or delete those rules first.", [users]);
			card.update(card.index);
			return;
		}
		const cards = [...this.list.children];
		const at = cards.indexOf(card.root);
		this.wizard.change((draft) => {
			const index = draft.zones.indexOf(zone);
			if (index >= 0) draft.zones.splice(index, 1);
		});
		const next = this.list.children[Math.min(at, this.list.children.length - 1)];
		(next?.querySelector('[data-testid="zone-delete"]') || this.add_button).focus();
		this.live.say(zone.name ? __("Zone deleted: {0}.", [zone.name]) : __("Zone deleted."));
	}

	// The rules that name this zone (none for a zone without a name).
	users_of(zone) {
		return zone.name ? this.rules.filter((rule) => rule.zone === zone.name) : [];
	}

	// Another zone that is on and lists the country, or null.
	owner_of(code, zone) {
		return (
			this.zones.find(
				(other) =>
					sp.is_record(other) &&
					other !== zone &&
					other.enabled &&
					Array.isArray(other.countries) &&
					other.countries.includes(code)
			) || null
		);
	}

	country_name(code) {
		return this.wizard.state.countries.find((country) => country.code === code)?.name || code;
	}
}

class ZoneCard {
	constructor(view, zone) {
		zone_cards += 1;
		const id = `sp-zone-${zone_cards}`;
		this.view = view;
		this.wizard = view.wizard;
		this.zone = zone;
		this.index = 0;
		this.refusal = ""; // why the last delete was refused, until the next change
		this.country_refusal = ""; // why the last country was not added, until the next change
		this.name_note = ""; // that < or > was taken out of the name, until the next change
		this.renaming = null; // {name, rules}: what this field's rename takes along (see begin_rename)
		this.shown_countries = null; // the codes the tokens show

		this.name = el("input", {
			type: "text",
			class: "sp-title-input",
			maxlength: NAME_LENGTH,
			autocomplete: "off",
			spellcheck: "false",
			placeholder: __("Name this zone"),
			"aria-label": __("Zone name"),
			"aria-describedby": `${id}-problems`,
			"data-testid": "zone-name",
		});
		this.name.addEventListener("focus", () => this.begin_rename());
		this.name.addEventListener("blur", () => {
			this.renaming = null;
		});
		this.name.addEventListener("input", () => this.rename());
		this.state = el("span", { class: "sp-states", "data-testid": "zone-state" });
		this.enabled = el("input", { type: "checkbox", id: `${id}-enabled`, "data-testid": "zone-enabled" });
		this.enabled.addEventListener("change", () => this.set("enabled", this.enabled.checked));
		this.delete = el(
			"button",
			{ type: "button", class: "sp-link sp-quiet", "data-testid": "zone-delete", onclick: () => view.remove(this) },
			__("Delete")
		);
		this.problems = el("ul", {
			class: "sp-problems",
			id: `${id}-problems`,
			"aria-live": "polite",
			"data-testid": "zone-problems",
		});

		// the countries
		this.picker = new dsi_catalogue.CountryPicker({
			id: `${id}-country`,
			countries: () => this.offered(),
			placeholder: __("Add a country by its name or code"),
			testid: "zone-country-add",
			on_change: (code) => this.add_country(code),
		});
		this.tokens = el("div", { class: "sp-token-box" });
		this.country_problem = el("p", {
			class: "sp-problem",
			"aria-live": "polite",
			"data-testid": "zone-country-problem",
		});
		this.everywhere_note = el(
			"p",
			{ class: "sp-hint", "data-testid": "zone-everywhere-else-note" },
			__("Every country that is in no other zone that is on. No country can be added here.")
		);
		this.unserved = el("p", { class: "sp-warning", "data-testid": "zone-unserved-warning" });

		// the switches
		const switch_row = (testid, label, hint) => {
			const input = el("input", { type: "checkbox", id: `${id}-${testid}`, "data-testid": testid });
			const row = el(
				"label",
				{ class: "sp-check sp-switch", for: `${id}-${testid}` },
				input,
				el("span", {}, el("span", { class: "sp-switch-name" }, label), " ", el("span", { class: "sp-hint" }, hint))
			);
			return [input, row];
		};
		const [not_shipped, not_shipped_row] = switch_row(
			"zone-not-shipped",
			__("Not shipped"),
			__("Delivery is not offered to these countries.")
		);
		const [everywhere, everywhere_row] = switch_row(
			"zone-everywhere-else",
			__("Everywhere else"),
			__("This zone takes every country that is in no other zone.")
		);
		this.not_shipped = not_shipped;
		this.everywhere_else = everywhere;
		this.not_shipped.addEventListener("change", () => this.set("not_shipped", this.not_shipped.checked));
		this.everywhere_else.addEventListener("change", () => this.switch_everywhere_else());

		// what customers read
		const text_field = (testid, label, hint) => {
			const input = el("input", {
				type: "text",
				id: `${id}-${testid}`,
				class: "form-control sp-input",
				autocomplete: "off",
				"aria-describedby": hint ? `${id}-${testid}-hint` : null,
				"data-testid": testid,
			});
			const field = el(
				"div",
				{ class: "sp-field" },
				el("label", { class: "sp-label", for: `${id}-${testid}` }, label),
				input,
				hint ? el("p", { class: "sp-hint", id: `${id}-${testid}-hint` }, hint) : null
			);
			return [input, field];
		};
		const [line, line_field] = text_field(
			"zone-line",
			__("What customers read about delivery to this zone"),
			__("The FAQ and the Shipping page show it. Optional.")
		);
		const [message, message_field] = text_field(
			"zone-not-offered-message",
			__("What customers read when delivery is not offered here"),
			__("Shown at checkout instead of a delivery price.")
		);
		this.line = line;
		this.message = message;
		this.message_field = message_field;
		this.line.addEventListener("input", () => this.set("customer_line", this.line.value));
		this.message.addEventListener("input", () => this.set("not_offered_message", this.message.value));
		this.usage = el("p", { class: "sp-hint sp-usage", "data-testid": "zone-usage" });

		this.root = el(
			"div",
			{ class: "sp-zone", role: "group", "data-testid": "zone-card" },
			el(
				"div",
				{ class: "sp-card-head" },
				el("div", { class: "sp-card-title" }, this.name, this.state),
				el(
					"div",
					{ class: "sp-card-actions" },
					el(
						"label",
						{ class: "sp-check", for: `${id}-enabled` },
						this.enabled,
						el("span", {}, __("Use this zone"))
					),
					this.delete
				)
			),
			this.problems,
			el(
				"div",
				{ class: "sp-field sp-countries" },
				el("label", { class: "sp-label", for: `${id}-country` }, __("Countries")),
				this.tokens,
				this.picker.root,
				this.everywhere_note,
				this.country_problem
			),
			this.unserved,
			el("div", { class: "sp-switches" }, not_shipped_row, everywhere_row),
			line_field,
			message_field,
			this.usage
		);
	}

	// One field of the zone changed.
	set(key, value) {
		this.wizard.change(() => {
			this.zone[key] = value;
		});
	}

	// The countries the picker offers: those the zone does not list yet.
	offered() {
		const listed = Array.isArray(this.zone.countries) ? this.zone.countries : [];
		return this.wizard.state.countries.filter((country) => !listed.includes(country.code));
	}

	add_country(code) {
		this.picker.set("");
		if (!code) return;
		const name = this.view.country_name(code);
		const owner = this.view.owner_of(code, this.zone);
		if (owner) {
			this.country_refusal = owner.name
				? __("{0} is already in the {1} zone.", [name, owner.name])
				: __("{0} is already in another zone that is on.", [name]);
			this.update(this.index);
			return;
		}
		this.wizard.change(() => {
			if (!Array.isArray(this.zone.countries)) this.zone.countries = [];
			if (!this.zone.countries.includes(code)) this.zone.countries.push(code);
		});
		this.view.live.say(__("{0} added.", [name]));
	}

	remove_country(code, index) {
		const name = this.view.country_name(code);
		this.wizard.change(() => {
			const at = this.zone.countries.indexOf(code);
			if (at >= 0) this.zone.countries.splice(at, 1);
		});
		focus_token(this.tokens.firstChild, index, this.picker.input);
		this.view.live.say(__("{0} removed.", [name]));
	}

	// Everywhere else takes every country that is in no other zone, so the zone cannot list any: turning it on
	// removes them, after asking when there are some.
	switch_everywhere_else() {
		const on = this.everywhere_else.checked;
		const listed = Array.isArray(this.zone.countries) ? this.zone.countries.length : 0;
		if (!on || !listed) {
			this.set("everywhere_else", on);
			return;
		}
		this.everywhere_else.checked = false; // until the question is answered
		const dialog = frappe.confirm(__("Remove this zone's countries, so that it takes every country that is in no other zone?"), () =>
			this.wizard.change(() => {
				this.zone.countries.splice(0);
				this.zone.everywhere_else = true;
			})
		);
		// once the dialog is gone: the desk's Escape takes the focus away while the dialog closes
		dialog.$wrapper.one("hidden.bs.modal", () => this.everywhere_else.focus());
	}

	// A rename takes along the rules that named the zone when its name field got the focus. They are found by
	// name then, and followed as they are from there on: while the name is typed it may pass through another
	// zone's name, and that zone's rules stay with it. A name another zone also has is ambiguous, and a blank
	// one names nothing: the rules found the last time are taken instead. A blank name is not given to the
	// rules (they keep the last one until a name is typed), so a zone renamed in two goes keeps its rules.
	begin_rename() {
		const zone = this.zone;
		const name = String(zone.name ?? "");
		const shared = this.view.zones.some((other) => other !== zone && sp.is_record(other) && other.name === name);
		const before = this.view.renames.get(zone);
		let tracked = { name, rules: [] };
		if (name && !shared) tracked = { name, rules: this.view.users_of(zone) };
		else if (before) {
			const rules = before.rules.filter((rule) => this.view.rules.includes(rule) && rule.zone === before.name);
			tracked = { name: before.name, rules };
		}
		this.view.renames.set(zone, tracked);
		this.renaming = tracked;
	}

	rename() {
		const stripped = strip_angles(this.name);
		const name = this.name.value;
		if (!this.renaming) this.begin_rename();
		const tracked = this.renaming;
		this.wizard.change((draft) => {
			this.zone.name = name;
			if (!name) return; // the rules keep the last name until a name is typed
			for (const rule of tracked.rules) if (draft.rules.includes(rule)) rule.zone = name;
			tracked.name = name;
		});
		if (stripped) {
			this.name_note = __("A zone's name cannot hold < or >.");
			this.update(this.index);
		}
	}

	// The tokens of the zone's countries: drawn again only when the list changed, so a token keeps the focus.
	show_countries(codes) {
		const key = JSON.stringify(codes);
		if (key === this.shown_countries) return;
		this.shown_countries = key;
		const tokens = codes.map((code) => ({ key: code, text: this.view.country_name(code) }));
		this.tokens.replaceChildren(
			...(tokens.length
				? [
						token_list({
							tokens,
							label: __("Countries of this zone"),
							testid: "zone-country-chip",
							remove_testid: "zone-country-remove",
							remove: (code, at) => this.remove_country(code, at),
						}),
				  ]
				: [])
		);
	}

	// What the card shows, from the zone and the rest of the draft.
	update(index) {
		const zone = this.zone;
		const view = this.view;
		this.index = index;
		const name = String(zone.name ?? "");
		if (document.activeElement !== this.name && this.name.value !== name) this.name.value = name;
		this.root.setAttribute("aria-label", name ? __("Zone {0}", [name]) : __("Zone {0}, without a name", [index + 1]));
		this.root.classList.toggle("is-off", !zone.enabled);
		this.root.classList.toggle("is-not-shipped", Boolean(zone.not_shipped));
		this.enabled.checked = Boolean(zone.enabled);
		this.not_shipped.checked = Boolean(zone.not_shipped);
		this.everywhere_else.checked = Boolean(zone.everywhere_else);
		const states = [];
		if (!zone.enabled) states.push(el("span", { class: "sp-state" }, __("Off")));
		if (zone.not_shipped) states.push(el("span", { class: "sp-state is-not-shipped" }, __("Not shipped")));
		if (zone.everywhere_else) states.push(el("span", { class: "sp-state" }, __("Everywhere else")));
		this.state.replaceChildren(...states);

		const codes = Array.isArray(zone.countries) ? zone.countries.map(String) : [];
		this.show_countries(codes);
		this.picker.input.disabled = Boolean(zone.everywhere_else);
		this.picker.root.hidden = Boolean(zone.everywhere_else);
		this.everywhere_note.hidden = !zone.everywhere_else;
		this.country_problem.textContent = this.country_refusal;

		// the countries no carrier serves (in rate-card mode the carrier is not asked)
		const served = new Set(this.wizard.state.served_codes || []);
		const unserved = codes.filter((code) => !served.has(code));
		const warn = unserved.length && !zone.not_shipped && this.wizard.price_source() === "carrier";
		this.unserved.textContent = warn
			? __("No carrier serves: {0}. While prices come from the carrier, customers there are not offered delivery.", [
					unserved.map((code) => view.country_name(code)).join(", "),
			  ])
			: "";
		this.unserved.hidden = !warn;

		// what customers read: the not-offered message only for a Not shipped zone (its value is kept otherwise)
		if (document.activeElement !== this.line) this.line.value = String(zone.customer_line ?? "");
		if (document.activeElement !== this.message) this.message.value = String(zone.not_offered_message ?? "");
		this.message_field.hidden = !zone.not_shipped;

		// the rules that use it, by their number (not for a Not shipped zone: no rule applies there)
		const rules = this.wizard.store.draft.rules;
		const users = view.users_of(zone).map((rule) => rules.indexOf(rule) + 1);
		this.usage.hidden = Boolean(zone.not_shipped);
		this.usage.textContent = !users.length
			? __("No rule uses this zone yet.")
			: users.length === 1
			? __("Rule {0} uses this zone.", [users[0]])
			: __("Rules {0} use this zone.", [sp.and_list(users.map(String))]);

		this.problems.replaceChildren(...this.problems_of(zone).map((text) => el("li", {}, text)));
	}

	// What is wrong with the zone, in plain sentences, as far as the page can see it: the server checks the
	// whole draft again in the Review step.
	problems_of(zone) {
		const view = this.view;
		const said = [];
		const others = view.zones.filter((other) => other !== zone && sp.is_record(other));
		const name = String(zone.name ?? "").trim();
		if (this.name_note) said.push(this.name_note);
		if (!name) said.push(__("Give the zone a name. Rules choose their zone by its name."));
		else if (others.some((other) => sp.fold_name(other.name) === sp.fold_name(name))) {
			said.push(
				__("Another zone has this name. Names that differ only in capital letters or accents count as the same.")
			);
		}
		const codes = Array.isArray(zone.countries) ? zone.countries : [];
		if (zone.everywhere_else && codes.length) {
			said.push(__("An Everywhere else zone cannot list countries. Remove them, or turn Everywhere else off."));
		}
		if (zone.enabled) {
			for (const code of codes) {
				const owner = view.owner_of(code, zone);
				if (owner && !owner.everywhere_else) {
					said.push(
						__("{0} is also in the {1} zone. A country can be in only one zone that is on.", [
							view.country_name(code),
							owner.name || __("unnamed"),
						])
					);
				}
			}
			if (zone.everywhere_else && others.some((other) => other.enabled && other.everywhere_else)) {
				said.push(__("Another zone that is on is set to Everywhere else. Only one zone can be."));
			}
		}
		if (this.refusal) said.push(this.refusal);
		return said;
	}
}

const step_zones = {
	render(wizard, panel) {
		wizard.ui.zones.view = new ZonesView(wizard, panel);
	},
	refresh(wizard, panel) {
		wizard.ui.zones.view?.refresh();
	},
	priced(wizard, panel, answer) {},
};
