// Part of the Shipping policy wizard page: shipping_policy_wizard.js stitches it in and says how.
//
// The Rates step's read and edit view of the draft's rate card (step_rates.js draws it): how much the card holds,
// in words, then a card per profile, in the card's order. Each item takes the first profile whose product codes
// fit it, so the order matters: the profiles are numbered, and they move up and down with keyboard buttons. A
// profile is a line until it is opened; then come its name, its product code patterns as chips, and its zones,
// each a line in turn (its countries, its delivery method and what it charges, the price on the right) that
// opens to its countries, method, days and prices (wizard_rates_zone.js). Nothing is drawn until it is opened, so
// the real card (14 zones, 232 countries) never puts its countries on the page at once.
//
// Edits change draft.rate_card only, in place, through wizard.change; new entries come from sp.new_card_profile,
// sp.new_card_zone and sp.new_card_tier (wizard_rate_card.js), keys in the order the ERP keeps them, so that an
// entry added and taken back leaves the draft clean. A new profile goes before the one that takes every item
// (a pattern of *), which has to stay last. Deleting a profile that has zones, or a zone that has countries or a
// price, asks first. What the view shows that is not the draft lives in wizard.ui.rates: `open` (the profiles and
// zones that are open), `all_countries` (the zones that show all their countries) and `typed` (an entry -> what is
// typed in its fields that is not a number, which stays out of the draft).

let rate_cards = 0; // ids for the labels of each profile, zone and tier

// Whether a profile takes every item: one of its patterns is nothing but asterisks.
function takes_every_item(profile) {
	return sp.card_patterns(profile).some((pattern) => !pattern.replace(/\*/g, ""));
}

// A name as the rate card compares two: runs of spaces as one, capital letters aside (shipping_rate_card).
function card_name_key(name) {
	return String(name ?? "")
		.trim()
		.split(/\s+/)
		.join(" ")
		.toLowerCase();
}

// A name typed into the card is kept without the spaces around it, once it is committed (the field is left, or
// Enter is pressed): while it is typed, a space at its end stays, so that words can be typed.
function commit_on_leave(input, commit) {
	input.addEventListener("blur", commit);
	input.addEventListener("keydown", (event) => {
		if (event.key === "Enter") {
			event.preventDefault();
			commit();
		}
	});
}

class RateCardView {
	constructor(view) {
		this.view = view;
		this.wizard = view.wizard;
		const ui = (this.ui = view.ui);
		ui.open = ui.open || new WeakSet();
		ui.all_countries = ui.all_countries || new WeakSet();
		ui.typed = ui.typed || new WeakMap();
		this.cards = new Map(); // profile object -> RateProfileCard
		this.card = null; // the card the profile cards were made for
		this.summary = el("p", { class: "sp-sheet-summary", "data-testid": "rates-summary" });
		this.list = el("ol", {
			class: "sp-profiles",
			"aria-label": __("Profiles of the rate card, in the order they take an item"),
		});
		this.add_button = el(
			"button",
			{
				type: "button",
				class: "btn btn-default btn-sm",
				"aria-describedby": "sp-profile-add-hint",
				"data-testid": "rates-profile-add",
				onclick: () => this.add_profile(),
			},
			__("Add a profile")
		);
		this.add_hint = el("p", { class: "sp-hint", id: "sp-profile-add-hint" });
		this.live = new Announcer("rates-live");
		this.root = el(
			"section",
			{ class: "sp-sheet", "aria-labelledby": "sp-sheet-title", "data-testid": "rates-card" },
			el("h3", { class: "sp-sheet-title", id: "sp-sheet-title" }, __("The rate card")),
			this.summary,
			el(
				"p",
				{ class: "sp-sheet-note" },
				__(
					"Weights are in pounds and prices in US dollars. Each item takes the first profile, from the top, whose product codes fit it."
				)
			),
			this.list,
			el("div", { class: "sp-add" }, this.add_button, this.add_hint),
			this.live.root
		);
	}

	get draft_card() {
		return this.wizard.store.draft.rate_card;
	}

	get profiles() {
		const profiles = sp.is_record(this.draft_card) ? this.draft_card.profiles : null;
		return Array.isArray(profiles) ? profiles : [];
	}

