// Part of the Shipping policy wizard page: shipping_policy_wizard.js stitches it in and says how.
//
// The Review tab's body: where the policy goes live. It reads top down, as the tape's receipt does: the problems
// that stop the draft from being applied (each with a link to the step that owns it), what applying it changes,
// the whole policy in plain words zone by zone, and, under a double rule, the Active switch and Apply.
//
// What will change and the problems are the server's word (preview_changes): asked when the step is drawn and
// 250 ms after each change of the draft while it is shown. Every request gets a number and only the newest
// one's answer is shown; until the answer is for the draft as it is, the last one is shown dimmed and Apply
// waits. The policy in plain words is the draft's own (sp.policy_summary, wizard_policy_words.js), its rules in
// the words of the Rules step's cards with commas (sp.rule_statement).
//
// Apply sends the whole draft with the version the wizard loaded (apply_policy). While it runs the desk is frozen
// and the wizard is busy (wizard.set_busy: nothing changes the draft or the step), and a second click sends
// nothing. Then:
//	applied      the saved policy is read again (wizard.reload_saved: the draft becomes it, the stored draft goes,
//	             the tape prices again) and the step says what was applied, in the words of what will change
//	stale        someone changed the policy meanwhile: the draft is kept, and the person chooses to review the
//	             differences (wizard.rebase_on_saved) or to reload the saved policy; nothing is sent again by itself
//	refused      the server's sentences in apply-error, each as plain text: they arrive escaped, through Frappe's
//	             message path, and are read with sp.refusal_lines. "Someone else is applying" is one of them
//	no word      no answer, or none the step can read (a lost connection, a status Frappe answers by itself, a 200
//	             it cannot read): the write may still have gone through, so the saved policy is read again before
//	             anything is said (written): "Applied. The server answered late." (adopted as above), "Could not
//	             apply: the server did not accept the change. Nothing was changed.", stale as above, or, when the
//	             server cannot be asked either, "Could not reach the server. Nothing was changed."
//	unread       applied, but the saved policy could not be read again: Apply waits for a reload of the page
// What the step shows that is not the draft lives in wizard.ui.review, since the panel is drawn again on every
// tab switch and after Apply: the newest answer and the draft it is for, the request on its way, the outcome of
// the last Apply.

const PREVIEW_DELAY = 250; // ms after the last change before the server checks the draft again

// An outcome that says something about the draft it was for: it goes once the draft changes.
const PASSING_OUTCOMES = ["applied", "refused", "not_accepted", "unreachable", "rebased"];

// Asks the server, now, what applying the draft would change and what stops it from being applied.
function preview_draft(wizard) {
	const ui = wizard.ui.review;
	clearTimeout(ui.timer);
	ui.timer = null;
	const draft = wizard.store.draft;
	const key = JSON.stringify(draft);
	const sequence = (ui.sequence = (ui.sequence || 0) + 1);
	ui.asked = key;
	ui.failed = false;
	ui.view?.show();
	wizard.call("preview_changes", { draft }, { quiet: true }).then(
		(answer) => {
			if (sequence !== ui.sequence) return;
			ui.asked = null;
			ui.answer = sp.is_record(answer) ? answer : { changes: null, problems: [] };
			ui.answer_for = key;
			ui.view?.show();
		},
		() => {
			if (sequence !== ui.sequence) return;
			ui.asked = null;
			ui.failed = true;
			ui.view?.show();
		}
	);
}

// The draft changed: an answer on its way is for an older draft now, and the server is asked 250 ms after the
// last change, if the step is still shown then (it asks again when it is drawn).
function schedule_preview(wizard) {
	const ui = wizard.ui.review;
	ui.sequence = (ui.sequence || 0) + 1;
	ui.asked = null;
	clearTimeout(ui.timer);
	ui.timer = setTimeout(() => {
		ui.timer = null;
		if (wizard.store.step === "review") preview_draft(wizard);
		else ui.view?.show();
	}, PREVIEW_DELAY);
}

