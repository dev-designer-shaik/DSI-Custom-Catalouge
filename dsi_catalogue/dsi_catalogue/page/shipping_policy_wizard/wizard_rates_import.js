// Part of the Shipping policy wizard page: shipping_policy_wizard.js stitches it in and says how.
//
// The Rates step's import of the Shopify export (step_rates.js draws it). Two files: the shipping rates and, if
// there is one, the countries not delivered. Each is chosen from the computer (read here as UTF-8 text, nothing
// stripped: the server reads a byte order mark) or pasted; pasted text wins over a chosen file, and the panel says
// so. A text of more than 1,000,000 characters is refused here, before the server is asked. Preview has the server
// read them (import_rate_card, which writes nothing) and prints what it found as a receipt: how much the card
// holds, the notes on the files, what stops the card from being applied, and the countries not delivered, by
// name, with those the ERP does not know. Under the receipt's double rule, "Use these rates" puts the card into
// the draft, its keys in the order the ERP keeps them (sp.card_as_kept), after asking when the draft has a card
// already (the line beside the button says how many profiles it replaces). The countries not delivered, less
// those the ERP does not know, go to the zone "Not delivered": its countries are replaced when it is there, and
// it is made as a Not shipped zone when it is not. A country that is already in another zone that is on is said,
// and stays where it is. Then the panel offers to switch the price source to the rate card. Nothing is written
// to the server. A rates file that cannot be read gives no card, and the draft keeps the one it has.
//
// What the panel shows that is not the draft lives in wizard.ui.rates.import, since the panel is drawn again on
// every tab switch (and a file input cannot be given its file back, so the file's name and text are kept):
// `files` (key -> {name, text, length, problem, reading}), `pasted` (key -> text), `open`, `preview` ({answer, key}:
// the server's answer and the texts it was for), `previewing`, `problem`, `taken` (what Use these rates did) and
// `panel` (the panel drawn last: an answer that arrives later goes to it).

const RATES_IMPORT_FILES = [
	{
		key: "config",
		title: __("Shipping rates"),
		file_testid: "rates-file-config",
		paste_testid: "rates-paste-config",
		too_long: __("The shipping rates are longer than 1,000,000 characters, more than the import reads."),
	},
	{
		key: "not_delivered",
		title: __("Countries not delivered (optional)"),
		file_testid: "rates-file-notdelivered",
		paste_testid: "rates-paste-notdelivered",
		too_long: __("The list of countries not delivered is longer than 1,000,000 characters, more than the import reads."),
	},
];
const RATES_MAX_CHARACTERS = 1000000; // what import_rate_card reads of each file (MAX_CSV_CHARACTERS)
// The zone the countries not delivered go to, and what customers there read: data of the policy, not page text.
const NOT_DELIVERED_ZONE = "Not delivered";
const NOT_DELIVERED_MESSAGE = "We do not deliver to this country.";

let rates_imports = 0; // ids for the labels of each panel

class RatesImport {
	constructor(view) {
		rates_imports += 1;
		const uid = (this.uid = `sp-import-${rates_imports}`);
		this.view = view;
		this.wizard = view.wizard;
		const ui = (this.ui = view.ui.import = view.ui.import || { files: {}, pasted: {}, reading: {} });
		// open while the draft has no card; once it has one, the panel is a line that opens on request
		if (ui.open === undefined) ui.open = !sp.is_record(this.wizard.store.draft.rate_card);

		this.toggle = el(
			"button",
			{
				type: "button",
				class: "sp-disclosure sp-import-toggle",
				"aria-controls": `${uid}-body`,
				"data-testid": "rates-import-toggle",
				onclick: () => this.set_open(!this.ui.open),
			},
			el("span", { class: "sp-chevron", "aria-hidden": "true" }),
			el("span", { class: "sp-import-title", id: `${uid}-title` }, __("Import the Shopify export"))
		);
		this.fields = {};
		this.preview_button = el(
			"button",
			{ type: "button", class: "btn btn-default btn-sm", "data-testid": "rates-preview", onclick: () => this.preview() },
			__("Preview")
		);
		this.status = el("span", { class: "sp-import-status", role: "status" });
		this.problem = el("p", { class: "sp-import-problem", role: "alert", "data-testid": "rates-import-problem" });
		this.result = el("div", {
			class: "sp-import-preview",
			"aria-live": "polite",
			"data-testid": "rates-import-preview",
			hidden: true,
		});
		this.taken = el("div", { class: "sp-import-taken", role: "status", "data-testid": "rates-import-taken" });
		this.body = el(
			"div",
			{ class: "sp-import-body", id: `${uid}-body` },
			el(
				"p",
				{ class: "sp-import-intro" },
				__(
					"Export two files from Shopify: the shipping rates, and the countries you do not deliver to. Choose each file, or paste its text. Nothing is saved until you apply the policy in the Review step."
				)
			),
			el(
				"div",
				{ class: "sp-import-files" },
				RATES_IMPORT_FILES.map((spec) => this.file_field(spec))
			),
			el("div", { class: "sp-import-actions" }, this.preview_button, this.status),
			this.problem,
			this.result,
			this.taken
		);
		this.root = el(
			"section",
			{ class: "sp-import", "aria-labelledby": `${uid}-title`, "data-testid": "rates-import" },
			el("h3", { class: "sp-import-head" }, this.toggle),
			this.body
		);
		this.show();
	}

