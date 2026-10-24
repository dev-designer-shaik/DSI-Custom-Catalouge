// Part of the Shipping policy wizard page: shipping_policy_wizard.js stitches it in and says how.
//
// The item set editor, opened from the items chip of a rule (step_rules.js): the set's name, its item groups
// (a group includes the groups beneath it) and its items. It edits a copy, `state`, which the Rules step keeps
// in wizard.ui.rules.set_editor so that it outlasts a tab switch, and it changes the draft only on Save:
//	- a new set is added to the draft's item sets and chosen for the rule the editor was opened from;
//	- an edited set changes for every rule that uses it (a new name takes those rules along), and the editor
//	  says so before it is saved.
// A set with no name, a name another set has (capital letters and accents aside), or nothing in it is not
// saved: the editor says why. The item groups are the desk's Item Group list, read once through
// frappe.client.get_list and searched as you type; the items come from the desk's own item search, the one
// Try an order uses. Names and codes reach the page as text only.
//
//	state = {rule, set (the draft's set being edited, or null for a new one), name, item_groups, items, problem}
//	done(name)  called after Save with the set's name, after Cancel with null

let item_set_editors = 0; // ids for the labels of each editor

dsi_catalogue.ItemSetEditor = class ItemSetEditor {
	constructor({ wizard, state, done }) {
		item_set_editors += 1;
		const id = `sp-set-${item_set_editors}`;
		this.wizard = wizard;
		this.ui = wizard.ui.rules;
		this.state = state;
		this.done = done;

		this.name = el("input", {
			type: "text",
			id: `${id}-name`,
			class: "form-control sp-input",
			maxlength: NAME_LENGTH,
			autocomplete: "off",
			spellcheck: "false",
			"data-testid": "itemset-name",
		});
		this.name.value = state.name;
		this.name.addEventListener("input", () => {
			const stripped = strip_angles(this.name);
			state.name = this.name.value;
			state.problem = stripped ? __("A set's name cannot hold < or >.") : "";
			this.show_problem();
		});

		this.groups = el("div", { class: "sp-token-box" });
		this.group_picker = new dsi_catalogue.CountryPicker({
			id: `${id}-group`,
			countries: () => this.offered_groups(),
			placeholder: __("Add an item group"),
			testid: "itemset-group-add",
			list_label: __("Item groups"),
			none: __("No item group has that name."),
			on_change: (name) => this.add_group(name),
		});
		this.groups_note = el("p", { class: "sp-hint" });

		this.items = el("div", { class: "sp-token-box" });
		const picker = el("div", { class: "sp-item-picker" });
		this.item_control = frappe.ui.form.make_control({
			parent: picker,
			df: {
				fieldtype: "Link",
				options: "Item",
				fieldname: "sp_set_item",
				placeholder: __("Search items by code or name"),
				only_select: 1,
				change: () => this.add_item(),
			},
			only_input: true,
			render_input: true,
		});
		this.item_control.$input.attr({ id: `${id}-item`, "data-testid": "itemset-item-add" });

		this.problem = el("p", { class: "sp-problem", "aria-live": "polite", "data-testid": "itemset-problem" });
		const shared = this.shared_note();
		this.root = el(
			"div",
			{ class: "sp-set-editor", "data-testid": "itemset-editor" },
			el(
				"p",
				{ class: "sp-editor-title" },
				state.set ? __("Edit the item set {0}", [String(state.set.name ?? "")]) : __("New item set")
			),
			el(
				"div",
				{ class: "sp-field" },
				el("label", { class: "sp-label", for: `${id}-name` }, __("Name")),
				this.name
			),
			el(
				"div",
				{ class: "sp-field" },
				el("label", { class: "sp-label", for: `${id}-group` }, __("Item groups")),
				this.groups,
				this.group_picker.root,
				this.groups_note
			),
			el(
				"div",
				{ class: "sp-field" },
				el("label", { class: "sp-label", for: `${id}-item` }, __("Items")),
				this.items,
				picker
			),
			shared ? el("p", { class: "sp-hint", "data-testid": "itemset-shared" }, shared) : null,
			this.problem,
			// while it is open the chips do not drop it (step_rules.js): it says so
			el("p", { class: "sp-hint" }, __("Save the set or cancel it to edit anything else in the rules.")),
			el(
				"div",
				{ class: "sp-editor-actions" },
				el(
					"button",
					{ type: "button", class: "btn btn-primary btn-xs", "data-testid": "itemset-save", onclick: () => this.save() },
					__("Save the set")
				),
				el(
					"button",
					{ type: "button", class: "btn btn-default btn-xs", "data-testid": "itemset-cancel", onclick: () => this.done(null) },
					__("Cancel")
				)
			)
		);
		this.read_groups();
		this.render_tokens();
		this.name_items();
		this.show_problem();
	}

	// The draft's sets and rules as records (a draft restored from storage may hold anything).
	get sets() {
		const sets = this.wizard.store.draft.item_sets;
		return Array.isArray(sets) ? sets.filter(sp.is_record) : [];
	}

	get rules() {
		const rules = this.wizard.store.draft.rules;
		return Array.isArray(rules) ? rules : [];
	}

	// An edited set changes for every rule that uses it: one sentence says which, before it is saved.
	shared_note() {
		const set = this.state.set;
		if (!set?.name) return "";
		const numbers = [];
		this.rules.forEach((rule, index) => {
			if (sp.is_record(rule) && rule !== this.state.rule && rule.item_set === set.name) numbers.push(String(index + 1));
		});
		if (!numbers.length) return "";
		return numbers.length === 1
			? __("Rule {0} uses this set too: saving changes it there as well.", [numbers[0]])
			: __("Rules {0} use this set too: saving changes it there as well.", [sp.and_list(numbers)]);
	}

	// The desk's item groups, read once for the session. Until they are there the picker says so, and a list
	// that is open when they arrive is drawn again from what was typed.
	read_groups() {
		const ui = this.ui;
		const picker = this.group_picker;
		if (!ui.item_groups_read) {
			ui.item_groups_read = desk_list("Item Group", ["name"]).then((rows) => {
				ui.item_groups = rows.map((row) => String(row.name));
			});
		}
		const ready = () => {
			this.groups_note.textContent = __("An item group includes the groups beneath it.");
			picker.none = __("No item group has that name.");
		};
		if (ui.item_groups) {
			ready();
			return;
		}
		this.groups_note.textContent = __("Reading the item groups…");
		picker.none = __("Reading the item groups…");
		ui.item_groups_read.then(() => {
			ready();
			if (!picker.list.hidden) picker.open(picker.input.value);
		});
	}

	offered_groups() {
		return (this.ui.item_groups || [])
			.filter((name) => !this.state.item_groups.includes(name))
			.map((name) => ({ code: name, name }));
	}

	add_group(name) {
		this.group_picker.set("");
		if (!name || this.state.item_groups.includes(name)) return;
		this.state.item_groups.push(name);
		this.state.problem = "";
		this.render_tokens();
		this.show_problem();
	}

	remove_group(name, index) {
		this.state.item_groups.splice(this.state.item_groups.indexOf(name), 1);
		this.render_tokens();
		focus_token(this.groups.firstChild, index, this.group_picker.input);
	}

	add_item() {
		const code = this.item_control.get_value();
		if (!code) return;
		this.item_control.set_value("");
		if (!this.state.items.includes(code)) this.state.items.push(String(code));
		this.state.problem = "";
		this.render_tokens();
		this.show_problem();
		this.name_items();
	}

	remove_item(code, index) {
		this.state.items.splice(this.state.items.indexOf(code), 1);
		this.render_tokens();
		focus_token(this.items.firstChild, index, this.item_control.$input.get(0));
	}

	// The names of the items, read once each (a token shows the code until its name is there).
	name_items() {
		const names = (this.ui.item_names = this.ui.item_names || {});
		const missing = this.state.items.filter((code) => !(code in names));
		if (!missing.length) return;
		for (const code of missing) names[code] = "";
		desk_list("Item", ["name", "item_name"], [["name", "in", missing]]).then((rows) => {
			for (const row of rows) names[String(row.name)] = String(row.item_name ?? "");
			if (this.root.isConnected) this.render_tokens();
		});
	}

	render_tokens() {
		const names = this.ui.item_names || {};
		const draw = (box, tokens, label, testid, remove) =>
			box.replaceChildren(
				...(tokens.length ? [token_list({ tokens, label, testid, remove_testid: `${testid}-remove`, remove })] : [])
			);
		draw(
			this.groups,
			this.state.item_groups.map((name) => ({ key: name, text: name })),
			__("Item groups of this set"),
			"itemset-group-chip",
			(name, index) => this.remove_group(name, index)
		);
		draw(
			this.items,
			this.state.items.map((code) => ({ key: code, text: names[code] ? `${names[code]} (${code})` : code })),
			__("Items of this set"),
			"itemset-item-chip",
			(code, index) => this.remove_item(code, index)
		);
	}

	show_problem() {
		this.problem.textContent = this.state.problem;
	}

	// What stops the set from being saved, or "".
	refusal() {
		const state = this.state;
		const name = state.name.trim();
		if (!name) return __("Give the set a name. Rules choose a set by its name.");
		const taken = this.sets.some((set) => set !== state.set && sp.fold_name(set.name) === sp.fold_name(name));
		if (taken) {
			return __(
				"Another item set has this name. Names that differ only in capital letters or accents count as the same."
			);
		}
		if (!state.item_groups.length && !state.items.length) return __("Add at least one item group or item.");
		return "";
	}

	save() {
		const state = this.state;
		state.problem = this.refusal();
		if (state.problem) {
			this.show_problem();
			return;
		}
		const name = state.name.trim();
		this.wizard.change((draft) => {
			if (!Array.isArray(draft.item_sets)) draft.item_sets = [];
			const set = state.set && draft.item_sets.includes(state.set) ? state.set : null;
			if (set) {
				const before = set.name;
				set.name = name;
				set.item_groups = [...state.item_groups];
				set.items = [...state.items];
				if (before !== name) {
					for (const rule of this.rules) if (sp.is_record(rule) && rule.item_set === before) rule.item_set = name;
				}
			} else {
				const added = sp.new_item_set(name);
				added.item_groups.push(...state.item_groups);
				added.items.push(...state.items);
				draft.item_sets.push(added);
				if (this.rules.includes(state.rule)) state.rule.item_set = name;
			}
		});
		this.done(name);
	}
};