// apply_policy, as a promise of {ok: true, answer} or {ok: false, stale, lines}; no lines and not stale is no
// usable word. A refusal is not shown in Frappe's own dialog (silent): the step shows it. While the request runs
// the desk is frozen (Frappe's freeze). The promise always settles: for some answers Frappe calls neither
// callback (a 200 it cannot read; its own handlers for a deadlock or a timeout on the server), so `always`, which
// runs once Frappe is done with the request (before its error handlers, hence the tick), settles it with no word.
function apply_draft(draft, version) {
	return new Promise((resolve) => {
		let settled = false;
		const settle = (reply) => {
			if (settled) return;
			settled = true;
			resolve(reply);
		};
		const no_word = { ok: false, stale: false, lines: [] };
		frappe.call({
			method: METHOD + "apply_policy",
			args: { draft, expected_version: version },
			silent: true,
			freeze: true,
			freeze_message: __("Applying the policy…"),
			callback: (response) => {
				// an answer is the new version and what changed; anything else is no word the step can use
				const answer = response?.message;
				settle(sp.is_record(answer) && typeof answer.version === "string" ? { ok: true, answer } : no_word);
			},
			error: (response) => {
				// the answer, or for a code Frappe has no handler for, the request that carries it
				const answer =
					response && typeof response.getResponseHeader === "function" ? response.responseJSON : response;
				settle({
					ok: false,
					stale: answer?.exc_type === "TimestampMismatchError",
					lines: sp.refusal_lines(response),
				});
			},
			always: () => setTimeout(() => settle(no_word), 0),
		});
	});
}

// Whether an Apply that gave no usable word was written, from the saved policy read again: "unchanged" (its
// version is the one the page sent, so nothing was written), "applied" (it is the draft that was sent, the row
// names Apply gave the new rules aside: compared by the server, preview_changes), "changed" (someone else's
// change, or one that cannot be compared), or "unreachable" (the server cannot be asked).
function written(wizard, sent, expected) {
	return wizard.call("wizard_state", {}, { quiet: true }).then(
		(state) => {
			if (state?.version === expected) return "unchanged";
			const saved = Array.isArray(state?.policy?.rules) ? state.policy.rules : [];
			const rules = Array.isArray(sent.rules) ? sent.rules : [];
			if (saved.length !== rules.length) return "changed";
			// Apply keeps the rules in the draft's order and gives each its row: the server pairs rules by row
			const named = {
				...sent,
				rules: rules.map((rule, at) => (sp.is_record(rule) ? { ...rule, name: saved[at]?.name ?? null } : rule)),
			};
			return wizard.call("preview_changes", { draft: named }, { quiet: true }).then(
				(answer) => {
					if (!sp.is_record(answer?.changes)) return "changed";
					return sp.change_lines(answer.changes).length ? "changed" : "applied";
				},
				() => "changed"
			);
		},
		() => "unreachable"
	);
}

// What the step says once the draft is applied, by whether the policy was and is on.
function applied_sentence(outcome) {
	const on = Boolean(outcome.after?.active);
	const was = Boolean(outcome.before?.active);
	if (on && was) return __("Applied. Customers see the new prices now.");
	if (on) return __("Applied and switched on. Customers see the new prices now.");
	if (was) return __("Applied and switched off. Customers pay the carrier's price again.");
	return __("Applied. The policy is off, so customers still pay the carrier's price.");
}

// Lines of what will change (sp.change_lines) as a list: the label, then what it says. A setting reads
// "off → on", the arrow said as "becomes".
function change_list(lines, testid) {
	return el(
		"ul",
		{ class: "sp-changes", "data-testid": testid },
		lines.map((line) =>
			el(
				"li",
				{ class: "sp-change" },
				line.label ? el("span", { class: "sp-change-label" }, __("{0}:", [line.label])) : null,
				"from" in line
					? el(
							"span",
							{ class: "sp-change-text" },
							line.from,
							" ",
							el("span", { class: "sp-change-arrow", role: "img", "aria-label": __("becomes") }, "→"),
							" ",
							line.to
					  )
					: el("span", { class: "sp-change-text" }, line.text)
			)
		)
	);
}

