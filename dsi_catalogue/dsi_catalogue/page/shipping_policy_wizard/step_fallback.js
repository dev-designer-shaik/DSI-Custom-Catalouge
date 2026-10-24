// Part of the Shipping policy wizard page: shipping_policy_wizard.js stitches it in and says how.
//
// The "If the carrier can't quote" tab's body. When the carrier gives no price for a whole order, the rules are
// not used: the zone's fallback sets what customers pay. A card per zone of the draft, in its order: its choice
// (the policy default, no delivery, a fixed amount, free, the rate card), the amount for a fixed charge, and
// one sentence of what that means for customers, with the price it comes to on the right, as a rule card shows
// its price. A Not shipped zone only says that delivery is never offered there. Below the zones, the policy
// default (for every zone that uses it) and "Anything not covered". The words are sp.fallback_sentence and its
// kin (wizard_policy_words.js), which the Review step prints too.
//
// With the rate card as the price source the carrier is not asked, so none of the choices is used (ruling P89):
// a quiet line says so at the top, and the cards' sentences and figures go quiet. The choices can still be made,
// for when the carrier is the price source again.
//
// The cards change only the fallback of their zone (or the policy's default_fallback and not_covered), through
// wizard.change. A fixed amount is kept to the cents the ERP keeps, rounded as the ERP rounds it (the desk's own
// flt, as the Rules step's limits are). Choosing "Charge a fixed amount" leaves the amount blank until one is
// typed, unless one was typed before; what is typed that is not an amount stays in its field and says so
// (wizard.ui.fallback.typed keeps it across a tab switch), and the draft keeps the last amount it held.

let fallback_cards = 0; // ids for the labels of each card

// The key of the policy default's typed amount in wizard.ui.fallback.typed (the zones' are the zone objects).
const POLICY_DEFAULT = {};

class FallbackView {
	constructor(wizard, panel) {
		this.wizard = wizard;
		this.ui = wizard.ui.fallback;
		this.ui.typed = this.ui.typed || new WeakMap(); // zone (or POLICY_DEFAULT) -> what is typed, not an amount
		this.cards = new Map(); // zone object -> FallbackCard
		this.list = el("div", { class: "sp-zones sp-fallbacks" });
		this.empty = el(
			"p",
			{ class: "sp-empty-state" },
			__("No zones yet. Each zone you add in the Zones step gets its choice here.")
		);
		this.policy_default = new FallbackCard(this, null);
		this.not_covered = new NotCoveredCard(this);
		this.unused = el(
			"p",
			{ class: "sp-mode-note", "data-testid": "fallback-unused" },
			__("While the price source is the rate card the carrier is not asked, so none of these choices is used.")
		);
		this.root = el(
			"div",
			{ class: "sp-fallback-step" },
			el(
				"p",
				{ class: "sp-step-intro" },
				__(
					"When the carrier gives no price for a whole order, the rules are not used: the zone's choice below sets what customers pay. Tick “The carrier can't quote” in Try an order to see it."
				)
			),
			this.unused,
			this.list,
			this.empty,
			el("div", { class: "sp-zones sp-fallback-policy" }, this.policy_default.root, this.not_covered.root)
		);
		panel.append(this.root);
		this.refresh();
	}

	get zones() {
		const zones = this.wizard.store.draft.zones;
		return Array.isArray(zones) ? zones : [];
	}

	refresh() {
		const count = keep_cards({
			entries: this.zones,
			cards: this.cards,
			list: this.list,
			make: (zone) => new FallbackCard(this, zone),
			each: (card) => card.update(),
		});
		this.empty.hidden = count > 0;
		this.policy_default.update();
		this.not_covered.update();
		const by_card = this.wizard.price_source() === "rate_card";
		this.unused.hidden = !by_card;
		this.root.classList.toggle("is-unused", by_card);
	}
}