	// The cards follow the draft's card: one per profile, in its order. A card that is replaced (a new import)
	// gets new cards; a profile keeps its card, and its open zones, while it is in the draft.
	refresh() {
		const card = this.draft_card;
		this.root.hidden = !sp.is_record(card);
		if (card !== this.card) {
			for (const profile_card of this.cards.values()) profile_card.root.remove();
			this.cards.clear();
			this.card = card;
		}
		if (!sp.is_record(card)) return;
		const profiles = this.profiles;
		this.summary.textContent = __("It holds {0}.", [sp.counts_words(sp.card_counts(card))]);
		keep_cards({
			entries: profiles,
			cards: this.cards,
			list: this.list,
			make: (profile) => new RateProfileCard(this, profile),
			each: (profile_card, index) => profile_card.update(index, profiles.length),
		});
		const full = profiles.length >= CARD_LIMITS.profiles;
		this.add_button.setAttribute("aria-disabled", String(full));
		this.add_hint.textContent = full ? __("A rate card holds at most {0} profiles.", [CARD_LIMITS.profiles]) : "";
	}

	add_profile() {
		const profiles = this.profiles;
		if (profiles.length >= CARD_LIMITS.profiles) return;
		const name = sp.unique_name(
			__("New profile"),
			profiles.filter(sp.is_record).map((profile) => String(profile.name ?? ""))
		);
		const profile = sp.new_card_profile(name);
		// the profile that takes every item stays last: a new one goes before it
		const last = profiles[profiles.length - 1];
		const at = sp.is_record(last) && takes_every_item(last) ? profiles.length - 1 : profiles.length;
		this.ui.open.add(profile);
		this.wizard.change((draft) => {
			if (!Array.isArray(draft.rate_card.profiles)) draft.rate_card.profiles = [];
			draft.rate_card.profiles.splice(at, 0, profile);
		});
		const added = this.cards.get(profile);
		if (added?.name) {
			added.name.focus();
			added.name.select();
		}
		this.live.say(__("Profile added: {0}.", [name]));
	}

	// Moves the card's profile to `to` (its place once it is taken out): only the order of the profiles changes.
	move(profile_card, to) {
		const profiles = this.profiles;
		const from = profiles.indexOf(profile_card.profile);
		if (from < 0 || to < 0 || to >= profiles.length || to === from) return;
		this.wizard.change((draft) => {
			draft.rate_card.profiles.splice(from, 1);
			draft.rate_card.profiles.splice(to, 0, profile_card.profile);
		});
		this.live.say(__("Profile {0} is now profile {1} of {2}.", [from + 1, to + 1, profiles.length]));
	}

	delete_profile(profile_card) {
		const profile = profile_card.profile;
		const remove = () => {
			const at = this.profiles.indexOf(profile);
			if (at < 0) return null;
			this.wizard.change((draft) => draft.rate_card.profiles.splice(at, 1));
			this.live.say(__("Profile deleted: {0}.", [String(profile.name ?? "")]));
			return at;
		};
		const focus = (at) => {
			const next = this.list.children[Math.min(at, this.list.children.length - 1)];
			(next?.querySelector('[data-testid="rates-profile-toggle"]') || this.add_button).focus();
		};
		if (!sp.card_zones(profile).length) {
			const at = remove();
			if (at !== null) focus(at);
			return;
		}
		let deleted = null;
		const dialog = frappe.confirm(__("Delete this profile, with its zones and prices?"), () => {
			deleted = remove();
		});
		// once the dialog is gone: the desk's Escape takes the focus away while the dialog closes
		dialog.$wrapper.one("hidden.bs.modal", () => {
			if (deleted === null) profile_card.delete?.focus();
			else focus(deleted);
		});
	}
}