class ReviewView {
	constructor(wizard, panel) {
		this.wizard = wizard;
		const ui = (this.ui = wizard.ui.review);
		ui.view = this; // the view drawn last: answers that arrive later go to it

		this.problem_list = el("ul", { class: "sp-review-problem-list" });
		this.problems = el(
			"section",
			{
				class: "sp-review-part sp-review-problems",
				"aria-labelledby": "sp-review-problems-title",
				"data-testid": "review-problems",
				hidden: true,
			},
			el("h3", { class: "sp-review-title", id: "sp-review-problems-title" }, __("Fix these before you apply")),
			this.problem_list
		);
		this.diff = el("div", { class: "sp-review-diff", "data-testid": "review-diff" });
		this.summary = el("div", { class: "sp-review-summary", "data-testid": "review-summary" });

		// going live: the Active switch, Apply and what it says
		this.active = el("input", {
			type: "checkbox",
			role: "switch",
			id: "sp-policy-active",
			class: "sp-switch",
			"aria-describedby": "sp-policy-active-state",
			"data-testid": "policy-active",
		});
		this.active.addEventListener("change", () => {
			const on = this.active.checked;
			const changed = wizard.change((draft) => {
				draft.active = on;
			});
			if (!changed) this.active.checked = Boolean(wizard.store.draft.active); // while Apply writes, it stays
		});
		this.active_state = el("span", {
			class: "sp-switch-state",
			id: "sp-policy-active-state",
			"data-testid": "policy-active-state",
		});
		this.button = el("button", {
			type: "button",
			class: "btn btn-primary btn-sm sp-apply",
			"aria-describedby": "sp-apply-why",
			"data-testid": "apply-button",
			onclick: () => this.apply(),
		});
		this.why = el("span", { id: "sp-apply-why", "data-testid": "apply-hint" });
		this.retry = el(
			"button",
			{ type: "button", class: "sp-link", "data-testid": "review-check-again", onclick: () => preview_draft(wizard) },
			__("Check again")
		);
		this.error = el("div", { class: "sp-apply-error", role: "alert", "data-testid": "apply-error" });
		this.done = el("div", { class: "sp-apply-done", role: "status", "data-testid": "apply-done" });

		panel.append(
			el(
				"p",
				{ class: "sp-step-intro" },
				__("Check what applying the draft changes, and read the whole policy as it will work. Nothing is live until you apply it.")
			),
			this.problems,
			el(
				"section",
				{ class: "sp-review-part", "aria-labelledby": "sp-review-diff-title" },
				el("h3", { class: "sp-review-title", id: "sp-review-diff-title", tabindex: "-1" }, __("What will change")),
				this.diff
			),
			el(
				"section",
				{ class: "sp-review-part", "aria-labelledby": "sp-review-summary-title" },
				el("h3", { class: "sp-review-title", id: "sp-review-summary-title" }, __("The policy in plain words")),
				this.summary
			),
			el(
				"section",
				{ class: "sp-golive", "aria-label": __("Going live") },
				el(
					"div",
					{ class: "sp-switch-row" },
					this.active,
					el("label", { class: "sp-switch-label", for: "sp-policy-active" }, __("Active")),
					this.active_state
				),
				el("div", { class: "sp-apply-row" }, this.button, el("p", { class: "sp-apply-why" }, this.why, this.retry)),
				this.error,
				this.done
			)
		);
		this.show();
		// the server's word on the draft as it is, unless it is being asked for this very draft already
		if (ui.asked !== JSON.stringify(wizard.store.draft)) preview_draft(wizard);
	}

	// Whether the newest answer is for the draft as it is, with nothing newer on its way.
	current() {
		const ui = this.ui;
		return !ui.timer && !ui.asked && ui.answer_for === JSON.stringify(this.wizard.store.draft);
	}

	problems_now() {
		const problems = this.ui.answer?.problems;
		return Array.isArray(problems) ? problems.map(String) : [];
	}

	// Everything the step shows, from the draft, the newest answer and the outcome of the last Apply. A part is
	// drawn again only when what it says changed, so that the focus stays where it is.
	show() {
		const ui = this.ui;
		const key = JSON.stringify(this.wizard.store.draft);
		if (PASSING_OUTCOMES.includes(ui.outcome?.kind) && ui.outcome.draft !== key) ui.outcome = null;
		const current = this.current();
		const problems = this.problems_now();
		this.show_problems(problems);
		this.problems.classList.toggle("is-stale", !current);
		this.show_changes(current);
		this.show_summary();
		this.show_going_live(current, problems);
		this.show_outcome();
	}

