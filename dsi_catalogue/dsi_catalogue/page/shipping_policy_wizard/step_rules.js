// Part of the Shipping policy wizard page: shipping_policy_wizard.js stitches it in and says how.
//
// The Rules tab's body. A rule is a card that reads as a sentence, "In [zone] for [every item]* charge [the
// carrier's price], rounded up", each bracket a chip that opens its editor under the line, and under it
// "Only when [no limits]", the chip of its order value, weight and volume limits. Beside the sentence is what
// the rule charges the order in the tape (try_order's previews): brass when the customer pays it, sage when
// it is free, muted when an earlier rule comes first, and a dash with the reason when it does not apply.
//
// Rules are checked from the top, so the cards are in the order of the draft's rules: the up and down buttons
// move a rule (the keyboard's way), its grip moves it with the mouse, and a live region says where it went. A
// rule keeps its `name` (the saved row it came from) whatever is done to it; a new rule has none. A chip's
// editor closes on Escape (the focus goes back to the chip), on its Done button and on a click outside it.
//
// This file is the list (RulesView: the cards in their order, adding, moving, deleting, the open editor,
// checking formulas, what is wrong with a rule); wizard_rule_card.js is one rule's card (RuleCard) and its
// editors.
//
// What the body shows that is not the draft lives in wizard.ui.rules, because the panel is emptied on every
// tab switch: `open` ({rule, chip}, the editor that is open), `set_editor` (the item set being edited, see
// wizard_itemset_editor.js), `checks` (formula -> what check_formula said, "" when it is fine), `typed`
// (rule -> limits typed that are not numbers), and the item groups and item names read from the desk.

const ASTERISK = __(
	"* A rule for selected items applies only when every item in the order is one of them. A rule for every item applies to every order."
);

// What a formula may use: VARIABLES and FUNCTIONS of shipping_formula.py, word for word (a hermetic test holds
// these two lists to the server's).
const FORMULA_VARIABLES = ["carrier_price", "order_value", "weight_kg", "volume_l", "boxes", "item_count"];
const FORMULA_FUNCTIONS = ["min", "max", "round", "ceil", "floor", "abs"];
const FORMULA_CHECK_DELAY = 300; // ms after the last key before the server checks a formula

// The three ways a rule charges, and the limits of its "Only when" row: the field's label (with its unit),
// the word for it in a sentence, the rule's two keys, and how a value reads.
const CHARGES = [
	{ method: "carrier_price", label: __("The carrier's price") },
	{ method: "free", label: __("Free") },
	{ method: "formula", label: __("A custom formula") },
];
const LIMITS = [
	{
		label: __("Order value ($)"),
		noun: __("order value"),
		low: "min_order_value",
		high: "max_order_value",
		show: (value) => sp.money(value),
	},
	{
		label: __("Weight (kg)"),
		noun: __("weight"),
		low: "min_weight_kg",
		high: "max_weight_kg",
		show: (value) => __("{0} kg", [sp.measure(value)]),
	},
	{
		label: __("Volume (litres)"),
		noun: __("volume"),
		low: "min_volume_l",
		high: "max_volume_l",
		show: (value) => __("{0} litres", [sp.measure(value)]),
	},
];

// A limit as the engine reads it: 0, blank or not a number is no limit.
function limit_of(value) {
	return sp.is_number(value) && value > 0 ? value : 0;
}

function charge_words(rule) {
	if (rule.method === "free") return __("nothing (free)");
	if (rule.method === "formula") return String(rule.formula ?? "").trim() || __("a formula");
	return __("the carrier's price");
}

function limits_words(rule) {
	const said = [];
	for (const band of LIMITS) {
		const low = limit_of(rule[band.low]);
		const high = limit_of(rule[band.high]);
		if (low && high) said.push(__("{0} from {1} up to {2}", [band.noun, band.show(low), band.show(high)]));
		else if (low) said.push(__("{0} from {1}", [band.noun, band.show(low)]));
		else if (high) said.push(__("{0} up to {1}", [band.noun, band.show(high)]));
	}
	return said.length ? sp.and_list(said) : __("no limits");
}

function set_summary(set) {
	const groups = Array.isArray(set.item_groups) ? set.item_groups.map(String) : [];
	const items = Array.isArray(set.items) ? set.items.length : 0;
	const said = groups.length ? [groups.join(", ")] : [];
	if (items) said.push(items === 1 ? __("1 item") : __("{0} items", [items]));
	return said.join("; ");
}