	// One file: its chooser, and a box to paste its text instead.
	file_field(spec) {
		const id = `${this.uid}-${spec.key}`;
		const input = el("input", {
			type: "file",
			id: `${id}-file`,
			class: "sp-file",
			accept: ".csv,text/csv,text/plain",
			"aria-describedby": `${id}-file-note`,
			"data-testid": spec.file_testid,
		});
		input.addEventListener("change", () => this.read_file(spec, input));
		const file_note = el("p", { class: "sp-hint sp-file-note", id: `${id}-file-note`, "aria-live": "polite" });
		const paste = el("textarea", {
			id: `${id}-paste`,
			class: "form-control sp-paste",
			rows: 3,
			spellcheck: "false",
			autocomplete: "off",
			wrap: "off",
			"aria-describedby": `${id}-paste-note`,
			"data-testid": spec.paste_testid,
		});
		paste.value = this.ui.pasted[spec.key] || "";
		paste.addEventListener("input", () => {
			this.ui.pasted[spec.key] = paste.value;
			this.inputs_changed();
		});
		const paste_note = el("p", { class: "sp-hint", id: `${id}-paste-note` });
		this.fields[spec.key] = { file_note, paste_note };
		return el(
			"fieldset",
			{ class: "sp-import-file" },
			el("legend", { class: "sp-import-legend" }, spec.title),
			el("label", { class: "sp-label", for: `${id}-file` }, __("Choose the file")),
			input,
			file_note,
			el("label", { class: "sp-label", for: `${id}-paste` }, __("Or paste its text")),
			paste,
			paste_note
		);
	}

	set_open(open) {
		this.ui.open = open;
		this.show();
	}

	// A file chosen: read as UTF-8 text. A file of more than four bytes a character for the most the server reads
	// is refused unread; a later choice outdates a reading still on its way.
	read_file(spec, input) {
		const ui = this.ui;
		const file = input.files && input.files[0];
		const token = (ui.reading[spec.key] = (ui.reading[spec.key] || 0) + 1);
		if (!file) {
			delete ui.files[spec.key];
			this.inputs_changed();
			return;
		}
		const name = String(file.name || "");
		if (file.size > RATES_MAX_CHARACTERS * 4) {
			ui.files[spec.key] = { name, text: null, problem: spec.too_long };
			this.inputs_changed();
			return;
		}
		ui.files[spec.key] = { name, text: null, problem: "", reading: true };
		this.inputs_changed();
		const reader = new FileReader();
		reader.onload = () => {
			if (token !== ui.reading[spec.key]) return;
			const text = String(reader.result ?? "");
			const length = sp.text_length(text);
			ui.files[spec.key] =
				length > RATES_MAX_CHARACTERS ? { name, text: null, problem: spec.too_long } : { name, text, length, problem: "" };
			this.inputs_changed();
		};
		reader.onerror = () => {
			if (token !== ui.reading[spec.key]) return;
			ui.files[spec.key] = {
				name,
				text: null,
				problem: __("The file could not be read. Choose it again, or paste its text."),
			};
			this.inputs_changed();
		};
		reader.readAsText(file, "UTF-8");
	}