// What a fallback comes to, on the right of its card: an amount in brass, Free in sage, Not offered in brick, or
// a quiet word. The sentence under the choice says the same in words.
function show_figure(figure, outcome) {
	figure.className = "sp-fallback-figure";
	let text = "";
	if (outcome.kind === "fixed" && outcome.amount > 0) {
		text = sp.money(outcome.amount);
		figure.classList.add("is-paid");
	} else if (outcome.kind === "fixed" || outcome.kind === "free") {
		text = __("Free");
		figure.classList.add("is-free");
	} else if (outcome.kind === "none") {
		text = __("Not offered");
		figure.classList.add("is-not-offered");
	} else if (outcome.kind === "rate_card") {
		text = __("Rate card");
	} else if (outcome.kind === "carrier") {
		text = __("Carrier's price");
	}
	figure.textContent = text;
}

// The choice of one zone, or of the policy default (zone null), for when the carrier cannot quote.
class FallbackCard {
	constructor(view, zone) {
		fallback_cards += 1;
		const uid = (this.uid = `sp-fallback-${fallback_cards}`);
		this.view = view;
		this.wizard = view.wizard;
		this.zone = zone;
		this.kept = null; // the amount kept when it differs from the one typed (the ERP keeps cents)
		const choices = zone ? FALLBACK_CHOICES : FALLBACK_CHOICES.slice(1);

		this.title = el("h3", { class: "sp-fallback-title", id: `${uid}-title` });
		this.states = el("span", { class: "sp-states", "data-testid": "fallback-state" });
		this.figure = el("p", { class: "sp-fallback-figure", "aria-hidden": "true", "data-testid": "fallback-figure" });
		this.note = el("p", { class: "sp-hint", "data-testid": "fallback-note" });

		this.amount = el("input", {
			type: "text",
			class: "form-control sp-input sp-amount",
			inputmode: "decimal",
			autocomplete: "off",
			"aria-describedby": `${uid}-amount-problem`,
			"data-testid": "fallback-amount",
		});
		this.amount.addEventListener("input", () => this.type_amount(this.amount.value));
		this.amount_box = el(
			"span",
			{ class: "sp-amount-box" },
			el("span", { class: "sp-amount-sign", "aria-hidden": "true" }, "$"),
			this.amount
		);
		this.amount_problem = el("p", {
			class: "sp-problem",
			id: `${uid}-amount-problem`,
			"aria-live": "polite",
			"data-testid": "fallback-problem",
		});
		this.card_note = el(
			"span",
			{ class: "sp-option-note", id: `${uid}-card-note` },
			__("There is no rate card yet. Add one in the Rates step.")
		);

		this.radios = [];
		const row = (choice) => {
			const input = el("input", {
				type: "radio",
				name: `${uid}-method`,
				value: choice.method,
				"data-testid": "fallback-method",
			});
			input.addEventListener("change", () => {
				if (input.checked) this.choose(choice.method);
			});
			this.radios.push(input);
			const after = { fixed: [this.amount_box], rate_card: [this.card_note] }[choice.method] || [];
			return el(
				"div",
				{ class: "sp-option-row" },
				el("label", { class: "sp-option" }, input, el("span", { class: "sp-option-text" }, choice.label)),
				...after
			);
		};
		this.choices = el(
			"fieldset",
			{ class: "sp-choices sp-fallback-choices" },
			el("legend", { class: "sp-sr-only", id: `${uid}-legend` }),
			choices.map(row)
		);
		this.sentence = el("p", { class: "sp-fallback-sentence", "data-testid": "fallback-sentence" });

		this.root = el(
			"section",
			{
				class: zone ? "sp-zone sp-fallback" : "sp-zone sp-fallback is-policy",
				"aria-labelledby": `${uid}-title`,
				"data-testid": zone ? "fallback-zone" : "fallback-policy-default",
			},
			el(
				"div",
				{ class: "sp-fallback-head" },
				el("div", { class: "sp-card-title" }, this.title, this.states),
				this.figure
			),
			this.note,
			this.choices,
			this.amount_problem,
			this.sentence
		);
	}

	// The fallback this card changes: the zone's, or the policy default. One that is missing (a draft restored
	// from storage may hold anything) is made as a new zone or a new policy has it, keys in the server's order.
	target(draft) {
		if (!this.zone) {
			if (!sp.is_record(draft.default_fallback)) draft.default_fallback = { method: "no_delivery", amount: 0 };
			return draft.default_fallback;
		}
		if (!sp.is_record(this.zone.fallback)) this.zone.fallback = { method: "policy_default", amount: 0 };
		return this.zone.fallback;
	}