class RulesView {
	constructor(wizard, panel) {
		this.wizard = wizard;
		this.ui = wizard.ui.rules;
		this.ui.open = this.ui.open || null;
		this.ui.set_editor = this.ui.set_editor || null;
		this.ui.checks = this.ui.checks || new Map();
		this.ui.typed = this.ui.typed || new WeakMap();
		this.ui.formula_sequence = this.ui.formula_sequence || 0;
		this.cards = new Map(); // rule object -> RuleCard
		this.list = el("ol", {
			class: "sp-rules",
			"aria-label": __("Rules, in the order they are checked"),
		});
		this.empty = el("p", { class: "sp-empty-state" });
		this.add_button = el(
			"button",
			{
				type: "button",
				class: "btn btn-default btn-sm",
				"aria-describedby": "sp-rule-add-hint",
				"data-testid": "rule-add",
				onclick: () => this.add(),
			},
			__("Add a rule")
		);
		this.add_hint = el("p", { class: "sp-hint", id: "sp-rule-add-hint" });
		this.live = new Announcer("rules-live");
		panel.append(
			el(
				"p",
				{ class: "sp-step-intro" },
				__("Rules are checked from the top. The first one that fits an order sets what the customer pays.")
			),
			this.list,
			this.empty,
			el("div", { class: "sp-add" }, this.add_button, this.add_hint),
			el("p", { class: "sp-rules-note", id: "sp-rules-note", "data-testid": "rules-note" }, ASTERISK),
			this.live.root
		);
		// A click outside the open editor closes it. One listener for the session, which asks the view drawn last.
		if (!this.ui.listening) {
			this.ui.listening = true;
			document.addEventListener("pointerdown", (event) => wizard.ui.rules.view?.pointer_down(event), true);
		}
		this.refresh();
		this.check_formulas();
		const open = this.ui.open;
		if (open) this.cards.get(open.rule)?.open_editor(open.chip, { focus: false });
	}

	// ---------------------------------------------------------- the draft
	get rules() {
		const rules = this.wizard.store.draft.rules;
		return Array.isArray(rules) ? rules : [];
	}

	get zones() {
		const zones = this.wizard.store.draft.zones;
		return Array.isArray(zones) ? zones.filter(sp.is_record) : [];
	}

	get item_sets() {
		const sets = this.wizard.store.draft.item_sets;
		return Array.isArray(sets) ? sets.filter(sp.is_record) : [];
	}

	// The cards follow the draft's rules, in their order; a card that moves takes the focus along. A rule that
	// left the draft takes its open editor with it. Every price waits for the answer to the change.
	refresh() {
		const rules = this.rules;
		for (const [rule, card] of this.cards) {
			if (!rules.includes(rule)) {
				card.root.remove();
				this.cards.delete(rule);
			}
		}
		if (this.ui.open && !rules.includes(this.ui.open.rule)) this.ui.open = null;
		if (this.ui.set_editor && !rules.includes(this.ui.set_editor.rule)) this.ui.set_editor = null;
		const active = document.activeElement;
		let position = 0;
		rules.forEach((rule, index) => {
			if (!sp.is_record(rule)) return;
			let card = this.cards.get(rule);
			if (!card) {
				card = new RuleCard(this, rule);
				this.cards.set(rule, card);
			}
			const at = this.list.children[position];
			if (at !== card.root) this.list.insertBefore(card.root, at || null);
			position += 1;
			card.update(index, rules.length);
			card.price.classList.add("is-stale");
		});
		if (active?.isConnected && document.activeElement !== active) active.focus({ preventScroll: true });
		const zone = this.default_zone();
		this.empty.hidden = position > 0;
		this.empty.textContent = zone
			? __("No rules yet. Add one to set what customers pay in a zone.")
			: __("No rules yet.");
		this.add_button.setAttribute("aria-disabled", String(!zone));
		this.add_hint.textContent = zone ? "" : __("Add a zone first, in the Zones step: a rule is always for a zone.");
	}

	// What each rule charges the order in the tape: the previews of the latest answer, by the rule's place.
	priced(answer) {
		for (const card of this.cards.values()) card.show_price(answer);
	}

	// A new rule is on, for the first zone that is on and shipped to, every item, the carrier's price.
	default_zone() {
		const named = this.zones.filter((zone) => String(zone.name ?? "").trim());
		return (
			named.find((zone) => zone.enabled && !zone.not_shipped) ||
			named.find((zone) => zone.enabled) ||
			named[0] ||
			null
		);
	}