	// What is to be read changed: a preview made from other texts is gone, and so is a problem said of them.
	inputs_changed() {
		this.ui.preview = null;
		this.ui.problem = "";
		this.ui.panel?.show();
	}

	// The text sent for one file: what is pasted when there is any, else the file read.
	text_of(spec) {
		const pasted = this.ui.pasted[spec.key] || "";
		if (pasted.trim()) {
			const long = pasted.length > RATES_MAX_CHARACTERS && sp.text_length(pasted) > RATES_MAX_CHARACTERS;
			return { text: pasted, problem: long ? spec.too_long : "" };
		}
		const file = this.ui.files[spec.key];
		if (!file) return { text: null, problem: "" };
		return { text: file.text, problem: file.problem, reading: Boolean(file.reading) };
	}

	inputs_key() {
		return JSON.stringify(RATES_IMPORT_FILES.map((spec) => this.text_of(spec).text));
	}

	preview() {
		const ui = this.ui;
		if (ui.previewing) return;
		const [config, missing] = RATES_IMPORT_FILES.map((spec) => this.text_of(spec));
		let refusal = "";
		if (config.reading || missing.reading) refusal = __("A file is still being read. Try again in a moment.");
		else if (config.problem || missing.problem) refusal = config.problem || missing.problem;
		else if (!String(config.text ?? "").trim()) refusal = __("Choose the shipping rates file, or paste its text.");
		if (refusal) {
			ui.problem = refusal;
			ui.preview = null;
			this.show();
			return;
		}
		const key = this.inputs_key();
		const args = { config_csv: config.text };
		if (missing.text !== null) args.not_delivered_csv = missing.text;
		ui.previewing = true;
		ui.problem = "";
		this.show();
		this.wizard.call("import_rate_card", args, { quiet: true }).then(
			(answer) => {
				ui.previewing = false;
				// the texts changed while the server read them: this answer is for texts that are gone
				if (key === ui.panel?.inputs_key()) {
					ui.preview = { answer: sp.is_record(answer) ? answer : {}, key };
					ui.taken = null;
				}
				ui.panel?.show();
				ui.panel?.result.querySelector('[data-testid="rates-import-read"]')?.focus();
			},
			(problem) => {
				ui.previewing = false;
				ui.problem = __("The files could not be read: {0}", [String(problem)]);
				ui.panel?.show();
			}
		);
	}

	// The number of profiles of the draft's card, or null when it has none.
	draft_profiles() {
		const card = this.wizard.store.draft.rate_card;
		return sp.is_record(card) ? sp.card_profiles(card).length : null;
	}

	use() {
		const answer = this.ui.preview?.answer;
		if (!sp.is_record(answer?.card)) return;
		if (this.draft_profiles() === null) {
			if (this.take(answer)) this.focus_taken();
			return;
		}
		let taken = false;
		const dialog = frappe.confirm(__("Replace the rate card in your draft with these rates?"), () => {
			taken = this.take(answer);
		});
		// once the dialog is gone: the desk's Escape takes the focus away while the dialog closes
		dialog.$wrapper.one("hidden.bs.modal", () => {
			if (taken) this.ui.panel?.focus_taken();
			else this.ui.panel?.result.querySelector('[data-testid="rates-import-apply"]')?.focus();
		});
	}

