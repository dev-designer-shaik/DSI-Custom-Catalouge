Object.assign(dsi_catalogue.ShippingPolicyWizard.prototype, {
	rowEditor(root, row, index) {
		const country = select(
			[
				["", "Everywhere else"],
				...this.draft.zones.filter((z) => !z.not_shipped).map((z) => [z.name, z.name]),
			],
			row.zone,
			(v) => {
				this.change(() => (row.zone = v));
				this.renderCards();
			},
		);
		const delivery = el("div", { class: "sp-services" });
		const pricing = el("div", { class: "sp-pricing" });
		root.append(
			field("Country group", country),
			field("Delivered by", delivery),
			field("Customer pays", pricing),
		);
		const zone = this.draft.zones.find((z) => z.name === row.zone);
		const countries = zone?.countries || this.state.countries.map((c) => c.code);
		if (this.state.services === null)
			delivery.append(el("p", { class: "sp-muted" }, "Provider services are not installed on this ERP."));
		for (const provider of this.state.providers) {
			const children = (this.state.services || []).filter((s) => s.provider === provider.name);
			const block = el(
				"fieldset",
				{},
				el("legend", {}, provider.name + (provider.enabled ? "" : " · Disabled")),
			);
			for (const service of children) {
				const serves = countries.filter((c) => service.countries.includes(c)).length;
				block.append(
					check(
						service.label +
							" · " +
							(service.estimated_days ? service.estimated_days + " days · " : "") +
							serves +
							" of " +
							countries.length +
							" countries" +
							(!service.enabled ? " · Disabled" : ""),
						row.services.includes(service.name),
						(v) => {
							this.change(() => {
								row.services = v
									? [...row.services, service.name]
									: row.services.filter((n) => n !== service.name);
							});
							this.renderCards();
						},
						!service.enabled && !row.services.includes(service.name),
					),
				);
			}
			if (!children.length) block.append(el("small", { class: "sp-muted" }, "No services configured"));
			delivery.append(block);
		}
		delivery.append(
			el(
				"small",
				{ class: "sp-muted" },
				row.services.length
					? "Only selected services will be offered."
					: row.method === "table"
						? "No provider selected. This delivery is arranged manually."
						: "All available provider services will be offered.",
			),
		);
		if (!row.services.length && row.method === "table")
			delivery.append(
				field(
					"Name shown at checkout",
					input(row.label, (v) => this.change(() => (row.label = v)), { maxlength: 140 }),
				),
				field(
					"Days",
					numberInput(row.days, (v) => this.change(() => (row.days = v))),
				),
			);
		const options = [
			["free", "Free"],
			["fixed", "One price"],
			["weight", "By weight"],
			["carrier", "Carrier’s price"],
			["formula", "Formula"],
		];
		const current = this.priceType(row);
		const segments = el("div", { class: "sp-segments", role: "group", "aria-label": "Price type" });
		for (const [kind, label] of options) {
			const b = button(
				label,
				() => {
					this.change(() => {
						row.method = kind === "carrier" ? "carrier" : kind === "formula" ? "formula" : "table";
						if (kind === "free") row.price_table = [{ up_to_lb: null, price: 0 }];
						if (kind === "fixed")
							row.price_table = [{ up_to_lb: null, price: row.price_table[0]?.price || null }];
						if (kind === "weight" && !row.price_table.some((t) => t.up_to_lb !== null))
							row.price_table = [
								{ up_to_lb: 1, price: row.price_table[0]?.price || 0 },
								{ up_to_lb: null, price: 0 },
							];
					});
					this.renderCards();
				},
				current === kind ? "sp-selected" : "",
			);
			b.setAttribute("aria-pressed", String(current === kind));
			segments.append(b);
		}
		pricing.append(segments);
		if (current === "fixed")
			pricing.append(
				field(
					"Price (USD)",
					numberInput(row.price_table[0].price, (v) => this.change(() => (row.price_table[0].price = v)), {
						placeholder: "0.00",
					}),
				),
			);
		if (current === "weight") {
			const tiers = el("div", { class: "sp-tiers" });
			row.price_table.forEach((tier, i) => {
				const weight = numberInput(tier.up_to_lb, (v) => this.change(() => (tier.up_to_lb = v)), {
					"aria-label": "Tier " + (i + 1) + " upper weight in pounds",
					placeholder: "and above",
				});
				const price = numberInput(tier.price, (v) => this.change(() => (tier.price = v)), {
					"aria-label": "Tier " + (i + 1) + " price in USD",
					placeholder: "0.00",
				});
				const remove = button("×", () => {
					this.change(() => row.price_table.splice(i, 1));
					this.renderCards();
				});
				remove.setAttribute("aria-label", "Remove tier " + (i + 1));
				tiers.append(
					el(
						"div",
						{ class: "sp-tier" },
						el("span", {}, "Up to"),
						weight,
						el("span", {}, "lb · $"),
						price,
						remove,
					),
				);
			});
			pricing.append(
				tiers,
				button(
					"+ Add tier",
					() => {
						this.change(() => {
							const pos = row.price_table.findIndex((t) => t.up_to_lb === null);
							row.price_table.splice(pos < 0 ? row.price_table.length : pos, 0, {
								up_to_lb: pos < 0 ? null : 0,
								price: 0,
							});
						});
						this.renderCards();
					},
					"sp-add",
				),
				el(
					"small",
					{ class: "sp-muted" },
					"Leave the final weight blank for “and above”. Orders above a bounded last tier are not offered delivery.",
				),
			);
		}
		if (current === "carrier")
			pricing.append(
				el(
					"p",
					{ class: "sp-muted" },
					"Each available service keeps its own live quote. The customer chooses at checkout.",
				),
			);
		if (current === "formula") {
			const formula = el("textarea", {
				class: "sp-input",
				rows: 3,
				placeholder: "carrier_price * 1.1 + 5",
				"aria-label": "Formula over provider price",
			});
			formula.value = row.formula || "";
			const status = el("small", { class: "sp-muted", "aria-live": "polite" });
			let timer;
			let seq = 0;
			formula.addEventListener("input", () => {
				this.change(() => (row.formula = formula.value));
				clearTimeout(timer);
				const n = ++seq;
				timer = setTimeout(async () => {
					try {
						const result = await this.call("check_formula", { source: formula.value });
						if (n === seq) status.textContent = result.ok ? "Formula is valid." : result.error;
					} catch {
						if (n === seq) status.textContent = "Could not check the formula. Try again.";
					}
				}, 350);
			});
			pricing.append(
				field("Formula applied to each quote", formula),
				el(
					"small",
					{ class: "sp-muted" },
					"Use carrier_price for that service’s quote. Also available: order_value, weight_kg, volume_l, boxes, item_count; min, max, round, ceil, floor, abs.",
				),
				status,
			);
		}
		root.append(
			button(
				"Done",
				() => {
					this.openRow = null;
					this.renderCards();
				},
				"sp-done",
			),
		);
	},
	zoneDialog(existing) {
		const zone = existing ? clone(existing) : newZone("");
		const old = existing?.name;
		const selected = new Set(zone.countries);
		const chips = el("div", { class: "sp-chips" });
		const list = el("div", { class: "sp-country-list" });
		const names = new Map(this.state.countries.map((c) => [c.code, c.name]));
		const search = input("", () => render(), {
			type: "search",
			placeholder: "Search countries",
			"aria-label": "Search countries",
		});
		const render = () => {
			chips.replaceChildren(
				...[...selected].map((c) =>
					button(
						(names.get(c) || c) + " ×",
						() => {
							selected.delete(c);
							render();
						},
						"sp-chip",
					),
				),
			);
			const term = search.value.toLowerCase();
			const matches = this.state.countries.filter(
				(c) =>
					!selected.has(c.code) &&
					(c.code.toLowerCase().includes(term) || c.name.toLowerCase().includes(term)),
			);
			list.replaceChildren(
				...matches.slice(0, 60).map((c) =>
					button(c.name + " (" + c.code + ")", () => {
						selected.add(c.code);
						render();
					}),
				),
			);
			if (matches.length > 60) list.append(el("small", { class: "sp-muted" }, "Type to narrow the list."));
		};
		const message = input(zone.not_offered_message, (v) => (zone.not_offered_message = v), {
			maxlength: 300,
			placeholder: "Message shown at checkout",
		});
		const messageField = field("Message", message);
		messageField.hidden = !zone.not_shipped;
		const body = el(
			"div",
			{},
			field(
				"Name",
				input(zone.name, (v) => (zone.name = v), { maxlength: 140 }),
			),
			field("Countries", search),
			chips,
			list,
			check("We don’t deliver to these countries", zone.not_shipped, (v) => {
				zone.not_shipped = v;
				messageField.hidden = !v;
			}),
			messageField,
		);
		const save = button(
			"Use country group",
			() => {
				zone.name = zone.name.trim();
				if (!zone.name) return this.notice("Enter a name.");
				zone.countries = [...selected];
				this.change((d) => {
					if (existing) {
						const at = d.zones.findIndex((z) => z.name === old);
						d.zones[at] = zone;
						d.rules.forEach((r) => {
							if (r.zone === old) r.zone = zone.name;
						});
					} else d.zones.push(zone);
				});
				this.closeDialog();
				this.renderZones();
				this.renderCards();
			},
			"sp-primary",
		);
		const actions = [save];
		if (existing)
			actions.unshift(
				button("Delete group", () => {
					if (window.confirm("Delete this country group and its delivery rows?")) {
						this.change((d) => {
							d.zones = d.zones.filter((z) => z.name !== old);
							d.rules = d.rules.filter((r) => r.zone !== old);
						});
						this.closeDialog();
						this.renderZones();
						this.renderCards();
					}
				}),
			);
		this.dialog(existing ? "Edit country group" : "Add country group", body, actions);
		render();
	},
	importDialog() {
		const config = el("input", { type: "file", accept: ".csv", "aria-label": "Shopify shipping CSV" });
		const blocked = el("input", {
			type: "file",
			accept: ".csv",
			"aria-label": "Not-delivered countries CSV",
		});
		const preview = el("div", {});
		let imported = null;
		const read = button("Read files", async () => {
			if (!config.files[0]) return this.notice("Choose the shipping CSV file.");
			read.disabled = true;
			try {
				imported = await this.call("import_shopify", {
					config_csv: await config.files[0].text(),
					not_delivered_csv: blocked.files[0] ? await blocked.files[0].text() : null,
				});
				preview.replaceChildren(
					el(
						"p",
						{},
						imported.summary.sets +
							" product groups · " +
							imported.summary.rows +
							" delivery rows · " +
							imported.summary.zones +
							" country groups",
					),
					el(
						"ul",
						{},
						imported.notes.map((t) => el("li", {}, t)),
					),
				);
				if (imported.problems_for_countries.length)
					preview.append(
						el(
							"p",
							{ class: "sp-blocked" },
							"Not in the ERP Country list: " +
								imported.problems_for_countries.join(", ") +
								". Remove these countries from the groups or update the Country list before saving.",
						),
					);
				use.disabled = false;
			} catch {
				this.notice("These files could not be imported.");
			} finally {
				read.disabled = false;
			}
		});
		const use = button(
			"Use as starting policy",
			() => {
				if (!imported) return;
				if (
					this.draft.rules.length &&
					!window.confirm("Replace the current draft’s groups and delivery rows with this import?")
				)
					return;
				this.change((d) => {
					d.zones = clone(imported.zones);
					d.item_sets = clone(imported.item_sets);
					d.rules = clone(imported.rules);
					d.rules.forEach((r) => (r.formula = ""));
				});
				this.closeDialog();
				this.render();
				this.changed();
			},
			"sp-primary",
		);
		use.disabled = true;
		this.dialog(
			"Import Shopify rates",
			el(
				"div",
				{},
				el(
					"p",
					{ class: "sp-muted" },
					"An optional starting point. Every product group, country, provider, and price remains editable. Nothing changes at checkout until you save.",
				),
				field("Shipping rates", config),
				field("Not-delivered countries · optional", blocked),
				read,
				preview,
			),
			[use],
		);
	},
});