	add() {
		const zone = this.default_zone();
		if (!zone) return;
		const rule = sp.new_rule(String(zone.name));
		this.wizard.change((draft) => {
			if (!Array.isArray(draft.rules)) draft.rules = [];
			draft.rules.push(rule);
		});
		this.cards.get(rule)?.chips.zone.focus();
		this.live.say(__("Rule {0} added.", [this.rules.length]));
	}

	// Moves the card's rule to `to` (its place once it is taken out): only the order of the rules changes.
	move(card, to) {
		const rules = this.rules;
		const from = rules.indexOf(card.rule);
		if (from < 0 || to < 0 || to >= rules.length || to === from) return;
		this.wizard.change((draft) => {
			draft.rules.splice(from, 1);
			draft.rules.splice(to, 0, card.rule);
		});
		this.live.say(__("Rule {0} is now rule {1} of {2}.", [from + 1, to + 1, rules.length]));
	}

	delete_rule(card) {
		let deleted = null; // the place of the rule once it is deleted
		const dialog = frappe.confirm(__("Delete this rule?"), () => {
			const at = this.rules.indexOf(card.rule);
			if (at < 0) return;
			deleted = at;
			this.wizard.change((draft) => draft.rules.splice(at, 1));
			this.live.say(__("Rule {0} deleted.", [at + 1]));
		});
		dialog.on_hide = () => {
			if (deleted === null) {
				card.delete.focus();
				return;
			}
			const next = this.list.children[Math.min(deleted, this.list.children.length - 1)];
			(next?.querySelector('[data-testid="rule-delete"]') || this.add_button).focus();
		};
	}

	// ---------------------------------------------------------- the editors
	is_open(rule, chip) {
		return this.ui.open?.rule === rule && this.ui.open?.chip === chip;
	}

	toggle(card, chip) {
		if (this.is_open(card.rule, chip)) this.close();
		else this.open(card, chip);
	}

	open(card, chip) {
		const before = this.ui.open ? this.cards.get(this.ui.open.rule) : null;
		if (before) before.close_editor();
		this.ui.open = { rule: card.rule, chip };
		card.open_editor(chip, { focus: true });
	}

	// Closes the open editor; the focus goes back to its chip unless a click put it elsewhere.
	close({ focus = true } = {}) {
		const open = this.ui.open;
		if (!open) return;
		const card = this.cards.get(open.rule);
		card?.close_editor();
		this.ui.open = null;
		if (focus) card?.chips[open.chip]?.focus();
	}

	// A press outside the open editor and its chip closes the editor, unless an item set is being made in it:
	// that has its own Save and Cancel, so a click on the tape does not lose it.
	pointer_down(event) {
		const open = this.ui.open;
		if (!open || !this.list.isConnected || !this.list.offsetParent) return;
		const card = this.cards.get(open.rule);
		if (!card || this.ui.set_editor?.rule === open.rule) return;
		const target = event.target;
		if (card.editor.contains(target) || card.chips[open.chip]?.contains(target)) return;
		this.close({ focus: false });
	}

	// ---------------------------------------------------------- formulas
	// The formula of the card's rule, checked by the server 300 ms after the last key (soon), or now. Each
	// check gets a number and only the newest one's answer is shown, so a slow answer to an older formula never
	// replaces it. What the server said of a formula is remembered, so it is asked once.
	check_formula(card, { soon = false } = {}) {
		const ui = this.ui;
		const sequence = ++ui.formula_sequence;
		clearTimeout(ui.formula_timer);
		const formula = String(card.rule.formula ?? "").trim();
		const show = (error) => {
			if (sequence === ui.formula_sequence) card.show_formula_error(error);
		};
		if (!formula) {
			show("");
			return;
		}
		if (ui.checks.has(formula)) {
			show(ui.checks.get(formula));
			return;
		}
		const ask = () => this.ask_formula(formula).then(show);
		if (soon) ui.formula_timer = setTimeout(ask, FORMULA_CHECK_DELAY);
		else ask();
	}

	// The server's word on a formula: "" when a rule can use it, else why not (plain text).
	ask_formula(formula) {
		return this.wizard.call("check_formula", { source: formula }, { quiet: true }).then(
			(answer) => {
				const error = answer?.ok ? "" : String(answer?.error || __("The formula could not be checked."));
				this.ui.checks.set(formula, error);
				return error;
			},
			(problem) => String(problem || __("The formula could not be checked.")) // not kept: it may pass next time
		);
	}

