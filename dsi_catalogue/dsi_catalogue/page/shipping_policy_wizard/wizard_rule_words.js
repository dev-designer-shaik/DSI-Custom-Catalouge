// Part of the Shipping policy wizard page: shipping_policy_wizard.js stitches it in and says how.
//
// The words of a rule: what the Rules step's cards say (wizard_rule_card.js, step_rules.js), and what any
// other step prints for a rule (the Review step reuses them). Exported on dsi_catalogue.sp:
//	sp.rule_words(rule)    the parts of its sentence, as plain text: {zone, items, charge, rounded, limits}
//	                       ("In [zone] for [items] charge [charge], rounded up"; limits is "" when it has none)
//	sp.rule_text(rule)     the whole rule as one plain sentence, its limits included
//	sp.charge_words(rule)  how it charges: "the carrier's price", "nothing (free)", or its formula
//	sp.limits_words(rule)  its limits in words, such as "order value up to $500.00", or ""
//	sp.limit_of(value)     a limit as the engine reads it: 0 (no limit) unless it is a number above 0
//	sp.set_summary(set)    an item set in a few words: its groups, and how many items
// The asterisk of "every item*" belongs to the Rules step, which shows the sentence it refers to; these words
// carry none. Shared constants of the page script (the parts share one scope), for the Rules step's editors:
// the names a formula may use, the three ways a rule charges, and the six limits with the decimals the ERP
// keeps each to (the precision of its doctype field: money 2, kilograms and litres 3).

// What a formula may use: VARIABLES and FUNCTIONS of shipping_formula.py, word for word (a hermetic test holds
// these two lists to the server's).
const FORMULA_VARIABLES = ["carrier_price", "order_value", "weight_kg", "volume_l", "boxes", "item_count"];
const FORMULA_FUNCTIONS = ["min", "max", "round", "ceil", "floor", "abs"];

const CHARGES = [
	{ method: "carrier_price", label: __("The carrier's price") },
	{ method: "free", label: __("Free") },
	{ method: "formula", label: __("A custom formula") },
];

// Each limit's field label (with its unit), its word in a sentence, the rule's two keys, the decimals the ERP
// keeps, and how a value reads.
const LIMITS = [
	{
		label: __("Order value ($)"),
		noun: __("order value"),
		low: "min_order_value",
		high: "max_order_value",
		places: 2,
		show: (value) => sp.money(value),
	},
	{
		label: __("Weight (kg)"),
		noun: __("weight"),
		low: "min_weight_kg",
		high: "max_weight_kg",
		places: 3,
		show: (value) => __("{0} kg", [sp.measure(value)]),
	},
	{
		label: __("Volume (litres)"),
		noun: __("volume"),
		low: "min_volume_l",
		high: "max_volume_l",
		places: 3,
		show: (value) => __("{0} litres", [sp.measure(value)]),
	},
];

Object.assign(sp, {
	limit_of(value) {
		return sp.is_number(value) && value > 0 ? value : 0;
	},

	charge_words(rule) {
		if (rule.method === "free") return __("nothing (free)");
		if (rule.method === "formula") return String(rule.formula ?? "").trim() || __("a formula");
		return __("the carrier's price");
	},

	limits_words(rule) {
		const said = [];
		for (const band of LIMITS) {
			const low = sp.limit_of(rule[band.low]);
			const high = sp.limit_of(rule[band.high]);
			if (low && high) said.push(__("{0} from {1} up to {2}", [band.noun, band.show(low), band.show(high)]));
			else if (low) said.push(__("{0} from {1}", [band.noun, band.show(low)]));
			else if (high) said.push(__("{0} up to {1}", [band.noun, band.show(high)]));
		}
		return sp.and_list(said);
	},

	set_summary(set) {
		const groups = Array.isArray(set.item_groups) ? set.item_groups.map(String) : [];
		const items = Array.isArray(set.items) ? set.items.length : 0;
		const said = groups.length ? [groups.join(", ")] : [];
		if (items) said.push(items === 1 ? __("1 item") : __("{0} items", [items]));
		return said.join("; ");
	},

	rule_words(rule) {
		return {
			zone: String(rule.zone ?? "") || __("choose a zone"),
			items: rule.item_set ? String(rule.item_set) : __("every item"),
			charge: sp.charge_words(rule),
			rounded: Boolean(rule.round_up) && rule.method !== "free",
			limits: sp.limits_words(rule),
		};
	},

	rule_text(rule) {
		const words = sp.rule_words(rule);
		let text = __("In {0} for {1} charge {2}", [words.zone, words.items, words.charge]);
		if (words.rounded) text += __(", rounded up");
		return words.limits ? __("{0} (only when: {1})", [text, words.limits]) : text;
	},
});
