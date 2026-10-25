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
});
const { el, clone, money } = sp;
function button(text, click, cls = "") {
	return el("button", { type: "button", class: "sp-button " + cls, onclick: click }, text);
}
let fieldId = 0;
function field(label, control) {
	const id = "sp-field-" + ++fieldId;
	const title = el("span", { id }, label);
	if (control.matches("input,select,textarea")) control.setAttribute("aria-labelledby", id);
	else {
		control.setAttribute("role", "group");
		control.setAttribute("aria-labelledby", id);
	}
	return el("div", { class: "sp-field" }, title, control);
}
function input(value, change, attrs = {}) {
	const node = el("input", { class: "sp-input", ...attrs });
	node.value = value ?? "";
	node.addEventListener("input", () => change(node.value));
	return node;
}
function newRow(group, zone = "") {
	return {
		name: null,
		item_set: group || null,
		zone,
		services: [],
		method: "table",
		price_table: [{ up_to_lb: null, price: 0 }],
		formula: "",
		label: "Delivery",
		days: null,
	};
}
function newGroup(name) {
	return { name, items: [], item_groups: [], patterns: [] };
}
function newZone(name) {
	return {
		name,
		enabled: true,
		countries: [],
		not_shipped: false,
		customer_line: "",
		not_offered_message: "",
	};
}
function select(options, value, change) {
	const node = el(
		"select",
		{ class: "sp-input" },
		options.map(([v, t]) => el("option", { value: v }, t)),
	);
	node.value = value;
	node.addEventListener("change", () => change(node.value));
	return node;
}
function check(label, value, change, disabled = false) {
	const node = el("input", { type: "checkbox", disabled });
	node.checked = !!value;
	node.addEventListener("change", () => change(node.checked));
	return el("label", { class: "sp-check" }, node, el("span", {}, label));
}
function numberInput(value, change, attrs = {}) {
	return input(
		value,
		(v) => {
			const n = sp.read_number(v);
			change(n.ok ? n.value : v);
		},
		{ type: "text", inputmode: "decimal", ...attrs },
	);
}
