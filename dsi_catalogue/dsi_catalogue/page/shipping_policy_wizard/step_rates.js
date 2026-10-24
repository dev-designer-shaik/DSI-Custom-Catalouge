// Part of the Shipping policy wizard page: shipping_policy_wizard.js stitches it in and says how.
//
// The Rates tab's body: where the interim rate card is pasted in and read back. It reads top down:
//	- the price source, the carrier or the rate card, a plain sentence for each (draft.price_source); with the rate
//	  card chosen and no card in the draft it says there is none yet (the Review step has the server's word);
//	- a quiet note when nothing uses the card: the carrier is the price source and no fallback says Use the rate card;
//	- what stops the card from being applied, as the server finds it (preview_changes): its sentences about the card
//	  (each "Rate card: ..." without those words, and the countries of the card the ERP does not know), asked when
//	  the step is drawn and 250 ms after each change while it is shown, the last answer dimmed until the next;
//	- the import of the Shopify export (wizard_rates_import.js);
//	- the card itself, to read and to edit (wizard_rates_card.js and wizard_rates_zone.js).
// The words of the card are sp.card_counts and its kin (wizard_rate_card.js), which the Review step uses too. What
// the body shows that is not the draft lives in wizard.ui.rates: `view` (the view drawn last), `import` (the
// import's files, preview and outcome), `open`, `all_countries` and `typed` (the card view's), and the card's
// problems with the draft they were found for.

const RATES_CHECK_DELAY = 250; // ms after the last change before the server checks the card again

// The website prices from the ERP's answer only while the policy is on with the rate card as the price source; with
// the carrier it charges the carrier's own prices, whatever the policy says (ruling P100).
const PRICE_SOURCES = [
	{
		value: "carrier",
		label: __("Carrier"),
		sentence: __(
			"Customers pay the carrier's own price: for now the website applies the policy only with the rate card as the price source. The rules are kept, and Try an order shows what they would charge; there the rate card is used only where a fallback says Use the rate card."
		),
	},
	{
		value: "rate_card",
		label: __("Rate card"),
		sentence: __("Customers pay from the rate card below once the policy is on; the carrier is not asked."),
	},
];

class RatesView {
	constructor(wizard, panel) {
		this.wizard = wizard;
		this.ui = wizard.ui.rates;
		this.ui.view = this; // the view drawn last: answers that arrive later go to it
		this.names = new Map(wizard.state.countries.map((country) => [country.code, country.name]));

		this.sources = PRICE_SOURCES.map((source) => {
			const input = el("input", {
				type: "radio",
				name: "sp-price-source",
				value: source.value,
				"aria-describedby": `sp-price-source-${source.value}`,
				"data-testid": "price-source",
			});
			input.addEventListener("change", () => {
				if (input.checked) this.set_source(source.value);
			});
			return [input, source];
		});
		this.no_card = el(
			"p",
			{ class: "sp-warning", "data-testid": "rates-no-card" },
			__("There is no rate card yet. Import one below.")
		);
		this.not_used = el(
			"p",
			{ class: "sp-quiet-note", "data-testid": "rates-not-used-note" },
			__("Nothing uses the rate card now: the price source is the carrier, and no fallback says Use the rate card.")
		);
		this.problem_list = el("ul", { class: "sp-rates-problem-list" });
		this.problems = el(
			"section",
			{
				class: "sp-rates-problems",
				"aria-labelledby": "sp-rates-problems-title",
				"data-testid": "rates-problems",
				hidden: true,
			},
			el("h3", { class: "sp-rates-problems-title", id: "sp-rates-problems-title" }, __("Before the rate card can be applied")),
			this.problem_list
		);
		this.import = new RatesImport(this);
		this.card = new RateCardView(this);
		panel.append(
			el(
				"p",
				{ class: "sp-step-intro" },
				__(
					"While the carrier account is blocked, customers can pay from a rate card instead: the Shopify store's table of prices by weight, kind of product and country."
				)
			),
			el(
				"fieldset",
				{ class: "sp-source" },
				el("legend", { class: "sp-source-title" }, __("Price source")),
				this.sources.map(([input, source]) =>
					el(
						"label",
						{ class: "sp-source-option" },
						input,
						el(
							"span",
							{ class: "sp-source-text" },
							el("span", { class: "sp-source-label" }, source.label),
							el("span", { class: "sp-source-sentence", id: `sp-price-source-${source.value}` }, source.sentence)
						)
					)
				),
				this.no_card,
				this.not_used
			),
			this.problems,
			this.import.root,
			this.card.root
		);
		this.refresh();
		this.check();
	}

