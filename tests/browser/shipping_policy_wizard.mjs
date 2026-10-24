// Browser checks of the Shipping policy wizard, run against the erp2 sandbox (never the live ERP).
//
//	source ~/.cache/browser-scripts/env.sh
//	ERP_USER=wizard-test@dsi.local ERP_PASSWORD="$(cat ~/.cache/shipping-policy/erp2-test-password)" \
//		OUT_DIR=/home/mdad/Downloads/shipping-policy-erp2/task7 node tests/browser/shipping_policy_wizard.mjs
//
// PUPPETEER (the puppeteer module to import), CHROME (the browser) and LD_LIBRARY_PATH come from env.sh.
// ERP_URL defaults to https://erp2.shaik.net and OUT_DIR, where the screenshots go, to ./shots. The password
// is read from the environment only and never printed. The script prints one PASS or FAIL line per check and
// exits with 1 when any check fails. Tasks 8, 9 and 16 add their checks to this file.
//
// Nothing here writes to the ERP: the wizard writes only on Apply, and these checks never apply. A draft is
// put in the browser's own storage, where the wizard keeps it, to test restoring it and showing hostile names.
// The Zones and Rules checks build the example policy (tests/hermetic/fixtures/shipping_policy_example.json)
// through the page as a draft, price orders with it, and leave the browser's storage empty again.

import { mkdir, readFile, rm, stat } from "node:fs/promises";
import path from "node:path";

const BASE = (process.env.ERP_URL || "https://erp2.shaik.net").replace(/\/+$/, "");
const OUT_DIR = process.env.OUT_DIR || "./shots";
const WIZARD = "/desk/shipping-policy-wizard";
const FORM = "/desk/shipping-policy";
const NEUTRAL = "/api/method/frappe.auth.get_logged_user"; // a page of the site that runs no desk script
const DRAFT_KEY = "dsi_catalogue.sp_draft";
const STEPS = ["zones", "rates", "rules", "fallback", "review", "test"];
const LABELS = ["Zones", "Rates", "Rules", "If the carrier can't quote", "Review", "Test"];
const HOSTILE = "<img src=x onerror=alert(1)>";
const HOSTILE_MESSAGE = "<img src=x onerror=alert(2)>";
const OFF =
	"The policy is switched off, so today customers pay the carrier's price. This receipt shows what they " +
	"would pay once you turn the policy on in the Review step.";
const ITEM = "10005020"; // a priced fragrance on erp2 (229.00 in USD - Online)
const EXAMPLE = JSON.parse(
	await readFile(new URL("../hermetic/fixtures/shipping_policy_example.json", import.meta.url), "utf8")
);
// erp2's item group for watches (ruling P17: the ERP's group names are upper case; the fixture says "Watches")
const WATCH_GROUP = "WATCHES";
// a published watch on erp2, priced in USD - Online: ASTROTIME MK I SAPPHIRE STEEL, in AUTOMATIC under WATCHES
const WATCH = "10000137";
const ASTERISK =
	"* A rule for selected items applies only when every item in the order is one of them. A rule for every " +
	"item applies to every order.";
const HOSTILE_SET = "<b>set</b>";

// What erp2 shows on every desk page, with or without the wizard: the dsi_plm app asks for a script it does
// not ship. Seen on /desk/shipping-policy and /desk/shipping-dashboard before the wizard existed.
const KNOWN_NOISE = [/\/assets\/dsi_plm\/js\/project_customization\.js/];

for (const name of ["PUPPETEER", "CHROME", "ERP_USER", "ERP_PASSWORD"]) {
	if (!process.env[name]) {
		console.error(`FAIL setup: ${name} is not set (see the comment at the top of this file)`);
		process.exit(1);
	}
}

const puppeteer = (await import(process.env.PUPPETEER)).default;

// ---------------------------------------------------------------- results
const results = [];

function record(ok, name, detail = "") {
	results.push({ ok, name });
	console.log(`${ok ? "PASS" : "FAIL"} ${name}${detail ? `: ${detail}` : ""}`);
}

async function check(name, run) {
	try {
		const detail = await run();
		record(true, name, typeof detail === "string" ? detail : "");
	} catch (error) {
		record(false, name, error?.message || String(error));
	}
}

function assert(condition, message) {
	if (!condition) {
		throw new Error(message);
	}
}

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// ---------------------------------------------------------------- the browser
const browser = await puppeteer.launch({
	headless: true,
	executablePath: process.env.CHROME,
	args: ["--no-sandbox"],
});
const page = await browser.newPage();
await page.setCacheEnabled(false);
await page.setViewport({ width: 1400, height: 1000 });

// What the page says on its console, what fails over the network, and any dialog it opens.
const trouble = [];
const noise = (text) => KNOWN_NOISE.some((pattern) => pattern.test(text));
page.on("console", (message) => {
	if (["error", "warn", "warning"].includes(message.type())) {
		const where = message.location()?.url || "";
		const line = `console ${message.type()}: ${message.text()}${where ? ` (${where})` : ""}`;
		if (!noise(line)) trouble.push(line);
	}
});
page.on("pageerror", (error) => trouble.push(`uncaught: ${error.message}`));
page.on("requestfailed", (request) => {
	const line = `request failed: ${request.method()} ${request.url()} ${request.failure()?.errorText}`;
	if (!noise(line)) trouble.push(line);
});
page.on("response", (response) => {
	const line = `HTTP ${response.status()}: ${response.request().method()} ${response.url()}`;
	if (response.status() >= 400 && !noise(line)) trouble.push(line);
});
page.on("dialog", async (dialog) => {
	trouble.push(`dialog ${dialog.type()}: ${dialog.message()}`);
	await dialog.dismiss();
});

// The try_order and check_formula requests the page sends, and a way to hold the next one back for a while.
let priced = 0;
let checked = 0;
let intercepting = false;
let holdNext = 0; // ms to hold the next try_order request before it goes out
let holdCheck = 0; // ms to hold the next check_formula request before it goes out
let holdGroups = 0; // ms to hold the next read of the Item Group list before it goes out
page.on("request", (request) => {
	const isTry = request.url().includes("shipping_policy_wizard.try_order");
	const isCheck = request.url().includes("shipping_policy_wizard.check_formula");
	const isGroups =
		request.url().includes("frappe.client.get_list") && (request.postData() || "").includes("doctype=Item+Group");
	if (isTry) priced += 1;
	if (isCheck) checked += 1;
	if (!intercepting) return;
	const wait = (isTry && holdNext) || (isCheck && holdCheck) || (isGroups && holdGroups) || 0;
	if (wait) {
		if (isTry) holdNext = 0;
		else if (isCheck) holdCheck = 0;
		else holdGroups = 0;
		setTimeout(() => request.continue().catch(() => {}), wait);
	} else {
		request.continue().catch(() => {});
	}
});

async function intercept(on) {
	intercepting = on;
	await page.setRequestInterception(on);
}

// ---------------------------------------------------------------- helpers
const by = (testid) => `[data-testid="${testid}"]`;

async function login() {
	await page.goto(`${BASE}/login`, { waitUntil: "networkidle2" });
	const status = await page.evaluate(
		async (usr, pwd) => {
			const response = await fetch("/api/method/login", {
				method: "POST",
				headers: { "Content-Type": "application/json", Accept: "application/json" },
				body: JSON.stringify({ usr, pwd }),
			});
			await response.text(); // read to the end, so that the next page does not cut the answer off
			return response.status;
		},
		process.env.ERP_USER,
		process.env.ERP_PASSWORD
	);
	assert(status === 200, `the login answered HTTP ${status}`);
}

// Leaving a page cuts off whatever it is still loading (the item search a focused Link field starts, say),
// which the browser reports as failed requests: every navigation first waits for the network to be quiet.
async function go(route) {
	await page.waitForNetworkIdle({ idleTime: 400, timeout: 20000 }).catch(() => {});
	await page.goto(`${BASE}${route}`, { waitUntil: "networkidle2", timeout: 60000 });
}

async function open(route = WIZARD) {
	await go(route);
	if (route === WIZARD) {
		await page.waitForSelector(by("tape-total"), { timeout: 20000 });
	}
}

// A full-page screenshot, written anew; the check that takes it fails unless the file is there and not empty.
async function shot(name) {
	await mkdir(OUT_DIR, { recursive: true });
	const file = path.resolve(OUT_DIR, `${name}.png`);
	await rm(file, { force: true });
	await page.screenshot({ path: file, fullPage: true });
	const written = await stat(file).catch(() => null);
	assert(written?.size > 0, `the screenshot ${file} was not written`);
	return file;
}

// The desk scrolls its main section, not the window, so a full-page screenshot stops at the window's foot. This
// one makes the window as tall as the main section's content for the shot (at most 4000 px), then puts the
// window back: the whole step is in the picture.
async function tallShot(name) {
	const size = page.viewport();
	const height = await page.evaluate(() => {
		const main = document.querySelector(".main-section");
		return Math.ceil(Math.max(main ? main.scrollHeight : 0, document.documentElement.scrollHeight));
	});
	await page.setViewport({ ...size, height: Math.min(Math.max(height + 24, size.height), 4000) });
	await pause(400);
	try {
		return await shot(name);
	} finally {
		await page.setViewport(size);
		await pause(300);
	}
}

// The text of an element as it reads on the screen (innerText: what is hidden is left out, and lines are
// apart), with the white space between words made single.
async function textOf(testid) {
	return page.$eval(by(testid), (element) => element.innerText.replace(/\s+/g, " ").trim());
}

// Waits until the element's text holds `text` (a string, or a regular expression).
async function expectText(testid, text, timeout = 10000) {
	const want = text instanceof RegExp ? { source: text.source, flags: text.flags } : { plain: text };
	try {
		await page.waitForFunction(
			(selector, want) => {
				const element = document.querySelector(selector);
				if (!element) return false;
				const seen = element.innerText.replace(/\s+/g, " ").trim();
				return want.plain !== undefined
					? seen.includes(want.plain)
					: new RegExp(want.source, want.flags).test(seen);
			},
			{ timeout },
			by(testid),
			want
		);
	} catch {
		const seen = (await page.$(by(testid))) ? await textOf(testid) : "(no such element)";
		throw new Error(`${testid} should hold ${text}, it says "${seen}"`);
	}
}

