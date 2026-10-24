// Part of the Shipping policy wizard page: shipping_policy_wizard.js stitches it in and says how.
//
// What the Zones and Rules steps share:
//	sp.fold_name, sp.unique_name  names compared as the ERP compares them, and a new name nobody has yet
//	sp.and_list                   "2", "2 and 3", "2, 3 and 4"
//	NAME_LENGTH, strip_angles     the limits of a name the ERP can keep: 140 characters, no < or >
//	token_list, focus_token       removable tokens: the countries of a zone, the groups and items of a set
//	Announcer                     a polite live region that says what a click added, moved or deleted
//	desk_list                     rows of a desk doctype through frappe.client.get_list

const NAME_LENGTH = 140; // a zone's, an item set's and a rule's name: the ERP keeps at most 140 characters

Object.assign(sp, {
	// A name as the ERP compares two names: capital letters, accents and the spaces around it aside (its
	// database reads "Gulf", "gulf" and "Gùlf" as one name, so two zones may not be called so).
	fold_name(name) {
		return dsi_catalogue.CountryPicker.fold(name ?? "");
	},

	// `base`, or "base 2", "base 3"...: the first of them that none of `names` already is.
	unique_name(base, names) {
		const taken = new Set(names.map((name) => sp.fold_name(name)));
		if (!taken.has(sp.fold_name(base))) return base;
		for (let number = 2; ; number += 1) {
			const name = `${base} ${number}`;
			if (!taken.has(sp.fold_name(name))) return name;
		}
	},

	// Words joined as a sentence lists them: "2", "2 and 3", "2, 3 and 4".
	and_list(words) {
		if (words.length < 2) return words.join("");
		return __("{0} and {1}", [words.slice(0, -1).join(", "), words[words.length - 1]]);
	},
});

// The ERP refuses < and > in the name of a zone or an item set, so a name field takes them out as they are
// typed (or pasted): the field keeps the rest, with the caret where it was. Returns whether any was taken out.
function strip_angles(input) {
	const text = input.value;
	if (!/[<>]/.test(text)) return false;
	const before = text.slice(0, input.selectionStart ?? text.length).replace(/[<>]/g, "").length;
	input.value = text.replace(/[<>]/g, "");
	input.setSelectionRange(before, before);
	return true;
}

// A list of removable tokens: [{key, text}], each a list item with its text and a button that removes it
// (named "Remove <text>", so it is found by its words). remove(key, index) is called on a click.
function token_list({ tokens, label, testid, remove_testid, remove }) {
	return el(
		"ul",
		{ class: "sp-tokens", "aria-label": label },
		tokens.map((token, index) =>
			el(
				"li",
				{ class: "sp-token", "data-testid": testid, "data-key": token.key },
				el("span", { class: "sp-token-text" }, token.text),
				el(
					"button",
					{
						type: "button",
						class: "sp-token-remove",
						"aria-label": __("Remove {0}", [token.text]),
						"data-testid": remove_testid,
						onclick: () => remove(token.key, index),
					},
					"×"
				)
			)
		)
	);
}

// After a token went: the remove button that took its place, else the one before it, else `fallback`.
function focus_token(list, index, fallback) {
	const buttons = list?.querySelectorAll ? list.querySelectorAll(".sp-token-remove") : [];
	(buttons[Math.min(index, buttons.length - 1)] || fallback)?.focus();
}

// A polite live region, visually hidden. A sentence said twice in a row is still read out: the region is
// emptied first and filled a moment later.
class Announcer {
	constructor(testid) {
		this.root = el("p", { class: "sp-sr-only", "aria-live": "polite", "data-testid": testid });
		this.timer = null;
	}

	say(text) {
		clearTimeout(this.timer);
		this.root.textContent = "";
		this.timer = setTimeout(() => {
			this.root.textContent = text;
		}, 60);
	}
}

// The rows of a desk doctype, read through frappe.client.get_list (the reader's own permissions apply), as a
// promise of a list. A refusal shows in Frappe's own dialog, as every desk read does, and gives no rows.
function desk_list(doctype, fields, filters = []) {
	return new Promise((resolve) => {
		frappe.call({
			method: "frappe.client.get_list",
			args: { doctype, fields, filters, order_by: "name asc", limit_page_length: 0 },
			callback: (response) => resolve(Array.isArray(response.message) ? response.message : []),
			error: () => resolve([]),
		});
	});
}
