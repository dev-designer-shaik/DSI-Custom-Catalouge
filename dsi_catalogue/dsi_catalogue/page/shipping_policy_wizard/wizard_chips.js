// Part of the Shipping policy wizard page: shipping_policy_wizard.js stitches it in and says how.
//
// What the steps share (the Zones and Rules steps first, the Rates step and the Review since):
//	sp.fold_name, sp.unique_name  names compared as the ERP compares them, and a new name nobody has yet
//	sp.and_list                   "2", "2 and 3", "2, 3 and 4"
//	sp.country_list               country names apart by semicolons: a name can hold a comma or an "and"
//	NAME_LENGTH, strip_angles     the limits of a name the ERP can keep: 140 characters, no < or >
//	commit_on_leave               a name field trimmed once its name is committed (the field left, or Enter)
//	sp.clip                       a text as it is shown: at most 140 characters (the whole of it stays in the draft)
//	token_list, focus_token       removable tokens: the countries of a zone, the groups and items of a set
//	Announcer                     a polite live region that says what a click added, moved or deleted
//	desk_list                     rows of a desk doctype through frappe.client.get_list
//	keep_cards                    a card per entry of the draft (a zone, a rule), kept in the entries' order

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

	// Country names as a list says them: apart by semicolons, since a country's own name can hold a comma or an
	// "and" ("Korea, Democratic Peoples Republic of", "Heard Island and McDonald Islands").
	country_list(names) {
		return names.join("; ");
	},

	// A text as the page shows it: at most `limit` characters, the last of them an ellipsis when it is cut. A name
	// a file brought in can be of any length (the rate card's own check refuses it); the draft keeps it whole, and
	// whatever shows a cut text gives the whole of it as its title.
	clip(text, limit = NAME_LENGTH) {
		const whole = String(text ?? "");
		return whole.length > limit ? `${whole.slice(0, limit - 1)}…` : whole;
	},
});

// A field whose text is kept without the spaces around it, as the ERP keeps a name: `commit` runs once the text is
// committed, when the field is left or Enter is pressed. While it is typed, a space at its end stays.
function commit_on_leave(input, commit) {
	input.addEventListener("blur", commit);
	input.addEventListener("keydown", (event) => {
		if (event.key === "Enter") {
			event.preventDefault();
			commit();
		}
	});
}

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
// (named "Remove <text>", so it is found by its words). remove(key, index) is called on a click. A text longer
// than a name may be is shown cut (sp.clip), with the whole of it as its title.
function token_list({ tokens, label, testid, remove_testid, remove }) {
	return el(
		"ul",
		{ class: "sp-tokens", "aria-label": label },
		tokens.map((token, index) => {
			const text = String(token.text ?? "");
			const shown = sp.clip(text);
			return el(
				"li",
				{ class: "sp-token", "data-testid": testid, "data-key": token.key },
				el("span", { class: "sp-token-text", title: shown === text ? null : text }, shown),
				el(
					"button",
					{
						type: "button",
						class: "sp-token-remove",
						"aria-label": __("Remove {0}", [shown]),
						"data-testid": remove_testid,
						onclick: () => remove(token.key, index),
					},
					"×"
				)
			);
		})
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

// Keeps one card per entry of `entries` (the draft's zones, its rules) in `list`, in the entries' order. A
// card belongs to its entry object, so an entry keeps its card while it is in the draft: make(entry) makes the
// card of a new entry (an object with a `root` element), the card of an entry that left is removed, a card out
// of place is moved, and each(card, index) is called for every entry, with its index in `entries`. An entry
// that is not a record (a draft restored from storage may hold anything) gets no card. The element that had
// the focus keeps it, since moving a card in the page would take it away. Returns the number of cards.
function keep_cards({ entries, cards, list, make, each }) {
	for (const [entry, card] of cards) {
		if (!entries.includes(entry)) {
			card.root.remove();
			cards.delete(entry);
		}
	}
	const active = document.activeElement;
	let position = 0;
	entries.forEach((entry, index) => {
		if (!sp.is_record(entry)) return;
		let card = cards.get(entry);
		if (!card) {
			card = make(entry);
			cards.set(entry, card);
		}
		const at = list.children[position];
		if (at !== card.root) list.insertBefore(card.root, at || null);
		position += 1;
		each(card, index);
	});
	if (active?.isConnected && document.activeElement !== active) active.focus({ preventScroll: true });
	return position;
}
