// Part of the Shipping policy wizard page: shipping_policy_wizard.js stitches it in and says how.
//
// Helpers: dsi_catalogue.sp (el builds elements from text, never HTML; clone, same, is_number, money,
// measure, read_number, is_record; new_zone, new_rule and new_item_set build entries of the draft in the
// server's key order; refusal_lines, the sentences of a refusal as plain text, one each), and problem_text,
// those sentences as one text.

const sp = (dsi_catalogue.sp = {
	// An element from a tag, attributes and children. A child that is a string becomes a text node, so
	// nothing passed here is ever read as HTML. Attribute keys: "class", "text" (the text content),
	// "on<event>" with a function (a listener); any other key is set as an attribute (false, null and
	// undefined leave it out, true sets it empty). An "on<event>" that is not a function throws: as an
	// attribute it would be an inline event handler, text run as code.
	el(tag, attributes = {}, ...children) {
		const element = document.createElement(tag);
		for (const [key, value] of Object.entries(attributes || {})) {
			if (value === false || value === null || value === undefined) continue;
			if (key === "class") element.className = value;
			else if (key === "text") element.textContent = value;
			else if (key.startsWith("on")) {
				if (typeof value !== "function") throw new TypeError(`sp.el: ${key} takes a function`);
				element.addEventListener(key.slice(2), value);
			} else element.setAttribute(key, value === true ? "" : String(value));
		}
		const absent = (child) => child === null || child === undefined || child === false;
		element.append(...children.flat(Infinity).filter((child) => !absent(child)));
		return element;
	},

	clone(value) {
		return JSON.parse(JSON.stringify(value));
	},

	same(one, other) {
		return JSON.stringify(one) === JSON.stringify(other);
	},

	is_number(value) {
		return typeof value === "number" && Number.isFinite(value);
	},

	// An amount as the tape prints it: a dollar sign and two decimals.
	money(amount) {
		return `$${amount.toLocaleString("en-US", {
			minimumFractionDigits: 2,
			maximumFractionDigits: 2,
		})}`;
	},

	// A weight or a volume as a hint shows it: at most three decimals.
	measure(value) {
		return value.toLocaleString("en-US", { maximumFractionDigits: 3 });
	},

	// What a person typed as a number: {value, ok, comma}. Blank is {value: null, ok: true}. A leading dollar
	// sign is allowed, and commas only between groups of thousands (1,234.50). Any other comma is not ok and
	// sets comma, so that the tape can say to use a period for decimals: 1,5 kg is never read as 15. A negative
	// number, an exponent, other text, or more than the server takes is not ok.
	read_number(text, { above_zero = false } = {}) {
		let typed = String(text ?? "")
			.trim()
			.replace(/^\$/, "");
		if (!typed) return { value: null, ok: true };
		if (typed.includes(",")) {
			if (!/^\d{1,3}(,\d{3})+(\.\d*)?$/.test(typed)) return { value: null, ok: false, comma: true };
			typed = typed.replace(/,/g, "");
		}
		if (!/^(\d+(\.\d*)?|\.\d+)$/.test(typed)) return { value: null, ok: false };
		const value = Number(typed);
		const ok = value <= MAX_NUMBER && (above_zero ? value > 0 : value >= 0);
		return { value: ok ? value : null, ok };
	},

	is_record(value) {
		return value !== null && typeof value === "object" && !Array.isArray(value);
	},

	// A new zone, rule and item set for the draft, with the server's keys in the server's order (as
	// wizard_state gives them: shipping_policy_data.policy_from_rows builds them so). The draft is dirty when
	// its JSON differs from the saved policy's, so an entry with its keys in another order would leave the
	// draft dirty even when it equals a saved one. A new rule is enabled: one added in the wizard is meant to
	// apply, and nothing goes live before Apply (the doctype's own new rows start disabled because they go
	// live on save).
	new_zone(name) {
		return {
			name,
			enabled: true,
			countries: [],
			not_shipped: false,
			everywhere_else: false,
			customer_line: "",
			not_offered_message: "",
			fallback: { method: "policy_default", amount: 0 },
		};
	},

	new_rule(zone) {
		return {
			name: null,
			enabled: true,
			label: "",
			zone,
			item_set: null,
			min_order_value: 0,
			max_order_value: 0,
			min_weight_kg: 0,
			max_weight_kg: 0,
			min_volume_l: 0,
			max_volume_l: 0,
			method: "carrier_price",
			formula: "",
			round_up: false,
		};
	},

	new_item_set(name) {
		return { name, item_groups: [], items: [] };
	},

	// The sentences of a refusal that came through Frappe's message path, each as plain text: the server escapes
	// what people typed in them (the desk shows them as HTML), so each is read with frappe.utils.html2text. A
	// message is one sentence, a list of them (as_list) or a table (as_table), and a <br> in it starts a new line.
	// `response` is what Frappe hands an error callback: the answer, or for a code it has no handler for, the
	// request (its responseJSON).
	refusal_lines(response) {
		let answer = response;
		if (answer && typeof answer.getResponseHeader === "function") {
			try {
				answer = answer.responseJSON || JSON.parse(answer.responseText);
			} catch (error) {
				answer = null;
			}
		}
		let messages = [];
		try {
			messages = JSON.parse(answer?._server_messages || "[]");
		} catch (error) {
			messages = [];
		}
		const lines = [];
		const add = (value) => {
			if (Array.isArray(value)) value.forEach(add);
			else if (value !== null && value !== undefined && value !== "") {
				// Frappe joins the sentences of one message with <br>: each is a line of its own
				for (const part of String(value).split(/<br\s*\/?>/i)) {
					const text = frappe.utils.html2text(part).trim();
					if (text) lines.push(text);
				}
			}
		};
		for (const entry of Array.isArray(messages) ? messages : []) {
			let message = entry;
			try {
				message = JSON.parse(entry);
			} catch (error) {
				message = entry; // a sentence sent as it is
			}
			add(sp.is_record(message) ? message.message : message);
		}
		return lines;
	},
});
const el = sp.el;

// The sentences of an error that came through Frappe's message path, as plain text.
function problem_text(response) {
	const text = sp.refusal_lines(response).join(" ");
	return text || __("The server did not answer. Check the connection and try again.");
}