	show_problems(problems) {
		const said = JSON.stringify(problems);
		if (said === this.problems_said) return;
		this.problems_said = said;
		this.problems.hidden = !problems.length;
		this.problem_list.replaceChildren(
			...problems.map((sentence) => {
				const step = sp.problem_step(sentence);
				const name = step ? STEPS.find((entry) => entry.id === step)?.label : null;
				return el(
					"li",
					{ class: "sp-review-problem", "data-testid": "review-problem" },
					el("span", { class: "sp-review-problem-text" }, sentence),
					name
						? el(
								"button",
								{
									type: "button",
									class: "sp-link sp-fix",
									"aria-label": __("Fix it in the step “{0}”", [name]),
									"data-step": step,
									"data-testid": "review-fix",
									onclick: () => this.wizard.set_step(step, { focus: true }),
								},
								__("Fix it")
						  )
						: null
				);
			})
		);
	}

	// What will change, as the newest answer says it (dimmed while the draft is checked again). Its settings
	// read before and after from the saved policy and from the draft that answer is for.
	show_changes(current) {
		const ui = this.ui;
		let content;
		if (!ui.answer) {
			content = ui.failed ? __("The draft could not be checked.") : __("Checking the draft…");
		} else {
			const lines = sp.change_lines(ui.answer.changes, this.wizard.store.original, JSON.parse(ui.answer_for));
			content = lines.length ? lines : __("Nothing to apply yet.");
		}
		// after a stale Apply what is shown compares the draft with the policy the page loaded, which has changed
		const outdated = ui.outcome?.kind === "stale";
		this.diff.classList.toggle("is-stale", outdated || (Boolean(ui.answer) && !current));
		const said = JSON.stringify([content, outdated]);
		if (said === this.changes_said) return;
		this.changes_said = said;
		// only nodes: replaceChildren would print a null as the text "null"
		const parts = [];
		if (outdated) {
			parts.push(
				el(
					"p",
					{ class: "sp-out-of-date", "data-testid": "review-out-of-date" },
					__(
						"This is out of date: someone changed the policy after you opened it. Review the differences to see what applying would change now."
					)
				)
			);
		}
		parts.push(typeof content === "string" ? el("p", { class: "sp-empty" }, content) : change_list(content, "review-changes"));
		this.diff.replaceChildren(...parts);
	}

	// The whole draft in plain words, zone by zone. With the rate card as the price source the rules, the fallbacks
	// and "Anything not covered" are said quietly: the first sentence says they are not used (ruling P89).
	show_summary() {
		const names = new Map(this.wizard.state.countries.map((country) => [country.code, country.name]));
		const summary = sp.policy_summary(this.wizard.store.draft, (code) => names.get(code) || code);
		const said = JSON.stringify(summary);
		if (said === this.summary_said) return;
		this.summary_said = said;
		this.summary.classList.toggle("is-unused", summary.unused);
		const paragraph = (...sentences) => {
			const text = sentences.filter(Boolean).join(" ");
			return text ? el("p", { class: "sp-summary-text" }, text) : null;
		};
		const pricing = (sentence) => (sentence ? el("p", { class: "sp-summary-text sp-summary-pricing" }, sentence) : null);
		this.summary.replaceChildren(
			el("p", { class: "sp-summary-source" }, summary.source),
			...summary.zones.map((zone) =>
				el(
					"article",
					{ class: "sp-summary-zone", "data-testid": "review-zone" },
					el(
						"h4",
						{ class: "sp-summary-name" },
						el("span", {}, zone.name),
						zone.states.map((state) =>
							el("span", { class: state.kind === "not_shipped" ? "sp-state is-not-shipped" : "sp-state" }, state.text)
						)
					),
					paragraph(zone.where, zone.off, zone.never, ...zone.read),
					zone.rules.length
						? el(
								"ul",
								{ class: "sp-summary-rules", "aria-label": __("Rules, in the order they are checked") },
								zone.rules.map((rule) => el("li", {}, rule))
						  )
						: null,
					pricing(zone.no_rules),
					pricing(zone.fallback)
				)
			),
			...summary.closing.map((line) => el("p", { class: "sp-summary-closing" }, line))
		);
	}