function expectNoConsoleErrors() {
	const found = trouble.splice(0);
	assert(found.length === 0, found.join(" | "));
}

async function theme(name) {
	await page.evaluate((name) => document.documentElement.setAttribute("data-theme", name), name);
	await pause(200);
}

async function viewport(width, height = 1000) {
	await page.setViewport({ width, height });
	await pause(300);
}

// Types into a field as a person would: what is in it selected and deleted, then the new text.
async function fill(testid, text) {
	const selector = by(testid);
	await page.focus(selector);
	await page.$eval(selector, (input) => input.select());
	await page.keyboard.press("Backspace");
	if (text) await page.type(selector, text);
}

async function pickCountry(text) {
	await fill("tape-country", text);
	await page.keyboard.press("Enter");
}

async function store() {
	return page.evaluate(() => JSON.parse(JSON.stringify(frappe.pages["shipping-policy-wizard"].wizard.store)));
}

async function stored() {
	return page.evaluate((key) => window.localStorage.getItem(key), DRAFT_KEY);
}

// Puts a draft into the browser's storage from a page of the site that runs no desk script (so no wizard is
// open there to tidy the storage as it unloads), then opens the wizard. The draft is the test user's unless
// `value` names another user.
async function openWithStored(value) {
	const stored = { user: process.env.ERP_USER, ...value };
	await go(NEUTRAL);
	await page.evaluate((key, value) => window.localStorage.setItem(key, JSON.stringify(value)), DRAFT_KEY, stored);
	await open();
}

const TABLIST = '.sp-wizard [role="tablist"]';

async function selectedTab() {
	return page.$eval(`${TABLIST} [aria-selected="true"]`, (tab) => tab.dataset.testid);
}

async function colourOf(testid) {
	return page.$eval(by(testid), (element) => getComputedStyle(element).color);
}

// The last thing the receipt prints above its total.
async function lastLine() {
	return page.$eval(`${by("tape-lines")} > :last-child`, (line) => line.innerText.replace(/\s+/g, " ").trim());
}

// A zone and a rule of a draft, in the shape wizard_state() gives them (shipping_engine.py documents it).
const zoneOf = (name, countries, extra = {}) => ({
	name,
	enabled: true,
	countries,
	not_shipped: false,
	everywhere_else: false,
	customer_line: "",
	not_offered_message: "",
	fallback: { method: "policy_default", amount: 0 },
	...extra,
});

const ruleOf = (zone, method, extra = {}) => ({
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
	method,
	formula: "",
	round_up: false,
	...extra,
});

// The example policy as the page builds it on erp2: the fixture's, with erp2's watch group, still switched off
// (the Review step turns it on) and every zone's fallback left as a new zone has it (that is the next step's).
const builtZones = () =>
	EXAMPLE.zones.map((zone) => ({ ...zone, fallback: { method: "policy_default", amount: 0 } }));
const builtSets = () => [{ name: "Watches", item_groups: [WATCH_GROUP], items: [] }];

// The n-th element (from 0) with a test id, the element with a test id inside the n-th card of a kind
// ("zone-card" or "rule-card"), and what a person does to it.
async function nth(testid, n = 0) {
	const found = await page.$$(by(testid));
	assert(found[n], `there is no ${testid} number ${n + 1}`);
	return found[n];
}

async function inCard(kind, n, testid) {
	const found = await (await nth(kind, n)).$(by(testid));
	assert(found, `${kind} ${n + 1} has no ${testid}`);
	return found;
}

async function clickIn(kind, n, testid) {
	await (await inCard(kind, n, testid)).click();
}

async function typeIn(kind, n, testid, text) {
	const field = await inCard(kind, n, testid);
	await field.evaluate((input) => {
		input.focus();
		input.select();
	});
	await page.keyboard.press("Backspace");
	if (text) await field.type(text);
}

async function textIn(kind, n, testid) {
	return (await inCard(kind, n, testid)).evaluate((element) => element.innerText.replace(/\s+/g, " ").trim());
}

async function valueIn(kind, n, testid) {
	return (await inCard(kind, n, testid)).evaluate((input) => input.value);
}

// Waits until the element with the test id inside the n-th card says `text` (a part of what it says).
async function expectIn(kind, n, testid, text, timeout = 10000) {
	try {
		await page.waitForFunction(
			(kind, n, testid, text) => {
				const card = document.querySelectorAll(`[data-testid="${kind}"]`)[n];
				const element = card?.querySelector(`[data-testid="${testid}"]`);
				return Boolean(element) && element.innerText.replace(/\s+/g, " ").includes(text);
			},
			{ timeout },
			kind,
			n,
			testid,
			text
		);
	} catch {
		const seen = await textIn(kind, n, testid).catch(() => "(no such element)");
		throw new Error(`${kind} ${n + 1}: ${testid} should hold "${text}", it says "${seen}"`);
	}
}

async function addCountry(n, text) {
	await typeIn("zone-card", n, "zone-country-add", text);
	await page.keyboard.press("Enter");
}

async function countriesOf(n) {
	return (await nth("zone-card", n)).$$eval(by("zone-country-chip"), (chips) => chips.map((chip) => chip.dataset.key));
}

// Opens a chip of the n-th rule and picks the option with this value in its editor.
async function choose(n, chip, option, value) {
	await clickIn("rule-card", n, chip);
	const picked = await (await nth("rule-card", n)).$(`${by(option)}[value="${value}"]`);
	assert(picked, `rule ${n + 1} offers no ${value}`);
	await picked.click();
}

async function focusedTestid() {
	return page.evaluate(() => document.activeElement?.dataset?.testid || document.activeElement?.tagName || "");
}

async function draftNow() {
	return (await store()).draft;
}

// Waits until the confirmation dialog has finished opening (while it fades in, the desk's modal ignores a
// request to close; Frappe sets cur_dialog once it is shown), presses one of its buttons, and waits until it is
// gone.
async function answerDialog(yes) {
	const button = yes ? ".modal.show .btn-modal-primary" : ".modal.show .btn-modal-secondary";
	const seen = () =>
		page.evaluate(() =>
			JSON.stringify({
				modals: [...document.querySelectorAll(".modal")].map((modal) => modal.className),
				dialog: window.cur_dialog ? { display: window.cur_dialog.display } : null,
				focus: document.activeElement?.dataset?.testid || document.activeElement?.className,
			})
		);
	try {
		await page.waitForFunction(() => Boolean(window.cur_dialog?.display && document.querySelector(".modal.show")), {
			timeout: 10000,
		});
	} catch {
		throw new Error(`no dialog opened: ${await seen()}`);
	}
	await page.waitForSelector(button, { visible: true });
	await page.click(button);
	// closed once its backdrop has faded out too: until then the backdrop takes the page's clicks
	try {
		await page.waitForFunction(() => !document.querySelector(".modal.show, .modal-backdrop"), { timeout: 10000 });
	} catch {
		throw new Error(`the dialog did not close: ${await seen()}`);
	}
	await pause(150);
}

// Types into the tape's item field and takes the desk's first suggestion.
async function addTapeItem(code) {
	await page.focus(by("tape-add-item"));
	await page.type(by("tape-add-item"), code);
	await page.waitForFunction(
		(code) =>
			[...document.querySelectorAll(".sp-item-picker [role='option']")].some((option) =>
				option.textContent.includes(code)
			),
		{ timeout: 15000 },
		code
	);
	await page.keyboard.press("Enter");
	await page.waitForSelector(by("tape-item"));
}

// ---------------------------------------------------------------- the checks
await check("log in as the test user", login);
trouble.splice(0); // what the login page itself does is not the wizard's
await open();

await check("the six step tabs and tape-total are on the page", async () => {
	try {
		await page.waitForSelector(by("tape-total"), { timeout: 15000 });
	} catch {
		throw new Error("tape-total is not on the page");
	}
	for (const tab of STEPS) {
		assert(await page.$(by(`step-tab-${tab}`)), `step-tab-${tab} is not on the page`);
	}
});

const { version, original } = await store();

await check("the tabs are one tablist in order, Zones selected, Test set apart with its own name", async () => {
	const tabs = await page.$$eval(`${TABLIST} [role="tab"]`, (found) =>
		found.map((tab) => ({
			testid: tab.dataset.testid,
			text: tab.textContent.trim(),
			selected: tab.getAttribute("aria-selected"),
			name: tab.getAttribute("aria-label"),
			index: tab.tabIndex,
			left: tab.getBoundingClientRect().left,
			right: tab.getBoundingClientRect().right,
		}))
	);
	assert((await page.$$(TABLIST)).length === 1, "one tablist");
	assert(tabs.map((tab) => tab.testid).join() === STEPS.map((step) => `step-tab-${step}`).join(), "order");
	assert(tabs.map((tab) => tab.text).join("|") === LABELS.join("|"), `labels: ${tabs.map((tab) => tab.text)}`);
	assert(tabs.map((tab) => tab.selected).join() === "true,false,false,false,false,false", "Zones selected");
	assert(tabs.map((tab) => tab.index).join() === "0,-1,-1,-1,-1,-1", "only the selected tab is in the tab order");
	assert(tabs[5].name === "Test the integration", `Test is named "${tabs[5].name}"`);
	assert(tabs[5].left - tabs[4].right > 48, "Test sits apart from Review");
});

await check("each step not built yet shows a plain placeholder line; Zones and Rules show their bodies", async () => {
	const BUILT = { zones: "zone-add", rules: "rule-add" }; // a control each built body has
	for (const step of STEPS) {
		await page.click(by(`step-tab-${step}`));
		const panel = await page.$eval(`#sp-panel-${step}`, (element) => ({
			hidden: element.hidden,
			text: element.textContent.trim(),
			labelled: element.getAttribute("aria-labelledby"),
		}));
		assert(!panel.hidden, `the ${step} panel is shown`);
		assert(panel.labelled === `sp-tab-${step}`, `the ${step} panel is labelled by its tab`);
		if (BUILT[step]) {
			assert(await page.$(`#sp-panel-${step} ${by(BUILT[step])}`), `${step} has its body: ${panel.text}`);
			assert(!panel.text.includes("built in the next stage"), `${step} has no placeholder`);
		} else {
			assert(panel.text === "This step is built in the next stage.", `${step}: ${panel.text}`);
		}
	}
	await page.click(by("step-tab-zones"));
});