class RateProfileCard {
	constructor(sheet, profile) {
		rate_cards += 1;
		this.uid = `sp-profile-${rate_cards}`;
		this.sheet = sheet;
		this.wizard = sheet.wizard;
		this.ui = sheet.ui;
		this.profile = profile;
		this.index = 0;
		this.zones = new Map(); // zone object -> RateZoneCard
		this.built = false;
		this.pattern_refusal = ""; // why the last pattern was not added, until the next is typed
		this.number = el("span", { class: "sp-profile-number", "aria-hidden": "true" });
		this.title = el("span", { class: "sp-profile-title" });
		this.about = el("span", { class: "sp-disclosure-about" });
		this.toggle = el(
			"button",
			{
				type: "button",
				class: "sp-disclosure sp-profile-toggle",
				"aria-expanded": "false",
				"aria-controls": `${this.uid}-body`,
				"data-testid": "rates-profile-toggle",
				onclick: () => this.set_open(!this.ui.open.has(profile)),
			},
			el("span", { class: "sp-chevron", "aria-hidden": "true" }),
			el("span", { class: "sp-disclosure-text" }, el("span", { class: "sp-profile-line" }, this.number, this.title), this.about)
		);
		const arrow = (testid, glyph, by) =>
			el("button", { type: "button", class: "sp-icon-button", "data-testid": testid, onclick: () => this.step(by) }, glyph);
		this.up = arrow("rates-profile-up", "↑", -1);
		this.down = arrow("rates-profile-down", "↓", 1);
		this.body = el("div", { class: "sp-profile-body", id: `${this.uid}-body`, hidden: true });
		this.root = el(
			"li",
			{ class: "sp-profile", "data-testid": "rates-profile" },
			el("div", { class: "sp-profile-head" }, this.toggle, el("span", { class: "sp-moves" }, this.up, this.down)),
			this.body
		);
	}

	set_open(open) {
		if (open) this.ui.open.add(this.profile);
		else this.ui.open.delete(this.profile);
		this.update(this.index, this.sheet.profiles.length);
	}

	step(by) {
		const to = this.index + by;
		if (to < 0 || to >= this.sheet.profiles.length) return; // at the top or the bottom already
		this.sheet.move(this, to);
	}

	update(index, total) {
		const profile = this.profile;
		this.index = index;
		const place = index + 1;
		const name = String(profile.name ?? "").trim();
		const shown = name || __("A profile without a name");
		this.number.textContent = String(place);
		this.title.textContent = shown;
		const zones = sp.card_zones(profile);
		const tiers = zones.reduce((sum, zone) => sum + sp.card_tiers(zone).length, 0);
		const codes = sp.card_patterns(profile).length
			? __("Product codes {0}.", [sp.and_list(sp.card_patterns(profile))])
			: __("No product codes yet, so it takes no item.");
		let holds = __("No zone yet.");
		if (zones.length) {
			const zone_words = zones.length === 1 ? __("1 zone") : __("{0} zones", [zones.length]);
			const tier_words = tiers === 1 ? __("1 weight tier") : __("{0} weight tiers", [tiers]);
			holds = __("{0} with {1}.", [zone_words, tier_words]);
		}
		this.about.textContent = __("{0} {1}", [codes, holds]);
		this.root.setAttribute("aria-label", __("Profile {0}: {1}", [place, shown]));
		this.up.setAttribute("aria-disabled", String(index === 0));
		this.down.setAttribute("aria-disabled", String(index >= total - 1));
		this.up.setAttribute("aria-label", __("Move profile {0} up", [place]));
		this.down.setAttribute("aria-label", __("Move profile {0} down", [place]));
		const open = this.ui.open.has(profile);
		this.toggle.setAttribute("aria-expanded", String(open));
		this.body.hidden = !open;
		if (open && !this.built) this.build();
		if (!open && this.built) this.unbuild();
		if (this.built) this.update_body();
	}