	// Why Apply can or cannot be used now: {ready, why, retry}.
	apply_state(current, problems) {
		const ui = this.ui;
		const store = this.wizard.store;
		const outcome = ui.outcome?.kind;
		if (ui.applying) return { ready: false, why: __("Applying the draft…") };
		if (outcome === "stale") {
			return { ready: false, why: __("Review the differences or reload the saved policy first.") };
		}
		if (outcome === "unread") return { ready: false, why: __("Reload the page before you apply again.") };
		if (!store.dirty) return { ready: false, why: __("Nothing to apply yet.") };
		if (!current) {
			return ui.failed && !ui.timer && !ui.asked
				? { ready: false, why: __("The draft could not be checked."), retry: true }
				: { ready: false, why: __("Checking the draft…") };
		}
		if (problems.length) {
			return {
				ready: false,
				why:
					problems.length === 1
						? __("Fix the problem at the top first.")
						: __("Fix the {0} problems at the top first.", [problems.length]),
			};
		}
		if (store.draft.active) return { ready: true, why: __("Customers pay what this policy says as soon as it is applied.") };
		return {
			ready: true,
			why: store.original?.active
				? __("Customers pay the carrier's price again as soon as it is applied.")
				: __("The policy stays off: customers keep paying the carrier's price."),
		};
	}

	show_going_live(current, problems) {
		const store = this.wizard.store;
		const on = Boolean(store.draft.active);
		this.active.checked = on;
		this.active_state.textContent = on
			? __("Switched on: customers pay what this policy says")
			: __("Switched off: customers pay the carrier's price");
		let label = __("Apply");
		if (this.ui.applying) label = __("Applying…");
		else if (on !== Boolean(store.original?.active)) label = on ? __("Apply and switch on") : __("Apply and switch off");
		this.button.textContent = label;
		const state = this.apply_state(current, problems);
		this.button.setAttribute("aria-disabled", String(!state.ready));
		this.button.classList.toggle("disabled", !state.ready);
		this.why.textContent = state.why;
		this.retry.hidden = !state.retry;
	}

	// What the last Apply came to: a problem in apply-error (an alert), what was applied in apply-done (a status).
	show_outcome() {
		const outcome = this.ui.outcome;
		const said = JSON.stringify(outcome ? [outcome.kind, outcome.lines, outcome.unreachable, outcome.late] : null);
		if (said === this.outcome_said) return;
		this.outcome_said = said;
		const error = [];
		const done = [];
		const kind = outcome?.kind;
		if (kind === "stale") {
			error.push(
				el("p", {}, __("Someone changed the policy after you opened it. Your draft is kept.")),
				outcome.unreachable ? el("p", {}, __("Could not reach the server. Try again.")) : null,
				el(
					"div",
					{ class: "sp-apply-actions" },
					el(
						"button",
						{
							type: "button",
							class: "btn btn-default btn-xs",
							"data-testid": "stale-review",
							onclick: () => this.review_differences(),
						},
						__("Review the differences")
					),
					el(
						"button",
						{
							type: "button",
							class: "btn btn-default btn-xs",
							"data-testid": "stale-reload",
							onclick: () => this.reload_saved(),
						},
						__("Reload the saved policy")
					)
				)
			);
		} else if (kind === "refused") {
			error.push(el("p", {}, outcome.lines.length ? __("The policy was not applied:") : __("The policy was not applied.")));
			if (outcome.lines.length) error.push(el("ul", { class: "sp-apply-lines" }, outcome.lines.map((line) => el("li", {}, line))));
		} else if (kind === "not_accepted") {
			error.push(el("p", {}, __("Could not apply: the server did not accept the change. Nothing was changed.")));
		} else if (kind === "unreachable") {
			error.push(el("p", {}, __("Could not reach the server. Nothing was changed.")));
		} else if (kind === "unread") {
			error.push(
				el(
					"p",
					{},
					__("The policy was applied, but the saved policy could not be read again. Reload the page before you change anything.")
				)
			);
		} else if (kind === "applied") {
			const lines = sp.change_lines(outcome.changes, outcome.before, outcome.after);
			// the title line takes the focus once the step is drawn again, not the whole receipt
			done.push(
				el(
					"p",
					{ class: "sp-apply-done-title", tabindex: "-1", "data-testid": "apply-done-title" },
					outcome.late ? __("Applied. The server answered late.") : applied_sentence(outcome)
				)
			);
			if (lines.length) done.push(change_list(lines, "apply-changes"));
		} else if (kind === "rebased") {
			done.push(
				el("p", {}, __("The saved policy was read again: what will change now compares your draft with it."))
			);
		}
		this.error.replaceChildren(...error.filter(Boolean));
		this.done.replaceChildren(...done);
	}