await check("the arrow keys, Home and End move between the tabs, with a visible focus ring", async () => {
	await page.focus(by("step-tab-zones"));
	const press = async (key, step) => {
		await page.keyboard.press(key);
		const focused = await page.evaluate(() => document.activeElement?.dataset?.testid);
		assert(focused === `step-tab-${step}` && (await selectedTab()) === `step-tab-${step}`, `${key}: ${focused}`);
	};
	await press("ArrowRight", "rates");
	await press("End", "test");
	await press("ArrowRight", "zones");
	await press("ArrowLeft", "test");
	await press("Home", "zones");
	const ring = await page.evaluate(() => {
		const style = getComputedStyle(document.activeElement);
		return { style: style.outlineStyle, width: parseFloat(style.outlineWidth) };
	});
	assert(ring.style !== "none" && ring.width >= 2, `focus ring ${JSON.stringify(ring)}`);
});

await check("the tape starts empty and says what to fill in", async () => {
	await expectText("tape-lines", "Choose where the order ships to.");
	assert((await textOf("tape-total")) === "Not priced yet", "nothing priced yet");
});

await check("screenshots of the initial state are written, light and dark (760 px wide comes last)", async () => {
	await page.evaluate(() => document.activeElement?.blur());
	const files = [await shot("wizard-light")];
	await theme("dark");
	files.push(await shot("wizard-dark"));
	await theme("light");
	return files.join(", ");
});

await check("at 1400 px the tape stands beside the steps", async () => {
	const beside = await page.evaluate(() => {
		const step = document.querySelector(".sp-step").getBoundingClientRect();
		const tape = document.querySelector(".sp-tape").getBoundingClientRect();
		return tape.left >= step.right && Math.abs(tape.top - step.top) < 40;
	});
	assert(beside, "the tape is not beside the steps");
});

await check("the three accents are the spec's, switched by the desk's dark theme", async () => {
	const accents = async () =>
		page.$eval(".sp-wizard", (root) =>
			["--sp-brass", "--sp-sage", "--sp-brick"].map((name) =>
				getComputedStyle(root).getPropertyValue(name).trim().toUpperCase()
			)
		);
	const light = await accents();
	await theme("dark");
	const dark = await accents();
	await theme("light");
	assert(light.join() === "#9A7116,#2B7A57,#A8473A", `light: ${light}`);
	assert(dark.join() === "#D8B259,#74C9A0,#E5907F", `dark: ${dark}`);
});

await check("in the dark theme the tape's edge stays visible", async () => {
	const edge = async () => page.$eval(".sp-tape", (tape) => getComputedStyle(tape).borderTopColor);
	const light = await edge();
	await theme("dark");
	const dark = await edge();
	await theme("light");
	assert(light === "rgb(237, 237, 237)", `light: the desk's own border, ${light}`);
	assert(dark === "rgb(66, 66, 66)", `dark: a rule lighter than the desk's #232323, ${dark}`);
});

await check("prefers-reduced-motion removes the receipt's transition", async () => {
	const duration = async () =>
		page.$eval(".sp-total", (total) => getComputedStyle(total).transitionDuration);
	assert((await duration()) !== "0s", "the receipt fades while it prices");
	await page.emulateMediaFeatures([{ name: "prefers-reduced-motion", value: "reduce" }]);
	const reduced = await duration();
	await page.emulateMediaFeatures([{ name: "prefers-reduced-motion", value: "no-preference" }]);
	assert(reduced === "0s", `with reduced motion: ${reduced}`);
});

await check("with the empty policy off, the tape says so and the customer would pay the typed quote", async () => {
	await pickCountry("United Arab Emirates");
	const country = await page.$eval(by("tape-country"), (input) => input.value);
	assert(country === "United Arab Emirates", `found by name: ${country}`);
	await fill("tape-quote", "38.20");
	await expectText("tape-lines", OFF);
	await expectText("tape-total", "$38.20");
	assert((await textOf("tape-total")) === "$38.20", "to the cent");
	const label = await textOf("tape-total-label");
	assert(label === "Customer would pay", `while the policy is off the total reads "${label}"`);
	const said = "No zone covers this country, so the carrier's price is passed through.";
	assert((await lastLine()) === said, `the statement comes after the lines: ${await lastLine()}`);
	const colour = await colourOf("tape-total");
	assert(colour === "rgb(154, 113, 22)", `what the customer pays is brass: ${colour}`);
	await page.evaluate(() => document.activeElement?.blur());
	const files = [await shot("wizard-priced-light")];
	await theme("dark");
	files.push(await shot("wizard-priced-dark"));
	await theme("light");
	return files.join(", ");
});

await check("a country is found by its two-letter code too", async () => {
	await pickCountry("bh");
	assert((await page.$eval(by("tape-country"), (input) => input.value)) === "Bahrain", "BH is Bahrain");
	await expectText("tape-total", "$38.20");
});

await check("“The carrier can't quote” prices the order without a quote", async () => {
	await page.click(by("tape-unavailable"));
	await expectText("tape-lines", "Delivery is not offered.");
	await expectText("tape-total", "Not offered");
	const colour = await colourOf("tape-total");
	assert(colour === "rgb(168, 71, 58)", `not offered is brick: ${colour}`);
	await page.click(by("tape-unavailable"));
	await expectText("tape-total", "$38.20");
});

await check("typing prices the order once, 250 ms after the last key", async () => {
	await pause(600);
	priced = 0;
	await page.focus(by("tape-quote"));
	await page.$eval(by("tape-quote"), (input) => input.select());
	await page.type(by("tape-quote"), "45.10", { delay: 60 });
	await expectText("tape-total", "$45.10");
	await pause(800);
	assert(priced === 1, `${priced} requests for five keys`);
});

await check("an older answer never overwrites a newer one", async () => {
	await intercept(true);
	try {
		holdNext = 2500;
		await fill("tape-quote", "50");
		await pause(700); // the request for 50 has gone out and is held back
		await fill("tape-quote", "60");
		await expectText("tape-total", "$60.00");
		await pause(3500); // the answer for 50 arrives now
		assert((await textOf("tape-total")) === "$60.00", `after the old answer: ${await textOf("tape-total")}`);
	} finally {
		await intercept(false);
	}
});

await check("a field that is not a number says so and prices nothing", async () => {
	await fill("tape-order-value", "about 300");
	await expectText("tape-lines", "Type the order value as a number, such as 310 or 310.50.");
	await expectText("tape-total", "Not priced yet");
	await fill("tape-order-value", "");
	await expectText("tape-total", "$60.00");
	await fill("tape-weight", "1,5"); // a decimal comma is never read as 15
	await expectText("tape-lines", "Use a period for decimals.");
	await expectText("tape-total", "Not priced yet");
	await fill("tape-weight", "");
	await expectText("tape-total", "$60.00");
});

await check("an item is added with the desk's Link field, valued from the price list", async () => {
	const hint = (text) =>
		page.waitForFunction((text) => document.querySelector("#sp-tape-value-hint")?.textContent === text, {
			timeout: 10000,
		}, text);
	await page.focus(by("tape-add-item"));
	await page.type(by("tape-add-item"), ITEM);
	await page.waitForFunction(
		(code) =>
			[...document.querySelectorAll(".sp-item-picker [role='option']")].some((option) =>
				option.textContent.includes(code)
			),
		{ timeout: 15000 },
		ITEM
	);
	await page.keyboard.press("Enter");
	await expectText("tape-items", "REFILL GOLD EDITION PARFUM FOR WOMEN 40ML");
	await hint("Items: $229.00");
	await fill("tape-item-qty", "2");
	await hint("Items: $458.00");
	assert((await page.$eval(by("tape-add-item"), (input) => input.value)) === "", "the Link field is empty again");
	await expectText("tape-total", "$60.00");
});

await check("asking the carrier fills the quote and lists what it said", async () => {
	const disabled = async () =>
		page.$eval(by("tape-ask-carrier"), (button) => button.getAttribute("aria-disabled"));
	assert((await disabled()) === "true", "not before a weight and a city");
	await fill("tape-weight", "1");
	await fill("tape-volume", "3");
	await fill("tape-city", "Manama");
	assert((await disabled()) === "false", "a country, items, a weight and a city: it can ask");
	await page.click(by("tape-ask-carrier"));
	await page.waitForFunction(
		(selector) => {
			const list = document.querySelector(selector);
			return list && !list.hidden && list.children.length > 0;
		},
		{ timeout: 45000 },
		by("tape-services")
	);
	await expectText("tape-total", /^(\$\d|Free$|Not offered$)/);
	const quote = await page.$eval(by("tape-quote"), (input) => input.value);
	const services = await textOf("tape-services");
	const cheapest = await page.$eval(`${by("tape-services")} .sp-service-name`, (name) => name.textContent);
	await expectText("tape-lines", `${cheapest} The carrier quoted ${quote}.`);
	assert(!(await textOf("tape-lines")).includes("[object"), "every line is printed as text");
	const others = await page.$$eval(`${by("tape-more")} li`, (rows) => rows.map((row) => row.innerText));
	const quoted = (await page.$$(`${by("tape-services")} li`)).length;
	assert(others.length === quoted - 1, `one quiet row for each other method: ${others.length}`);
	assert(!(await textOf("tape-more")).includes("pay"), "no second Customer pays line");
	const file = await shot("wizard-asked-the-carrier");
	return `the carrier said "${services}", the quote field holds ${quote}, the total reads ${await textOf("tape-total")}; ${file}`;
});

