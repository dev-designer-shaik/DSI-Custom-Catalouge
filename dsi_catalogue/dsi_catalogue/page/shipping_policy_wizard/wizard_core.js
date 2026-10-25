dsi_catalogue.ShippingPolicyWizard = class ShippingPolicyWizard {
	constructor(page, wrapper) {
		this.page = page;
		this.wrapper = wrapper;
		this.openRow = null;
		this.sample = { country_code: "", city: "", postcode: "", items: [] };
		this.sampleNames = {};
		this.pricing = 0;
		this.root = el("main", { class: "sp-page" });
		page.main.get(0).append(this.root);
		this.root.append(el("p", { class: "sp-muted" }, "Loading shipping policy…"));
		this.ready = this.load();
	}
	async call(name, args = {}) {
		const reply = await frappe.call({ method: METHOD + name, args, type: "POST", silent: true });
		return reply.message;
	}
	async load() {
		clearTimeout(this.storeTimer);
		try {
			this.state = await this.call("wizard_state");
			this.original = clone(this.state.policy);
			this.draft = clone(this.original);
			this.version = this.state.version;
			this.openRow = null;
			this.render();
			const saved = JSON.parse(localStorage.getItem(DRAFT_KEY) || "null");
			if (saved?.user === frappe.session.user && saved?.draft) {
				const body = el(
					"div",
					{},
					el(
						"p",
						{},
						saved.version === this.version
							? "You have an unfinished draft in this browser."
							: "The saved policy changed while this draft was unfinished. You can inspect it, but saving will require reloading the current policy.",
					),
				);
				this.dialog("Continue your draft?", body, [
					button("Discard draft", () => {
						localStorage.removeItem(DRAFT_KEY);
						this.closeDialog();
					}),
					button(
						"Continue editing",
						() => {
							this.draft = saved.draft;
							this.version = saved.version;
							this.closeDialog();
							this.render();
							this.changed();
						},
						"sp-primary",
					),
				]);
			}
		} catch (e) {
			this.root.replaceChildren(
				el("p", { role: "alert" }, "The shipping policy could not be loaded."),
				button("Try again", () => this.load()),
			);
		}
	}
	async receiveHelperDraft(proposal) {
		const latest = await this.call("wizard_state");
		if (proposal.expected_version !== latest.version)
			throw new Error("The saved policy changed. Ask the Helper to rebuild the proposal before applying it.");
		const use = () => {
			this.state = latest;
			this.original = clone(latest.policy);
			this.version = latest.version;
			this.draft = clone(proposal.draft);
			this.openRow = null;
			this.closeDialog();
			this.render();
			this.changed();
			const details = [...(proposal.notes || []), ...(proposal.problems || [])];
			const body = el(
				"div",
				{},
				el(
					"p",
					{},
					"The Helper filled an unsaved policy from your file. Check the products, country groups and prices, then use Review and save.",
				),
			);
			if (details.length)
				body.append(
					el(
						"ul",
						{},
						details.map((t) => el("li", {}, t)),
					),
				);
			this.dialog("Policy prepared from your file", body, [
				button("Review the policy", () => this.closeDialog(), "sp-primary"),
			]);
		};
		let storedDraft;
		try {
			storedDraft = JSON.parse(localStorage.getItem(DRAFT_KEY) || "null");
		} catch {}
		if (this.dirty || (storedDraft?.user === frappe.session.user && storedDraft?.draft)) {
			this.dialog(
				"Replace your unsaved edits?",
				el(
					"p",
					{},
					"You have unsaved changes on this page. Use the Helper's proposal only if you want to replace those edits. The saved policy will not change.",
				),
				[button("Use Helper proposal", use, "sp-primary")],
			);
		} else use();
	}

	onShow() {
		if (this.state && !this.dirty) this.ready = this.reload(false);
	}
	get dirty() {
		return JSON.stringify(this.draft) !== JSON.stringify(this.original);
	}
	async reload(ask = true) {
		if (ask && this.dirty && !window.confirm("Discard your unsaved changes?")) return;
		localStorage.removeItem(DRAFT_KEY);
		await this.load();
	}
	change(fn, render = false) {
		fn(this.draft);
		this.changed();
		if (render) this.renderCards();
	}
	changed() {
		this.renderSaveBar();
		clearTimeout(this.storeTimer);
		this.storeTimer = setTimeout(() => {
			try {
				if (this.dirty)
					localStorage.setItem(
						DRAFT_KEY,
						JSON.stringify({ user: frappe.session.user, version: this.version, draft: this.draft }),
					);
				else localStorage.removeItem(DRAFT_KEY);
			} catch {}
		}, 250);
		clearTimeout(this.priceTimer);
		this.priceTimer = setTimeout(() => this.priceSample(), 500);
	}
	render() {
		this.root.replaceChildren();
		const header = el(
			"header",
			{ class: "sp-header" },
			el(
				"div",
				{},
				el("p", { class: "sp-eyebrow" }, "PRODUCTS AND DELIVERY"),
				el("h2", {}, "Your shipping policy"),
				el("p", { class: "sp-muted" }, "Choose what ships, who delivers it, and what customers pay."),
			),
			el(
				"div",
				{ class: "sp-actions" },
				button("Check it works", () => this.checkWorks()),
			),
		);
		this.root.append(
			header,
			check("Use this policy at checkout", this.draft.active, (v) => this.change((d) => (d.active = v))),
		);
		const required = (this.state.services || []).filter((s) => s.required_for_group);
		for (const service of required) {
			this.root.append(
				el(
					"p",
					{ class: "sp-muted" },
					`${service.label} is required for matching ${service.restricted_item_group} products. Other services and manual fallback cannot replace it.`,
				),
			);
		}
		const layout = el("div", { class: "sp-layout" });
		this.cards = el("section", { class: "sp-cards", "aria-label": "Product groups" });
		const left = el(
			"div",
			{ class: "sp-main" },
			this.cards,
			button("+ Add product group", () => this.addGroup(), "sp-add"),
		);
		this.zones = el("section", { class: "sp-section" });
		left.append(this.zones);
		this.renderZones();
		const fallback = el("section", { class: "sp-section" }, el("h3", {}, "If the carrier gives no price"));
		const fbControls = el("div", { class: "sp-fallback" });
		const fbAmount = numberInput(
			this.draft.fallback.amount,
			(v) => this.change((d) => (d.fallback.amount = v ?? 0)),
			{ "aria-label": "Fallback amount in USD", placeholder: "0.00" },
		);
		const updateFallback = () => {
			fbAmount.hidden = this.draft.fallback.method !== "fixed";
		};
		for (const [v, label] of [
			["no_delivery", "Don’t offer delivery"],
			["fixed", "Charge a flat amount (USD)"],
		]) {
			const radio = el("input", { type: "radio", name: "sp-fallback", value: v });
			radio.checked = this.draft.fallback.method === v;
			radio.addEventListener("change", () => {
				this.change((d) => (d.fallback.method = v));
				updateFallback();
			});
			fbControls.append(el("label", { class: "sp-check" }, radio, label));
		}
		fbControls.append(fbAmount);
		updateFallback();
		fallback.append(fbControls);
		left.append(fallback);
		this.preview = el("aside", { class: "sp-preview", "aria-label": "Try an order" });
		layout.append(left, this.preview);
		this.root.append(layout);
		this.saveBar = el("div", { class: "sp-savebar", hidden: true });
		this.root.append(this.saveBar);
		this.renderCards();
		this.renderPreview();
		this.renderSaveBar();
	}
	groups() {
		return [
			...this.draft.item_sets.map((g) => ({ key: g.name, group: g })),
			...(this.draft.rules.some((r) => !r.item_set) ? [{ key: "", group: null }] : []),
		];
	}
	renderCards() {
		this.cards.replaceChildren();
		if (!this.groups().length) {
			this.cards.append(
				el(
					"div",
					{ class: "sp-empty" },
					el("h3", {}, "Start with how you want to ship"),
					el(
						"p",
						{ class: "sp-muted" },
						"Create your own product groups and prices, or use the providers’ live offers.",
					),
					button(
						"Start with the carrier’s own prices",
						() => {
							this.change((d) => (d.rules = [{ ...newRow(""), method: "carrier" }]), true);
						},
						"sp-primary",
					),
				),
			);
			return;
		}
		for (const { key, group } of this.groups()) {
			const rows = this.draft.rules.map((r, i) => ({ r, i })).filter(({ r }) => (r.item_set || "") === key);
			const products = group
				? [
						...group.patterns.map((p) => "Code: " + p),
						...group.item_groups.map((g) => "Item group: " + g),
						...(group.items.length ? [group.items.length + " selected products"] : []),
					]
				: ["Every product not listed above"];
			const tools = el("div", { class: "sp-card-tools" });
			if (group) {
				const pos = this.draft.item_sets.indexOf(group);
				const up = button("↑", () => this.moveGroup(key, -1));
				up.setAttribute("aria-label", "Move " + key + " up");
				up.disabled = pos === 0;
				const down = button("↓", () => this.moveGroup(key, 1));
				down.setAttribute("aria-label", "Move " + key + " down");
				down.disabled = pos === this.draft.item_sets.length - 1;
				const menu = el(
					"details",
					{ class: "sp-menu" },
					el("summary", { "aria-label": "Manage " + key }, "…"),
					el(
						"div",
						{},
						button("Rename", () => this.renameGroup(key)),
						button("Delete group", () => {
							if (window.confirm("Delete " + key + " and its delivery rows?"))
								this.change((d) => {
									d.item_sets = d.item_sets.filter((g) => g.name !== key);
									d.rules = d.rules.filter((r) => r.item_set !== key);
								}, true);
						}),
					),
				);
				tools.append(up, down, menu);
			} else
				tools.append(
					button("Delete", () => this.change((d) => (d.rules = d.rules.filter((r) => r.item_set)), true)),
				);
			const card = el(
				"article",
				{ class: "sp-card", "data-group": key },
				el(
					"header",
					{ class: "sp-card-header" },
					el(
						"div",
						{},
						el("h3", {}, key || "Everything else"),
						el(
							"div",
							{ class: "sp-chips" },
							products.length
								? products.map((p) => el("span", { class: "sp-chip" }, p))
								: el("span", { class: "sp-muted" }, "No products selected"),
						),
					),
					tools,
				),
			);
			if (group) card.append(button("Edit products", () => this.productPicker(key), "sp-edit-products"));
			card.append(
				el(
					"div",
					{ class: "sp-row-head", "aria-hidden": "true" },
					el("span", {}, "Country group"),
					el("span", {}, "Delivered by"),
					el("span", {}, "Customer pays"),
				),
			);
			for (const { r, i } of rows) {
				const summary = button(
					"",
					() => {
						this.openRow = this.openRow === i ? null : i;
						this.renderCards();
					},
					"sp-row-summary",
				);
				summary.setAttribute("aria-expanded", String(this.openRow === i));
				summary.setAttribute("aria-controls", "sp-row-editor-" + i);
				summary.append(
					el("span", {}, r.zone || "Everywhere else"),
					el("span", { class: "sp-delivery-summary" }, this.deliveryText(r)),
					el(
						"span",
						{ class: "sp-price " + (this.priceType(r) === "free" ? "sp-free" : "") },
						this.priceText(r),
					),
				);
				const remove = button("×", () => {
					this.openRow = null;
					this.change((d) => d.rules.splice(i, 1), true);
				});
				remove.setAttribute(
					"aria-label",
					"Remove " + (r.zone || "Everywhere else") + " row from " + (key || "Everything else"),
				);
				const wrap = el("div", { class: "sp-row" }, el("div", { class: "sp-row-line" }, summary, remove));
				if (this.openRow === i) {
					const editor = el("div", { class: "sp-row-editor", id: "sp-row-editor-" + i });
					this.rowEditor(editor, r, i);
					wrap.append(editor);
				}
				card.append(wrap);
			}
			card.append(
				button(
					"+ Add country group",
					() => {
						const row = newRow(key);
						this.change((d) => {
							const last = d.rules
								.map((r, i) => ((r.item_set || "") === key ? i : -1))
								.filter((i) => i >= 0)
								.pop();
							let index = last === undefined ? d.rules.findIndex((r) => !r.item_set) : last + 1;
							if (index < 0) index = d.rules.length;
							d.rules.splice(index, 0, row);
							this.openRow = index;
						});
						this.renderCards();
					},
					"sp-add",
				),
			);
			this.cards.append(card);
		}
	}
	priceType(r) {
		if (r.method === "carrier" || r.method === "formula") return r.method;
		return r.price_table.length === 1 && r.price_table[0].up_to_lb === null
			? r.price_table[0].price === 0
				? "free"
				: "fixed"
			: "weight";
	}
	priceText(r) {
		const kind = this.priceType(r);
		if (kind === "carrier") return "Carrier’s price";
		if (kind === "formula") return "Formula over carrier price";
		if (kind === "free") return "Free";
		if (kind === "fixed")
			return typeof r.price_table[0].price === "number" ? money(r.price_table[0].price) : "Enter a price";
		return "By weight · " + r.price_table.length + " tiers";
	}
	deliveryText(r) {
		if (!r.services.length) return r.method === "table" ? "Arrange manually" : "All available services";
		if (r.services.length === 1) return r.services[0];
		return r.services.length + " services";
	}
	moveGroup(key, delta) {
		this.change((d) => {
			const pos = d.item_sets.findIndex((g) => g.name === key);
			const [g] = d.item_sets.splice(pos, 1);
			d.item_sets.splice(pos + delta, 0, g);
			const order = [...d.item_sets.map((g) => g.name), ""];
			d.rules.sort((a, b) => order.indexOf(a.item_set || "") - order.indexOf(b.item_set || ""));
			this.openRow = null;
		}, true);
	}
	addGroup() {
		const name = input("", () => {}, { placeholder: "e.g. Gift boxes", maxlength: 140 });
		this.dialog("Add product group", field("Name", name), [
			button(
				"Add group",
				() => {
					const v = name.value.trim();
					if (!v) return;
					if (this.draft.item_sets.some((g) => g.name.toLowerCase() === v.toLowerCase()))
						return this.notice("That product group already exists.");
					this.change((d) => d.item_sets.push(newGroup(v)), true);
					this.closeDialog();
					this.productPicker(v);
				},
				"sp-primary",
			),
		]);
		name.focus();
	}
	renameGroup(key) {
		const name = input(key, () => {}, { maxlength: 140 });
		this.dialog("Rename product group", field("Name", name), [
			button(
				"Rename",
				() => {
					const v = name.value.trim();
					if (!v) return;
					this.change((d) => {
						d.item_sets.find((g) => g.name === key).name = v;
						d.rules.forEach((r) => {
							if (r.item_set === key) r.item_set = v;
						});
					}, true);
					this.closeDialog();
				},
				"sp-primary",
			),
		]);
	}
	renderZones() {
		this.zones.replaceChildren(
			el("h3", {}, "Country groups"),
			el(
				"div",
				{ class: "sp-zone-list" },
				this.draft.zones.map((z) =>
					button(
						z.name + " · " + z.countries.length + (z.not_shipped ? " · Not delivered" : ""),
						() => this.zoneDialog(z),
						z.not_shipped ? "sp-blocked" : "",
					),
				),
				button("+ Add country group", () => this.zoneDialog(null)),
			),
		);
	}
	renderSaveBar() {
		if (!this.saveBar) return;
		this.saveBar.hidden = !this.dirty;
		if (!this.dirty) return;
		this.saveBar.replaceChildren(
			el("span", {}, "Changes not saved"),
			el(
				"div",
				{ class: "sp-actions" },
				button("Discard", () => this.reload()),
				button("Review and save", () => this.review(), "sp-primary"),
			),
		);
	}
	dialog(title, body, actions = []) {
		this.closeDialog();
		const previous = document.activeElement;
		const d = el(
			"dialog",
			{ class: "sp-dialog", "aria-label": title },
			el(
				"header",
				{},
				el("h3", {}, title),
				button("×", () => this.closeDialog()),
			),
			el("div", { class: "sp-dialog-body" }, body),
			el(
				"footer",
				{},
				button("Cancel", () => this.closeDialog()),
				...actions,
			),
		);
		d.querySelector("header button").setAttribute("aria-label", "Close " + title);
		document.body.append(d);
		this.modal = d;
		d.addEventListener("close", () => {
			d.remove();
			if (this.modal === d) this.modal = null;
			previous?.focus();
		});
		d.showModal();
		return d;
	}
	closeDialog() {
		if (this.modal) {
			this.modal.close();
			this.modal.remove();
			this.modal = null;
		}
	}
	notice(message) {
		const node =
			this.modal?.querySelector(".sp-dialog-error") || el("p", { class: "sp-dialog-error", role: "alert" });
		node.textContent = message;
		if (!node.isConnected) (this.modal?.querySelector(".sp-dialog-body") || this.root).append(node);
	}
	async review() {
		const body = el("div", {}, el("p", {}, "Checking your changes…"));
		this.dialog("Review and save", body);
		try {
			const result = await this.call("preview_changes", { draft: JSON.stringify(this.draft) });
			body.replaceChildren(
				el(
					"ul",
					{},
					result.changes.map((t) => el("li", {}, t)),
				),
			);
			if (result.warnings.length)
				body.append(
					el("h4", {}, "Please check"),
					el(
						"ul",
						{},
						result.warnings.map((t) => el("li", {}, t)),
					),
				);
			if (result.problems.length)
				body.append(
					el("h4", { class: "sp-blocked" }, "Before saving"),
					el(
						"ul",
						{},
						result.problems.map((t) => el("li", {}, t)),
					),
				);
			else {
				const save = button(
					"Save policy",
					async () => {
						save.disabled = true;
						try {
							await this.call("apply_policy", {
								draft: JSON.stringify(this.draft),
								expected_version: this.version,
							});
							this.closeDialog();
							localStorage.removeItem(DRAFT_KEY);
							await this.load();
							frappe.show_alert({ message: __("Shipping policy saved"), indicator: "green" });
						} catch {
							save.disabled = false;
							this.notice(
								"The policy was not saved. Check the error; if someone changed it, reload before saving.",
							);
						}
					},
					"sp-primary",
				);
				this.modal.querySelector("footer").append(save);
			}
		} catch {
			body.replaceChildren(el("p", { role: "alert" }, "The draft could not be checked. Please try again."));
		}
	}
	renderPreview() {
		this.preview.replaceChildren(
			el("h3", {}, "Try an order"),
			el("p", { class: "sp-muted" }, "Preview these edits before saving."),
		);
		const picker = new dsi_catalogue.CountryPicker({
			countries: () => this.state.countries,
			placeholder: "Choose a destination",
			on_change: (code) => {
				this.sample.country_code = code;
				this.priceSample();
			},
		});
		picker.set(this.sample.country_code);
		this.preview.append(
			field("Destination country", picker.root),
			field(
				"City · for live provider quotes",
				input(
					this.sample.city,
					(v) => {
						this.sample.city = v;
						clearTimeout(this.sampleTimer);
						this.sampleTimer = setTimeout(() => this.priceSample(), 700);
					},
					{ placeholder: "Destination city" },
				),
			),
			field(
				"Postcode · where required",
				input(
					this.sample.postcode,
					(v) => {
						this.sample.postcode = v;
						clearTimeout(this.sampleTimer);
						this.sampleTimer = setTimeout(() => this.priceSample(), 700);
					},
					{ placeholder: "Destination postcode" },
				),
			),
		);
		const items = el("div", { class: "sp-sample-items" });
		this.preview.append(items);
		for (const [i, row] of this.sample.items.entries())
			items.append(
				el(
					"div",
					{ class: "sp-sample-item" },
					el("span", {}, this.sampleNames[row.item_code] || row.item_code),
					numberInput(
						row.qty,
						(v) => {
							row.qty = v ?? 0;
							this.priceSample();
						},
						{ "aria-label": "Quantity of " + row.item_code },
					),
					button("×", () => {
						this.sample.items.splice(i, 1);
						this.renderPreview();
						this.priceSample();
					}),
				),
			);
		this.preview.append(button("+ Add product", () => this.productPicker(null, true), "sp-add"));
		this.receipt = el("div", { class: "sp-receipt", "aria-live": "polite" });
		this.preview.append(this.receipt);
		this.priceSample();
	}
	async priceSample() {
		if (!this.receipt) return;
		const seq = ++this.pricing;
		if (!this.sample.country_code || !this.sample.items.length) {
			this.receipt.replaceChildren(
				el("p", { class: "sp-muted" }, "Choose a country and products to see delivery options."),
			);
			return;
		}
		this.receipt.replaceChildren(el("p", { class: "sp-muted" }, "Calculating delivery…"));
		try {
			const { result } = await this.call("try_order", {
				draft: JSON.stringify(this.draft),
				order: JSON.stringify(this.sample),
			});
			if (seq !== this.pricing) return;
			this.receipt.replaceChildren();
			if (!result.offered) {
				this.receipt.append(el("p", { class: "sp-blocked" }, result.message || "Delivery is not offered."));
				return;
			}
			for (const offer of result.offers) {
				const part = el(
					"section",
					{ class: "sp-offer" },
					el(
						"div",
						{ class: "sp-offer-title" },
						el("strong", {}, offer.name),
						el("strong", { class: "sp-price" }, money(offer.amount)),
					),
				);
				if (offer.provider)
					part.append(
						el(
							"small",
							{ class: "sp-muted" },
							offer.provider + (offer.estimated_days ? " · " + offer.estimated_days + " days" : ""),
						),
					);
				for (const line of offer.trace || [])
					part.append(
						el("div", { class: "sp-share" }, el("span", {}, line.text), el("span", {}, money(line.amount))),
					);
				this.receipt.append(part);
			}
			const warnings = [...new Set(result.shipments.flatMap((s) => s.warnings || []))];
			for (const t of warnings) this.receipt.append(el("p", { class: "sp-muted" }, t));
			if (result.preview_note) this.receipt.append(el("small", { class: "sp-muted" }, result.preview_note));
		} catch {
			if (seq === this.pricing)
				this.receipt.replaceChildren(
					el(
						"p",
						{ role: "alert" },
						"Could not price this preview. For live quotes, enter a valid destination city.",
					),
				);
		}
	}
	async checkWorks() {
		if (!this.sample.items.length || !this.sample.country_code || !this.sample.city) {
			this.dialog(
				"Check it works",
				el("p", {}, "Choose products, a country and a city in Try an order first."),
			);
			return;
		}
		const body = el("div", {}, el("p", {}, "Checking the ERP, providers, and website…"));
		this.dialog("Check it works", body);
		try {
			const result = await this.call("test_integration", {
				order: JSON.stringify(this.sample.items.length ? this.sample : {}),
			});
			body.replaceChildren(
				...result.checks.map((c) =>
					el(
						"section",
						{ class: "sp-check-result" },
						el("strong", {}, c.name.replaceAll("_", " ") + " · " + (c.ok ? "Passed" : "Needs attention")),
						el("p", {}, c.summary),
					),
				),
			);
		} catch {
			body.replaceChildren(el("p", {}, "The integration checks could not finish."));
		}
	}
};