	// The formulas of the draft's rules that were never checked, so that a card can say when one cannot be used.
	check_formulas() {
		for (const card of this.cards.values()) {
			const formula = String(card.rule.formula ?? "").trim();
			if (card.rule.method !== "formula" || !formula || this.ui.checks.has(formula)) continue;
			this.ask_formula(formula).then(() => {
				if (card.root.isConnected) card.update(card.index, this.rules.length);
			});
		}
	}

	// ---------------------------------------------------------- moving with the mouse
	// The grip moves a rule: the card follows the pointer (not when motion is reduced) and a line shows where it
	// lands. The keyboard has the up and down buttons instead.
	drag(card, event) {
		if (event.button !== 0 || this.cards.size < 2) return;
		event.preventDefault();
		const grip = event.currentTarget;
		grip.setPointerCapture?.(event.pointerId);
		const others = [...this.list.children].filter((item) => item !== card.root);
		const start = event.clientY;
		const follow = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
		let before = null; // the place among the other cards where it would land, once the pointer moved
		this.list.classList.add("is-reordering");
		card.root.classList.add("is-dragging");
		const move = (moved) => {
			if (follow) card.root.style.transform = `translateY(${moved.clientY - start}px)`;
			const found = others.findIndex((item) => {
				const box = item.getBoundingClientRect();
				return moved.clientY < box.top + box.height / 2;
			});
			before = found < 0 ? others.length : found;
			others.forEach((item, at) => {
				item.classList.toggle("is-drop-before", at === before);
				item.classList.toggle("is-drop-after", before === others.length && at === others.length - 1);
			});
		};
		const finish = (dropped) => {
			grip.removeEventListener("pointermove", move);
			grip.removeEventListener("pointerup", up);
			grip.removeEventListener("pointercancel", cancel);
			this.list.classList.remove("is-reordering");
			card.root.classList.remove("is-dragging");
			card.root.style.transform = "";
			for (const item of others) item.classList.remove("is-drop-before", "is-drop-after");
			if (!dropped || before === null) return;
			// its new place is the place, once it is taken out, of the rule it lands in front of
			const ahead = [...this.cards.values()].find((other) => other.root === others[before]);
			const rest = this.rules.filter((rule) => rule !== card.rule);
			this.move(card, ahead ? rest.indexOf(ahead.rule) : rest.length);
		};
		const up = () => finish(true);
		const cancel = () => finish(false);
		grip.addEventListener("pointermove", move);
		grip.addEventListener("pointerup", up);
		grip.addEventListener("pointercancel", cancel);
	}

	// ---------------------------------------------------------- what is wrong
	// What the page can see is wrong with a rule, in plain sentences: the Review step has the server's word.
	problems_of(card) {
		const rule = card.rule;
		const said = [];
		const zone_name = String(rule.zone ?? "");
		const zone = this.zones.find((candidate) => candidate.name === zone_name);
		if (!zone_name) said.push(__("Choose the zone this rule is for."));
		else if (!zone) said.push(__("There is no zone called “{0}”. Choose another zone.", [zone_name]));
		else if (!zone.enabled) said.push(__("The zone {0} is off, so this rule never applies.", [zone_name]));
		else if (zone.not_shipped) {
			said.push(__("Nothing is shipped to the zone {0}, so this rule never applies.", [zone_name]));
		}
		if (rule.item_set && !this.item_sets.some((set) => set.name === rule.item_set)) {
			said.push(__("There is no item set called “{0}”. Choose the items again.", [String(rule.item_set)]));
		}
		if (rule.method === "formula") {
			const formula = String(rule.formula ?? "").trim();
			const error = this.ui.checks.get(formula);
			if (!formula) said.push(__("Write the formula, or choose another way to charge."));
			else if (error && card.chip !== "charge") said.push(__("The formula cannot be used: {0}", [error]));
		}
		for (const band of LIMITS) {
			const low = limit_of(rule[band.low]);
			const high = limit_of(rule[band.high]);
			if (low && high && low >= high) {
				said.push(
					__("The limits on the {0} are the wrong way round: “up to” has to be higher than “from”.", [band.noun])
				);
			}
		}
		return said;
	}

	// The limits typed into a rule's fields that are not numbers, by key (they are not in the draft).
	typed_of(rule) {
		let typed = this.ui.typed.get(rule);
		if (!typed) {
			typed = {};
			this.ui.typed.set(rule, typed);
		}
		return typed;
	}
}

const step_rules = {
	render(wizard, panel) {
		wizard.ui.rules.view = new RulesView(wizard, panel);
	},
	refresh(wizard, panel) {
		wizard.ui.rules.view?.refresh();
	},
	priced(wizard, panel, answer) {
		wizard.ui.rules.view?.priced(answer);
	},
};
