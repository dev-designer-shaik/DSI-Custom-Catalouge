Object.assign(dsi_catalogue.ShippingPolicyWizard.prototype, {
	productPicker(key, sample = false) {
		const original = sample ? null : this.draft.item_sets.find((g) => g.name === key);
		const group = original ? clone(original) : newGroup("");
		const chosen = new Map(group.items.map((code) => [code, { item_code: code, item_name: code }]));
		let serial = 0,
			start = 0,
			total = 0,
			visible = [];
		const chips = el("div", { class: "sp-chips" });
		const filters = { text: "", item_group: "", pattern: "", published_only: false };
		const count = el("p", { class: "sp-muted", "aria-live": "polite" }, "Loading products…");
		const results = el("div", { class: "sp-product-results" });
		const more = button("Show more", () => {
			start += 50;
			search(true);
		});
		more.hidden = true;
		const selectAll = button("Select these results", () => {
			for (const r of visible) chosen.set(r.item_code, r);
			renderResults();
		});
		const add = button(
			sample ? "Add products" : "Use selected products",
			() => {
				if (sample) {
					for (const [code, row] of chosen) {
						const held = this.sample.items.find((i) => i.item_code === code);
						if (!held) this.sample.items.push({ item_code: code, qty: 1 });
						this.sampleNames[code] = row.item_name;
					}
					this.closeDialog();
					this.renderPreview();
				} else {
					group.items = [...chosen.keys()];
					this.change(
						(d) =>
							Object.assign(
								d.item_sets.find((g) => g.name === key),
								group,
							),
						true,
					);
					this.closeDialog();
				}
			},
			"sp-primary",
		);
		const follow = button("Follow this filter", () => {
			const field = filters.pattern ? "patterns" : "item_groups";
			const value = filters.pattern || filters.item_group;
			if (!value) return;
			group[field] = [...new Set([...group[field], value])];
			group.items = [...chosen.keys()];
			this.change(
				(d) =>
					Object.assign(
						d.item_sets.find((g) => g.name === key),
						group,
					),
				true,
			);
			this.closeDialog();
		});
		const updateFollow = () => {
			follow.disabled =
				sample ||
				!!filters.text ||
				!!filters.published_only ||
				(!filters.item_group && !filters.pattern) ||
				!!(filters.item_group && filters.pattern);
		};
		const drawChips = () => {
			chips.replaceChildren();
			for (const name of ["patterns", "item_groups"])
				for (const value of group[name])
					chips.append(
						button(
							(name === "patterns" ? "Code: " : "Item group: ") + value + " ×",
							() => {
								group[name] = group[name].filter((x) => x !== value);
								drawChips();
							},
							"sp-chip",
						),
					);
		};
		drawChips();
		const renderResults = () => {
			results.replaceChildren();
			if (!visible.length) results.append(el("p", { class: "sp-muted" }, "No products match these filters."));
			for (const row of visible) {
				const checkbox = el("input", { type: "checkbox", "aria-label": "Select " + row.item_name });
				checkbox.checked = chosen.has(row.item_code);
				checkbox.addEventListener("change", () => {
					if (checkbox.checked) chosen.set(row.item_code, row);
					else chosen.delete(row.item_code);
					add.textContent = sample
						? "Add " + chosen.size + " products"
						: "Use " + chosen.size + " selected products";
				});
				results.append(
					el(
						"label",
						{ class: "sp-product-result" },
						checkbox,
						el(
							"span",
							{},
							el("strong", {}, row.item_name),
							el("small", {}, [row.item_code, row.index_key, row.item_group].filter(Boolean).join(" · ")),
						),
						el(
							"small",
							{ class: "sp-muted" },
							row.listed_by ? "In " + row.listed_by : row.published ? "Published" : "Unpublished",
						),
					),
				);
			}
			add.textContent = sample
				? "Add " + chosen.size + " products"
				: "Use " + chosen.size + " selected products";
		};
		const search = async (append = false) => {
			const seq = ++serial;
			count.textContent = "Finding products…";
			try {
				const answer = await this.call("search_products", {
					...filters,
					sets: JSON.stringify(this.draft.item_sets),
					start,
					limit: 50,
				});
				if (seq !== serial || !results.isConnected) return;
				visible = append ? [...visible, ...answer.rows] : answer.rows;
				total = answer.count;
				count.textContent = total + " matching products";
				more.hidden = !answer.more;
				renderResults();
			} catch {
				if (seq === serial)
					count.textContent = "Products could not be loaded. Narrow the filter and try again.";
			}
		};
		let debounce;
		const changed = () => {
			start = 0;
			updateFollow();
			clearTimeout(debounce);
			debounce = setTimeout(() => search(), 220);
		};
		const text = input(
			"",
			(v) => {
				filters.text = v;
				changed();
			},
			{ type: "search", placeholder: "Search name or product code", "aria-label": "Search products" },
		);
		const itemGroup = select(
			[["", "All item groups"], ...this.state.item_groups.map((n) => [n, n])],
			"",
			(v) => {
				filters.item_group = v;
				changed();
			},
		);
		const pattern = input(
			"",
			(v) => {
				filters.pattern = v;
				changed();
			},
			{ placeholder: "e.g. ABC-*", maxlength: 80 },
		);
		const body = el(
			"div",
			{},
			chips,
			field("Find products", text),
			el("div", { class: "sp-filter-grid" }, field("Item group", itemGroup), field("Code pattern", pattern)),
			check("Published products only", false, (v) => {
				filters.published_only = v;
				changed();
			}),
			el("div", { class: "sp-results-heading" }, count, selectAll),
			results,
			more,
		);
		if (!sample)
			body.append(
				el(
					"p",
					{ class: "sp-muted" },
					"Follow one item group or one code pattern to include future matching products automatically. For combined filters, select the products you want.",
				),
			);
		this.dialog(
			sample ? "Add products to the test order" : "Products in " + key,
			body,
			sample ? [add] : [follow, add],
		);
		updateFollow();
		text.focus();
		search();
	},
});