	// ---------------------------------------------------------- Apply
	apply() {
		const wizard = this.wizard;
		const ui = this.ui;
		const store = wizard.store;
		if (!this.apply_state(this.current(), this.problems_now()).ready) return;
		const before = sp.clone(store.original);
		const sent = sp.clone(store.draft);
		const sent_key = JSON.stringify(sent);
		const expected = store.version;
		ui.applying = true;
		ui.outcome = null;
		wizard.set_busy(true);
		this.show();
		// The end of an Apply: the wizard is free again, and the step says how it went.
		const finish = (outcome) => {
			ui.applying = false;
			wizard.set_busy(false);
			ui.outcome = outcome;
			ui.view?.show();
		};
		// The write went through: the saved policy is read again, and the step says what was applied.
		const adopt = (changes, late) =>
			wizard.reload_saved().then(
				() => {
					finish({ kind: "applied", late, before, after: sent, changes, draft: JSON.stringify(store.draft) });
					ui.view?.done.querySelector('[data-testid="apply-done-title"]')?.focus();
				},
				() => finish({ kind: "unread" })
			);
		apply_draft(sent, expected).then((reply) => {
			if (reply.ok) return adopt(reply.answer.changes, false);
			// Refused: nothing was written. A refusal the preview could not foresee (someone else applying, a save
			// the ERP itself refused) leaves Apply as it was, with the server's sentences beside it.
			if (reply.stale) return finish({ kind: "stale" });
			if (reply.lines.length) return finish({ kind: "refused", lines: reply.lines, draft: sent_key });
			// No word the step can use: the write may have gone through, so the saved policy says what happened.
			return written(wizard, sent, expected).then((found) => {
				if (found === "applied") return adopt(null, true);
				if (found === "unchanged") return finish({ kind: "not_accepted", draft: sent_key });
				if (found === "changed") return finish({ kind: "stale" });
				return finish({ kind: "unreachable", draft: sent_key });
			});
		});
	}

	// After someone else changed the policy: the saved policy, read again, becomes what the draft is compared
	// with and applied over. The draft stays, and what will change now says what it changes in that policy.
	review_differences() {
		const wizard = this.wizard;
		const ui = this.ui;
		wizard.rebase_on_saved().then(
			() => {
				ui.outcome = { kind: "rebased", draft: JSON.stringify(wizard.store.draft) };
				ui.view?.show();
				document.getElementById("sp-review-diff-title")?.focus();
			},
			() => {
				ui.outcome = { kind: "stale", unreachable: true };
				ui.view?.show();
			}
		);
	}

	// After someone else changed the policy: drop the draft for the policy as it is saved now, once asked.
	reload_saved() {
		const wizard = this.wizard;
		const ui = this.ui;
		let chosen = false;
		const dialog = frappe.confirm(__("Discard your draft and load the policy as it is saved now?"), () => {
			chosen = true;
		});
		// once the dialog is gone: the desk's Escape takes the focus away while the dialog closes
		dialog.$wrapper.one("hidden.bs.modal", () => {
			if (!chosen) {
				ui.view?.error.querySelector('[data-testid="stale-reload"]')?.focus();
				return;
			}
			wizard.reload_saved().then(
				() => {
					ui.outcome = null;
					ui.view?.show();
					ui.view?.button.focus();
				},
				() => {
					ui.outcome = { kind: "stale", unreachable: true };
					ui.view?.show();
				}
			);
		});
	}
}

const step_review = {
	render(wizard, panel) {
		wizard.ui.review.view = new ReviewView(wizard, panel);
	},
	refresh(wizard, panel) {
		schedule_preview(wizard);
		wizard.ui.review.view?.show();
	},
	priced(wizard, panel, answer) {},
};
