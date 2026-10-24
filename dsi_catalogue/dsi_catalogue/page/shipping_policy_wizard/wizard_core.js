// Part of the Shipping policy wizard page: shipping_policy_wizard.js stitches it in and says how.
//
// The wizard: the store and the draft kept in this browser, the regions of the page, the server calls.

// The browser keeps one draft, {user, version, draft, step}, and it belongs to the person who made it: for
// anyone else signed in on this browser it is not there (not restored, no notice, not removed), until their own
// draft takes its place. Storage can be switched off or full: the draft then lives in this tab only.

// The stored draft of the person signed in, or null.
function read_stored() {
	try {
		const text = window.localStorage.getItem(DRAFT_KEY);
		if (!text) return null;
		const stored = JSON.parse(text);
		if (sp.is_record(stored)) return stored.user === frappe.session.user ? stored : null;
	} catch (error) {
		// not readable: dropped below
	}
	forget_stored();
	return null;
}

// Removes the stored draft, unless it is someone else's.
function forget_stored() {
	try {
		let owner = null;
		try {
			owner = JSON.parse(window.localStorage.getItem(DRAFT_KEY))?.user || null;
		} catch (error) {
			owner = null; // not readable: nobody's
		}
		if (owner && owner !== frappe.session.user) return;
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
		// What a step's body shows that is not the draft (an open editor, a scroll position), by step id. The
		// panel is emptied and drawn again on every tab switch, so the body keeps such things here; they last for
		// the session and are not stored with the draft.
		this.ui = Object.fromEntries(STEPS.map((step) => [step.id, {}]));
		this.notice = null; // "restored" or "discarded": what happened to a stored draft on load
		this.busy = false; // Apply is writing the draft: nothing changes it, and the step stays, until it is done
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
		this.priced = null; // an answer was for the draft that is gone
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

	// The saved policy, read again: the draft becomes it (Apply wrote the draft, or the person dropped their draft
	// for what someone else saved). Nothing is left in storage, and the tape prices the order again.
	reload_saved() {
		return this.call("wizard_state", {}, { quiet: true }).then((state) => {
			this.take_state(state);
			this.notice = null;
			this.save_draft(); // the draft is the saved policy: the stored draft goes
			this.render();
			this.tape.draft_changed();
		});
	}

	// The saved policy, read again, becomes what the draft is compared with and applied over, and the draft
	// stays as it is: after someone else changed the policy, the Review step then shows what the draft changes in
	// the policy as it is saved now, and Apply writes it over that. The draft is stored again with the new version.
	rebase_on_saved() {
		return this.call("wizard_state", {}, { quiet: true }).then((state) => {
			const draft = this.store.draft;
			this.take_state(state);
			this.store.draft = draft;
			this.store.dirty = !sp.same(draft, this.store.original);
			this.save_draft();
			this.render();
			this.tape.draft_changed();
		});
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
		this.step_box = el("div", { class: "sp-step", "aria-busy": "false" }, Object.values(this.panels));
		// The layout is one element inside the root, so that the style can lay it out by the root's own width.
		this.root.replaceChildren(
			el(
				"div",
				{ class: "sp-layout" },
				this.banner,
				this.tabs,
				this.step_box,
				tape
			)
		);
		this.tape = new dsi_catalogue.ShippingPolicyTape(this, tape);
	}

	render() {
		this.render_banner();
		this.render_tabs();
		this.render_step();
	}

	// What the banner says, a line each: {text, action: {label, testid, run}}. While the saved policy is on and
	// the draft differs from it, a line says that what is changed goes live on Apply.
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
		if (this.store.original?.active && this.store.dirty) {
			lines.push({ text: __("This policy is on: edits go live when you apply them.") });
		}
		return lines;
	}

	// The banner is a live region: it is drawn again only when what it says changes, so that a line is not read
	// out again after every change of the draft.
	render_banner() {
		const lines = this.banner_lines();
		const said = JSON.stringify(lines.map((line) => [line.text, line.action?.testid]));
		if (said === this.banner_said) return;
		this.banner_said = said;
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

	// Draws the selected step's body into its emptied panel, then hands it the latest answer of try_order
	// (null while nothing is priced), so a body drawn on its first show or on a tab switch is never without it.
	// While a change is still being priced (tape.pending) that answer is for an older draft, whose rules may
	// stand in other places: the body gets null, and the next answer when it comes. The tape is told which step it
	// stands beside (its title says that it prices the draft beside the Test tab).
	render_step() {
		for (const step of STEPS) this.panels[step.id].hidden = step.id !== this.store.step;
		const step = this.current_step();
		const panel = this.panels[step.id];
		panel.replaceChildren();
		step.render(this, panel);
		this.tape?.show_step(step.id);
		step.priced?.(this, panel, this.tape?.pending ? null : this.priced);
	}

	current_step() {
		return STEPS.find((step) => step.id === this.store.step) || STEPS[0];
	}

	set_step(id, { focus = false } = {}) {
		if (this.busy || !STEPS.some((step) => step.id === id)) return; // the step stays while Apply writes
		if (id !== this.store.step) {
			this.store.step = id;
			this.render_tabs();
			this.render_step();
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
	// Changes the draft, and says whether it did: while Apply writes the draft nothing changes it (a key pressed
	// under the desk's freeze would otherwise be lost when the saved policy is read again).
	change(mutate) {
		if (this.busy) return false;
		mutate(this.store.draft);
		this.after_change();
		return true;
	}

	// Apply is writing the draft (on), or has finished (off): the step area says so to assistive technology.
	set_busy(on) {
		this.busy = Boolean(on);
		this.step_box?.setAttribute("aria-busy", String(this.busy));
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
		if (!this.store.dirty) {
			forget_stored(); // a draft equal to the saved policy is not kept (someone else's stays)
			return;
		}
		try {
			const { version, draft, step } = this.store;
			const stored = { user: frappe.session.user, version, draft, step };
			window.localStorage.setItem(DRAFT_KEY, JSON.stringify(stored));
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
		if (this.busy) return;
		this.store.draft = sp.clone(this.store.original);
		this.notice = null;
		this.priced = null; // an answer was for the draft that is gone
		this.render_step();
		this.after_change();
	}

	dismiss_notice() {
		this.notice = null;
		this.render_banner();
	}
};