	// ---------------------------------------------------------- the open profile
	build() {
		const uid = this.uid;
		const profile = this.profile;
		this.name = el("input", {
			type: "text",
			id: `${uid}-name`,
			class: "form-control sp-input",
			maxlength: CARD_LIMITS.text,
			autocomplete: "off",
			spellcheck: "false",
			"aria-describedby": `${uid}-name-problem`,
			"data-testid": "rates-profile-name",
		});
		this.name.addEventListener("input", () =>
			this.wizard.change(() => {
				profile.name = this.name.value;
			})
		);
		commit_on_leave(this.name, () => {
			const trimmed = this.name.value.trim();
			if (trimmed === this.name.value) return;
			this.name.value = trimmed;
			this.wizard.change(() => {
				profile.name = trimmed;
			});
		});
		this.name_problem = el("p", { class: "sp-problem", id: `${uid}-name-problem`, "aria-live": "polite" });

		this.patterns = el("div", { class: "sp-token-box" });
		this.shown_patterns = null;
		this.pattern_input = el("input", {
			type: "text",
			id: `${uid}-pattern`,
			class: "form-control sp-input sp-pattern-input",
			autocomplete: "off",
			spellcheck: "false",
			placeholder: __("Such as F-*-PT"),
			"aria-describedby": `${uid}-pattern-problem ${uid}-pattern-help`,
			"data-testid": "rates-pattern-input",
		});
		this.pattern_input.addEventListener("input", () => {
			if (!this.pattern_refusal) return;
			this.pattern_refusal = "";
			this.pattern_problem.textContent = "";
		});
		this.pattern_input.addEventListener("keydown", (event) => {
			if (event.key === "Enter") {
				event.preventDefault();
				this.add_pattern();
			}
		});
		this.pattern_add = el(
			"button",
			{ type: "button", class: "btn btn-default btn-xs", "data-testid": "rates-pattern-add", onclick: () => this.add_pattern() },
			__("Add")
		);
		this.pattern_problem = el("p", {
			class: "sp-problem",
			id: `${uid}-pattern-problem`,
			"aria-live": "polite",
			"data-testid": "rates-pattern-problem",
		});

		this.zone_list = el("ul", { class: "sp-czones", "aria-label": __("Zones of this profile") });
		this.zone_add = el(
			"button",
			{
				type: "button",
				class: "btn btn-default btn-xs",
				"aria-describedby": `${uid}-zone-hint`,
				"data-testid": "rates-zone-add",
				onclick: () => this.add_zone(),
			},
			__("Add a zone")
		);
		this.zone_hint = el("p", { class: "sp-hint", id: `${uid}-zone-hint` });
		this.delete = el(
			"button",
			{
				type: "button",
				class: "sp-link sp-quiet",
				"data-testid": "rates-profile-delete",
				onclick: () => this.sheet.delete_profile(this),
			},
			__("Delete this profile")
		);
		this.body.replaceChildren(
			el(
				"div",
				{ class: "sp-field sp-profile-name" },
				el("label", { class: "sp-label", for: `${uid}-name` }, __("Name")),
				this.name,
				this.name_problem
			),
			el(
				"div",
				{ class: "sp-field" },
				el("label", { class: "sp-label", for: `${uid}-pattern` }, __("Product codes")),
				this.patterns,
				el("div", { class: "sp-pattern-row" }, this.pattern_input, this.pattern_add),
				this.pattern_problem,
				el(
					"p",
					{ class: "sp-hint", id: `${uid}-pattern-help` },
					__(
						"Write a product code without braces. * stands for any run of characters, ? for any one character and [AB] for one of the characters listed. Each item takes the first profile, from the top, with a pattern that fits its product code, so the profile with * goes last."
					)
				)
			),
			el(
				"div",
				{ class: "sp-field" },
				el("p", { class: "sp-label" }, __("Zones")),
				this.zone_list,
				el("div", { class: "sp-add sp-zone-add" }, this.zone_add, this.zone_hint)
			),
			el("div", { class: "sp-profile-foot" }, this.delete)
		);
		this.built = true;
	}

	// A closed profile draws nothing: its zones and their countries leave the page.
	unbuild() {
		this.body.replaceChildren();
		this.zones.clear();
		this.built = false;
		this.name = null;
	}

	update_body() {
		const profile = this.profile;
		const name = String(profile.name ?? "");
		if (document.activeElement !== this.name && this.name.value !== name) this.name.value = name;
		const key = card_name_key(name);
		const others = this.sheet.profiles.filter((other) => sp.is_record(other) && other !== profile);
		let problem = "";
		if (!key) problem = __("Give the profile a name.");
		else if (others.some((other) => card_name_key(other.name) === key)) {
			problem = __("Another profile has this name. Names that differ only in capital letters count as the same.");
		}
		this.name_problem.textContent = problem;
		this.show_patterns();
		this.pattern_problem.textContent = this.pattern_refusal;
		const zones = Array.isArray(profile.zones) ? profile.zones : [];
		const count = keep_cards({
			entries: zones,
			cards: this.zones,
			list: this.zone_list,
			make: (zone) => new RateZoneCard(this, zone),
			each: (zone_card, index) => zone_card.update(index),
		});
		this.zone_list.hidden = count === 0;
		const full = count >= CARD_LIMITS.zones;
		this.zone_add.setAttribute("aria-disabled", String(full));
		this.zone_hint.textContent = full
			? __("A profile holds at most {0} zones.", [CARD_LIMITS.zones])
			: count
			? ""
			: __("No zone yet: this profile delivers nowhere until it has one.");
		this.delete.setAttribute("aria-label", __("Delete the profile {0}", [name.trim() || String(this.index + 1)]));
	}