await check("at 1400 x 768 the tape scrolls inside and keeps its total in view (screenshot)", async () => {
	await viewport(1400, 768);
	const seen = await page.evaluate(() => {
		const tape = document.querySelector(".sp-tape");
		const edge = tape.getBoundingClientRect();
		const total = document.querySelector(".sp-total").getBoundingClientRect();
		return {
			fits: edge.bottom <= window.innerHeight - 72, // clear of the desk's floating buttons in that corner
			scrolls: tape.scrollHeight > tape.clientHeight,
			total: total.top >= 0 && total.bottom <= window.innerHeight,
			pinned: Math.abs(edge.bottom - 1 - total.bottom) <= 2, // at the foot: nothing shows under it
		};
	});
	const file = await shot("wizard-1400x768-asked");
	await viewport(1400);
	assert(Object.values(seen).every(Boolean), JSON.stringify(seen));
	return file;
});

await check("a quote typed while the carrier is asked wins over its late answer", async () => {
	await intercept(true);
	try {
		holdNext = 2500; // the carrier's answer arrives late
		await page.click(by("tape-ask-carrier"));
		await pause(300);
		await fill("tape-quote", "40");
		await expectText("tape-total", "$40.00");
		await pause(3500); // the answer to Ask arrives now
		const quote = await page.$eval(by("tape-quote"), (input) => input.value);
		assert(quote === "40", `the typed quote stays: ${quote}`);
		assert((await textOf("tape-total")) === "$40.00", `and prices the order: ${await textOf("tape-total")}`);
		assert(await page.$eval(by("tape-services"), (list) => list.hidden), "the carrier's list is gone");
	} finally {
		await intercept(false);
	}
});

await check("removing the item takes it out of the order", async () => {
	await page.click(by("tape-item-remove"));
	assert((await page.$$(by("tape-item"))).length === 0, "no item lines left");
});

await check("with one measure typed, the other is packed again whenever the items change", async () => {
	const sent = [];
	const listen = (request) => {
		if (!request.url().includes("shipping_policy_wizard.try_order")) return;
		sent.push(JSON.parse(new URLSearchParams(request.postData() || "").get("order") || "null"));
	};
	await fill("tape-volume", ""); // only the weight (1 kg) is typed now
	await page.waitForNetworkIdle({ idleTime: 600 });
	page.on("request", listen);
	try {
		await page.focus(by("tape-add-item"));
		await page.type(by("tape-add-item"), ITEM);
		await page.waitForFunction(
			(code) =>
				[...document.querySelectorAll(".sp-item-picker [role='option']")].some((option) =>
					option.textContent.includes(code)
				),
			{ timeout: 15000 },
			ITEM
		);
		await page.keyboard.press("Enter");
		await page.waitForSelector(by("tape-item"));
		await page.waitForNetworkIdle({ idleTime: 600 });
		const [packing, pricing] = sent.splice(0);
		assert(packing?.boxes === null && JSON.stringify(packing?.quotes) === "[]", "first the ERP packs the items");
		const carton = JSON.stringify(pricing?.boxes);
		assert(carton === JSON.stringify([{ weight_kg: 1, volume_l: 0 }]), `then the typed weight, packed volume: ${carton}`);
		await fill("tape-weight", "2");
		await page.waitForNetworkIdle({ idleTime: 600 });
		const again = sent.splice(0);
		assert(again.length === 1 && again[0].boxes[0].weight_kg === 2, "the same items are not packed again");
		await page.click(by("tape-item-remove"));
		await page.waitForNetworkIdle({ idleTime: 600 });
	} finally {
		page.off("request", listen);
	}
	return `erp2 has no packer, so the packed volume is 0 there`;
});

await check("the receipt prints a trace kind it does not know, and the rate card's lines, as text", async () => {
	const seen = await page.evaluate(() => {
		const tape = frappe.pages["shipping-policy-wizard"].wizard.tape;
		const trace = [
			{ kind: "rate_card", text: "Dangerous Goods, USA: 1.984 lb -> $60.00", amount: 60 },
			{ kind: "mystery", text: "<i>x</i>", amount: 60 },
			{ kind: "result", text: "Customer pays 60.00.", amount: 60 },
		];
		const offer = { service_id: "rate-card", name: "International Express (4-7 days)", amount: 60, estimated_days: 7, trace };
		tape.render_receipt({ answer: { result: { offered: true, price_source: "rate_card", offers: [offer] } } });
		return {
			lines: [...document.querySelectorAll('[data-testid="tape-lines"] .sp-line')].map((line) => line.innerText),
			italics: document.querySelectorAll(".sp-wizard i").length,
			total: document.querySelector('[data-testid="tape-total"]').textContent,
			days: document.querySelector(".sp-after-total").textContent,
		};
	});
	const lines = seen.lines.map((line) => line.replace(/\s+/g, " "));
	const want = ["Dangerous Goods, USA: 1.984 lb -> $60.00 $60.00", "<i>x</i> $60.00"];
	assert(JSON.stringify(lines) === JSON.stringify(want), `lines: ${JSON.stringify(lines)}`);
	assert(seen.italics === 0, "the unknown kind's text stays text");
	assert(seen.total === "$60.00" && seen.days === "About 7 days", `total ${seen.total}, ${seen.days}`);
});

await check("a stored draft of this version is restored with its step, and can be discarded", async () => {
	const draft = { ...original, not_covered: "no_delivery" };
	await openWithStored({ version, draft, step: "rules" });
	await expectText("draft-banner", "Restored your unsaved draft. Nothing is live until you apply it.");
	assert((await selectedTab()) === "step-tab-rules", "back on the Rules step");
	assert((await store()).dirty === true, "the draft is dirty");
	const file = await shot("wizard-restored-draft");
	await page.click(by("draft-discard"));
	await page.waitForSelector(".modal.show .btn-primary", { visible: true });
	await page.click(".modal.show .btn-primary");
	await page.waitForFunction((selector) => document.querySelector(selector)?.hidden, {}, by("draft-banner"));
	await pause(500);
	assert((await stored()) === null, "the stored draft is gone");
	assert((await store()).dirty === false, "the draft is the saved policy again");
	return file;
});

await check("a draft someone else stored on this browser is not there for this user, and stays", async () => {
	const theirs = { user: "someone-else@dsi.local", version, draft: { ...original, not_covered: "no_delivery" } };
	await openWithStored({ ...theirs, step: "rules" });
	await pause(500);
	assert(await page.$eval(by("draft-banner"), (banner) => banner.hidden), "no banner about it");
	assert((await selectedTab()) === "step-tab-zones", "not restored: the steps start at Zones");
	assert((await store()).dirty === false, "the draft is the saved policy");
	const kept = JSON.parse(await stored());
	assert(kept?.user === theirs.user && kept?.draft?.not_covered === "no_delivery", "their draft is left as it was");
	await page.evaluate((key) => window.localStorage.removeItem(key), DRAFT_KEY);
});

await check("a stored draft of another version is dropped, and the banner says so", async () => {
	const draft = { ...original, not_covered: "no_delivery" };
	await openWithStored({ version: "an-older-version", draft, step: "review" });
	await expectText("draft-banner", "Someone changed the policy after you started, so your old draft was discarded.");
	assert((await stored()) === null, "the old draft is gone from storage");
	assert((await selectedTab()) === "step-tab-zones", "the steps start at Zones");
	assert((await store()).dirty === false, "the draft is the saved policy");
	await page.click(by("draft-dismiss"));
	await page.waitForFunction((selector) => document.querySelector(selector)?.hidden, {}, by("draft-banner"));
});

await check("a changed draft is stored 300 ms later, and removed once it equals the saved policy", async () => {
	const change = (value) =>
		page.evaluate((value) => {
			frappe.pages["shipping-policy-wizard"].wizard.change((draft) => {
				draft.not_covered = value;
			});
		}, value);
	await change("no_delivery");
	await pause(100);
	assert((await stored()) === null, "not yet after 100 ms");
	await pause(450);
	const kept = JSON.parse(await stored());
	assert(kept?.version === version && kept?.draft?.not_covered === "no_delivery" && kept?.step === "zones", "stored");
	assert(kept?.user === process.env.ERP_USER, "with the user it belongs to");
	await change("pass_through");
	await pause(550);
	assert((await stored()) === null, "removed when the draft is the saved policy again");
});

await check("names from the draft and messages from the server are shown as text, never as HTML", async () => {
	const closed = { not_shipped: true, not_offered_message: HOSTILE_MESSAGE };
	const draft = {
		...original,
		zones: [zoneOf(HOSTILE, ["AE"]), zoneOf("Closed <b>zone</b>", ["BH"], closed)],
		rules: [ruleOf(HOSTILE, "carrier_price", { label: "<b>bold</b>", round_up: true })],
	};
	await openWithStored({ version, draft, step: "zones" });
	await expectText("draft-banner", "Restored your unsaved draft.");
	await pickCountry("United Arab Emirates");
	await fill("tape-quote", "38.20");
	await expectText("tape-lines", `Rule 1 ("<b>bold</b>") applies: ${HOSTILE}, every item`);
	await expectText("tape-total", "$39.00");
	assert((await page.$$(".sp-wizard img, .sp-wizard b")).length === 0, "no element was made from a name");
	await pickCountry("Bahrain");
	await expectText("tape-lines", HOSTILE_MESSAGE);
	await expectText("tape-total", "Not offered");
	assert((await page.$$(".sp-wizard img, .sp-wizard b")).length === 0, "no element was made from a message");
	const file = await shot("wizard-hostile-names");
	await page.evaluate((key) => window.localStorage.removeItem(key), DRAFT_KEY);
	return file;
});