	// The card into the draft, and the countries not delivered into their zone. Says whether the draft took it
	// (while Apply writes, nothing changes the draft).
	take(answer) {
		const draft = this.wizard.store.draft;
		const missing = new Set(Array.isArray(answer.not_delivered_missing) ? answer.not_delivered_missing.map(String) : []);
		const codes = (Array.isArray(answer.not_delivered) ? answer.not_delivered.map(String) : []).filter(
			(code) => !missing.has(code)
		);
		const zones = Array.isArray(draft.zones) ? draft.zones.filter(sp.is_record) : [];
		const folded = sp.fold_name(NOT_DELIVERED_ZONE);
		const existing = zones.find((zone) => sp.fold_name(zone.name) === folded) || null;
		const kept = [];
		const conflicts = [];
		for (const code of codes) {
			const owner = zones.find(
				(zone) => zone !== existing && zone.enabled && Array.isArray(zone.countries) && zone.countries.includes(code)
			);
			if (owner) conflicts.push({ code, zone: String(owner.name ?? "") });
			else kept.push(code);
		}
		// no list of countries not delivered, or none of them left to add: the zones stay as they are
		const made = !existing && kept.length > 0;
		const changed = this.wizard.change((draft) => {
			draft.rate_card = sp.card_as_kept(answer.card);
			if (existing && codes.length) existing.countries = kept;
			else if (made) {
				const zone = sp.new_zone(NOT_DELIVERED_ZONE);
				zone.countries.push(...kept);
				zone.not_shipped = true;
				zone.not_offered_message = NOT_DELIVERED_MESSAGE;
				if (!Array.isArray(draft.zones)) draft.zones = [];
				draft.zones.push(zone);
			}
		});
		if (!changed) return false;
		this.ui.taken = {
			zone: existing && codes.length ? "updated" : made ? "made" : "",
			name: existing ? String(existing.name ?? "") : NOT_DELIVERED_ZONE,
			countries: kept.length,
			off: Boolean(existing && !existing.enabled),
			conflicts,
		};
		this.ui.preview = null;
		this.ui.panel?.show();
		return true;
	}

	focus_taken() {
		this.taken.querySelector('[data-testid="rates-import-taken-title"]')?.focus();
	}

	// ---------------------------------------------------------- what the panel shows
	show() {
		const ui = this.ui;
		ui.panel = this;
		this.toggle.setAttribute("aria-expanded", String(Boolean(ui.open)));
		this.body.hidden = !ui.open;
		for (const spec of RATES_IMPORT_FILES) this.show_field(spec);
		this.preview_button.setAttribute("aria-disabled", String(Boolean(ui.previewing)));
		this.preview_button.classList.toggle("disabled", Boolean(ui.previewing));
		this.status.textContent = ui.previewing ? __("Reading the files…") : "";
		this.problem.textContent = ui.problem || "";
		this.show_preview();
		this.show_taken();
	}

	show_field(spec) {
		const { file_note, paste_note } = this.fields[spec.key];
		const file = this.ui.files[spec.key];
		let note = "";
		if (file?.reading) note = __("Reading {0}…", [file.name]);
		else if (file?.problem) note = file.problem;
		else if (file) note = __("{0}: {1} characters read.", [file.name, file.length.toLocaleString("en-US")]);
		file_note.textContent = note;
		file_note.classList.toggle("is-problem", Boolean(file?.problem));
		const pasted = (this.ui.pasted[spec.key] || "").trim() !== "";
		paste_note.textContent = pasted && file ? __("The pasted text is used, not the chosen file.") : "";
	}

