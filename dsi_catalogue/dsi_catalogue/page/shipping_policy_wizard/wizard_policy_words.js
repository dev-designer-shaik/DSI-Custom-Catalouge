// Part of the Shipping policy wizard page: shipping_policy_wizard.js stitches it in and says how.
//
// The words of the policy beyond its rules, as plain text: what the "If the carrier can't quote" step says of
// each choice, and what the Review step prints. Exported on dsi_catalogue.sp:
//	sp.fallback_choices, sp.not_covered_choices  the choices of a zone's fallback (the policy default has all
//	                                    but the first) and of "Anything not covered", with the engine's labels
//	sp.fallback_outcome(policy, zone)  what customers meet when the carrier cannot quote, as the engine works
//	                                    it out (_fallback_choice): {kind: "fixed" | "free" | "rate_card" | "none"
//	                                    | "missing", amount, by_default}; zone null is the policy default itself
//	sp.fallback_sentence(policy, zone)  that in one sentence: the Fallback step and the Review step say the same
//	sp.not_covered_sentence(policy)    what "Anything not covered" means for customers
//	sp.change_lines(changes, before, after)  what preview_changes (or apply_policy) says will change, as lines:
//	                                    {label, text}, or {label, from, to} for a setting
//	sp.change_text(line)               one of those lines as plain text
//	sp.rule_statement(rule)            a rule as the Review says it: the words of its card in the Rules step
//	                                    (sp.rule_words, wizard_rule_words.js) with commas, and a rule for an item
//	                                    set says what the Rules step's asterisk says (ruling P90)
//	sp.policy_summary(policy, country_name)  the whole policy in plain words, zone by zone (and, with the rate
//	                                    card as the price source, the card's own lines first)
//	sp.problem_step(sentence)          the step that owns a problem preview_changes found, or null when unsure
//	sp.is_card_problem(sentence)       whether the sentence is about the rate card itself (the Rates step lists it)

const FALLBACK_CHOICES = [
	{ method: "policy_default", label: __("Use the policy default") },
	{ method: "no_delivery", label: __("Don't offer delivery") },
	{ method: "fixed", label: __("Charge a fixed amount") },
	{ method: "free", label: __("Free") },
	{ method: "rate_card", label: __("Use the rate card") },
];

const NOT_COVERED_CHOICES = [
	{ method: "pass_through", label: __("Pass the carrier price through") },
	{ method: "no_delivery", label: __("Don't offer delivery") },
];

// How the end of a fallback reads inside a sentence, by the kind fallback_outcome gives.
const FALLBACK_CLAUSES = {
	fixed: (outcome) => __("customers pay a fixed {0}", [sp.money(outcome.amount)]),
	free: () => __("delivery is free"),
	rate_card: () => __("customers pay what the rate card charges"),
	none: () => __("delivery is not offered"),
	missing: () => __("customers pay a fixed amount that is not typed yet"),
};

// A setting of the policy as a line says it, before and after the change.
const SETTING_WORDS = {
	active: { label: __("Active"), value: (policy) => (policy?.active ? __("on") : __("off")) },
	default_fallback: { label: __("The policy default"), value: (policy) => default_words(policy) },
	not_covered: {
		label: __("Anything not covered"),
		value: (policy) => (passes_through(policy) ? __("pass the carrier price through") : __("don't offer delivery")),
	},
	price_source: {
		label: __("Price source"),
		value: (policy) => (policy?.price_source === "rate_card" ? __("rate card") : __("carrier")),
	},
};

// "Anything not covered" passes the carrier's price through unless it says otherwise (the engine reads a
// missing setting so).
function passes_through(policy) {
	return !policy?.not_covered || policy.not_covered === "pass_through";
}

function default_words(policy) {
	const outcome = sp.fallback_outcome(policy, null);
	if (outcome.kind === "fixed") return __("charge a fixed {0}", [sp.money(outcome.amount)]);
	if (outcome.kind === "missing") return __("charge a fixed amount");
	if (outcome.kind === "free") return __("free");
	if (outcome.kind === "rate_card") return __("use the rate card");
	return __("don't offer delivery");
}

// A rule of preview_changes' lists, {index, name, label, zone}, as "Rule 3 (Gulf at the carrier price)".
function rule_reference(entry) {
	const number = Number.isInteger(entry.index) ? entry.index + 1 : "?";
	const label = String(entry.label ?? "").trim();
	if (label) return __("Rule {0} ({1})", [number, label]);
	const zone = String(entry.zone ?? "").trim();
	return zone ? __("Rule {0} (in {1})", [number, zone]) : __("Rule {0}", [number]);
}

// The sentences of preview_changes about the rate card itself: its own (validate_card's, after "Rate card: "), a card
// that cannot be saved, and its countries the ERP does not know. The Rates step lists them (sp.is_card_problem); the
// price source's own sentences belong to the Rates step too, which says them with its switch.
const CARD_PROBLEMS = [/^Rate card: /, /^The rate card cannot be saved/, /^Country code \S+ in zone /];