await check("the receipt says how a draft that is on priced the order, in plain sentences", async () => {
	const draft = {
		...original,
		active: true,
		zones: [
			zoneOf("Gulf", ["AE"], { fallback: { method: "fixed", amount: 35 } }),
			zoneOf("Levant", ["JO"]),
			zoneOf("Island", ["BH"]),
		],
		rules: [ruleOf("Island", "free"), ruleOf("Gulf", "carrier_price", { round_up: true })],
	};
	await openWithStored({ version, draft, step: "rules" });
	await pickCountry("United Arab Emirates");
	await fill("tape-quote", "38.20");
	await expectText("tape-total", "$39.00");
	await expectText("tape-lines", "Rule 2 applies: Gulf, every item, charges the carrier's price, rounded up.");
	await expectText("tape-lines", "Rounded up to the next whole dollar, which adds 0.80. +$0.80");
	assert(!(await textOf("tape-lines")).includes("switched off"), "the draft is on: no note that it is off");
	assert((await textOf("tape-total-label")) === "Customer pays", "the draft is on: the customer pays");
	await page.click(by("tape-unavailable"));
	await expectText("tape-lines", "The carrier could not quote, so the fallback price applies.");
	await expectText("tape-lines", "Standard delivery");
	await expectText("tape-total", "$35.00");
	assert((await lastLine()).startsWith("The carrier could not quote"), "the statement is the last line");
	await page.click(by("tape-unavailable"));
	await pickCountry("Jordan");
	await expectText("tape-lines", "No rule fits this order, so the carrier's price is passed through.");
	await expectText("tape-total", "$38.20");
	await pickCountry("Bahrain");
	await expectText("tape-total", "Free");
	assert((await colourOf("tape-total")) === "rgb(43, 122, 87)", `free is sage: ${await colourOf("tape-total")}`);
	const file = await shot("wizard-free");
	await pickCountry("Afghanistan");
	await expectText("tape-lines", "Delivery is not offered.");
	await expectText("tape-lines", "No carrier service delivers to this country.");
	await expectText("tape-total", "Not offered");
	await page.evaluate((key) => window.localStorage.removeItem(key), DRAFT_KEY);
	return file;
});

await check("for the step bodies: priced on every draw, wizard.ui, a picker's own id and live list", async () => {
	await go(NEUTRAL);
	await page.evaluate((key) => window.localStorage.removeItem(key), DRAFT_KEY);
	await open();
	const seen = await page.evaluate(() => {
		const wizard = frappe.pages["shipping-policy-wizard"].wizard;
		const zones = dsi_catalogue.ShippingPolicyWizard.STEPS.find((step) => step.id === "zones");
		const stub = zones.priced;
		const calls = [];
		zones.priced = (_, panel, answer) => calls.push([panel.id, answer === wizard.priced]);
		wizard.set_step("rates");
		wizard.set_step("zones");
		zones.priced = stub;
		const countries = () => wizard.state.countries;
		const one = new dsi_catalogue.CountryPicker({ countries, on_change() {} });
		const two = new dsi_catalogue.CountryPicker({ countries, on_change() {} });
		wizard.state.countries.push({ code: "ZZ", name: "Zedland" }); // the list changes after the picker is made
		const found = one.find("zedland").map((country) => country.code);
		wizard.state.countries.pop();
		wizard.change((draft) => draft.zones.push(dsi_catalogue.sp.new_zone("Gulf")));
		const added = wizard.store.dirty;
		wizard.change((draft) => draft.zones.pop());
		return { calls, ids: [one.id, two.id], found, added, dirty: wizard.store.dirty, ui: Object.keys(wizard.ui) };
	});
	assert(JSON.stringify(seen.calls) === '[["sp-panel-zones",true]]', `priced: ${JSON.stringify(seen.calls)}`);
	assert(seen.ids[0] !== seen.ids[1], `two pickers, two ids: ${seen.ids}`);
	assert(seen.found.join() === "ZZ", "the picker reads the wizard's list when it uses it");
	assert(seen.added && !seen.dirty, "a zone added and taken back leaves the draft clean");
	assert(seen.ui.join() === STEPS.join(), `wizard.ui has a place for each step: ${seen.ui}`);
	await pause(500);
	assert((await stored()) === null, "and nothing is left in storage");
});

// ---------------------------------------------------------------- Task 8: the Zones and Rules steps
await check("Zones starts with no zone, and a new zone gets a name no other zone has", async () => {
	await go(NEUTRAL);
	await page.evaluate((key) => window.localStorage.removeItem(key), DRAFT_KEY);
	await open();
	await page.click(by("step-tab-zones"));
	assert((await page.$$(by("zone-card"))).length === 0, "no zone yet");
	assert((await page.$eval("#sp-panel-zones", (panel) => panel.innerText)).includes("No zones yet."), "it says so");
	await page.click(by("zone-add"));
	assert((await focusedTestid()) === "zone-name", "the new zone's name has the focus");
	await page.click(by("zone-add"));
	const names = await page.$$eval(by("zone-name"), (inputs) => inputs.map((input) => input.value));
	assert(names.join("|") === "New zone|New zone 2", `names: ${names.join("|")}`);
	await clickIn("zone-card", 1, "zone-delete");
	assert((await page.$$(by("zone-card"))).length === 1, "the second zone is deleted");
	assert((await focusedTestid()) === "zone-delete", `then the focus is on the zone left: ${await focusedTestid()}`);
});

await check("the example policy's four zones are built through the page", async () => {
	await typeIn("zone-card", 0, "zone-name", "Bahrain");
	await addCountry(0, "Bahrain"); // by name
	await typeIn("zone-card", 0, "zone-line", "Free delivery in Bahrain.");
	await page.click(by("zone-add"));
	await typeIn("zone-card", 1, "zone-name", "GCC");
	for (const code of ["ae", "sa", "kw", "om", "qa"]) await addCountry(1, code); // by code
	await typeIn("zone-card", 1, "zone-line", "Gulf delivery, priced by the carrier. Watches ship free.");
	await page.click(by("zone-add"));
	await typeIn("zone-card", 2, "zone-name", "Rest of world");
	await clickIn("zone-card", 2, "zone-everywhere-else");
	const picker = await (await inCard("zone-card", 2, "zone-country-add")).evaluate((input) => ({
		disabled: input.disabled,
		shown: Boolean(input.offsetParent),
	}));
	assert(picker.disabled && !picker.shown, `Everywhere else takes no country: ${JSON.stringify(picker)}`);
	await expectIn("zone-card", 2, "zone-everywhere-else-note", "Every country that is in no other zone");
	await typeIn("zone-card", 2, "zone-line", "International delivery, priced at checkout.");
	await page.click(by("zone-add"));
	await typeIn("zone-card", 3, "zone-name", "Not shipped");
	await addCountry(3, "ru");
	await addCountry(3, "by");
	const hidden = async () =>
		(await inCard("zone-card", 3, "zone-not-offered-message")).evaluate((input) => !input.offsetParent);
	assert(await hidden(), "the not-offered message is asked for only in a Not shipped zone");
	await clickIn("zone-card", 3, "zone-not-shipped");
	assert(!(await hidden()), "a Not shipped zone asks what customers read");
	await typeIn("zone-card", 3, "zone-not-offered-message", "We don't deliver to this country.");
	await typeIn("zone-card", 3, "zone-line", "We do not deliver to these countries.");
	await expectIn("zone-card", 3, "zone-state", "Not shipped");
	assert((await countriesOf(1)).join() === "AE,SA,KW,OM,QA", `GCC: ${await countriesOf(1)}`);
	assert((await textIn("zone-card", 1, "zone-country-chip")) .startsWith("United Arab Emirates"), "a chip names its country");
	const zones = (await draftNow()).zones;
	assert(JSON.stringify(zones) === JSON.stringify(builtZones()), `the zones: ${JSON.stringify(zones)}`);
});

await check("a country already in another zone that is on is refused, naming that zone", async () => {
	await addCountry(1, "Bahrain");
	await expectIn("zone-card", 1, "zone-country-problem", "Bahrain is already in the Bahrain zone.");
	assert((await countriesOf(1)).join() === "AE,SA,KW,OM,QA", "and it is not added");
	// a country's remove button works from the keyboard, and the focus moves to the next one
	const remove = await (await nth("zone-card", 1)).$(`${by("zone-country-chip")}[data-key="QA"] ${by("zone-country-remove")}`);
	await remove.focus();
	await page.keyboard.press("Enter");
	assert((await countriesOf(1)).join() === "AE,SA,KW,OM", "Qatar is removed");
	const label = await page.evaluate(() => document.activeElement?.getAttribute("aria-label"));
	assert(label === "Remove Oman", `then the focus is on the next remove button: ${label}`);
	await addCountry(1, "qa");
	assert((await countriesOf(1)).join() === "AE,SA,KW,OM,QA", "Qatar is back, last");
	assert((await textIn("zone-card", 1, "zone-country-problem")) === "", "the refusal is gone after the change");
});

await check("a zone's name keeps out < and >, a taken name is a problem, unserved countries are named", async () => {
	await page.click(by("zone-add"));
	await typeIn("zone-card", 4, "zone-name", "Temp<b>");
	assert((await valueIn("zone-card", 4, "zone-name")) === "Tempb", "< and > are taken out as they are typed");
	await expectIn("zone-card", 4, "zone-problems", "A zone's name cannot hold < or >.");
	await typeIn("zone-card", 4, "zone-name", "gcc");
	await expectIn("zone-card", 4, "zone-problems", "Another zone has this name.");
	await typeIn("zone-card", 4, "zone-name", "Temporary");
	await addCountry(4, "Afghanistan");
	await expectIn("zone-card", 4, "zone-unserved-warning", "No carrier serves: Afghanistan.");
	const gulf = await (await inCard("zone-card", 1, "zone-unserved-warning")).evaluate((warning) => warning.hidden);
	assert(gulf, "erp2's carrier serves the whole Gulf: no warning there");
	// with the rate card as the price source the carrier is not asked, so there is nothing to warn about
	const source = (await draftNow()).price_source;
	const set_source = (value) =>
		page.evaluate((value) => {
			frappe.pages["shipping-policy-wizard"].wizard.change((draft) => {
				draft.price_source = value;
			});
		}, value);
	await set_source("rate_card");
	const hidden = await (await inCard("zone-card", 4, "zone-unserved-warning")).evaluate((warning) => warning.hidden);
	await set_source(source);
	assert(hidden, "no warning while the rate card prices orders");
	await expectIn("zone-card", 4, "zone-unserved-warning", "No carrier serves: Afghanistan.");
	// a zone that is off says so in words, not only by its look
	await clickIn("zone-card", 4, "zone-enabled");
	await expectIn("zone-card", 4, "zone-state", "Off");
	await clickIn("zone-card", 4, "zone-enabled");
	assert((await textIn("zone-card", 4, "zone-state")) === "", "and on again");
});