	// The server's answer as a receipt: what the card holds, the notes, what stops it, the countries not
	// delivered; then, under a double rule, Use these rates. Drawn again only when the answer or the draft's card
	// changes, so the focus stays where it is.
	show_preview() {
		const preview = this.ui.preview;
		const said = JSON.stringify(preview ? [preview.key, this.draft_profiles()] : null);
		if (said === this.preview_said) return;
		this.preview_said = said;
		this.result.hidden = !preview;
		if (!preview) {
			this.result.replaceChildren();
			return;
		}
		const answer = preview.answer;
		const list = (value) => (Array.isArray(value) ? value.map(String) : []);
		const card = sp.is_record(answer.card) ? answer.card : null;
		const notes = list(answer.notes);
		const problems = list(answer.problems);
		const parts = [];
		const counts = sp.is_record(answer.summary) ? answer.summary : sp.card_counts(card);
		const number = (value) => (Number.isInteger(value) ? value : 0);
		parts.push(
			el(
				"p",
				{ class: "sp-import-read", tabindex: "-1", "data-testid": "rates-import-read" },
				card
					? __("The shipping rates hold {0}.", [
							sp.counts_words({
								profiles: number(counts.profiles),
								zones: number(counts.zones),
								tiers: number(counts.tiers),
								countries: number(counts.countries),
							}),
					  ])
					: __("The shipping rates could not be used.")
			)
		);
		if (notes.length) {
			parts.push(
				el(
					"details",
					{ class: "sp-import-notes", "data-testid": "rates-import-notes" },
					el("summary", {}, notes.length === 1 ? __("1 note on the files") : __("{0} notes on the files", [notes.length])),
					el("ul", { class: "sp-import-list" }, notes.map((note) => el("li", {}, note)))
				)
			);
		}
		if (problems.length) {
			parts.push(
				el(
					"div",
					{ class: "sp-import-problems", "data-testid": "rates-import-problems" },
					card
						? el(
								"p",
								{ class: "sp-import-problems-title" },
								__("These stop the card from being applied. You can use the rates now and fix them below.")
						  )
						: null,
					el("ul", { class: "sp-import-list" }, problems.map((problem) => el("li", {}, problem)))
				)
			);
		}
		const codes = list(answer.not_delivered);
		const missing = list(answer.not_delivered_missing);
		if (codes.length) {
			const names = sp.and_list(codes.map((code) => this.view.country_name(code)));
			parts.push(
				el(
					"p",
					{ class: "sp-import-closed", "data-testid": "rates-import-not-delivered" },
					el("span", { class: "sp-state is-not-shipped" }, __("Not delivered")),
					" ",
					codes.length === 1 ? __("1 country: {0}.", [names]) : __("{0} countries: {1}.", [codes.length, names])
				)
			);
		}
		if (missing.length) {
			parts.push(
				el(
					"p",
					{ class: "sp-import-missing", "data-testid": "rates-import-missing" },
					__("Left out of the Not shipped zone, because the ERP does not know them: {0}.", [sp.and_list(missing)])
				)
			);
		}
		if (card) {
			const replacing = this.draft_profiles();
			let line = __("They go into your draft. Nothing is live until you apply it.");
			if (replacing === 1) line = __("They replace the rate card in your draft, which has 1 profile.");
			else if (replacing !== null) line = __("They replace the rate card in your draft, which has {0} profiles.", [replacing]);
			parts.push(
				el(
					"div",
					{ class: "sp-import-use" },
					el(
						"button",
						{
							type: "button",
							class: "btn btn-primary btn-sm",
							"aria-describedby": `${this.uid}-replaces`,
							"data-testid": "rates-import-apply",
							onclick: () => this.use(),
						},
						__("Use these rates")
					),
					el("p", { class: "sp-hint", id: `${this.uid}-replaces` }, line)
				)
			);
		}
		this.result.replaceChildren(...parts);
	}

	// What Use these rates did, and the offer to switch the price source to the rate card.
	show_taken() {
		const taken = this.ui.taken;
		const by_card = this.wizard.price_source() === "rate_card";
		const said = JSON.stringify(taken ? [taken, by_card] : null);
		if (said === this.taken_said) return;
		this.taken_said = said;
		if (!taken) {
			this.taken.replaceChildren();
			return;
		}
		const focused = this.taken.contains(document.activeElement);
		const parts = [
			el(
				"p",
				{ class: "sp-import-taken-title", tabindex: "-1", "data-testid": "rates-import-taken-title" },
				__("These rates are in your draft now. Nothing is live until you apply it in the Review step.")
			),
		];
		const countries = taken.countries === 1 ? __("1 country") : __("{0} countries", [taken.countries]);
		if (taken.zone === "made") {
			parts.push(el("p", {}, __("The new Not shipped zone {0} lists {1} not delivered to.", [taken.name, countries])));
		} else if (taken.zone === "updated") {
			parts.push(el("p", {}, __("The zone {0} now lists {1} not delivered to.", [taken.name, countries])));
			if (taken.off) parts.push(el("p", {}, __("It is off: turn it on in the Zones step.")));
		}
		for (const conflict of taken.conflicts) {
			parts.push(
				el(
					"p",
					{ class: "sp-import-conflict", "data-testid": "rates-import-conflict" },
					__("{0} is already in the zone {1}, so it was not added to {2}.", [
						this.view.country_name(conflict.code),
						conflict.zone,
						taken.name,
					])
				)
			);
		}
		if (by_card) {
			parts.push(el("p", { class: "sp-hint" }, __("The price source is the rate card.")));
		} else {
			parts.push(
				el(
					"div",
					{ class: "sp-import-switch" },
					el(
						"button",
						{
							type: "button",
							class: "btn btn-default btn-sm",
							"data-testid": "rates-switch-source",
							onclick: () => this.view.set_source("rate_card", { focus: true }),
						},
						__("Switch the price source to Rate card")
					)
				)
			);
		}
		this.taken.replaceChildren(...parts);
		if (focused) this.focus_taken();
	}
}