	fallback() {
		const fallback = this.zone ? this.zone.fallback : this.wizard.store.draft.default_fallback;
		return sp.is_record(fallback) ? fallback : {};
	}

	method() {
		return this.fallback().method || (this.zone ? "policy_default" : "no_delivery");
	}

	get typed_key() {
		return this.zone || POLICY_DEFAULT;
	}

	// A fixed amount is blank until it is typed, unless one was typed before (a choice switched away from keeps
	// its amount, as the Rules step keeps a charge's formula); a blank one goes back to 0 when another is chosen.
	choose(method) {
		this.view.ui.typed.delete(this.typed_key);
		this.kept = null;
		this.wizard.change((draft) => {
			const fallback = this.target(draft);
			const before = fallback.method;
			fallback.method = method;
			if (method === "fixed" && before !== "fixed" && !(sp.is_number(fallback.amount) && fallback.amount > 0)) {
				fallback.amount = null;
			} else if (method !== "fixed" && !sp.is_number(fallback.amount)) {
				fallback.amount = 0;
			}
		});
	}

	// An amount typed: a number (or nothing) goes into the draft at once, in cents as the ERP keeps it; anything
	// else stays in its field, is not in the draft, and says so.
	type_amount(text) {
		const typed = this.view.ui.typed;
		const reading = sp.read_number(text);
		this.kept = null;
		if (!reading.ok) {
			typed.set(this.typed_key, text);
			this.update();
			return;
		}
		typed.delete(this.typed_key);
		const amount = reading.value === null ? null : flt(reading.value, 2);
		if (reading.value !== null && amount !== reading.value) this.kept = amount;
		this.wizard.change((draft) => {
			this.target(draft).amount = amount;
		});
	}

	// What the card says, from the draft. The amount field is left alone while it is typed into.
	update() {
		const draft = this.wizard.store.draft;
		const zone = this.zone;
		const name = zone ? String(zone.name ?? "").trim() || __("A zone without a name") : __("The policy default");
		this.title.textContent = name;
		this.choices.firstChild.textContent = zone
			? __("If the carrier can't quote in {0}", [name])
			: __("The policy default, if the carrier can't quote");
		this.amount.setAttribute(
			"aria-label",
			zone ? __("Fixed amount in {0}, in dollars", [name]) : __("Fixed amount of the policy default, in dollars")
		);
		const states = [];
		if (zone && !zone.enabled) states.push(el("span", { class: "sp-state" }, __("Off")));
		if (zone?.not_shipped) states.push(el("span", { class: "sp-state is-not-shipped" }, __("Not shipped")));
		if (zone?.everywhere_else) states.push(el("span", { class: "sp-state" }, __("Everywhere else")));
		this.states.replaceChildren(...states);
		this.root.classList.toggle("is-off", Boolean(zone && !zone.enabled));

		// A Not shipped zone never offers delivery, so there is nothing to fall back to.
		const closed = Boolean(zone?.not_shipped);
		this.choices.hidden = closed;
		const method = this.method();
		for (const radio of this.radios) radio.checked = radio.value === method;
		// "Use the rate card" says, while there is none, that a card has to be added first
		const no_card = !sp.is_record(draft.rate_card);
		this.card_note.hidden = !no_card;
		const card_radio = this.radios.find((radio) => radio.value === "rate_card");
		if (no_card) card_radio.setAttribute("aria-describedby", `${this.uid}-card-note`);
		else card_radio.removeAttribute("aria-describedby");
		const fixed = method === "fixed" && !closed;
		this.amount_box.hidden = !fixed;
		const typed = this.view.ui.typed.get(this.typed_key);
		const amount = this.fallback().amount;
		if (document.activeElement !== this.amount) {
			this.amount.value = typed ?? (sp.is_number(amount) ? amount.toFixed(2) : "");
		}
		let problem = "";
		if (fixed && typed !== undefined) {
			problem = sp.read_number(typed).comma
				? __("Use a period for decimals.")
				: __("Type an amount of 0 or more, such as 35.");
		} else if (fixed && !sp.is_number(amount)) {
			problem = __("Type the amount customers pay, such as 35.");
		} else if (fixed && this.kept !== null) {
			problem = __("The ERP keeps cents, so this is {0}.", [sp.money(this.kept)]);
		}
		this.amount_problem.textContent = problem;

		let note = "";
		if (closed) note = "";
		else if (zone && !zone.enabled) note = __("This zone is off: its choice applies once it is on.");
		else if (!zone) note = this.users_note();
		this.note.textContent = note;

		if (closed) {
			show_figure(this.figure, { kind: "none" });
			this.sentence.textContent = __("Delivery is never offered here: the zone is not shipped to.");
			return;
		}
		show_figure(this.figure, sp.fallback_outcome(draft, zone));
		this.sentence.textContent = sp.fallback_sentence(draft, zone);
	}