// Which step owns a sentence of preview_changes, from the way the engine and the wizard's methods start them
// (shipping_engine.validate_policy, shipping_policy_wizard._problems): the first pattern that fits wins.
const PROBLEM_STEPS = [
	...CARD_PROBLEMS.map((pattern) => [pattern, "rates"]),
	[/^The setting for "Price source"/, "rates"],
	[/^The price source is the rate card/, "rates"],
	[/^The policy default /, "fallback"],
	[/^The setting for "Anything not covered"/, "fallback"],
	[
		/^Zone (?:number \d+|".*") (?:has a choice for when|uses the rate card when|charges a fixed amount|has an amount below zero for when) /,
		"fallback",
	],
	[/^Rule \d+ /, "rules"],
	[/^(?:Item set |Item sets |More than one item set )/, "rules"],
	[/^(?:Zone |Zones |More than one zone |More than one enabled zone |The country \S+ is in more than one )/, "zones"],
	[/^Country code \S+ is not a country/, "zones"],
];

Object.assign(sp, {
	fallback_choices: FALLBACK_CHOICES,
	not_covered_choices: NOT_COVERED_CHOICES,

	fallback_outcome(policy, zone) {
		let source = sp.is_record(policy?.default_fallback) ? policy.default_fallback : {};
		let method = source.method || "no_delivery";
		let by_default = false;
		if (zone) {
			const own = sp.is_record(zone.fallback) ? zone.fallback : {};
			if ((own.method || "policy_default") !== "policy_default") {
				source = own;
				method = own.method;
			} else by_default = true;
		}
		if (method === "free" || method === "rate_card") return { kind: method, amount: null, by_default };
		if (method === "fixed") {
			const amount = source.amount;
			const kind = sp.is_number(amount) && amount >= 0 ? "fixed" : "missing";
			return { kind, amount: kind === "fixed" ? amount : null, by_default };
		}
		return { kind: "none", amount: null, by_default };
	},

	fallback_sentence(policy, zone) {
		const outcome = sp.fallback_outcome(policy, zone);
		const clause = FALLBACK_CLAUSES[outcome.kind](outcome);
		if (!zone) return __("When a zone uses the policy default and the carrier cannot quote, {0}.", [clause]);
		if (outcome.by_default) return __("If the carrier cannot quote, the policy default applies: {0}.", [clause]);
		if (outcome.kind === "fixed") {
			return __("If the carrier cannot quote, customers here pay a fixed {0}.", [sp.money(outcome.amount)]);
		}
		if (outcome.kind === "free") return __("If the carrier cannot quote, delivery here is free.");
		if (outcome.kind === "rate_card") {
			return __("If the carrier cannot quote, customers here pay what the rate card charges.");
		}
		if (outcome.kind === "missing") {
			return __("If the carrier cannot quote, customers here pay a fixed amount that is not typed yet.");
		}
		return __("If the carrier cannot quote, delivery is not offered here.");
	},

	// "In GCC, for every item, charge the carrier's price, rounded up", and for an item set "In GCC, for orders
	// made up only of items in Watches, charge nothing (free)": the Review is what the owner reads to go live, so
	// it says the asterisk's condition in the sentence. Its zone and set are named as the ERP keeps them.
	rule_statement(rule) {
		const words = sp.rule_words(rule);
		const zone = String(rule.zone ?? "").trim() || __("choose a zone");
		const set = String(rule.item_set ?? "").trim();
		const items = set ? __("orders made up only of items in {0}", [set]) : words.items;
		let text = __("In {0}, for {1}, charge {2}", [zone, items, words.charge]);
		if (words.rounded) text += __(", rounded up");
		return words.limits ? __("{0} (only when: {1})", [text, words.limits]) : text;
	},

	not_covered_sentence(policy) {
		return passes_through(policy)
			? __("When no rule fits an order, customers pay the carrier's price.")
			: __("When no rule fits an order, delivery is not offered.");
	},

	change_lines(changes, before = {}, after = {}) {
		if (!sp.is_record(changes)) return [];
		const lines = [];
		const names = (list) => (Array.isArray(list) ? list.map(String) : []);
		const listed = (part, labels) => {
			const found = sp.is_record(changes[part]) ? changes[part] : {};
			let any = false;
			for (const [key, label] of labels) {
				const entries = names(found[key]);
				if (!entries.length) continue;
				lines.push({ label, text: sp.and_list(entries) });
				any = true;
			}
			return any;
		};
		listed("zones", [
			["added", __("Zones added")],
			["removed", __("Zones removed")],
			["changed", __("Zones changed")],
		]);
		listed("item_sets", [
			["added", __("Item sets added")],
			["removed", __("Item sets removed")],
			["changed", __("Item sets changed")],
		]);
		const rules = sp.is_record(changes.rules) ? changes.rules : {};
		for (const [key, label] of [
			["added", __("Rules added")],
			["removed", __("Rules removed")],
			["changed", __("Rules changed")],
		]) {
			const entries = Array.isArray(rules[key]) ? rules[key].filter(sp.is_record) : [];
			if (entries.length) lines.push({ label, text: sp.and_list(entries.map(rule_reference)) });
		}
		if (rules.order_changed === true) lines.push({ label: "", text: __("The rules were reordered.") });
		const profiles = listed("rate_card", [
			["added", __("Rate card profiles added")],
			["removed", __("Rate card profiles removed")],
			["changed", __("Rate card profiles changed")],
		]);
		for (const key of Array.isArray(changes.settings) ? changes.settings : []) {
			if (key === "rate_card") {
				// the profiles' own lines say it, except what they cannot show (its profiles trading places, say)
				if (!profiles) lines.push({ label: __("Rate card"), text: __("changed") });
				continue;
			}
			const words = SETTING_WORDS[key];
			if (words) lines.push({ label: words.label, from: words.value(before), to: words.value(after) });
		}
		return lines;
	},

	change_text(line) {
		const said = "from" in line ? __("{0} → {1}", [line.from, line.to]) : line.text;
		return line.label ? __("{0}: {1}", [line.label, said]) : said;
	},

	// {source, unused, card, zones: [{name, states: [{kind, text}], where, read, off, never, rules, no_rules,
	// fallback}], closing}: plain sentences, the zones in the draft's order. `country_name(code)` names a country.
	// With the rate card as the price source (ruling P89) the carrier is not asked: `source` says that the rules
	// and the fallbacks are not used, and `unused` is true, for the step to show them quietly; they are still
	// said, as they stand in the draft. `card` is then the card's own lines (sp.card_summary, wizard_rate_card.js:
	// its profiles, their zones and the prices of each in words, and the countries not delivered to), else null.
	policy_summary(policy, country_name = (code) => code) {
		const zones = (Array.isArray(policy?.zones) ? policy.zones : []).filter(sp.is_record);
		const rules = Array.isArray(policy?.rules) ? policy.rules : [];
		const by_card = policy?.price_source === "rate_card";
		const text = (value) => String(value ?? "").trim();
		const summary = {
			source: by_card
				? __("Prices come from the rate card: the carrier is not asked, and the rules and fallbacks below are not used.")
				: __("Prices start from the carrier's quote for each order."),
			unused: by_card,
			card: by_card ? sp.card_summary(policy, country_name) : null,
			zones: zones.map((zone) => {
				const name = text(zone.name);
				const codes = Array.isArray(zone.countries) ? zone.countries.map(String) : [];
				const line = text(zone.customer_line);
				const message = text(zone.not_offered_message);
				const priced = Boolean(zone.enabled) && !zone.not_shipped;
				let where = __("No country yet.");
				if (zone.everywhere_else) where = __("Every other country.");
				else if (codes.length) where = __("{0}.", [sp.and_list(codes.map((code) => country_name(code)))]);
				// the ERP keeps a zone's name, and the zone a rule names, without the spaces around them: so are they
				// matched here (the Zones step writes a name as it is typed, spaces and all, to the zone and its rules)
				const own = name ? rules.filter((rule) => sp.is_record(rule) && text(rule.zone) === name) : [];
				return {
					name: name || __("A zone without a name"),
					states: [
						!zone.enabled && { kind: "off", text: __("Off") },
						zone.not_shipped && { kind: "not_shipped", text: __("Not shipped") },
						zone.everywhere_else && { kind: "everywhere_else", text: __("Everywhere else") },
					].filter(Boolean),
					where,
					// what customers read of a zone that is on (one that is off is shown to nobody)
					read: zone.enabled
						? [
								line && __("Customers read: “{0}”", [line]),
								zone.not_shipped && message && __("At checkout they read: “{0}”", [message]),
						  ].filter(Boolean)
						: [],
					off: zone.enabled ? "" : __("This zone is off: its countries are priced as if it were not there."),
					never: zone.not_shipped && zone.enabled ? __("Delivery is never offered here.") : "",
					rules: priced
						? own.map((rule) => {
								const number = rules.indexOf(rule) + 1;
								const label = text(rule.label);
								const ref = label ? __("{0} ({1})", [number, label]) : String(number);
								const words = sp.rule_statement(rule);
								return rule.enabled
									? __("Rule {0}: {1}.", [ref, words])
									: __("Rule {0} is off: {1}.", [ref, words]);
						  })
						: [],
					no_rules: priced && !own.length ? __("No rule is for this zone yet.") : "",
					fallback: priced ? sp.fallback_sentence(policy, zone) : "",
				};
			}),
			closing: [],
		};
		if (!zones.length) summary.closing.push(__("There is no zone yet."));
		const catches_all = zones.some((zone) => zone.enabled && zone.everywhere_else);
		summary.closing.push(
			catches_all
				? sp.not_covered_sentence(policy)
				: passes_through(policy)
				? __("When no zone or no rule fits an order, customers pay the carrier's price.")
				: __("When no zone or no rule fits an order, delivery is not offered.")
		);
		return summary;
	},

	problem_step(sentence) {
		const said = String(sentence ?? "");
		const found = PROBLEM_STEPS.find(([pattern]) => pattern.test(said));
		return found ? found[1] : null;
	},

	is_card_problem(sentence) {
		const said = String(sentence ?? "");
		return CARD_PROBLEMS.some((pattern) => pattern.test(said));
	},
});