	country_name(code) {
		return this.names.get(code) || String(code);
	}

	// The price source chosen. The carrier, for a saved policy that says nothing (or "") and so prices from the
	// carrier, is put back as it was saved, so that a choice taken back leaves the draft clean.
	set_source(value, { focus = false } = {}) {
		const saved = this.wizard.store.original?.price_source;
		const changed = this.wizard.change((draft) => {
			draft.price_source = value === "carrier" && (saved || "carrier") === "carrier" ? saved : value;
		});
		if (!changed) this.show_source(); // while Apply writes, the switch stays
		if (focus) this.sources.find(([input]) => input.value === value)?.[0].focus();
	}

	show_source() {
		const source = this.wizard.price_source();
		for (const [input] of this.sources) input.checked = input.value === source;
	}

	refresh() {
		const draft = this.wizard.store.draft;
		const has_card = sp.is_record(draft.rate_card);
		this.show_source();
		this.no_card.hidden = has_card || this.wizard.price_source() !== "rate_card";
		this.not_used.hidden = !has_card || sp.uses_the_card(draft);
		this.import.show();
		this.card.refresh();
		this.show_problems();
	}

	// The card's problems as the server finds them, now or 250 ms after the last change (soon). Each request gets
	// a number, and only the newest one's answer is shown.
	check({ soon = false } = {}) {
		const ui = this.ui;
		const wizard = this.wizard;
		clearTimeout(ui.check_timer);
		const sequence = (ui.check_sequence = (ui.check_sequence || 0) + 1);
		if (!sp.is_record(wizard.store.draft.rate_card)) {
			ui.card_problems = [];
			ui.problems_for = JSON.stringify(wizard.store.draft);
			this.show_problems();
			return;
		}
		const ask = () => {
			const draft = wizard.store.draft;
			const key = JSON.stringify(draft);
			wizard.call("preview_changes", { draft }, { quiet: true }).then(
				(answer) => {
					if (sequence !== ui.check_sequence) return;
					const problems = Array.isArray(answer?.problems) ? answer.problems.map(String) : [];
					// the card's own sentences (sp.is_card_problem: the patterns the Review's "Fix it" sends to this
					// step, less the price source's, which the switch says itself)
					ui.card_problems = problems
						.filter((problem) => sp.is_card_problem(problem))
						.map((problem) => problem.replace(/^Rate card: /, ""));
					ui.problems_for = key;
					ui.view?.show_problems();
				},
				() => {} // the Review step says when the draft cannot be checked
			);
		};
		if (soon) {
			ui.check_timer = setTimeout(() => {
				if (wizard.store.step === "rates") ask();
			}, RATES_CHECK_DELAY);
		} else ask();
	}

	// Drawn again only when the sentences change; dimmed while they are for an older draft.
	show_problems() {
		const problems = this.ui.card_problems || [];
		this.problems.hidden = !problems.length;
		this.problems.classList.toggle("is-stale", this.ui.problems_for !== JSON.stringify(this.wizard.store.draft));
		const said = JSON.stringify(problems);
		if (said === this.problems_said) return;
		this.problems_said = said;
		this.problem_list.replaceChildren(
			...problems.map((problem) => el("li", { class: "sp-rates-problem", "data-testid": "rates-problem" }, problem))
		);
	}
}

const step_rates = {
	render(wizard, panel) {
		wizard.ui.rates.view = new RatesView(wizard, panel);
	},
	refresh(wizard, panel) {
		wizard.ui.rates.view?.refresh();
		wizard.ui.rates.view?.check({ soon: true });
	},
	priced(wizard, panel, answer) {},
};