await check("Everywhere else asks before it removes a zone's countries, then takes none", async () => {
	await clickIn("zone-card", 4, "zone-everywhere-else");
	await answerDialog(false);
	assert((await countriesOf(4)).join() === "AF", "No keeps the countries");
	assert(!(await (await inCard("zone-card", 4, "zone-everywhere-else")).evaluate((box) => box.checked)), "and the switch off");
	assert((await focusedTestid()) === "zone-everywhere-else", "the focus is back on the switch");
	await clickIn("zone-card", 4, "zone-everywhere-else");
	await answerDialog(true);
	assert((await countriesOf(4)).length === 0, "Yes removes them");
	await expectIn("zone-card", 4, "zone-problems", "Another zone that is on is set to Everywhere else.");
	assert(await (await inCard("zone-card", 4, "zone-unserved-warning")).evaluate((warning) => warning.hidden), "no warning");
	await clickIn("zone-card", 4, "zone-delete");
	assert((await page.$$(by("zone-card"))).length === 4, "the temporary zone is deleted");
	const zones = (await draftNow()).zones;
	assert(JSON.stringify(zones) === JSON.stringify(builtZones()), "the four zones are as they were");
});

await check("an item set is made from the items chip, and the example's four rules are built with chips", async () => {
	await page.click(by("step-tab-rules"));
	assert((await page.$eval("#sp-panel-rules", (panel) => panel.innerText)).includes("No rules yet."), "no rule yet");
	// rule 1: Bahrain, every item, free
	await page.click(by("rule-add"));
	assert((await focusedTestid()) === "chip-zone", "a new rule's zone chip has the focus");
	assert((await textIn("rule-card", 0, "rule-number")) === "Rule 1", "it says Rule 1");
	const sentence = await textIn("rule-card", 0, "rule-sentence");
	assert(sentence === "In Bahrain for every item* charge the carrier's price", `a new rule: ${sentence}`);
	await choose(0, "chip-charge", "rule-charge-option", "free");
	await page.keyboard.press("Escape");
	assert((await focusedTestid()) === "chip-charge", "Escape closes the editor and gives the chip the focus back");
	const ring = await page.evaluate(() => {
		const style = getComputedStyle(document.activeElement);
		return { style: style.outlineStyle, width: parseFloat(style.outlineWidth) };
	});
	assert(ring.style === "solid" && ring.width >= 2, `the chip shows where the focus is: ${JSON.stringify(ring)}`);
	assert(!(await page.$(`${by("rule-card")} ${by("rule-editor")}:not([hidden])`)), "the editor is closed");
	await typeIn("rule-card", 0, "rule-label", "Free in Bahrain");
	// rule 2: GCC, a new item set, free
	await page.click(by("rule-add"));
	await choose(1, "chip-zone", "rule-zone-option", "GCC");
	await page.keyboard.press("Escape");
	await clickIn("rule-card", 1, "chip-items");
	// the item groups are read when the first set editor opens: held back here, so that the name is typed
	// before they arrive, and the open list says they are being read, then offers them
	const offered = async () =>
		(await inCard("rule-card", 1, "itemset-group-add")).evaluate((input) =>
			[...(document.getElementById(input.getAttribute("aria-controls"))?.children || [])].map((item) => item.textContent)
		);
	await intercept(true);
	try {
		holdGroups = 2500;
		await clickIn("rule-card", 1, "itemset-new");
		assert((await focusedTestid()) === "itemset-name", "the set's name has the focus");
		await typeIn("rule-card", 1, "itemset-name", "Watches");
		await clickIn("rule-card", 1, "itemset-save");
		await expectIn("rule-card", 1, "itemset-problem", "Add at least one item group or item.");
		await typeIn("rule-card", 1, "itemset-group-add", WATCH_GROUP.toLowerCase());
		const early = await offered();
		assert(early.join() === "Reading the item groups…", `before the groups arrive: ${JSON.stringify(early)}`);
		try {
			await page.waitForFunction(
				(group) => [...document.querySelectorAll("[role='option']")].some((option) => option.textContent === group),
				{ timeout: 15000 },
				WATCH_GROUP
			);
		} catch {
			throw new Error(`the item group list offers ${JSON.stringify((await offered()).slice(0, 8))}`);
		}
	} finally {
		await intercept(false);
	}
	await page.keyboard.press("Enter");
	await expectIn("rule-card", 1, "itemset-group-chip", WATCH_GROUP);
	await expectIn("rule-card", 1, "itemset-editor", "An item group includes the groups beneath it.");
	await page.evaluate(() => document.activeElement?.blur());
	const editor = await tallShot("wizard-itemset-editor");
	await clickIn("rule-card", 1, "itemset-save");
	assert(!(await (await nth("rule-card", 1)).$(by("itemset-editor"))), "the set editor closes on Save");
	const picked = await page.evaluate(() => ({
		value: document.activeElement?.value,
		checked: document.activeElement?.checked,
		testid: document.activeElement?.dataset?.testid,
	}));
	assert(picked.testid === "rule-items-option" && picked.value === "Watches" && picked.checked, JSON.stringify(picked));
	await page.keyboard.press("Escape");
	assert((await textIn("rule-card", 1, "chip-items")) === "Watches*", "the rule is for the new set");
	await choose(1, "chip-charge", "rule-charge-option", "free");
	await page.keyboard.press("Escape");
	await typeIn("rule-card", 1, "rule-label", "Free watches in the Gulf");
	// rule 3: GCC, every item, the carrier's price, rounded up
	await page.click(by("rule-add"));
	await choose(2, "chip-zone", "rule-zone-option", "GCC");
	await page.keyboard.press("Escape");
	await clickIn("rule-card", 2, "chip-charge");
	await clickIn("rule-card", 2, "rule-round-up");
	await page.keyboard.press("Escape");
	await typeIn("rule-card", 2, "rule-label", "Gulf at the carrier price");
	// rule 4: Rest of world, every item, a formula, rounded up
	await page.click(by("rule-add"));
	await choose(3, "chip-zone", "rule-zone-option", "Rest of world");
	await page.keyboard.press("Escape");
	await choose(3, "chip-charge", "rule-charge-option", "formula");
	await expectIn("rule-card", 3, "rule-formula-names", "carrier_price, order_value, weight_kg, volume_l, boxes and item_count");
	await typeIn("rule-card", 3, "rule-formula-input", "max(carrier_price * 1.1, 45)");
	await clickIn("rule-card", 3, "rule-round-up");
	await pause(900);
	assert((await textIn("rule-card", 3, "rule-formula-error")) === "", "the server takes the formula");
	await page.keyboard.press("Escape");
	await typeIn("rule-card", 3, "rule-label", "International with a minimum");
	const sentences = [];
	for (let n = 0; n < 4; n += 1) sentences.push(await textIn("rule-card", n, "rule-sentence"));
	const want = [
		"In Bahrain for every item* charge nothing (free)",
		"In GCC for Watches* charge nothing (free)",
		"In GCC for every item* charge the carrier's price, rounded up",
		"In Rest of world for every item* charge max(carrier_price * 1.1, 45), rounded up",
	];
	assert(JSON.stringify(sentences) === JSON.stringify(want), `the sentences: ${JSON.stringify(sentences)}`);
	const draft = await draftNow();
	assert(JSON.stringify(draft.item_sets) === JSON.stringify(builtSets()), `the set: ${JSON.stringify(draft.item_sets)}`);
	assert(JSON.stringify(draft.rules) === JSON.stringify(EXAMPLE.rules), `the rules: ${JSON.stringify(draft.rules)}`);
	return editor;
});

await check("the example policy prices the brief's orders, and each rule shows what it charges", async () => {
	await pickCountry("United Arab Emirates");
	await fill("tape-city", "Dubai");
	await fill("tape-quote", "38.20");
	await expectText("tape-total", "$39.00");
	assert((await textOf("tape-total")) === "$39.00", "GCC, Dubai, a quote of 38.20: $39.00");
	await expectIn("rule-card", 2, "rule-price", "$39.00");
	assert((await textIn("rule-card", 2, "rule-price")) === "$39.00", "the GCC every-item rule reads $39");
	const paid = await (await inCard("rule-card", 2, "rule-price")).evaluate(
		(price) => getComputedStyle(price.querySelector(".sp-rule-amount")).color
	);
	assert(paid === "rgb(154, 113, 22)", `what the customer pays is brass: ${paid}`);
	assert((await textIn("rule-card", 1, "rule-price")) === "– Conditions not met", "no watch in the order");
	assert((await textIn("rule-card", 0, "rule-price")) === "– Not for this country", "Bahrain's rule");
	await addTapeItem(WATCH);
	await expectText("tape-total", "Free");
	await expectIn("rule-card", 1, "rule-price", "Free");
	await expectIn("rule-card", 2, "rule-price", "$39.00 Rule 2 comes first");
	await page.click(by("tape-item-remove"));
	await pickCountry("United States");
	await expectText("tape-total", "$45.00");
	await expectIn("rule-card", 3, "rule-price", "$45.00");
	await pickCountry("Russian Federation");
	await expectText("tape-total", "Not offered");
	await expectText("tape-lines", "We don't deliver to this country.");
	assert((await textOf("rules-note")) === ASTERISK, `the asterisk sentence: ${await textOf("rules-note")}`);
	const chips = await page.$$eval(by("chip-items"), (found) => found.map((chip) => chip.textContent));
	assert(chips.every((text) => text.endsWith("*")), `each items chip carries the asterisk: ${chips}`);
});

