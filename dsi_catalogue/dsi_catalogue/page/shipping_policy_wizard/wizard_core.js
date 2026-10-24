// Part of the Shipping policy wizard page: shipping_policy_wizard.js stitches it in and says how.
//
// The wizard: the store and the draft kept in this browser, the regions of the page, the server calls.

// The stored draft, or null. Storage can be switched off or full: the draft then lives in this tab only.
function read_stored() {
	try {
		const text = window.localStorage.getItem(DRAFT_KEY);
		if (!text) return null;
		const stored = JSON.parse(text);
		if (sp.is_record(stored)) return stored;
	} catch (error) {
		// not readable: dropped below
	}
	forget_stored();
	return null;
}

function forget_stored() {
	try {
		window.localStorage.removeItem(DRAFT_KEY);
	} catch (error) {
		// nothing to do: storage is not available
	}
}

dsi_catalogue.ShippingPolicyWizard = class ShippingPolicyWizard {
	constructor(page, wrapper) {
		this.page = page;
		this.wrapper = wrapper;
		this.store = { original: null, draft: null, version: null, step: STEPS[0].id, dirty: false };
		this.state = null;
		this.priced = null;
		this.notice = null; // "restored" or "discarded": what happened to a stored draft on load
		this.save_timer = null;
		this.loading = true;
		this.root = el("div", { class: "sp-wizard" });
		this.page.main.empty().append(this.root);
		this.root.append(el("p", { class: "sp-loading" }, __("Loading the saved policy…")));
		// a change made in the last 300 ms is stored before the page goes away
		window.addEventListener("beforeunload", () => {
			if (this.save_timer) this.save_draft();
		});
		this.load();
	}

	// ---------------------------------------------------------- the server
	// A wizard method, as a promise of its answer. With quiet, a refusal is not shown in Frappe's dialog: the
	// promise is rejected with its sentences as plain text, for the page to show where it belongs.
	call(method, args = {}, { quiet = false } = {}) {
		return new Promise((resolve, reject) => {
			frappe.call({
				method: METHOD + method,
				args,
				silent: quiet,
				callback: (response) => resolve(response.message),
				error: (response) => reject(quiet ? problem_text(response) : null),
			});
		});
	}

	load() {
		return this.call("wizard_state").then(
			(state) => {
				this.loading = false;
				this.take_state(state, { restore: true });
				this.build();
				this.render();
			},
			() => {
				this.loading = false;
				this.root.replaceChildren(
					el(
						"p",
						{ class: "sp-problem" },
						__("The saved policy could not be loaded. Reload the page to try again.")
					)
				);
			}
		);
	}

	take_state(state, { restore = false } = {}) {
		const { policy, version, ...rest } = state;
		this.state = rest;
		this.store.original = sp.clone(policy);
		this.store.draft = sp.clone(policy);
		this.store.version = version;
		if (restore) this.restore_draft();
		this.store.dirty = !sp.same(this.store.draft, this.store.original);
	}

	restore_draft() {
		const stored = read_stored();
		if (!stored) return;
		if (!sp.is_record(stored.draft) || sp.same(stored.draft, this.store.original)) {
			forget_stored(); // unreadable, or nothing in it that is not saved already
			return;
		}
		if (stored.version !== this.store.version) {
			forget_stored();
			this.notice = "discarded";
			return;
		}
		this.store.draft = stored.draft;
		if (STEPS.some((step) => step.id === stored.step)) this.store.step = stored.step;
		this.notice = "restored";
	}

	// The page came back into view: when the draft is clean, take the saved policy again, so that a change
	// made on the form meanwhile is what the wizard shows.
	on_show() {
		if (this.loading || !this.tape || this.store.dirty) return;
		this.call("wizard_state").then(
			(state) => {
				if (this.store.dirty || state.version === this.store.version) return;
				this.take_state(state);
				this.render();
				this.tape.draft_changed();
			},
			() => {}
		);
	}

	// ---------------------------------------------------------- the page
	build() {
		this.banner = el("div", {
			class: "sp-banner",
			role: "status",
			"data-testid": "draft-banner",
			hidden: true,
		});
		this.tabs = el("div", {
			class: "sp-tabs",
			role: "tablist",
			"aria-label": __("Shipping policy steps"),
			onkeydown: (event) => this.on_tab_key(event),
		});
		this.panels = {};
		for (const step of STEPS) {
			this.tabs.append(
				el(
					"button",
					{
						type: "button",
						class: step.apart ? "sp-tab sp-tab-apart" : "sp-tab",
						role: "tab",
						id: `sp-tab-${step.id}`,
						"aria-controls": `sp-panel-${step.id}`,
						"aria-label": step.name || null,
						"data-label": step.label,
						"data-testid": `step-tab-${step.id}`,
						onclick: () => this.set_step(step.id),
					},
					step.label
				)
			);
			this.panels[step.id] = el("div", {
				class: "sp-panel",
				role: "tabpanel",
				id: `sp-panel-${step.id}`,
				"aria-labelledby": `sp-tab-${step.id}`,
				tabindex: "0",
				hidden: true,
			});
		}
		const tape = el("section", { class: "sp-tape", "aria-labelledby": "sp-tape-title" });
		this.root.replaceChildren(
			this.banner,
			this.tabs,
			el("div", { class: "sp-step" }, Object.values(this.panels)),
			tape
		);
		this.tape = new dsi_catalogue.ShippingPolicyTape(this, tape);
	}

	render() {
		this.render_banner();
		this.render_tabs();
		this.render_step();
	}

	// What the banner says, a line each: {text, action: {label, testid, run}}. Task 9 adds its line here.
	banner_lines() {
		const lines = [];
		if (this.notice === "restored") {
			lines.push({
				text: __("Restored your unsaved draft. Nothing is live until you apply it."),
				action: {
					label: __("Discard the draft"),
					testid: "draft-discard",
					run: () => this.confirm_discard(),
				},
			});
		} else if (this.notice === "discarded") {
			lines.push({
				text: __("Someone changed the policy after you started, so your old draft was discarded."),
				action: { label: __("Dismiss"), testid: "draft-dismiss", run: () => this.dismiss_notice() },
			});
		}
		return lines;
	}

	render_banner() {
		const lines = this.banner_lines();
		this.banner.replaceChildren(
			...lines.map((line) =>
				el(
					"p",
					{ class: "sp-banner-line" },
					el("span", {}, line.text),
					line.action
						? el(
								"button",
								{
									type: "button",
									class: "sp-link",
									"data-testid": line.action.testid,
									onclick: line.action.run,
								},
								line.action.label
						  )
						: null
				)
			)
		);
		this.banner.hidden = !lines.length;
	}

	render_tabs() {
		for (const tab of this.tabs.children) {
			const selected = tab.id === `sp-tab-${this.store.step}`;
			tab.setAttribute("aria-selected", String(selected));
			tab.tabIndex = selected ? 0 : -1;
		}
	}

	render_step() {
		for (const step of STEPS) this.panels[step.id].hidden = step.id !== this.store.step;
		const step = this.current_step();
		const panel = this.panels[step.id];
		panel.replaceChildren();
		step.render(this, panel);
	}

	current_step() {
		return STEPS.find((step) => step.id === this.store.step) || STEPS[0];
	}

	set_step(id, { focus = false } = {}) {
		if (!STEPS.some((step) => step.id === id)) return;
		if (id !== this.store.step) {
			this.store.step = id;
			this.render_tabs();
			this.render_step();
			const step = this.current_step();
			step.priced?.(this, this.panels[step.id], this.priced);
			if (this.store.dirty) this.save_draft_soon();
		}
		if (focus) document.getElementById(`sp-tab-${id}`)?.focus();
	}

	// The tab row as a tablist: the arrow keys move between tabs (and select them), Home and End jump.
	on_tab_key(event) {
		const ids = STEPS.map((step) => step.id);
		const at = ids.indexOf(this.store.step);
		const back = getComputedStyle(this.tabs).direction === "rtl" ? "ArrowRight" : "ArrowLeft";
		const ahead = back === "ArrowLeft" ? "ArrowRight" : "ArrowLeft";
		const to = { [ahead]: at + 1, [back]: at - 1, Home: 0, End: ids.length - 1 }[event.key];
		if (to === undefined) return;
		event.preventDefault();
		this.set_step(ids[(to + ids.length) % ids.length], { focus: true });
	}

	// ---------------------------------------------------------- the draft
	change(mutate) {
		mutate(this.store.draft);
		this.after_change();
	}

	after_change() {
		this.store.dirty = !sp.same(this.store.draft, this.store.original);
		if (!this.store.dirty && this.notice === "restored") this.notice = null;
		this.save_draft_soon();
		this.render_banner();
		this.render_tabs();
		const step = this.current_step();
		step.refresh?.(this, this.panels[step.id]);
		this.tape.draft_changed();
	}

	price_source() {
		return this.store.draft?.price_source === "rate_card" ? "rate_card" : "carrier";
	}

	set_priced(answer) {
		this.priced = answer;
		const step = this.current_step();
		step.priced?.(this, this.panels[step.id], answer);
	}

	save_draft_soon() {
		clearTimeout(this.save_timer);
		this.save_timer = setTimeout(() => this.save_draft(), SAVE_DELAY);
	}

	save_draft() {
		clearTimeout(this.save_timer);
		this.save_timer = null;
		if (!this.store.draft) return;
		try {
			if (this.store.dirty) {
				const { version, draft, step } = this.store;
				window.localStorage.setItem(DRAFT_KEY, JSON.stringify({ version, draft, step }));
			} else {
				window.localStorage.removeItem(DRAFT_KEY);
			}
		} catch (error) {
			// storage is full or switched off: the draft then lives in this tab only
		}
	}

	confirm_discard() {
		frappe.confirm(__("Discard your unsaved draft and start again from the saved policy?"), () =>
			this.discard_draft()
		);
	}

	discard_draft() {
		this.store.draft = sp.clone(this.store.original);
		this.notice = null;
		this.render_step();
		this.after_change();
	}

	dismiss_notice() {
		this.notice = null;
		this.render_banner();
	}
};