	// The patterns as removable chips: drawn again only when the list changed, so a chip keeps the focus.
	show_patterns() {
		const patterns = Array.isArray(this.profile.match) ? this.profile.match.map((pattern) => String(pattern ?? "")) : [];
		const key = JSON.stringify(patterns);
		if (key === this.shown_patterns) return;
		this.shown_patterns = key;
		const tokens = patterns.map((pattern, index) => ({ key: String(index), text: pattern }));
		this.patterns.replaceChildren(
			...(tokens.length
				? [
						token_list({
							tokens,
							label: __("Product code patterns of this profile"),
							testid: "rates-pattern-chip",
							remove_testid: "rates-pattern-remove",
							remove: (key, at) => this.remove_pattern(Number(key), at),
						}),
				  ]
				: [])
		);
	}

	add_pattern() {
		const text = this.pattern_input.value.trim();
		if (!text) return;
		const patterns = Array.isArray(this.profile.match) ? this.profile.match : [];
		let refusal = "";
		if (/[{}]/.test(text)) refusal = __("Write the product code without braces, such as F-CR-XYZ-*.");
		else if (text.length > CARD_LIMITS.pattern_length) {
			refusal = __("A pattern can be at most {0} characters long.", [CARD_LIMITS.pattern_length]);
		} else if (patterns.some((pattern) => String(pattern ?? "").trim().toUpperCase() === text.toUpperCase())) {
			refusal = __("This profile has that pattern already.");
		} else if (patterns.length >= CARD_LIMITS.patterns) {
			refusal = __("A profile holds at most {0} patterns.", [CARD_LIMITS.patterns]);
		}
		this.pattern_refusal = refusal;
		this.pattern_problem.textContent = refusal;
		if (refusal) return;
		this.wizard.change(() => {
			if (!Array.isArray(this.profile.match)) this.profile.match = [];
			this.profile.match.push(text);
		});
		this.pattern_input.value = "";
		this.sheet.live.say(__("Pattern added: {0}.", [text]));
	}

	remove_pattern(at, index) {
		const removed = String(this.profile.match[at] ?? "");
		this.wizard.change(() => this.profile.match.splice(at, 1));
		focus_token(this.patterns.firstChild, index, this.pattern_input);
		this.sheet.live.say(__("Pattern removed: {0}.", [removed]));
	}

	add_zone() {
		const profile = this.profile;
		const zones = Array.isArray(profile.zones) ? profile.zones : [];
		if (zones.length >= CARD_LIMITS.zones) return;
		const name = sp.unique_name(__("New zone"), zones.filter(sp.is_record).map((zone) => String(zone.name ?? "")));
		const zone = sp.new_card_zone(name);
		this.ui.open.add(zone);
		this.wizard.change(() => {
			if (!Array.isArray(profile.zones)) profile.zones = [];
			profile.zones.push(zone);
		});
		const added = this.zones.get(zone);
		if (added?.name) {
			added.name.focus();
			added.name.select();
		}
		this.sheet.live.say(__("Zone added: {0}.", [name]));
	}

	delete_zone(zone_card) {
		const zone = zone_card.zone;
		const remove = () => {
			const at = this.profile.zones.indexOf(zone);
			if (at < 0) return null;
			this.wizard.change(() => this.profile.zones.splice(at, 1));
			this.sheet.live.say(__("Zone deleted: {0}.", [String(zone.name ?? "")]));
			return at;
		};
		const focus = (at) => {
			const next = this.zone_list.children[Math.min(at, this.zone_list.children.length - 1)];
			(next?.querySelector('[data-testid="rates-zone-toggle"]') || this.zone_add).focus();
		};
		const countries = Array.isArray(zone.countries) && zone.countries.length > 0;
		const priced = sp.card_tiers(zone).some((tier) => sp.is_number(tier.price));
		if (!countries && !priced) {
			const at = remove();
			if (at !== null) focus(at);
			return;
		}
		let deleted = null;
		const dialog = frappe.confirm(__("Delete this zone, with its countries and prices?"), () => {
			deleted = remove();
		});
		// once the dialog is gone: the desk's Escape takes the focus away while the dialog closes
		dialog.$wrapper.one("hidden.bs.modal", () => {
			if (deleted === null) zone_card.delete?.focus();
			else focus(deleted);
		});
	}

	// Another zone of this profile that lists the country, or null: a country is in one zone of a profile at most.
	owner_of(code, zone) {
		return (
			sp
				.card_zones(this.profile)
				.find((other) => other !== zone && Array.isArray(other.countries) && other.countries.includes(code)) || null
		);
	}
}