await check("screenshots of Zones and Rules with the example policy, light and dark", async () => {
	await pickCountry("United Arab Emirates");
	await expectText("tape-total", "$39.00");
	await page.evaluate(() => document.activeElement?.blur());
	const files = [await tallShot("wizard-rules-light")];
	await theme("dark");
	files.push(await tallShot("wizard-rules-dark"));
	const price = await (await inCard("rule-card", 2, "rule-price")).evaluate(
		(price) => getComputedStyle(price.querySelector(".sp-rule-amount")).color
	);
	await theme("light");
	assert(price === "rgb(216, 178, 89)", `brass in the dark too: ${price}`);
	await page.click(by("step-tab-zones"));
	await page.evaluate(() => document.activeElement?.blur());
	files.push(await tallShot("wizard-zones-light"));
	await theme("dark");
	files.push(await tallShot("wizard-zones-dark"));
	await theme("light");
	await page.click(by("step-tab-rules"));
	return files.join(", ");
});

await check("a renamed zone takes its rules along, and a zone that rules use is not deleted", async () => {
	await page.click(by("step-tab-zones"));
	await expectIn("zone-card", 1, "zone-usage", "Rules 2 and 3 use this zone.");
	await typeIn("zone-card", 1, "zone-name", "Gulf");
	let rules = (await draftNow()).rules;
	assert(rules[1].zone === "Gulf" && rules[2].zone === "Gulf", "the two GCC rules follow the new name");
	await clickIn("zone-card", 1, "zone-delete");
	await expectIn("zone-card", 1, "zone-problems", "2 rules use this zone. Change or delete those rules first.");
	assert((await page.$$(by("zone-card"))).length === 4, "the zone stays");
	// typed letter by letter, "Bahrain 2" passes through "Bahrain", the first zone's name: its rule stays with it
	await typeIn("zone-card", 1, "zone-name", "Bahrain 2");
	rules = (await draftNow()).rules;
	assert(rules[0].zone === "Bahrain", `rule 1 stays with Bahrain: ${rules[0].zone}`);
	assert(rules[1].zone === "Bahrain 2" && rules[2].zone === "Bahrain 2", "the GCC rules follow their zone");
	await typeIn("zone-card", 1, "zone-name", "GCC");
	rules = (await draftNow()).rules;
	assert(rules[1].zone === "GCC" && rules[2].zone === "GCC", "and back");
	assert(rules[0].zone === "Bahrain", "rule 1 is still Bahrain's");
	await page.click(by("step-tab-rules"));
	assert((await textIn("rule-card", 2, "chip-zone")) === "GCC", "the chip says so");
});

await check("a formula is checked 300 ms after the last key, and an older answer never replaces a newer one", async () => {
	await clickIn("rule-card", 3, "chip-charge");
	await pause(600);
	checked = 0;
	await typeIn("rule-card", 3, "rule-formula-input", "max(carrier_price * 1.1, 45");
	await pause(1200);
	const error = await textIn("rule-card", 3, "rule-formula-error");
	assert(checked === 1, `${checked} checks for one formula typed`);
	assert(error.length > 0, "a formula with a missing bracket is refused in a plain sentence");
	const role = await (await inCard("rule-card", 3, "rule-formula-error")).evaluate((found) => found.getAttribute("role"));
	assert(role === "alert", `the refusal is an alert: ${role}`);
	await intercept(true);
	try {
		holdCheck = 2500;
		await typeIn("rule-card", 3, "rule-formula-input", "carrier_price +");
		await pause(800); // its check has gone out and is held back
		await typeIn("rule-card", 3, "rule-formula-input", "carrier_price * 2");
		await pause(900);
		assert((await textIn("rule-card", 3, "rule-formula-error")) === "", "the newer formula is fine");
		await pause(2500); // the older answer arrives now
		assert((await textIn("rule-card", 3, "rule-formula-error")) === "", "and stays so after the older answer");
	} finally {
		await intercept(false);
	}
	await typeIn("rule-card", 3, "rule-formula-input", "max(carrier_price * 1.1, 45)");
	await pause(500);
	assert((await textIn("rule-card", 3, "rule-formula-error")) === "", "the example's formula is fine");
	// a click outside the open editor closes it; the focus stays where the click put it
	await page.click(by("tape-city"));
	const open = await page.$$eval(`${by("rule-editor")}:not([hidden])`, (found) => found.length);
	assert(open === 0, "a click outside closes the editor");
	assert((await focusedTestid()) === "tape-city", `the focus stays where the click put it: ${await focusedTestid()}`);
	return `the server said: "${error}"`;
});

await check("rules move with the up and down buttons and with the grip, and each move is said", async () => {
	const labels = async () => (await draftNow()).rules.map((rule) => rule.label);
	const first = await labels();
	assert((await (await inCard("rule-card", 0, "rule-up")).evaluate((button) => button.getAttribute("aria-disabled"))) === "true", "the first rule cannot go up");
	await (await inCard("rule-card", 3, "rule-up")).focus();
	await page.keyboard.press("Enter");
	const moved = await labels();
	assert(moved[2] === first[3] && moved[3] === first[2], `up: ${moved}`);
	await expectText("rules-live", "Rule 4 is now rule 3 of 4.");
	const focus = await page.evaluate(() => ({
		testid: document.activeElement?.dataset?.testid,
		card: document.activeElement?.closest('[data-testid="rule-card"]')?.querySelector('[data-testid="rule-label"]')?.value,
	}));
	assert(focus.testid === "rule-up" && focus.card === first[3], `the moved rule keeps the focus: ${JSON.stringify(focus)}`);
	await (await inCard("rule-card", 2, "rule-down")).focus();
	await page.keyboard.press("Enter");
	assert(JSON.stringify(await labels()) === JSON.stringify(first), "down puts it back");
	// with the mouse: the grip of rule 1 dropped under rule 2, and back
	const drag = async (from, to, below) => {
		const grip = await (await inCard("rule-card", from, "rule-grip")).boundingBox();
		const target = await (await nth("rule-card", to)).boundingBox();
		const y = below ? target.y + target.height - 6 : target.y + 6;
		await page.mouse.move(grip.x + grip.width / 2, grip.y + grip.height / 2);
		await page.mouse.down();
		await page.mouse.move(grip.x + grip.width / 2, y, { steps: 12 });
		const line = await page.$$eval(".sp-rule.is-drop-before, .sp-rule.is-drop-after", (found) => found.length);
		await page.mouse.up();
		return line;
	};
	const line = await drag(0, 1, true);
	const dragged = await labels();
	assert(dragged[0] === first[1] && dragged[1] === first[0], `dragged: ${dragged}`);
	assert(line === 1, `a line shows where it lands: ${line}`);
	await drag(1, 0, false);
	assert(JSON.stringify(await labels()) === JSON.stringify(first), "and dragged back");
	// with reduced motion the card does not follow the pointer; the line still shows where it lands
	await page.emulateMediaFeatures([{ name: "prefers-reduced-motion", value: "reduce" }]);
	try {
		const grip = await (await inCard("rule-card", 0, "rule-grip")).boundingBox();
		await page.mouse.move(grip.x + grip.width / 2, grip.y + grip.height / 2);
		await page.mouse.down();
		await page.mouse.move(grip.x + grip.width / 2, grip.y + 40, { steps: 5 });
		const moved = await page.$eval(".sp-rule.is-dragging", (card) => card.style.transform);
		await page.mouse.move(grip.x + grip.width / 2, grip.y + grip.height / 2, { steps: 5 });
		await page.mouse.up();
		assert(moved === "", `reduced motion: the card stays put (${moved})`);
	} finally {
		await page.emulateMediaFeatures([{ name: "prefers-reduced-motion", value: "no-preference" }]);
	}
	assert(JSON.stringify((await draftNow()).rules) === JSON.stringify(EXAMPLE.rules), "the rules are the example's");
});

await check("a rule switched off says Off, and a rule is deleted only after a question", async () => {
	await clickIn("rule-card", 2, "rule-disable");
	await expectIn("rule-card", 2, "rule-state", "Off");
	await expectIn("rule-card", 2, "rule-price", "– Off");
	await clickIn("rule-card", 2, "rule-disable");
	assert((await textIn("rule-card", 2, "rule-state")) === "", "on again");
	await page.click(by("rule-add"));
	assert((await page.$$(by("rule-card"))).length === 5, "a fifth rule");
	await clickIn("rule-card", 4, "rule-delete");
	await answerDialog(false);
	assert((await page.$$(by("rule-card"))).length === 5, "No keeps it");
	assert((await focusedTestid()) === "rule-delete", "the focus is back on Delete");
	await clickIn("rule-card", 4, "rule-delete");
	await answerDialog(true);
	assert((await page.$$(by("rule-card"))).length === 4, "Yes deletes it");
	await expectText("rules-live", "Rule 5 deleted.");
	assert((await focusedTestid()) === "rule-delete", "the focus goes to the Delete of the rule now last");
	assert(JSON.stringify((await draftNow()).rules) === JSON.stringify(EXAMPLE.rules), "the rules are the example's");
});