	// Which zones use the policy default (a Not shipped zone never falls back).
	users_note() {
		const users = this.view.zones
			.filter(
				(zone) =>
					sp.is_record(zone) &&
					!zone.not_shipped &&
					((sp.is_record(zone.fallback) && zone.fallback.method) || "policy_default") === "policy_default"
			)
			.map((zone) => String(zone.name ?? "").trim() || __("a zone without a name"));
		if (!users.length) return __("No zone uses the policy default now.");
		return users.length === 1
			? __("{0} uses it.", [users[0]])
			: __("{0} use it.", [sp.and_list(users)]);
	}
}

// "Anything not covered": what customers pay when no zone or no rule of its zone fits an order.
class NotCoveredCard {
	constructor(view) {
		fallback_cards += 1;
		const uid = `sp-fallback-${fallback_cards}`;
		this.wizard = view.wizard;
		this.figure = el("p", { class: "sp-fallback-figure", "aria-hidden": "true", "data-testid": "fallback-figure" });
		this.radios = NOT_COVERED_CHOICES.map((choice) => {
			const input = el("input", {
				type: "radio",
				name: `${uid}-not-covered`,
				value: choice.method,
				"data-testid": "not-covered-option",
			});
			input.addEventListener("change", () => {
				if (input.checked) {
					this.wizard.change((draft) => {
						draft.not_covered = choice.method;
					});
				}
			});
			return [input, choice];
		});
		this.sentence = el("p", { class: "sp-fallback-sentence", "data-testid": "fallback-sentence" });
		this.root = el(
			"section",
			{ class: "sp-zone sp-fallback is-policy", "aria-labelledby": `${uid}-title`, "data-testid": "not-covered" },
			el(
				"div",
				{ class: "sp-fallback-head" },
				el("div", { class: "sp-card-title" }, el("h3", { class: "sp-fallback-title", id: `${uid}-title` }, __("Anything not covered"))),
				this.figure
			),
			el("p", { class: "sp-hint" }, __("For an order that no zone covers, or that no rule of its zone fits.")),
			el(
				"fieldset",
				{ class: "sp-choices sp-fallback-choices" },
				el("legend", { class: "sp-sr-only" }, __("Anything not covered")),
				this.radios.map(([input, choice]) =>
					el(
						"div",
						{ class: "sp-option-row" },
						el("label", { class: "sp-option" }, input, el("span", { class: "sp-option-text" }, choice.label))
					)
				)
			),
			this.sentence
		);
	}

	update() {
		const draft = this.wizard.store.draft;
		const passes = !draft.not_covered || draft.not_covered === "pass_through";
		for (const [input, choice] of this.radios) {
			input.checked = choice.method === (passes ? "pass_through" : "no_delivery");
		}
		show_figure(this.figure, { kind: passes ? "carrier" : "none" });
		this.sentence.textContent = sp.not_covered_sentence(draft);
	}
}

const step_fallback = {
	render(wizard, panel) {
		wizard.ui.fallback.view = new FallbackView(wizard, panel);
	},
	refresh(wizard, panel) {
		wizard.ui.fallback.view?.refresh();
	},
	priced(wizard, panel, answer) {},
};
