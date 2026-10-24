// Part of the Shipping policy wizard page: shipping_policy_wizard.js stitches it in and says how.
//
// Helpers: dsi_catalogue.sp (el builds elements from text, never HTML; clone, same, is_number, money,
// measure, read_number, is_record), and problem_text, the sentences of a refusal as plain text.

const sp = (dsi_catalogue.sp = {
	// An element from a tag, attributes and children. A child that is a string becomes a text node, so
	// nothing passed here is ever read as HTML. Attribute keys: "class", "text" (the text content),
	// "on<event>" with a function (a listener); any other key is set as an attribute (false, null and
	// undefined leave it out, true sets it empty).
	el(tag, attributes = {}, ...children) {
		const element = document.createElement(tag);
		for (const [key, value] of Object.entries(attributes || {})) {
			if (value === false || value === null || value === undefined) continue;
			if (key === "class") element.className = value;
			else if (key === "text") element.textContent = value;
			else if (key.startsWith("on") && typeof value === "function") {
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

	// What a person typed as a number: {value, ok}. Blank is {value: null, ok: true}. A leading dollar sign
	// and thousands commas are allowed; a negative number, text, or more than the server takes is not ok.
	read_number(text, { above_zero = false } = {}) {
		const typed = String(text ?? "")
			.trim()
			.replace(/^\$/, "")
			.replace(/,/g, "");
		if (!typed) return { value: null, ok: true };
		if (!/^(\d+(\.\d*)?|\.\d+)$/.test(typed)) return { value: null, ok: false };
		const value = Number(typed);
		const ok = value <= MAX_NUMBER && (above_zero ? value > 0 : value >= 0);
		return { value: ok ? value : null, ok };
	},

	is_record(value) {
		return value !== null && typeof value === "object" && !Array.isArray(value);
	},
});
const el = sp.el;

// The sentences of an error that came through Frappe's message path, as plain text.
function problem_text(response) {
	let messages = [];
	try {
		messages = JSON.parse(response?._server_messages || "[]").map((entry) => {
			const message = JSON.parse(entry);
			return typeof message === "string" ? message : message?.message;
		});
	} catch (error) {
		messages = [];
	}
	const text = messages
		.filter(Boolean)
		.map((message) => frappe.utils.html2text(String(message)).trim())
		.filter(Boolean)
		.join(" ");
	return text || __("The server did not answer. Check the connection and try again.");
}