await check("Only when: six limits with no-limit placeholders and their units, read as they are typed", async () => {
	await clickIn("rule-card", 2, "chip-limits");
	const fields = await (await nth("rule-card", 2)).$$eval(by("rule-limit"), (inputs) =>
		inputs.map((input) => ({
			key: input.dataset.key,
			placeholder: input.placeholder,
			label: input.labels?.[0]?.textContent,
			measure: document.getElementById(input.closest('[role="group"]')?.getAttribute("aria-labelledby"))
				?.textContent,
		}))
	);
	assert(fields.length === 6 && fields.every((field) => field.placeholder === "no limit"), JSON.stringify(fields));
	const measures = [...new Set(fields.map((field) => field.measure))].join("|");
	assert(measures === "Order value ($)|Weight (kg)|Volume (litres)", `the units: ${measures}`);
	assert(fields.map((field) => field.label).join("|") === "from|up to|from|up to|from|up to", "from and up to");
	const limit = async (key, text) => {
		const input = await (await nth("rule-card", 2)).$(`${by("rule-limit")}[data-key="${key}"]`);
		await input.evaluate((field) => {
			field.focus();
			field.select();
		});
		await page.keyboard.press("Backspace");
		if (text) await input.type(text);
	};
	await limit("max_order_value", "500");
	assert((await draftNow()).rules[2].max_order_value === 500, "500 is in the draft");
	await expectIn("rule-card", 2, "chip-limits", "order value up to $500.00");
	// a field that stops holding a number keeps its text to itself: the draft keeps the last number it held
	await page.keyboard.type(" or so");
	await expectIn("rule-card", 2, "rule-limits-problem", "type a number such as 500");
	assert((await draftNow()).rules[2].max_order_value === 500, "what is not a number stays out of the draft");
	await limit("max_order_value", "5,5");
	await expectIn("rule-card", 2, "rule-limits-problem", "use a period for decimals");
	assert((await draftNow()).rules[2].max_order_value === 5, "a decimal comma is never read as 55");
	await limit("min_order_value", "600");
	await limit("max_order_value", "500");
	await expectIn("rule-card", 2, "rule-problems", "the wrong way round");
	await limit("min_order_value", "");
	await limit("max_order_value", "");
	await expectIn("rule-card", 2, "chip-limits", "no limits");
	await page.keyboard.press("Escape");
	assert(JSON.stringify((await draftNow()).rules) === JSON.stringify(EXAMPLE.rules), "no limits again");
});

await check("an item set that a rule uses is not deleted, a taken name is refused, an edit says who else changes", async () => {
	await clickIn("rule-card", 1, "chip-items");
	await clickIn("rule-card", 1, "itemset-delete");
	await expectIn("rule-card", 1, "rule-items-problem", "1 rule uses this set. Change that rule first.");
	await clickIn("rule-card", 1, "itemset-new");
	await typeIn("rule-card", 1, "itemset-name", "watches");
	await typeIn("rule-card", 1, "itemset-group-add", WATCH_GROUP.toLowerCase());
	await page.keyboard.press("Enter");
	await clickIn("rule-card", 1, "itemset-save");
	await expectIn("rule-card", 1, "itemset-problem", "Another item set has this name.");
	await clickIn("rule-card", 1, "itemset-cancel");
	assert((await focusedTestid()) === "itemset-new", "Cancel goes back to the sets");
	await page.keyboard.press("Escape");
	await clickIn("rule-card", 2, "chip-items");
	await clickIn("rule-card", 2, "itemset-edit");
	await expectIn("rule-card", 2, "itemset-shared", "Rule 2 uses this set too: saving changes it there as well.");
	await page.keyboard.press("Escape"); // in the set editor, Escape is Cancel
	assert(!(await (await nth("rule-card", 2)).$(by("itemset-editor"))), "Escape leaves the set editor");
	await page.keyboard.press("Escape");
	const draft = await draftNow();
	assert(JSON.stringify(draft.item_sets) === JSON.stringify(builtSets()), "the one set, unchanged");
	assert(JSON.stringify(draft.rules) === JSON.stringify(EXAMPLE.rules), "the rules, unchanged");
});

await check("hostile zone and item set names are shown as text in Zones and Rules", async () => {
	const draft = {
		...original,
		zones: [zoneOf(HOSTILE, ["AE"])],
		item_sets: [{ name: HOSTILE_SET, item_groups: [WATCH_GROUP], items: [] }],
		rules: [ruleOf(HOSTILE, "free", { item_set: HOSTILE_SET })],
	};
	await openWithStored({ version, draft, step: "rules" });
	await expectText("draft-banner", "Restored your unsaved draft.");
	assert((await textIn("rule-card", 0, "chip-zone")) === HOSTILE, "the zone chip says the name");
	assert((await textIn("rule-card", 0, "chip-items")) === `${HOSTILE_SET}*`, "the items chip too");
	await clickIn("rule-card", 0, "chip-items");
	await expectIn("rule-card", 0, "rule-editor", HOSTILE_SET);
	assert((await page.$$(".sp-wizard img, .sp-wizard b")).length === 0, "no element was made from a name");
	await page.evaluate(() => document.activeElement?.blur());
	const file = await tallShot("wizard-hostile-names-rules");
	await page.keyboard.press("Escape");
	await page.click(by("step-tab-zones"));
	assert((await valueIn("zone-card", 0, "zone-name")) === HOSTILE, "the zone's name field holds the name");
	await expectIn("zone-card", 0, "zone-usage", "Rule 1 uses this zone.");
	assert((await page.$$(".sp-wizard img, .sp-wizard b")).length === 0, "no element in Zones either");
	await page.evaluate((key) => window.localStorage.removeItem(key), DRAFT_KEY);
	return file;
});

await check("the Shipping Policy form opens the wizard from a primary button, under a quiet hint", async () => {
	await go(NEUTRAL);
	await page.evaluate((key) => window.localStorage.removeItem(key), DRAFT_KEY);
	await open(FORM);
	const button = await page.waitForFunction(
		() =>
			[...document.querySelectorAll(".page-head button")].find(
				(found) => found.textContent.trim() === "Open the wizard" && found.offsetParent
			),
		{ timeout: 20000 }
	);
	assert(await button.evaluate((found) => found.classList.contains("btn-primary")), "the button is primary");
	const hint = await page.$eval(".form-message", (message) => ({
		text: message.textContent.trim(),
		muted: Boolean(message.querySelector(".text-muted")),
	}));
	assert(hint.text.startsWith("The wizard edits this policy step by step") && hint.muted, `hint: ${hint.text}`);
	const file = await shot("form-open-the-wizard");
	await button.click();
	await page.waitForFunction(() => location.pathname === "/desk/shipping-policy-wizard", { timeout: 15000 });
	await page.waitForSelector(by("tape-total"), { timeout: 20000 });
	return file;
});

await check("the tape stands beside the steps only while the steps keep 460 px", async () => {
	const seen = [];
	for (const width of [900, 1000, 1100, 1200, 1400]) {
		await viewport(width);
		seen.push(
			await page.evaluate((width) => {
				const step = document.querySelector(".sp-step").getBoundingClientRect();
				const tape = document.querySelector(".sp-tape").getBoundingClientRect();
				const root = document.documentElement;
				const beside = tape.left >= step.right;
				return { width, beside, step: Math.round(step.width), sideways: root.scrollWidth > root.clientWidth };
			}, width)
		);
	}
	await viewport(1400);
	for (const at of seen) {
		assert(!at.beside || at.step >= 460, `at ${at.width} px the steps have ${at.step} px beside the tape`);
		assert(!at.sideways, `at ${at.width} px the page scrolls sideways`);
	}
	assert(seen.at(-1).beside, "at 1400 px the tape is beside the steps");
	return seen.map((at) => `${at.width}: ${at.beside ? `beside, steps ${at.step} px` : "stacked"}`).join("; ");
});

// Last, because the desk remembers the narrow layout's closed sidebar for the pages loaded after it.
await check("at 760 px the tape stacks under the steps and nothing scrolls sideways (screenshot)", async () => {
	await viewport(760);
	await open(); // loaded at this width, as the desk lays itself out for a narrow screen
	const narrow = await page.evaluate(() => {
		const step = document.querySelector(".sp-step").getBoundingClientRect();
		const tape = document.querySelector(".sp-tape").getBoundingClientRect();
		const tabs = document.querySelector('.sp-wizard [role="tablist"]').getBoundingClientRect();
		const root = document.documentElement;
		const wizard = document.querySelector(".sp-wizard");
		return {
			stacked: tape.top >= step.bottom,
			page: root.scrollWidth <= root.clientWidth,
			wizard: wizard.scrollWidth <= wizard.clientWidth,
			tabs: tabs.right <= root.clientWidth,
		};
	});
	assert(Object.values(narrow).every(Boolean), JSON.stringify(narrow));
	return shot("wizard-760");
});

await check("at 760 px the zone and rule cards fit, the sentences wrap and nothing scrolls sideways", async () => {
	const draft = { ...original, zones: builtZones(), item_sets: builtSets(), rules: EXAMPLE.rules };
	await openWithStored({ version, draft, step: "rules" });
	const fits = async () =>
		page.evaluate(() => {
			const root = document.documentElement;
			const panel = document.querySelector(".sp-step");
			const cards = [...document.querySelectorAll('[data-testid="zone-card"], [data-testid="rule-card"]')];
			return {
				page: root.scrollWidth <= root.clientWidth,
				cards: cards.length > 0 && cards.every((card) => card.getBoundingClientRect().right <= panel.getBoundingClientRect().right + 1),
				inside: cards.every((card) => card.scrollWidth <= card.clientWidth + 1),
			};
		});
	await clickIn("rule-card", 3, "chip-charge");
	const rules = await fits();
	const files = [await tallShot("wizard-rules-760")];
	await page.keyboard.press("Escape");
	await page.click(by("step-tab-zones"));
	const zones = await fits();
	files.push(await tallShot("wizard-zones-760"));
	// the restored draft was not changed here, so nothing writes it back: the browser's storage is left empty
	await page.evaluate((key) => window.localStorage.removeItem(key), DRAFT_KEY);
	await pause(500);
	assert((await stored()) === null, "nothing is left in the browser's storage");
	assert(Object.values(rules).every(Boolean), `rules: ${JSON.stringify(rules)}`);
	assert(Object.values(zones).every(Boolean), `zones: ${JSON.stringify(zones)}`);
	return files.join(", ");
});

await check("no console errors or warnings and no failed requests", expectNoConsoleErrors);

await check("log out, so that the run leaves no session behind on the ERP", async () => {
	const user = await page.evaluate(async () => {
		const headers = { Accept: "application/json", "X-Frappe-CSRF-Token": frappe.csrf_token };
		await (await fetch("/api/method/logout", { method: "POST", headers })).text();
		const after = await fetch("/api/method/frappe.auth.get_logged_user", { headers });
		return after.ok ? (await after.json()).message : `HTTP ${after.status}`;
	});
	assert(user !== process.env.ERP_USER, "still signed in");
});

await browser.close();

const failed = results.filter((result) => !result.ok).length;
console.log(`${results.length - failed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
