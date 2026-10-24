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
// The wizard writes only on Apply. A draft is put in the browser's own storage, where the wizard keeps it, to
// test restoring it and showing hostile names. The Zones and Rules checks build the example policy
// (tests/hermetic/fixtures/shipping_policy_example.json) through the page as a draft and price orders with it;
// the Fallback and Review checks finish it and APPLY it on erp2, change it from a second tab to see a stale
// Apply refused, and then empty it again through the page's own Apply. The Rates checks import the real Shopify
// export (from RATES_DIR, default ~/shipping-interim-rates, outside this public repository: the files are handed
// to the page's file choosers and never copied anywhere; without them the repository's trimmed sample is imported
// instead, and the run says so), edit the card, APPLY it as the price source, switched on, and run the Test tab
// against it. Whatever happens, the run ends with erp2's policy empty and switched off again, as it started
// (ensureEmptyPolicy, in a finally), and the browser's storage empty.

import { access, mkdir, mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";
import os from "node:os";
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
// The website prices from the policy only while it is on with the rate card as the price source; with the carrier
// it charges the carrier's own prices, whatever the policy says (ruling P100), and the page says so.
const STILL_THE_CARRIER =
	"The website still charges the carrier's own prices: it applies the policy only with the rate card as the price source.";
// The receipt's note while the draft is off: with the carrier as the price source (as erp2's empty policy has it),
// and with the rate card.
const OFF =
	"The policy is switched off, so today customers pay the carrier's price. This receipt shows what the policy " +
	"would charge, but the website applies it only with the rate card as the price source.";
const OFF_BY_CARD =
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
// A zone name the server's sentences quote: what it says must come back as typed, never as markup (P59).
const HOSTILE_ZONE = "Gulf & <b>Co</b>";
const METHOD = "dsi_catalogue.shipping_policy_wizard.";
// The policy erp2 starts from and is left with: switched off, no zones, item sets or rules (as wizard_state()
// gives it, keys in the same order).
const EMPTY = {
	active: false,
	price_source: "carrier",
	rate_card: null,
	not_covered: "pass_through",
	default_fallback: { method: "no_delivery", amount: 0 },
	zones: [],
	item_sets: [],
	rules: [],
};

// The Shopify export the Rates checks import, and what the server reads from it: the real one, from RATES_DIR
// (outside this public repository: the files are handed to the page's file choosers and never copied), or, when
// it is not there, the repository's trimmed sample (tests/hermetic/fixtures), which prices the USA order alike.
const RATES_DIR = process.env.RATES_DIR || path.join(os.homedir(), "shipping-interim-rates");
const RATES_REAL = {
	what: "the real Shopify export",
	config: path.join(RATES_DIR, "shopify_shipping_config.csv"),
	closed: path.join(RATES_DIR, "shopify_countries_not_delivered.csv"),
	read: "The shipping rates hold 4 profiles, 14 zones, 49 weight tiers and 232 countries.",
	notes: 3,
	closed_count: 12,
	missing: ["AC", "AN", "TA"],
	unknown: "XK", // in the Worldwide zone of the catch-all profile: no country of the ERP
	petite: "1 Dangerous Goods Petite Product codes F-*-PT and F-CR-PC-*. 3 zones with 6 weight tiers.",
	usa: "USA 1 country. 15 weight tiers from 0.1 to 20 lb, $35.00 to $400.00, International Express (4-7 days). $35.00 to $400.00",
	usa_review: "USA: United States. 15 weight tiers from 0.1 to 20 lb, $35.00 to $400.00, International Express (4-7 days).",
	usa_tiers: 15,
	worldwide: 225, // the countries of the Worldwide zone that the ERP knows
	after: "It holds 4 profiles, 14 zones, 49 weight tiers and 231 countries.", // once XK is taken out
};
const RATES_SAMPLE = {
	what: "the repository's trimmed sample (RATES_DIR has no export)",
	config: new URL("../hermetic/fixtures/shopify_shipping_config_sample.csv", import.meta.url).pathname,
	closed: new URL("../hermetic/fixtures/shopify_countries_not_delivered_sample.csv", import.meta.url).pathname,
	read: "The shipping rates hold 4 profiles, 10 zones, 20 weight tiers and 18 countries.",
	notes: 5,
	closed_count: 3,
	missing: [],
	unknown: null,
	petite: "1 Dangerous Goods Petite Product codes F-*-PT and F-CR-PC-*. 2 zones with 3 weight tiers.",
	usa: "USA 1 country. 5 weight tiers from 0.1 to 20 lb, $35.00 to $400.00, International Express (4-7 days). $35.00 to $400.00",
	usa_review: "USA: United States. 5 weight tiers from 0.1 to 20 lb, $35.00 to $400.00, International Express (4-7 days).",
	usa_tiers: 5,
	worldwide: 5,
	after: "It holds 4 profiles, 10 zones, 20 weight tiers and 18 countries.",
};
const RATES = await access(RATES_REAL.config)
	.then(() => access(RATES_REAL.closed))
	.then(
		() => RATES_REAL,
		() => RATES_SAMPLE
	);

// What erp2 shows on every desk page, with or without the wizard: the dsi_plm app asks for a script it does
// not ship. Seen on /desk/shipping-policy and /desk/shipping-dashboard before the wizard existed. And the
// dsi_copilot app pings its model every 3 minutes while a desk page is open (copilot.js, setInterval 180000):
// a ping still on its way when the run opens another page is aborted by the browser itself.
const KNOWN_NOISE = [
	/\/assets\/dsi_plm\/js\/project_customization\.js/,
	/^request failed: POST \S+\/api\/method\/dsi_copilot\.api\.keep_warm net::ERR_ABORTED$/,
];

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

// An amount as the page prints it: a dollar sign and two decimals.
const sp_money = (amount) => `$${amount.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

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

// The try_order, check_formula, apply_policy, import_rate_card and test_integration requests the page sends, a way
// to hold the next one back for a while, and a way to answer the next request of a wizard method in the server's
// place (fakeNext[method]: {status, body} (JSON) or {status, raw} (text as it is), or "abort" for a request that
// never reaches the server).
let priced = 0;
const tryVerbs = new Set(); // the HTTP verbs of the try_order requests the page sends
let checked = 0;
let applied = 0;
let imported = 0;
let tested = 0;
let intercepting = false;
let holdNext = 0; // ms to hold the next try_order request before it goes out
let holdCheck = 0; // ms to hold the next check_formula request before it goes out
let holdGroups = 0; // ms to hold the next read of the Item Group list before it goes out
let holdApply = 0; // ms to hold the next apply_policy request before it goes out
let holdPreview = 0; // ms to hold the next preview_changes request before it goes out
let holdTest = 0; // ms to hold the next test_integration request before it goes out
let holdImport = 0; // ms to hold the next import_rate_card request before it goes out
const fakeNext = {};
page.on("request", (request) => {
	const isTry = request.url().includes("shipping_policy_wizard.try_order");
	const isCheck = request.url().includes("shipping_policy_wizard.check_formula");
	const isGroups =
		request.url().includes("frappe.client.get_list") && (request.postData() || "").includes("doctype=Item+Group");
	const isApply = request.url().includes("shipping_policy_wizard.apply_policy");
	const isPreview = request.url().includes("shipping_policy_wizard.preview_changes");
	const isTest = request.url().includes("shipping_policy_wizard.test_integration");
	const isImport = request.url().includes("shipping_policy_wizard.import_rate_card");
	if (isTry) {
		priced += 1;
		tryVerbs.add(request.method());
	}
	if (isCheck) checked += 1;
	if (isApply) applied += 1;
	if (isImport) imported += 1;
	if (isTest) tested += 1;
	if (!intercepting) return;
	const fake = Object.keys(fakeNext).find((method) => request.url().includes(`shipping_policy_wizard.${method}`));
	if (fake) {
		const answer = fakeNext[fake];
		delete fakeNext[fake];
		if (answer === "abort") request.abort("failed").catch(() => {});
		else {
			const body = "raw" in answer ? answer.raw : JSON.stringify(answer.body);
			request.respond({ status: answer.status, contentType: "application/json", body }).catch(() => {});
		}
		return;
	}
	const wait =
		(isTry && holdNext) ||
		(isCheck && holdCheck) ||
		(isGroups && holdGroups) ||
		(isApply && holdApply) ||
		(isPreview && holdPreview) ||
		(isTest && holdTest) ||
		(isImport && holdImport) ||
		0;
	if (wait) {
		if (isTry) holdNext = 0;
		else if (isCheck) holdCheck = 0;
		else if (isApply) holdApply = 0;
		else if (isPreview) holdPreview = 0;
		else if (isTest) holdTest = 0;
		else if (isImport) holdImport = 0;
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
	// the pointer out of the way: once the window grows, it would rest on whatever is under it, hovered
	await page.mouse.move(size.width - 2, 2);
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

// Waits until the n-th rule's open formula editor shows the server's answer for `formula` (its error box says
// which formula its answer is for: an empty box alone may only mean that no answer has come yet).
async function formulaChecked(n, formula, timeout = 10000) {
	try {
		await page.waitForFunction(
			(n, formula) =>
				document.querySelectorAll('[data-testid="rule-card"]')[n]?.querySelector('[data-testid="rule-formula-error"]')
					?.dataset.checked === formula,
			{ timeout },
			n,
			formula
		);
	} catch {
		throw new Error(`rule ${n + 1}: no answer from the server for the formula ${formula}`);
	}
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

// Puts on (or takes off again) a draft that charges a dollar more than the carrier in Bahrain: the zone "Island"
// and one formula rule. The receipt then has other delivery methods at prices of the policy's own.
async function priceUp(on) {
	await page.evaluate((on) => {
		frappe.pages["shipping-policy-wizard"].wizard.change((draft) => {
			if (!on) {
				draft.zones.pop();
				draft.rules.pop();
				return;
			}
			const zone = dsi_catalogue.sp.new_zone("Island");
			zone.countries.push("BH");
			draft.zones.push(zone);
			const rule = dsi_catalogue.sp.new_rule("Island");
			rule.method = "formula";
			rule.formula = "carrier_price + 1";
			draft.rules.push(rule);
		});
	}, on);
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

// A check that has the server refuse on purpose leaves traces in the list the last check reads: the refusal's
// HTTP status, the browser's "Failed to load resource" line, and the traceback Frappe prints with console.error
// on a site that allows tracebacks (erp2 does). Each pattern must be there, once for each time it is given; it
// is taken out, and nothing else is.
function expected(...patterns) {
	for (const pattern of patterns) {
		const at = trouble.findIndex((line) => pattern.test(line));
		assert(at >= 0, `expected ${pattern} among what the page reported: ${trouble.join(" | ") || "(nothing)"}`);
		trouble.splice(at, 1);
	}
}

// The traces of one Apply refused with status 417: its status, then the traceback of the exception the server
// raised (an answer the test gives in the server's place has none).
const REFUSED_STATUS = [
	/^HTTP 417: POST .*shipping_policy_wizard\.apply_policy/,
	/^console error: Failed to load resource: .*417.*shipping_policy_wizard\.apply_policy/,
];
const REFUSED = (exception) => [...REFUSED_STATUS, new RegExp(`^console error: [^]*${exception}`)];

// A wizard method called from the page as the page calls it (frappe.call, the test user's session): its
// answer, or an error naming the exception.
async function wizardCall(method, args = {}, on = page) {
	return on.evaluate(
		(method, args) =>
			new Promise((resolve, reject) => {
				frappe.call({
					method,
					args,
					silent: true,
					callback: (response) => resolve(response.message),
					error: (response) => reject(new Error(response?.exc_type || "the server did not answer")),
				});
			}),
		METHOD + method,
		args
	);
}

// erp2's policy as the run found it: switched off, with no zones, item sets or rules. Puts it back through the
// policy's own Apply (apply_policy, the test user's session) unless it is so already. It runs at the end of
// every run (in a finally), so it opens a desk page of its own first: frappe.call needs the desk.
async function ensureEmptyPolicy() {
	await go(NEUTRAL);
	await page.evaluate((key) => window.localStorage.removeItem(key), DRAFT_KEY);
	await go(FORM);
	await page.waitForFunction(() => Boolean(window.frappe?.csrf_token && frappe.call), { timeout: 20000 });
	const state = await wizardCall("wizard_state");
	if (JSON.stringify(state.policy) === JSON.stringify(EMPTY)) return { version: state.version, applied: false };
	const answer = await wizardCall("apply_policy", { draft: EMPTY, expected_version: state.version });
	return { version: answer.version, applied: true };
}

// Someone else changes the saved policy while the wizard has it open: a second tab of the same browser (the
// test user's session) applies the saved policy with GCC's customer line changed. Returns the new version.
async function changeUnderFoot(line) {
	const other = await browser.newPage();
	try {
		await other.setCacheEnabled(false);
		await other.goto(`${BASE}${FORM}`, { waitUntil: "networkidle2", timeout: 60000 });
		await other.waitForFunction(() => Boolean(window.frappe?.csrf_token && frappe.call), { timeout: 20000 });
		const state = await wizardCall("wizard_state", {}, other);
		const policy = state.policy;
		policy.zones.find((zone) => zone.name === "GCC").customer_line = line;
		const answer = await wizardCall("apply_policy", { draft: policy, expected_version: state.version }, other);
		await other.waitForNetworkIdle({ idleTime: 400, timeout: 20000 }).catch(() => {});
		return answer.version;
	} finally {
		await other.close();
	}
}

// Waits until Apply can be used: the draft checked by the server as it is, with nothing in the way.
async function applyReady(timeout = 15000) {
	try {
		await page.waitForFunction(
			() => document.querySelector('[data-testid="apply-button"]')?.getAttribute("aria-disabled") === "false",
			{ timeout }
		);
	} catch {
		const why = (await page.$(by("apply-hint"))) ? await textOf("apply-hint") : "(no hint)";
		throw new Error(`Apply did not become ready: ${why}`);
	}
}

// The text of each element with the test id, as it reads on the screen.
async function textsOf(testid, within = "") {
	return page.$$eval(`${within} ${by(testid)}`.trim(), (found) =>
		found.map((element) => element.innerText.replace(/\s+/g, " ").trim())
	);
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

// The checks below expect erp2's policy to be the empty one: a policy left by a run that was cut off short is
// emptied first.
await check("erp2's policy is the empty one, switched off, when the run starts", async () => {
	if (JSON.stringify((await store()).original) === JSON.stringify(EMPTY)) return "";
	const cleaned = await ensureEmptyPolicy();
	await open();
	assert(JSON.stringify((await store()).original) === JSON.stringify(EMPTY), "the policy is empty now");
	return `a policy left by an earlier run was emptied first (version ${cleaned.version})`;
});

let { version, original } = await store();

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

await check("every step shows its body, and none the placeholder line", async () => {
	// a control each built body has
	const BUILT = {
		zones: "zone-add",
		rates: "price-source",
		rules: "rule-add",
		fallback: "not-covered",
		review: "apply-button",
		test: "test-run",
	};
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
	await page.click(by("step-tab-zones")); // from Zones, whatever an earlier check left selected
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
	// the empty policy passes the carrier's prices through: the receipt does not list them all again
	assert(await page.$eval(".sp-more", (block) => block.hidden), "the carrier's list is not repeated");
	const total = await textOf("tape-total");
	const file = await shot("wizard-asked-the-carrier");
	// a draft that changes the prices: the other methods are listed, a quiet row each, at what the customer
	// would pay (the draft stays on for the next two checks, which need a tall tape)
	await priceUp(true);
	await expectText("tape-total", sp_money(Number(quote) + 1));
	const others = await page.$$eval(`${by("tape-more")} li`, (rows) => rows.map((row) => row.innerText));
	const quoted = (await page.$$(`${by("tape-services")} li`)).length;
	assert(others.length === quoted - 1, `one quiet row for each other method: ${others.length}`);
	assert(!(await textOf("tape-more")).includes("pay"), "no second Customer pays line");
	return `the carrier said "${services}", the quote field holds ${quote}, the total reads ${total}; ${file}`;
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
	await priceUp(false);
	assert(Object.values(seen).every(Boolean), JSON.stringify(seen));
	return file;
});

// The desk scrolls its main section, not the window: the tape must measure its room again when that scrolls,
// or a room measured while the page was scrolled down stays too tall once it is back at the top.
await check("beside the steps the tape is measured again as the desk scrolls, so its total stays in view", async () => {
	await viewport(1400, 768);
	await priceUp(true); // its other delivery methods make the tape tall
	const extra = (count) =>
		page.evaluate((count) => {
			frappe.pages["shipping-policy-wizard"].wizard.change((draft) => {
				if (count < 0) draft.rules.splice(count);
				else for (let n = 0; n < count; n += 1) draft.rules.push(dsi_catalogue.sp.new_rule("Island"));
			});
		}, count);
	await extra(3); // and three more rules make the step taller than the window
	try {
		await page.click(by("step-tab-rules"));
		const scroll = (top) => page.evaluate((top) => document.querySelector(".main-section").scrollTo(0, top), top);
		await scroll(400);
		await pause(400);
		// the layout grows while the page is scrolled down: the tape measures its room where it stands then
		await page.evaluate(() =>
			document.querySelectorAll('[data-testid="rule-card"]')[3].querySelector('[data-testid="chip-charge"]').click()
		);
		await pause(400);
		await scroll(0);
		await pause(400);
		const seen = await page.evaluate(() => ({
			tape: Math.round(document.querySelector(".sp-tape").getBoundingClientRect().bottom),
			total: Math.round(document.querySelector(".sp-total").getBoundingClientRect().bottom),
			window: window.innerHeight,
		}));
		await page.keyboard.press("Escape");
		assert(seen.tape <= seen.window - 72 && seen.total <= seen.window - 72, `back at the top: ${JSON.stringify(seen)}`);
	} finally {
		await extra(-3);
		await priceUp(false);
		await page.click(by("step-tab-zones"));
		await viewport(1400);
	}
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
	await answerDialog(true);
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
	// on, with the carrier as the price source: what the policy would charge, which the website does not apply yet
	await expectText("tape-lines", `${STILL_THE_CARRIER} This receipt shows what the policy would charge.`);
	const label = await textOf("tape-total-label");
	assert(label === "Customer would pay", `on, with the carrier as the price source, the total reads "${label}"`);
	await page.click(by("tape-unavailable"));
	await expectText("tape-lines", "The carrier could not quote, so the fallback applies.");
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
	// what the switches and the customer texts mean is said once, in the step's legend, not on every card
	const said = await page.$eval("#sp-panel-zones", (panel) => panel.innerText);
	for (const words of ["Delivery is not offered to the zone's countries.", "The FAQ and the Shipping page show"]) {
		assert(said.split(words).length === 2, `said once: ${words}`);
	}
	const described = await (await inCard("zone-card", 3, "zone-not-shipped")).evaluate(
		(box) => document.getElementById(box.getAttribute("aria-describedby"))?.textContent
	);
	assert(described?.startsWith("Delivery is not offered"), `a switch is described by its legend line: ${described}`);
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
	await formulaChecked(3, "max(carrier_price * 1.1, 45)");
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
	assert((await textIn("rule-card", 1, "rule-price")) === "Conditions not met", "no watch in the order");
	assert((await textIn("rule-card", 0, "rule-price")) === "Not for this country", "Bahrain's rule");
	const quiet = await page.$$eval(`${by("rule-price")} .sp-rule-amount`, (found) => found.length);
	assert(quiet === 1, `a figure only where there is one: ${quiet}`);
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

await check("a step drawn while a change is being priced waits for its price, never shows another rule's", async () => {
	await expectIn("rule-card", 2, "rule-price", "$39.00"); // the GCC order of the screenshots
	const prices = () => page.$$eval(by("rule-price"), (found) => found.map((price) => price.textContent));
	await intercept(true);
	try {
		holdNext = 2500; // the price of the move arrives late
		await (await inCard("rule-card", 0, "rule-down")).click();
		await page.click(by("step-tab-zones"));
		await page.click(by("step-tab-rules"));
		const meanwhile = await prices();
		assert(meanwhile.every((text) => text === ""), `while the move is priced: ${JSON.stringify(meanwhile)}`);
		await expectIn("rule-card", 2, "rule-price", "$39.00", 15000);
		const after = await prices();
		assert(after[0].includes("Conditions not met") && after[1].includes("Not for this country"), JSON.stringify(after));
	} finally {
		await intercept(false);
	}
	await (await inCard("rule-card", 1, "rule-up")).click();
	await expectIn("rule-card", 2, "rule-price", "$39.00");
	assert(JSON.stringify((await draftNow()).rules) === JSON.stringify(EXAMPLE.rules), "the rules are the example's");
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
	// a name cleared and left empty, then typed later: the zone's rules are still its own
	await typeIn("zone-card", 1, "zone-name", "");
	await page.evaluate(() => document.activeElement?.blur());
	await pause(300);
	await typeIn("zone-card", 1, "zone-name", "Gulf");
	rules = (await draftNow()).rules;
	assert(rules[1].zone === "Gulf" && rules[2].zone === "Gulf", `a name left empty a while: ${rules[1].zone}`);
	await typeIn("zone-card", 1, "zone-name", "GCC");
	rules = (await draftNow()).rules;
	assert(rules[1].zone === "GCC" && rules[2].zone === "GCC", "and back");
	assert(rules[0].zone === "Bahrain", "rule 1 is still Bahrain's");
	await page.click(by("step-tab-rules"));
	assert((await textIn("rule-card", 2, "chip-zone")) === "GCC", "the chip says so");
});

// Ruling P92: the ERP keeps a zone's and an item set's name without the spaces around it, and the engine matches
// names exactly, so a name is trimmed once it is committed (the field left, or Enter), and its rules follow.
await check("a zone or item set name typed with spaces around it is kept without them once it is left, and its rules follow", async () => {
	try {
		await page.click(by("step-tab-zones"));
		await typeIn("zone-card", 1, "zone-name", "GCC ");
		let draft = await draftNow();
		assert(draft.zones[1].name === "GCC " && draft.rules[1].zone === "GCC ", "while it is typed, a space at its end stays");
		await page.evaluate(() => document.activeElement?.blur());
		draft = await draftNow();
		assert(draft.zones[1].name === "GCC", `once left, the zone is "${draft.zones[1].name}"`);
		assert(draft.rules[1].zone === "GCC" && draft.rules[2].zone === "GCC", `and its rules: "${draft.rules[1].zone}", "${draft.rules[2].zone}"`);
		assert((await valueIn("zone-card", 1, "zone-name")) === "GCC", "and so does the field");
		await expectIn("zone-card", 1, "zone-usage", "Rules 2 and 3 use this zone.");
		// Enter commits a name as well, and the field keeps the focus
		await typeIn("zone-card", 1, "zone-name", " GCC");
		await page.keyboard.press("Enter");
		draft = await draftNow();
		assert(draft.zones[1].name === "GCC" && draft.rules[2].zone === "GCC", `Enter trims it: "${draft.zones[1].name}"`);
		assert((await focusedTestid()) === "zone-name", `the field keeps the focus: ${await focusedTestid()}`);
		await page.evaluate(() => document.activeElement?.blur());
		// an item set's name in its editor, which Save writes trimmed too
		await page.click(by("step-tab-rules"));
		await clickIn("rule-card", 1, "chip-items");
		await clickIn("rule-card", 1, "itemset-edit");
		await typeIn("rule-card", 1, "itemset-name", "  Watches  ");
		await page.keyboard.press("Tab");
		assert((await valueIn("rule-card", 1, "itemset-name")) === "Watches", "a set's name is kept without its spaces");
		await clickIn("rule-card", 1, "itemset-cancel");
		await clickIn("rule-card", 1, "rule-done");
		draft = await draftNow();
		assert(draft.zones[1].name === "GCC" && draft.item_sets[0].name === "Watches", "the zone and the set as they were");
		assert(JSON.stringify(draft.rules) === JSON.stringify(EXAMPLE.rules), "the rules are the example's");
	} finally {
		// the checks after this one start on the Rules step, with no editor open and GCC named as it was
		await page.evaluate(() => document.activeElement?.blur());
		await page.evaluate(() => {
			const wizard = frappe.pages["shipping-policy-wizard"].wizard;
			wizard.ui.rules.set_editor = null;
			wizard.ui.rules.open = null;
			wizard.change((draft) => {
				const rules = draft.rules.filter((rule) => rule.zone.trim() === "GCC");
				draft.zones[1].name = "GCC";
				for (const rule of rules) rule.zone = "GCC";
			});
		});
		await page.click(by("step-tab-rules"));
	}
});

await check("a formula is checked 300 ms after the last key, and an older answer never replaces a newer one", async () => {
	await clickIn("rule-card", 3, "chip-charge");
	await pause(600);
	checked = 0;
	await typeIn("rule-card", 3, "rule-formula-input", "max(carrier_price * 1.1, 45");
	await formulaChecked(3, "max(carrier_price * 1.1, 45");
	await pause(500); // a second check would have gone out by now
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
		await formulaChecked(3, "carrier_price * 2");
		assert((await textIn("rule-card", 3, "rule-formula-error")) === "", "the newer formula is fine");
		await page.waitForNetworkIdle({ idleTime: 500, timeout: 15000 }); // the older answer has arrived now
		const after = await (await inCard("rule-card", 3, "rule-formula-error")).evaluate((box) => ({
			text: box.textContent,
			checked: box.dataset.checked,
		}));
		assert(after.text === "" && after.checked === "carrier_price * 2", `after the older answer: ${JSON.stringify(after)}`);
	} finally {
		await intercept(false);
	}
	await typeIn("rule-card", 3, "rule-formula-input", "max(carrier_price * 1.1, 45)");
	await formulaChecked(3, "max(carrier_price * 1.1, 45)");
	assert((await textIn("rule-card", 3, "rule-formula-error")) === "", "the example's formula is fine");
	// a click outside the open editor closes it; the focus stays where the click put it
	await page.click(by("tape-city"));
	const open = await page.$$eval(`${by("rule-editor")}:not([hidden])`, (found) => found.length);
	assert(open === 0, "a click outside closes the editor");
	assert((await focusedTestid()) === "tape-city", `the focus stays where the click put it: ${await focusedTestid()}`);
	// each rule's formula has its own check: opening another formula rule's charge right after typing does not
	// cancel the first one's, whose card then says what the server said
	await choose(2, "chip-charge", "rule-charge-option", "formula");
	await page.evaluate(() => {
		// typed, and in the same moment (well inside the 300 ms) the other formula rule's charge opened
		const cards = document.querySelectorAll('[data-testid="rule-card"]');
		const input = cards[2].querySelector('[data-testid="rule-formula-input"]');
		input.value = "boxes +"; // a formula not checked before, so the answer has to come from the server
		input.dispatchEvent(new Event("input", { bubbles: true }));
		cards[3].querySelector('[data-testid="chip-charge"]').click();
	});
	await expectIn("rule-card", 2, "rule-problems", "The formula cannot be used:", 10000);
	await page.keyboard.press("Escape");
	await clickIn("rule-card", 2, "chip-charge");
	await typeIn("rule-card", 2, "rule-formula-input", "");
	await (await (await nth("rule-card", 2)).$(`${by("rule-charge-option")}[value="carrier_price"]`)).click();
	await page.keyboard.press("Escape");
	assert(JSON.stringify((await draftNow()).rules) === JSON.stringify(EXAMPLE.rules), "the rules are the example's");
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
	await expectIn("rule-card", 2, "rule-price", "Off");
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

await check("a charge switched away from keeps its Round up and its formula, and switching back shows them", async () => {
	// ruling P88c: what a charge had is kept as a remembered choice; the sentence says only what applies
	await choose(2, "chip-charge", "rule-charge-option", "free");
	await expectIn("rule-card", 2, "rule-sentence", "charge nothing (free)");
	assert(!(await textIn("rule-card", 2, "rule-sentence")).includes("rounded up"), "Free is never rounded up");
	assert((await draftNow()).rules[2].round_up === true, "but Round up is kept");
	await (await (await nth("rule-card", 2)).$(`${by("rule-charge-option")}[value="carrier_price"]`)).click();
	await expectIn("rule-card", 2, "rule-sentence", "charge the carrier's price, rounded up");
	await page.keyboard.press("Escape");
	await choose(3, "chip-charge", "rule-charge-option", "carrier_price");
	await expectIn("rule-card", 3, "rule-sentence", "charge the carrier's price, rounded up");
	assert((await draftNow()).rules[3].formula === "max(carrier_price * 1.1, 45)", "the formula is kept");
	await (await (await nth("rule-card", 3)).$(`${by("rule-charge-option")}[value="formula"]`)).click();
	await expectIn("rule-card", 3, "rule-sentence", "charge max(carrier_price * 1.1, 45), rounded up");
	await page.keyboard.press("Escape");
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
	// a limit is kept to the decimals the ERP keeps (money 2, kilograms and litres 3), as the ERP would round it
	await limit("max_order_value", "");
	await limit("min_weight_kg", "0.0004");
	assert((await draftNow()).rules[2].min_weight_kg === 0, `0.0004 kg is kept as 0: ${(await draftNow()).rules[2].min_weight_kg}`);
	await expectIn("rule-card", 2, "chip-limits", "no limits");
	await expectIn("rule-card", 2, "rule-limits-problem", "Weight (kg), from: the ERP keeps 3 decimals, so this is no limit.");
	await limit("min_weight_kg", "1.23456");
	assert((await draftNow()).rules[2].min_weight_kg === 1.235, "1.23456 kg is kept as 1.235");
	await expectIn("rule-card", 2, "chip-limits", "weight from 1.235 kg");
	await limit("min_weight_kg", "");
	await limit("max_order_value", "99.999");
	assert((await draftNow()).rules[2].max_order_value === 100, "$99.999 is kept as $100.00");
	await expectIn("rule-card", 2, "rule-limits-problem", "Order value ($), up to: the ERP keeps 2 decimals, so this is $100.00.");
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
	// a set being made is not dropped by a chip, of this rule or another: the editor says to save or cancel it
	await expectIn("rule-card", 1, "itemset-editor", "Save the set or cancel it to edit anything else in the rules.");
	for (const [n, chip] of [
		[1, "chip-zone"],
		[1, "chip-items"],
		[2, "chip-charge"],
	]) {
		await clickIn("rule-card", n, chip);
		assert(await (await nth("rule-card", 1)).$(by("itemset-editor")), `${chip} of rule ${n + 1} leaves the set editor open`);
		assert((await valueIn("rule-card", 1, "itemset-name")) === "watches", "with what was typed in it");
		assert((await focusedTestid()) === "itemset-name", `and shows where it is: ${await focusedTestid()}`);
	}
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

// The desk listens for Escape on the window and takes the focus away from wherever it is: what Escape closes
// in the wizard must not let it through.
await check("Escape closes a list, an editor or a question, and the focus stays where it belongs", async () => {
	const expanded = (testid, n) =>
		inCard(n.kind, n.at, testid).then((input) => input.evaluate((found) => found.getAttribute("aria-expanded")));
	// a zone's country list
	await page.click(by("step-tab-zones"));
	await typeIn("zone-card", 1, "zone-country-add", "ba");
	assert((await expanded("zone-country-add", { kind: "zone-card", at: 1 })) === "true", "the list is open");
	await page.keyboard.press("Escape");
	assert((await expanded("zone-country-add", { kind: "zone-card", at: 1 })) === "false", "Escape closes the list");
	assert((await focusedTestid()) === "zone-country-add", `and the field keeps the focus: ${await focusedTestid()}`);
	// a question asked by Everywhere else, dismissed with Escape
	await clickIn("zone-card", 1, "zone-everywhere-else");
	await page.waitForFunction(() => Boolean(window.cur_dialog?.display), { timeout: 10000 });
	await page.keyboard.press("Escape");
	await page.waitForFunction(() => !document.querySelector(".modal.show, .modal-backdrop"), { timeout: 10000 });
	await pause(200);
	assert((await focusedTestid()) === "zone-everywhere-else", `the switch has the focus again: ${await focusedTestid()}`);
	assert((await countriesOf(1)).join() === "AE,SA,KW,OM,QA", "and nothing changed");
	// a chip whose editor is open: Escape on the chip itself
	await page.click(by("step-tab-rules"));
	await clickIn("rule-card", 2, "chip-charge");
	await page.keyboard.down("Shift");
	await page.keyboard.press("Tab");
	await page.keyboard.up("Shift");
	assert((await focusedTestid()) === "chip-charge", `Shift+Tab goes back to the chip: ${await focusedTestid()}`);
	await page.keyboard.press("Escape");
	assert(!(await page.$(`${by("rule-editor")}:not([hidden])`)), "Escape on the chip closes its editor");
	assert((await focusedTestid()) === "chip-charge", `and the chip keeps the focus: ${await focusedTestid()}`);
	// the desk's item search in the set editor: Escape closes its list, then cancels the set, then the editor
	await clickIn("rule-card", 1, "chip-items");
	await clickIn("rule-card", 1, "itemset-edit");
	await typeIn("rule-card", 1, "itemset-item-add", WATCH.slice(0, 6));
	await page.waitForFunction(
		() => document.querySelector('[data-testid="itemset-item-add"]')?.getAttribute("aria-expanded") === "true",
		{ timeout: 15000 }
	);
	await page.keyboard.press("Escape");
	assert((await expanded("itemset-item-add", { kind: "rule-card", at: 1 })) === "false", "Escape closes the item list");
	assert((await focusedTestid()) === "itemset-item-add", `the item field keeps the focus: ${await focusedTestid()}`);
	assert(await (await nth("rule-card", 1)).$(by("itemset-editor")), "the set editor is still open");
	await page.keyboard.press("Escape");
	assert(!(await (await nth("rule-card", 1)).$(by("itemset-editor"))), "a second Escape cancels the set");
	assert((await focusedTestid()) === "itemset-new", `then New item set has the focus: ${await focusedTestid()}`);
	await page.keyboard.press("Escape");
	assert((await focusedTestid()) === "chip-items", `a third closes the editor, back on its chip: ${await focusedTestid()}`);
	// Delete's question, dismissed with Escape
	await clickIn("rule-card", 3, "rule-delete");
	await page.waitForFunction(() => Boolean(window.cur_dialog?.display), { timeout: 10000 });
	await page.keyboard.press("Escape");
	await page.waitForFunction(() => !document.querySelector(".modal.show, .modal-backdrop"), { timeout: 10000 });
	await pause(200);
	const back = await page.evaluate(() => ({
		testid: document.activeElement?.dataset?.testid,
		label: document.activeElement?.getAttribute("aria-label"),
	}));
	assert(back.testid === "rule-delete" && back.label === "Delete rule 4", `Delete has the focus again: ${JSON.stringify(back)}`);
	const draft = await draftNow();
	assert(JSON.stringify(draft.rules) === JSON.stringify(EXAMPLE.rules), "the rules, unchanged");
	assert(JSON.stringify(draft.item_sets) === JSON.stringify(builtSets()), "the set, unchanged");
});

// ---------------------------------------------------------------- Task 9: the fallbacks, Review and Apply
// These checks finish the example policy built above, APPLY it on erp2 and change it there. The last one
// empties it again through the page's own Apply; the finally makes sure of it whatever happened.

// The index of the fallback card of a zone, by its name.
async function fallbackCard(name) {
	const titles = await page.$$eval(by("fallback-zone"), (cards) =>
		cards.map((card) => card.querySelector(".sp-fallback-title")?.textContent)
	);
	assert(titles.includes(name), `no fallback card for ${name}: ${titles}`);
	return titles.indexOf(name);
}

// Picks the radio with this value in the n-th card of a kind, and what is picked there.
async function pick(kind, n, value, testid = "fallback-method") {
	const radio = await (await nth(kind, n)).$(`${by(testid)}[value="${value}"]`);
	assert(radio, `${kind} ${n + 1} offers no ${value}`);
	await radio.click();
}

async function picked(kind, n, testid = "fallback-method") {
	return (await nth(kind, n)).$$eval(by(testid), (radios) => radios.find((radio) => radio.checked)?.value || "");
}

const ariaDisabled = () => page.$eval(by("apply-button"), (button) => button.getAttribute("aria-disabled"));

// The example policy as the page finishes it on erp2: the fixture's (GCC charges a fixed $35 when the carrier
// cannot quote), with erp2's watch group, still switched off.
const finished = () => ({ ...EXAMPLE, active: false, item_sets: builtSets() });

try {
	await check("If the carrier can't quote: a card per zone, then the policy default and Anything not covered", async () => {
		await page.click(by("step-tab-fallback"));
		const titles = await page.$$eval(by("fallback-zone"), (cards) =>
			cards.map((card) => card.querySelector(".sp-fallback-title").textContent)
		);
		assert(titles.join("|") === "Bahrain|GCC|Rest of world|Not shipped", `a card per zone, in order: ${titles}`);
		for (const n of [0, 1, 2]) assert((await picked("fallback-zone", n)) === "policy_default", `zone ${n + 1}: the default`);
		const labels = await (await nth("fallback-zone", 1)).$$eval(".sp-option-text", (found) => found.map((label) => label.textContent));
		const words = "Use the policy default|Don't offer delivery|Charge a fixed amount|Free|Use the rate card";
		assert(labels.join("|") === words, `the five choices in plain words, the policy default first: ${labels}`);
		// a Not shipped zone has nothing to fall back to, and says so in words
		const closed = await (await nth("fallback-zone", 3)).evaluate((card) => ({
			choices: [...card.querySelectorAll('[data-testid="fallback-method"]')].some((radio) => radio.offsetParent),
			said: card.querySelector('[data-testid="fallback-sentence"]').textContent,
		}));
		assert(!closed.choices, "a Not shipped zone has no choice");
		assert(closed.said === "Delivery is never offered here: this zone is not shipped to.", closed.said);
		const offered = await page.$$eval(`${by("fallback-policy-default")} ${by("fallback-method")}`, (radios) =>
			radios.map((radio) => radio.value)
		);
		assert(offered.join() === "no_delivery,fixed,free,rate_card", `the policy default offers four: ${offered}`);
		assert((await picked("fallback-policy-default", 0)) === "no_delivery", "the policy default does not offer delivery");
		assert((await picked("not-covered", 0, "not-covered-option")) === "pass_through", "and passes the carrier's price");
		// "Use the rate card" is offered; once chosen while there is no card, and only then, it says to add one
		const note = "There is no rate card yet. Add one in the Rates step.";
		const notes = async () =>
			page.$$eval(".sp-fallback .sp-option-note", (found) => found.filter((one) => one.offsetParent).map((one) => one.textContent));
		assert((await notes()).length === 0, `no note while no zone uses the rate card: ${await notes()}`);
		await pick("fallback-zone", 1, "rate_card");
		assert(JSON.stringify(await notes()) === JSON.stringify([note]), `the note, once, beside the choice: ${await notes()}`);
		await expectIn("fallback-zone", 1, "fallback-sentence", "customers here pay what the rate card charges.");
		// a zone that only takes the policy default shows what that comes to quietly: brick is for a choice made
		const tones = await page.evaluate(() => {
			const colour = (element) => getComputedStyle(element).color;
			const figure = (n) => document.querySelectorAll('[data-testid="fallback-zone"]')[n].querySelector('[data-testid="fallback-figure"]');
			return {
				inherited: [colour(figure(0)), figure(0).textContent],
				closed: colour(figure(3)),
				quiet: colour(document.querySelector("#sp-panel-fallback .sp-step-intro")),
			};
		});
		assert(tones.inherited[0] === tones.quiet && tones.inherited[1] === "Not offered", `inherited, quiet: ${JSON.stringify(tones)}`);
		assert(tones.closed === "rgb(168, 71, 58)", `a Not shipped zone's own outcome is brick: ${tones.closed}`);
		// GCC: a fixed amount, blank until it is typed, kept in cents
		await pick("fallback-zone", 1, "fixed");
		assert((await valueIn("fallback-zone", 1, "fallback-amount")) === "", "the amount is blank until it is typed");
		await expectIn("fallback-zone", 1, "fallback-problem", "Type the amount customers pay, such as 35.");
		assert((await draftNow()).zones[1].fallback.amount === null, "and the draft has none");
		await typeIn("fallback-zone", 1, "fallback-amount", "-5");
		await expectIn("fallback-zone", 1, "fallback-problem", "Type an amount of 0 or more, such as 35.");
		await typeIn("fallback-zone", 1, "fallback-amount", "35,5");
		await expectIn("fallback-zone", 1, "fallback-problem", "Use a period for decimals.");
		assert((await draftNow()).zones[1].fallback.amount === 35, "what is not an amount stays out of the draft");
		await typeIn("fallback-zone", 1, "fallback-amount", "35.126");
		await expectIn("fallback-zone", 1, "fallback-problem", "The ERP keeps cents, so this is $35.13.");
		assert((await draftNow()).zones[1].fallback.amount === 35.13, "35.126 is kept as 35.13");
		await typeIn("fallback-zone", 1, "fallback-amount", "35");
		await expectIn("fallback-zone", 1, "fallback-sentence", "If the carrier cannot quote, customers here pay a fixed $35.00.");
		assert((await textIn("fallback-zone", 1, "fallback-problem")) === "", "no problem left");
		const figure = await (await inCard("fallback-zone", 1, "fallback-figure")).evaluate((found) => ({
			text: found.textContent,
			colour: getComputedStyle(found).color,
		}));
		assert(figure.text === "$35.00" && figure.colour === "rgb(154, 113, 22)", `$35.00 in brass: ${JSON.stringify(figure)}`);
		// the policy default: a change shows in the zones that use it; then back to don't offer
		await pick("fallback-policy-default", 0, "free");
		await expectIn("fallback-zone", 0, "fallback-sentence", "If the carrier cannot quote, the policy default applies: delivery is free.");
		await expectIn("fallback-policy-default", 0, "fallback-note", "Bahrain and Rest of world use it.");
		await pick("fallback-policy-default", 0, "no_delivery");
		await expectIn("fallback-zone", 0, "fallback-sentence", "the policy default applies: delivery is not offered.");
		await expectIn("fallback-policy-default", 0, "fallback-sentence", "When a zone uses the policy default and the carrier cannot quote, delivery is not offered.");
		// a fixed amount left behind waits outside the draft: the policy default is as it was saved again (M3)
		await pick("fallback-policy-default", 0, "fixed");
		await typeIn("fallback-policy-default", 0, "fallback-amount", "35");
		await pick("fallback-policy-default", 0, "no_delivery");
		const left = (await draftNow()).default_fallback;
		assert(JSON.stringify(left) === JSON.stringify(original.default_fallback), `the saved default again: ${JSON.stringify(left)}`);
		await pick("fallback-policy-default", 0, "fixed");
		assert((await valueIn("fallback-policy-default", 0, "fallback-amount")) === "35.00", "and the amount comes back with Fixed");
		await pick("fallback-policy-default", 0, "no_delivery");
		// Anything not covered: no delivery, then back to passing the carrier's price through
		await pick("not-covered", 0, "no_delivery", "not-covered-option");
		await expectIn("not-covered", 0, "fallback-sentence", "When no rule fits an order, delivery is not offered.");
		await pick("not-covered", 0, "pass_through", "not-covered-option");
		await expectIn("not-covered", 0, "fallback-sentence", "When no rule fits an order, customers pay the carrier's price.");
		const draft = await draftNow();
		assert(JSON.stringify(draft) === JSON.stringify(finished()), `the draft is the example: ${JSON.stringify(draft)}`);
	});

	await check("“The carrier can't quote” prices a GCC order at its fixed fallback at once, and Bahrain gets no delivery", async () => {
		await pickCountry("United Arab Emirates");
		await expectText("tape-total", "$39.00");
		await page.click(by("tape-unavailable"));
		await expectText("tape-total", "$35.00");
		await expectText("tape-lines", "Standard delivery");
		await expectText(
			"tape-lines",
			"The carrier gave no price for the whole shipment. The fallback applies: a fixed charge of 35.00. $35.00"
		);
		assert((await lastLine()) === "The carrier could not quote, so the fallback applies.", `said last: ${await lastLine()}`);
		assert((await textOf("tape-total-label")) === "Customer would pay", "the draft is still off");
		// a change of the fallback shows on the tape at once, and an amount typed before comes back with its choice
		await pick("fallback-zone", 1, "free");
		await expectText("tape-total", "Free");
		await pick("fallback-zone", 1, "fixed");
		await expectText("tape-total", "$35.00");
		assert((await valueIn("fallback-zone", 1, "fallback-amount")) === "35.00", "the amount comes back with Fixed");
		await pickCountry("Bahrain");
		await expectText("tape-total", "Not offered");
		await expectText("tape-lines", "Delivery is not offered.");
		await page.click(by("tape-unavailable"));
		await pickCountry("United Arab Emirates");
		await expectText("tape-total", "$39.00");
		assert(JSON.stringify(await draftNow()) === JSON.stringify(finished()), "the draft is the example");
	});

	await check("screenshots of the fallbacks with the example policy, light and dark", async () => {
		await page.click(by("tape-unavailable")); // the tape shows a fallback price
		await expectText("tape-total", "$35.00");
		await page.evaluate(() => document.activeElement?.blur());
		const files = [await tallShot("wizard-fallback-light")];
		await theme("dark");
		files.push(await tallShot("wizard-fallback-dark"));
		const brass = await (await inCard("fallback-zone", 1, "fallback-figure")).evaluate((found) => getComputedStyle(found).color);
		const brick = await (await inCard("fallback-zone", 3, "fallback-figure")).evaluate((found) => getComputedStyle(found).color);
		await theme("light");
		await page.click(by("tape-unavailable"));
		await expectText("tape-total", "$39.00");
		assert(brass === "rgb(216, 178, 89)" && brick === "rgb(229, 144, 127)", `the accents in the dark: ${brass}, ${brick}`);
		return files.join(", ");
	});

	await check("Review: what will change lists the zones, set and rules, and the policy reads zone by zone in plain words", async () => {
		await page.click(by("step-tab-review"));
		await applyReady();
		const diff = await textOf("review-diff");
		assert(diff.startsWith("Zones added:"), `what will change starts with its first line: ${diff}`);
		for (const said of [
			"Zones added: Bahrain, GCC, Not shipped and Rest of world",
			"Item sets added: Watches",
			"Rules added: Rule 1 (Free in Bahrain), Rule 2 (Free watches in the Gulf), Rule 3 (Gulf at the carrier price) and Rule 4 (International with a minimum)",
		]) {
			assert(diff.includes(said), `what will change says "${said}": ${diff}`);
		}
		assert(!diff.includes("Active"), `nothing about Active yet: ${diff}`);
		assert((await page.$$(by("review-problem"))).length === 0, "no problems");
		const zones = await textsOf("review-zone");
		const want = [
			"Bahrain Bahrain. Customers read: “Free delivery in Bahrain.” Rule 1 (Free in Bahrain): In Bahrain, for every item, charge nothing (free). If the carrier cannot quote, the policy default applies: delivery is not offered.",
			"GCC United Arab Emirates; Saudi Arabia; Kuwait; Oman; Qatar. Customers read: “Gulf delivery, priced by the carrier. Watches ship free.” Rule 2 (Free watches in the Gulf): In GCC, for orders made up only of items in Watches, charge nothing (free). Rule 3 (Gulf at the carrier price): In GCC, for every item, charge the carrier's price, rounded up. If the carrier cannot quote, customers here pay a fixed $35.00.",
			"Rest of world Everywhere else Every other country. Customers read: “International delivery, priced at checkout.” Rule 4 (International with a minimum): In Rest of world, for every item, charge max(carrier_price * 1.1, 45), rounded up. If the carrier cannot quote, the policy default applies: delivery is not offered.",
			"Not shipped Not shipped Russian Federation; Belarus. Delivery is never offered here. Customers read: “We do not deliver to these countries.” At checkout they read: “We don't deliver to this country.”",
		];
		zones.forEach((text, n) => assert(text === want[n], `zone ${n + 1} reads: ${text}`));
		assert(zones.length === 4, `four zones: ${zones.length}`);
		// each rule in the words of its card in the Rules step (sp.rule_words), with commas: sp.rule_statement
		const summary = await textOf("review-summary");
		const sentences = await page.evaluate(() =>
			frappe.pages["shipping-policy-wizard"].wizard.store.draft.rules.map((rule) => dsi_catalogue.sp.rule_statement(rule))
		);
		sentences.forEach((text, n) => assert(summary.includes(`: ${text}.`), `rule ${n + 1} in its card's words: ${text}`));
		const source =
			"Prices start from the carrier's quote for each order. For now the website applies the policy only with the rate card as the price source, so customers still pay the carrier's own prices.";
		assert(summary.startsWith(source), `where prices start, and that the website does not apply them yet, first: ${summary.slice(0, 220)}`);
		assert(summary.endsWith("When no rule fits an order, customers pay the carrier's price."), "Anything not covered, last");
		assert(!(await page.$eval(by("policy-active"), (box) => box.checked)), "the switch is off");
		// its pale track has a ring in the muted text colour, so the control is seen on the desk's surface
		const ring = await page.evaluate(() => {
			const style = getComputedStyle(document.querySelector('[data-testid="policy-active"]'));
			const quiet = getComputedStyle(document.querySelector("#sp-panel-review .sp-step-intro")).color;
			return { width: style.borderTopWidth, style: style.borderTopStyle, colour: style.borderTopColor, quiet };
		});
		assert(ring.width === "1px" && ring.style === "solid" && ring.colour === ring.quiet, `the ring: ${JSON.stringify(ring)}`);
		const state = await textOf("policy-active-state");
		assert(state === "Switched off: customers pay the carrier's price", `the switch's state in words: ${state}`);
		assert((await textOf("apply-button")) === "Apply", "Apply: nothing about Active changes");
		const why = await textOf("apply-hint");
		assert(why === "The policy stays off: customers keep paying the carrier's price.", `beside Apply: ${why}`);
		await page.evaluate(() => document.activeElement?.blur());
		const files = [await tallShot("wizard-review-light")];
		await theme("dark");
		files.push(await tallShot("wizard-review-dark"));
		await theme("light");
		return files.join(", ");
	});

	// The ERP keeps names without the spaces around them, and the Zones step writes a name as it is typed, to the
	// zone and to its rules: the Review still reads the zone's rules (finding I1).
	await check("a zone named with a space at its end keeps its rules in the Review", async () => {
		await page.click(by("step-tab-zones"));
		await typeIn("zone-card", 1, "zone-name", "GCC ");
		const draft = await draftNow();
		assert(draft.zones[1].name === "GCC " && draft.rules[2].zone === "GCC ", "the zone and its rules have the space");
		await page.click(by("step-tab-review"));
		await applyReady();
		const gcc = (await textsOf("review-zone"))[1];
		assert(gcc.startsWith("GCC United Arab Emirates"), `the zone, named as the ERP will keep it: ${gcc}`);
		for (const rule of ["Rule 2 (Free watches in the Gulf)", "Rule 3 (Gulf at the carrier price)"]) {
			assert(gcc.includes(rule), `GCC keeps ${rule}: ${gcc}`);
		}
		assert(!gcc.includes("No rule is for this zone yet."), "it has rules");
		await page.click(by("step-tab-zones"));
		await typeIn("zone-card", 1, "zone-name", "GCC");
		await page.click(by("step-tab-review"));
		assert(JSON.stringify(await draftNow()) === JSON.stringify(finished()), "the draft is the example again");
	});

	// Ruling P89: with the rate card as the price source the carrier is not asked, so no fallback is used.
	await check("with the rate card as the price source, the fallbacks and the Review say they are not used", async () => {
		const source = (value) =>
			page.evaluate((value) => {
				frappe.pages["shipping-policy-wizard"].wizard.change((draft) => {
					draft.price_source = value;
				});
			}, value);
		await source("rate_card");
		try {
			await page.click(by("step-tab-fallback"));
			await expectText(
				"fallback-unused",
				"While the price source is the rate card the carrier is not asked, so none of these choices is used."
			);
			const colours = await page.evaluate(() => {
				const gcc = document.querySelectorAll('[data-testid="fallback-zone"]')[1];
				const colour = (element) => getComputedStyle(element).color;
				return {
					sentence: colour(gcc.querySelector('[data-testid="fallback-sentence"]')),
					figure: colour(gcc.querySelector('[data-testid="fallback-figure"]')),
					muted: colour(document.querySelector("#sp-panel-fallback .sp-step-intro")),
				};
			});
			assert(colours.sentence === colours.muted && colours.figure === colours.muted, `quiet: ${JSON.stringify(colours)}`);
			await page.click(by("step-tab-review"));
			const summary = await textOf("review-summary");
			const first = "Prices come from the rate card: the carrier is not asked, and the rules and fallbacks below are not used.";
			assert(summary.startsWith(first), `the summary says it first: ${summary}`);
			assert(summary.includes("Rule 3 (Gulf at the carrier price)"), "and still says the rules");
			const quiet = await page.evaluate(() => [
				getComputedStyle(document.querySelector('[data-testid="review-summary"] .sp-summary-rules')).color,
				getComputedStyle(document.querySelector("#sp-panel-review .sp-step-intro")).color,
			]);
			assert(quiet[0] === quiet[1], `the rules read quietly: ${quiet}`);
		} finally {
			await source("carrier");
		}
		await page.click(by("step-tab-fallback"));
		assert(await page.$eval(by("fallback-unused"), (note) => note.hidden), "no note while the carrier is the source");
		await page.click(by("step-tab-review"));
		assert(JSON.stringify(await draftNow()) === JSON.stringify(finished()), "the draft is the example again");
	});

	// The example policy has the carrier as its price source: switched on, it is still the carrier's own prices that
	// customers pay on the website (ruling P100), and the switch, the line beside Apply and the tape say so.
	await check("the Active switch says its state in words, and Apply then says it switches the policy on", async () => {
		assert((await textOf("tape-total-label")) === "Customer would pay", "off: the customer would pay");
		await page.click(by("policy-active"));
		await expectText("policy-active-state", "Switched on: customers still pay the carrier's own prices");
		assert((await textOf("apply-button")) === "Apply and switch on", `the button: ${await textOf("apply-button")}`);
		await applyReady();
		await expectText("review-diff", "Active: off → on");
		const arrow = await page.$eval(`${by("review-diff")} .sp-change-arrow`, (found) => found.getAttribute("aria-label"));
		assert(arrow === "becomes", `the arrow is said as a word: ${arrow}`);
		// the receipt once the draft is on: what the policy would charge, which the website does not apply yet
		await expectText("tape-lines", STILL_THE_CARRIER);
		assert((await textOf("tape-total-label")) === "Customer would pay", "on, with the carrier: the customer would pay");
		await expectText("tape-total", "$39.00");
		// from the keyboard: Space switches it off and on again
		await page.focus(by("policy-active"));
		await page.keyboard.press("Space");
		await expectText("policy-active-state", "Switched off: customers pay the carrier's price");
		await page.keyboard.press("Space");
		await expectText("policy-active-state", "Switched on: customers still pay the carrier's own prices");
		await applyReady();
		const why = await textOf("apply-hint");
		const still = "The website will still charge the carrier's own prices: it applies the policy only with the rate card as the price source.";
		assert(why === still, `beside Apply: ${why}`);
		assert((await draftNow()).active === true, "the draft is on");
	});

	await check("Apply writes the draft once, says what was applied, and leaves no draft in storage", async () => {
		applied = 0;
		await intercept(true);
		try {
			holdApply = 2500; // the write is held a moment: what is pressed meanwhile changes nothing (finding M7)
			// a double click: the second finds the first one's request on its way, and sends nothing
			await page.$eval(by("apply-button"), (button) => {
				button.click();
				button.click();
			});
			await pause(400);
			const meanwhile = await page.evaluate(() => {
				// as a key would under the desk's freeze: the Active switch, and another step's tab
				const box = document.querySelector('[data-testid="policy-active"]');
				box.click();
				document.querySelector('[data-testid="step-tab-zones"]').click();
				const wizard = frappe.pages["shipping-policy-wizard"].wizard;
				return {
					active: wizard.store.draft.active,
					switched: box.checked,
					step: wizard.store.step,
					busy: document.querySelector(".sp-wizard .sp-step").getAttribute("aria-busy"),
				};
			});
			assert(meanwhile.active === true && meanwhile.switched === true, `the draft stays: ${JSON.stringify(meanwhile)}`);
			assert(meanwhile.step === "review" && meanwhile.busy === "true", `the step stays: ${JSON.stringify(meanwhile)}`);
			await expectText("apply-done", `Applied and switched on. ${STILL_THE_CARRIER}`, 30000);
		} finally {
			await intercept(false);
		}
		assert(applied === 1, `${applied} requests for a double click`);
		const busy = await page.$eval(".sp-wizard .sp-step", (box) => box.getAttribute("aria-busy"));
		assert(busy === "false", `not busy once it is applied: ${busy}`);
		const done = await textOf("apply-done");
		for (const said of [
			"Zones added: Bahrain, GCC, Not shipped and Rest of world",
			"Item sets added: Watches",
			"Rules added: Rule 1 (Free in Bahrain)",
			"Active: off → on",
		]) {
			assert(done.includes(said), `what was applied says "${said}": ${done}`);
		}
		assert((await focusedTestid()) === "apply-done-title", `its first line has the focus: ${await focusedTestid()}`);
		const saved = await store();
		assert(!saved.dirty && saved.original.active === true, "the draft is the saved policy, and it is on");
		assert(saved.version !== version, "a new version");
		assert(saved.original.rules.length === 4 && saved.original.rules.every((rule) => rule.name), "the rules have rows");
		await pause(500);
		assert((await stored()) === null, "no draft is left in storage");
		await expectText("review-diff", "Nothing to apply yet.");
		assert((await ariaDisabled()) === "true", "Apply waits for a change");
		assert(await page.$eval(by("draft-banner"), (banner) => banner.hidden), "no banner: nothing is edited");
		await expectText("tape-total", "$39.00");
		assert((await textOf("tape-total-label")) === "Customer would pay", "on, with the carrier: the customer would pay");
		const state = await wizardCall("wizard_state");
		assert(state.version === saved.version && state.policy.active && state.policy.zones.length === 4, "the server agrees");
		return tallShot("wizard-applied");
	});

	await check("after a reload the policy is the applied one, on, and prices a GCC order at $39.00", async () => {
		await open();
		await pause(500);
		assert(await page.$eval(by("draft-banner"), (banner) => banner.hidden), "no draft banner");
		const saved = await store();
		assert(!saved.dirty && saved.original.active === true, "the saved policy is on");
		// the ERP reads zones back sorted by name (load_policy): the order of the zones is not part of a policy
		const by_name = [...EXAMPLE.zones].sort((one, other) => (one.name < other.name ? -1 : 1));
		assert(JSON.stringify(saved.original.zones) === JSON.stringify(by_name), "the example's zones, by name");
		const names = await page.$$eval(by("zone-name"), (inputs) => inputs.map((input) => input.value));
		assert(names.join("|") === "Bahrain|GCC|Not shipped|Rest of world", `the zones are there: ${names}`);
		await pickCountry("United Arab Emirates");
		await fill("tape-quote", "38.20");
		await expectText("tape-total", "$39.00");
		assert((await textOf("tape-total-label")) === "Customer would pay", "on, with the carrier: the customer would pay");
		assert(!(await textOf("tape-lines")).includes("switched off"), "no note that the policy is off");
		await expectText("tape-lines", STILL_THE_CARRIER);
	});

	await check("an edit while the policy is on: the banner says that edits go live when they are applied", async () => {
		// a saved zone's choice switched to a fixed amount and back changes nothing: no edit, no banner (M3)
		await page.click(by("step-tab-fallback"));
		await pick("fallback-zone", 0, "fixed");
		await typeIn("fallback-zone", 0, "fallback-amount", "35");
		assert((await store()).dirty, "a fixed amount is an edit");
		await pick("fallback-zone", 0, "policy_default");
		const back = await store();
		assert(!back.dirty, `back to its saved choice, the draft is the saved policy: ${JSON.stringify(back.draft.zones[0].fallback)}`);
		assert(await page.$eval(by("draft-banner"), (banner) => banner.hidden), "and no banner");
		await page.click(by("step-tab-rules"));
		await typeIn("rule-card", 2, "rule-label", "Gulf at the carrier price, edited");
		await expectText("draft-banner", "This policy is on: edits go live when you apply them.");
		for (const step of ["zones", "fallback", "review"]) {
			await page.click(by(`step-tab-${step}`));
			assert(!(await page.$eval(by("draft-banner"), (banner) => banner.hidden)), `the banner stays on ${step}`);
		}
		await applyReady();
		await expectText("review-diff", "Rules changed: Rule 3 (Gulf at the carrier price, edited)");
		assert((await textOf("apply-button")) === "Apply", "Apply: Active does not change");
	});

	await check("a policy changed under the page's feet: Apply says so, keeps the draft, and the differences can be reviewed", async () => {
		const before = await store();
		const changed = await changeUnderFoot("Gulf delivery, changed in another tab.");
		assert(changed !== before.version, "the other tab changed the policy");
		applied = 0;
		await page.click(by("apply-button"));
		await expectText("apply-error", "Someone changed the policy after you opened it. Your draft is kept.", 30000);
		expected(...REFUSED("TimestampMismatchError"));
		assert((await page.$eval(by("apply-error"), (box) => box.getAttribute("role"))) === "alert", "an alert");
		const kept = await store();
		assert(kept.dirty && kept.draft.rules[2].label === "Gulf at the carrier price, edited", "the draft is kept");
		assert(kept.version === before.version, "and the version the page loaded");
		assert(JSON.parse(await stored())?.draft?.rules?.[2]?.label === "Gulf at the carrier price, edited", "stored too");
		assert((await ariaDisabled()) === "true", "Apply waits for a choice");
		const why = await textOf("apply-hint");
		assert(why === "Review the differences or reload the saved policy first.", `beside Apply: ${why}`);
		await pause(1500);
		assert(applied === 1, `nothing is sent again by itself: ${applied}`);
		// what will change is out of date now: it says so, and the comparison under it is dimmed
		await expectText(
			"review-out-of-date",
			"This is out of date: someone changed the policy after you opened it. Review the differences to see what applying would change now."
		);
		const dim = await page.$eval(`${by("review-diff")} .sp-changes`, (list) => getComputedStyle(list).opacity);
		assert(dim === "0.55", `the comparison is dimmed: ${dim}`);
		const file = await tallShot("wizard-stale");
		await page.click(by("stale-review"));
		await expectText("apply-done", "The saved policy was read again: what will change now compares your draft with it.");
		assert(!(await page.$(by("review-out-of-date"))), "what will change is current again");
		const rebased = await store();
		assert(rebased.version === changed, "Apply now goes over the policy as it is saved");
		assert(rebased.dirty && rebased.draft.rules[2].label === "Gulf at the carrier price, edited", "the draft stays");
		assert(rebased.draft.zones[1].customer_line !== "Gulf delivery, changed in another tab.", "as the person left it");
		await applyReady();
		const diff = await textOf("review-diff");
		assert(diff.includes("Zones changed: GCC"), `the other change shows as a difference: ${diff}`);
		assert(diff.includes("Rules changed: Rule 3 (Gulf at the carrier price, edited)"), `and the edit: ${diff}`);
		assert((await textOf("apply-error")) === "", "the message is gone");
		const focus = await page.evaluate(() => document.activeElement?.id);
		assert(focus === "sp-review-diff-title", `the focus is on what will change: ${focus}`);
		assert(JSON.parse(await stored())?.version === changed, "the stored draft has the new version");
		return file;
	});

	await check("Reload the saved policy asks first, then drops the draft for the policy as it is saved now", async () => {
		// the stale answer once more, this time from the test, so that nothing is written for it
		const stale = JSON.stringify({
			message:
				"Someone changed the shipping policy after you opened the wizard. Reload the wizard to see the change, then apply again.",
			title: "The shipping policy cannot be applied",
			indicator: "red",
			raise_exception: 1,
		});
		await intercept(true);
		try {
			fakeNext.apply_policy = {
				status: 417,
				body: { exc_type: "TimestampMismatchError", _server_messages: JSON.stringify([stale]) },
			};
			await page.click(by("apply-button"));
			await expectText("apply-error", "Someone changed the policy after you opened it. Your draft is kept.");
		} finally {
			await intercept(false);
		}
		expected(...REFUSED_STATUS);
		await page.click(by("stale-reload"));
		await answerDialog(false);
		assert((await focusedTestid()) === "stale-reload", `No gives the focus back: ${await focusedTestid()}`);
		assert((await store()).dirty, "and keeps the draft");
		await page.click(by("stale-reload"));
		await answerDialog(true);
		await page.waitForFunction(() => !frappe.pages["shipping-policy-wizard"].wizard.store.dirty, { timeout: 15000 });
		const saved = await store();
		assert(saved.draft.zones[1].customer_line === "Gulf delivery, changed in another tab.", "the policy as saved now");
		assert(saved.draft.rules[2].label === "Gulf at the carrier price", "the edit is dropped");
		await expectText("review-diff", "Nothing to apply yet.");
		assert((await textOf("apply-error")) === "", "the message is gone");
		await page.waitForFunction(() => document.activeElement?.dataset?.testid === "apply-button", { timeout: 5000 });
		await pause(500);
		assert((await stored()) === null, "no draft is left in storage");
	});

	await check("a zone named Gulf & <b>Co</b>: the Review and a refused Apply show the server's sentences as typed", async () => {
		const saved = await wizardCall("wizard_state");
		const zone = zoneOf(HOSTILE_ZONE, ["JO"], { fallback: { method: "rate_card", amount: 0 } });
		const draft = { ...saved.policy, zones: [...saved.policy.zones, zone] };
		await openWithStored({ version: saved.version, draft, step: "review" });
		const sentence = `Zone "${HOSTILE_ZONE}" uses the rate card when the carrier can't quote, but there is no rate card yet.`;
		await expectText("review-problems", sentence);
		const fix = await page.$eval(by("review-fix"), (button) => [button.dataset.step, button.getAttribute("aria-label")]);
		assert(fix[0] === "fallback", `Fix it goes to the step that owns it: ${fix}`);
		await expectText("apply-hint", "Fix the problem at the top first.");
		const zones = await textsOf("review-zone");
		const said = `${HOSTILE_ZONE} Jordan. No rule is for this zone yet. If the carrier cannot quote, customers here pay what the rate card charges.`;
		assert(zones.at(-1) === said, `the summary says the name as typed: ${zones.at(-1)}`);
		assert((await page.$$(".sp-wizard b, .sp-wizard img")).length === 0, "no element was made from the name");
		const files = [await tallShot("wizard-review-problems")];
		await page.click(by("review-fix"));
		assert((await selectedTab()) === "step-tab-fallback", "Fix it opens the step that owns the problem");
		assert((await focusedTestid()) === "step-tab-fallback", "with the focus on its tab");
		// The server refuses what the preview found. The test answers the next preview itself with "no problems",
		// so that Apply sends the draft and the server's own refusal (sentences it escaped) comes back.
		await intercept(true);
		try {
			const real = await wizardCall("preview_changes", { draft });
			fakeNext.preview_changes = { status: 200, body: { message: { changes: real.changes, problems: [] } } };
			await page.click(by("step-tab-fallback"));
			await page.click(by("step-tab-review"));
			await applyReady();
		} finally {
			await intercept(false);
		}
		applied = 0;
		await page.click(by("apply-button"));
		await expectText("apply-error", "The policy was not applied:", 30000);
		expected(...REFUSED("ValidationError"));
		const lines = await page.$$eval(`${by("apply-error")} li`, (found) => found.map((line) => line.textContent));
		assert(JSON.stringify(lines) === JSON.stringify([sentence]), `the server's sentence, as typed: ${JSON.stringify(lines)}`);
		const box = await textOf("apply-error");
		assert(!/&amp;|&lt;|&gt;|&quot;/.test(box), `no escaped text shows: ${box}`);
		assert((await page.$$(".sp-wizard b, .sp-wizard img")).length === 0, "no element was made from the refusal");
		assert(applied === 1, "one request");
		files.push(await tallShot("wizard-hostile-refused"));
		await page.evaluate((key) => window.localStorage.removeItem(key), DRAFT_KEY);
		return files.join(", ");
	});

	await check("someone else applying: the server's sentence, and Apply stays usable", async () => {
		await go(NEUTRAL);
		await page.evaluate((key) => window.localStorage.removeItem(key), DRAFT_KEY);
		await open();
		await page.click(by("step-tab-rules"));
		await typeIn("rule-card", 2, "rule-label", "Gulf at the carrier price, again");
		await page.click(by("step-tab-review"));
		await applyReady();
		const busy = JSON.stringify({
			message: "Someone else is applying a change right now. Try again in a moment.",
			title: "The shipping policy cannot be applied",
			indicator: "red",
			raise_exception: 1,
		});
		await intercept(true);
		try {
			fakeNext.apply_policy = { status: 417, body: { exc_type: "ValidationError", _server_messages: JSON.stringify([busy]) } };
			await page.click(by("apply-button"));
			await expectText("apply-error", "The policy was not applied: Someone else is applying a change right now. Try again in a moment.");
			assert((await ariaDisabled()) === "false", "the same button stays usable");
			// the desk's real html2text on what a refusal may hold: escaped text, and sentences joined with <br>
			const escaped = JSON.stringify({
				message: ["Zone &quot;A &amp; B&quot; has a &lt;b&gt;bold&lt;/b&gt; name.<br>It is said on a second line."],
				as_list: 1,
				title: "The shipping policy cannot be applied",
				indicator: "red",
				raise_exception: 1,
			});
			fakeNext.apply_policy = { status: 417, body: { exc_type: "ValidationError", _server_messages: JSON.stringify([escaped]) } };
			await page.click(by("apply-button"));
			await expectText("apply-error", "It is said on a second line.");
		} finally {
			await intercept(false);
		}
		expected(...REFUSED_STATUS, ...REFUSED_STATUS);
		const lines = await page.$$eval(`${by("apply-error")} li`, (found) => found.map((line) => line.textContent));
		const want = ['Zone "A & B" has a <b>bold</b> name.', "It is said on a second line."];
		assert(JSON.stringify(lines) === JSON.stringify(want), `the real html2text, a line each: ${JSON.stringify(lines)}`);
		assert((await page.$$(".sp-wizard b")).length === 0, "no element was made from it");
		assert((await store()).dirty, "the draft is kept");
	});

	// Apply waits while the draft is being checked, even by a request for the very draft it would send (M8).
	await check("Apply sends nothing while the draft is being checked", async () => {
		await applyReady();
		await intercept(true);
		try {
			holdPreview = 2500;
			await page.click(by("step-tab-fallback"));
			await page.click(by("step-tab-review")); // the step drawn again asks again, for the same draft
			await pause(300);
			applied = 0;
			await page.click(by("apply-button"));
			await pause(300);
			assert(applied === 0, `${applied} Apply requests while the draft is being checked`);
			assert((await textOf("apply-hint")) === "Checking the draft…", `beside Apply: ${await textOf("apply-hint")}`);
			await applyReady(); // the held answer arrives
		} finally {
			await intercept(false);
		}
	});

	// An Apply that hears no usable word (finding M2): the write may have gone through, so the page reads the
	// saved policy before it says anything, and it never stays "Applying…". The answers are given in the server's
	// place, so none of them writes. The saved policy still has the version the page sent, but the write could
	// yet come (the worker may be waiting for the lock): the step says the change is not confirmed, never that
	// nothing was changed (final review M6, ruling P102).
	await check("an Apply that hears no usable word reads the saved policy first, and never stays stuck", async () => {
		const notAccepted =
			"The server did not confirm the change, and it may still be saving. Reload the page to see the policy as it is; your draft stays in this browser until then.";
		// [what, the answer, its traces, whether Frappe opens a dialog of its own for it]
		const answers = [
			["a lost connection", "abort", [/^request failed: POST .*apply_policy/, /^console error: Failed to load resource: .*apply_policy/], false],
			["a refusal without a word", { status: 417, body: { exc_type: "ValidationError" } }, REFUSED_STATUS, false],
			// Frappe calls neither callback for these two: the step settles on its own
			["a 200 Frappe cannot read", { status: 200, raw: "not json" }, [/^console error: TypeError: Cannot read properties of undefined \(reading 'responseText'\)/], false],
			[
				"Frappe's own deadlock handler",
				{ status: 500, body: { exception: "frappe.exceptions.QueryDeadlockError: (1213, 'Deadlock')", exc_type: "QueryDeadlockError" } },
				[/^HTTP 500: POST .*apply_policy/, /^console error: Failed to load resource: .*500.*apply_policy/],
				true,
			],
		];
		const said = [];
		for (const [what, answer, traces, dialog] of answers) {
			await intercept(true);
			try {
				fakeNext.apply_policy = answer;
				await page.click(by("apply-button"));
				try {
					await expectText("apply-error", notAccepted, 20000);
				} catch (error) {
					throw new Error(`${what}: ${error.message}`);
				}
			} finally {
				await intercept(false);
			}
			// Frappe's own dialog for a deadlock is closed again, once it has finished opening (it fades in)
			if (dialog) {
				await page.waitForFunction(() => Boolean(frappe.msg_dialog?.display && document.querySelector(".modal.show")), {
					timeout: 10000,
				});
				await page.evaluate(() => frappe.hide_msgprint(true));
				await page.waitForFunction(() => !document.querySelector(".modal.show, .modal-backdrop"), { timeout: 10000 });
			}
			expected(...traces);
			assert(!(await textOf("apply-error")).includes("Nothing was changed"), `${what}: never "Nothing was changed"`);
			assert((await store()).dirty, `${what}: the draft stays`);
			assert((await textOf("apply-button")) === "Apply", `${what}: not stuck in Applying`);
			assert((await ariaDisabled()) === "false", `${what}: Apply is usable again`);
			said.push(what);
		}
		// the server cannot be asked at all: the old sentence
		await intercept(true);
		try {
			fakeNext.apply_policy = "abort";
			fakeNext.wizard_state = "abort";
			await page.click(by("apply-button"));
			await expectText("apply-error", "Could not reach the server. Nothing was changed.", 20000);
		} finally {
			await intercept(false);
		}
		expected(
			/^request failed: POST .*apply_policy/,
			/^console error: Failed to load resource: .*apply_policy/,
			/^request failed: POST .*wizard_state/,
			/^console error: Failed to load resource: .*wizard_state/
		);
		assert((await ariaDisabled()) === "false", "unreachable: Apply is usable again");
		assert((await store()).dirty, "the draft is kept");
		return `${said.join(", ")}: "${notAccepted}"`;
	});

	// The write went through but its answer was lost: the saved policy is the draft that was sent (finding M2).
	await check("an Apply whose answer is lost after the write is taken as applied, and adopted", async () => {
		const before = await store();
		// the draft is written as the page would write it, then the page's own Apply loses its answer
		const written = await wizardCall("apply_policy", { draft: before.draft, expected_version: before.version });
		await intercept(true);
		try {
			fakeNext.apply_policy = "abort";
			await page.click(by("apply-button"));
			await expectText("apply-done", "Applied. The server answered late.", 20000);
		} finally {
			await intercept(false);
		}
		expected(/^request failed: POST .*apply_policy/, /^console error: Failed to load resource: .*apply_policy/);
		const after = await store();
		assert(!after.dirty && after.version === written.version, "the saved policy, adopted");
		assert(after.original.rules[2].label === "Gulf at the carrier price, again", "with the edit");
		// said as a prompt answer is said (B1): whether the policy is on, and what the last check of the draft found
		const done = await textOf("apply-done");
		assert(done.includes(STILL_THE_CARRIER), `whether it is on, with the carrier as the price source: ${done}`);
		assert(done.includes("Rules changed: Rule 3 (Gulf at the carrier price, again)"), `what changed: ${done}`);
		await pause(500);
		assert((await stored()) === null, "no draft is left in storage");
		return tallShot("wizard-answered-late");
	});

	await check("the policy is emptied and switched off again through the page's own Apply", async () => {
		await go(NEUTRAL);
		await page.evaluate((key) => window.localStorage.removeItem(key), DRAFT_KEY);
		await open();
		await page.evaluate((empty) => {
			frappe.pages["shipping-policy-wizard"].wizard.change((draft) => {
				for (const key of Object.keys(draft)) delete draft[key];
				Object.assign(draft, JSON.parse(JSON.stringify(empty)));
			});
		}, EMPTY);
		await page.click(by("step-tab-review"));
		await applyReady();
		assert((await textOf("apply-button")) === "Apply and switch off", `the button: ${await textOf("apply-button")}`);
		const diff = await textOf("review-diff");
		assert(diff.includes("Zones removed: Bahrain, GCC, Not shipped and Rest of world"), diff);
		assert(diff.includes("Active: on → off"), diff);
		await page.click(by("apply-button"));
		await expectText("apply-done", "Applied and switched off. Customers pay the carrier's price again.", 30000);
		const state = await wizardCall("wizard_state");
		assert(JSON.stringify(state.policy) === JSON.stringify(EMPTY), `erp2's policy: ${JSON.stringify(state.policy)}`);
		assert(state.version === version, `the version the run started from: ${state.version}`);
		await pause(500);
		assert((await stored()) === null, "no draft is left in storage");
		return tallShot("wizard-emptied");
	});
} finally {
	await check("erp2's policy is empty and switched off again, whatever happened above", async () => {
		const end = await ensureEmptyPolicy();
		assert(end.version === version, `the version the run started from: ${end.version}`);
		return end.applied ? "it had to be emptied here" : "";
	});
}

// ---------------------------------------------------------------- Task 16: the Rates step and the Test tab
// These checks import the Shopify export into a draft through the Rates step, edit its card, make it the price
// source, APPLY it on erp2 switched on, and run the Test tab against it. The last of them empties the policy again
// through the page's own Apply; the finally makes sure of it whatever happened. The checks after the block write
// nothing.

// Words joined as a sentence lists them, as the page does: "AC, AN and TA".
const and = (words) => (words.length < 2 ? words.join("") : `${words.slice(0, -1).join(", ")} and ${words.at(-1)}`);

// A value with the keys of every object in it sorted: the order the ERP keeps a rate card's keys in.
const sorted = (value) =>
	Array.isArray(value)
		? value.map(sorted)
		: value && typeof value === "object"
		? Object.fromEntries(Object.keys(value).sort().map((key) => [key, sorted(value[key])]))
		: value;

// The zone of the n-th profile of the card with this name (the profile is open).
async function zoneOfProfile(n, name) {
	for (const zone of await (await nth("rates-profile", n)).$$(by("rates-zone"))) {
		if ((await zone.$eval(".sp-czone-title", (title) => title.textContent)) === name) return zone;
	}
	throw new Error(`profile ${n + 1} has no zone ${name}`);
}

// Opens the n-th profile of the card, and a zone of it, when they are closed.
async function openProfile(n) {
	const toggle = await inCard("rates-profile", n, "rates-profile-toggle");
	if ((await toggle.evaluate((button) => button.getAttribute("aria-expanded"))) !== "true") await toggle.click();
}

async function openZone(n, name) {
	await openProfile(n);
	const toggle = await (await zoneOfProfile(n, name)).$(by("rates-zone-toggle"));
	if ((await toggle.evaluate((button) => button.getAttribute("aria-expanded"))) !== "true") await toggle.click();
	return zoneOfProfile(n, name);
}

// Types into the n-th field with a test id inside an element, as a person would.
async function typeInto(element, testid, text, n = 0) {
	const field = (await element.$$(by(testid)))[n];
	assert(field, `there is no ${testid} number ${n + 1} there`);
	await field.evaluate((input) => {
		input.focus();
		input.select();
	});
	await page.keyboard.press("Backspace");
	if (text) await field.type(text);
	return field;
}

// Types over what the n-th field holds, as a person does who selects it and types (no blank on the way).
async function typeOver(element, testid, text, n = 0) {
	const field = (await element.$$(by(testid)))[n];
	assert(field, `there is no ${testid} number ${n + 1} there`);
	await field.evaluate((input) => {
		input.focus();
		input.select();
	});
	await field.type(text);
}

// Puts text into a box at once, as a paste does (one input event).
async function paste(testid, text) {
	await page.$eval(
		by(testid),
		(box, text) => {
			box.value = text;
			box.dispatchEvent(new Event("input", { bubbles: true }));
		},
		text
	);
}

const expanded = (testid) => page.$eval(by(testid), (button) => button.getAttribute("aria-expanded"));

let ratesCard = null; // the card as Use these rates put it into the draft (the 760 px check draws it again)
let appliedCard = null; // the card's JSON as Apply sent it
let liveRun = null; // the answer of the Test tab's run against the policy live on erp2

try {
	await check("Rates: the price source says what each choice means, and the rate card with no card says there is none yet", async () => {
		await go(NEUTRAL);
		await page.evaluate((key) => window.localStorage.removeItem(key), DRAFT_KEY);
		await open();
		await page.click(by("step-tab-rates"));
		const options = await page.$$eval("#sp-panel-rates .sp-source-option", (labels) =>
			labels.map((label) => label.innerText.replace(/\s+/g, " ").trim())
		);
		const want = [
			"Carrier Customers pay the carrier's own price: for now the website applies the policy only with the rate card as the price source. The rules are kept, and Try an order shows what they would charge; there the rate card is used only where a fallback says Use the rate card.",
			"Rate card Customers pay from the rate card below; the carrier is not asked.",
		];
		assert(JSON.stringify(options) === JSON.stringify(want), `the two choices: ${JSON.stringify(options)}`);
		const chosen = () => page.$$eval(by("price-source"), (radios) => radios.find((radio) => radio.checked)?.value);
		assert((await chosen()) === "carrier", "the carrier, as saved");
		assert(await page.$eval(by("rates-no-card"), (note) => note.hidden), "nothing to say while the carrier is the source");
		assert(await page.$eval(by("rates-card"), (sheet) => sheet.hidden), "there is no card to show");
		assert((await expanded("rates-import-toggle")) === "true", "while there is no card, the import is open");
		await page.click(`${by("price-source")}[value="rate_card"]`);
		await expectText("rates-no-card", "There is no rate card yet. Import one below.");
		assert((await draftNow()).price_source === "rate_card", "the draft's price source");
		// the tape: in rate-card mode the carrier's controls give way to one quiet line
		const tape = await page.evaluate(() => ({
			carrier: document.querySelector('[data-block="carrier"]').hidden,
			note: document.querySelector('[data-testid="tape-card-note"]').innerText,
		}));
		assert(tape.carrier && tape.note === "The rate card prices from the items' product codes and weights.", `the tape: ${JSON.stringify(tape)}`);
		await page.click(`${by("price-source")}[value="carrier"]`);
		assert(!(await store()).dirty, "back to the carrier, the draft is the saved policy again");
		assert(await page.$eval('[data-block="carrier"]', (block) => !block.hidden), "and the carrier's controls are back");
	});

	await check("a rates file of more than 1,000,000 characters is refused in plain words before the server is asked", async () => {
		const folder = await mkdtemp(path.join(os.tmpdir(), "sp-rates-"));
		const big = path.join(folder, "too-long.csv");
		const said = "The shipping rates are longer than 1,000,000 characters, more than the import reads.";
		try {
			await writeFile(big, "x".repeat(1000001));
			imported = 0;
			await (await page.$(by("rates-file-config"))).uploadFile(big);
			await expectText("rates-import", said); // the file's own line says so at once
			await page.click(by("rates-preview"));
			await expectText("rates-import-problem", said);
			await paste("rates-paste-config", "y".repeat(1000001)); // pasted text is measured too
			await page.click(by("rates-preview"));
			await expectText("rates-import-problem", said);
			await pause(300);
			assert(imported === 0, `${imported} requests reached the server`);
			await paste("rates-paste-config", "");
			assert((await textOf("rates-import-problem")) === "", "a change of the texts takes the sentence away");
		} finally {
			await rm(folder, { recursive: true, force: true });
		}
	});

	await check("the Shopify export is read through the Rates step: what the card holds, its notes, what stops it, the countries not delivered by name", async () => {
		await (await page.$(by("rates-file-config"))).uploadFile(RATES.config);
		await (await page.$(by("rates-file-notdelivered"))).uploadFile(RATES.closed);
		const read = () =>
			page.waitForFunction(
				() =>
					[...document.querySelectorAll('[data-testid="rates-import"] .sp-file-note')].every((note) =>
						/characters read\.$/.test(note.textContent)
					),
				{ timeout: 10000 }
			);
		await read();
		// after a tab switch the files the panel holds are still the fields' state, not the browser's "No file chosen"
		await page.click(by("step-tab-zones"));
		await page.click(by("step-tab-rates"));
		await read();
		const buttons = await page.$$eval(`${by("rates-import")} .sp-file-button`, (found) => found.map((label) => label.textContent));
		assert(buttons.join("|") === "Choose another file|Choose another file", `the fields say they hold a file: ${buttons}`);
		// texts that change while the server reads them: the answer is for texts that are gone, and the panel says so
		await intercept(true);
		try {
			holdImport = 2500;
			await page.click(by("rates-preview"));
			await pause(300);
			await paste("rates-paste-notdelivered", "ISO-2,Country\nCU,Cuba\n");
			await expectText("rates-import-problem", "The files changed while they were read. Preview again.", 15000);
		} finally {
			await intercept(false);
		}
		assert(await page.$eval(by("rates-import-preview"), (box) => box.hidden), "no preview of texts that are gone");
		await paste("rates-paste-notdelivered", "");
		imported = 0;
		await page.click(by("rates-preview"));
		await expectText("rates-import-read", RATES.read, 30000);
		assert(imported === 1, `${imported} requests`);
		assert((await focusedTestid()) === "rates-import-read", `the focus goes to what was read: ${await focusedTestid()}`);
		const notes = await page.$eval(by("rates-import-notes"), (details) => ({
			open: details.open,
			said: details.querySelector("summary").textContent,
		}));
		assert(!notes.open && notes.said === `${RATES.notes} notes on the files`, `the notes, folded: ${JSON.stringify(notes)}`);
		const problems = (await page.$(by("rates-import-problems"))) ? await textOf("rates-import-problems") : "";
		const sentences = [
			...(RATES.unknown
				? [`Country code ${RATES.unknown} in zone Worldwide (Allowed Items) (Rest of the World Allowed Items) is not a country in the ERP.`]
				: []),
			...RATES.missing.map((code) => `Country code ${code} in the not-delivered list is not a country in the ERP.`),
		];
		for (const sentence of sentences) assert(problems.includes(sentence), `the problems say "${sentence}": ${problems}`);
		if (!sentences.length) assert(problems === "", `no problem: ${problems}`);
		const closed = await textOf("rates-import-not-delivered");
		assert(
			closed.startsWith(`Not delivered ${RATES.closed_count} countries:`) && closed.includes("Afghanistan") && closed.includes("Russian Federation"),
			`by name: ${closed}`
		);
		if (RATES.missing.length) {
			const missing = `Left out of the Not shipped zone, because the ERP does not know them: ${and(RATES.missing)}.`;
			assert((await textOf("rates-import-missing")) === missing, await textOf("rates-import-missing"));
		}
		assert((await textOf("rates-import-apply")) === "Use these rates", "the rates can be used");
		await expectText("rates-import-preview", "They go into your draft. Nothing is live until you apply it.");
		await page.evaluate(() => document.activeElement?.blur());
		const file = await tallShot("wizard-rates-import-preview");
		return `${RATES.what}; ${file}`;
	});

	await check("Use these rates puts the card into the draft as the ERP keeps it, and the countries not delivered into a new Not shipped zone", async () => {
		await page.click(by("rates-import-apply")); // the draft has no card: nothing to ask
		await expectText("rates-import-taken", "These rates are in your draft now. Nothing is live until you apply it in the Review step.");
		assert((await focusedTestid()) === "rates-import-taken-title", `the focus goes to what was done: ${await focusedTestid()}`);
		const draft = await draftNow();
		const card = draft.rate_card;
		ratesCard = card; // the later checks compare with it, whatever this one finds
		assert(JSON.stringify(card) === JSON.stringify(sorted(card)), "the card's keys are in the order the ERP keeps them");
		assert(Object.keys(card).join() === "currency,profiles,unit,version", Object.keys(card).join());
		const names = card.profiles.map((profile) => profile.name).join("|");
		assert(names === "Dangerous Goods Petite|Refill Service|Dangerous Goods|Rest of the World Allowed Items", names);
		const zone = draft.zones.find((entry) => entry.name === "Not delivered");
		const kept = RATES.closed_count - RATES.missing.length;
		assert(
			zone?.enabled && zone.not_shipped && zone.not_offered_message === "We do not deliver to this country." && zone.customer_line === "",
			`the zone: ${JSON.stringify(zone)}`
		);
		assert(zone.countries.length === kept && !RATES.missing.some((code) => zone.countries.includes(code)), `its countries: ${zone.countries}`);
		assert(JSON.stringify(Object.keys(zone)) === JSON.stringify(Object.keys(zoneOf("x", []))), "in the server's key order");
		await expectText("rates-import-taken", `The new Not shipped zone “Not delivered” lists ${kept} countries not delivered to.`);
		assert(draft.price_source === "carrier", "the price source is not switched by itself");
		assert((await textOf("rates-switch-source")) === "Switch the price source to Rate card", "switching it is offered");
		await expectText(
			"rates-not-used-note",
			"Nothing uses the rate card now: the price source is the carrier, and no fallback says Use the rate card."
		);
		// its work done, the import folds to its title line, with what was done under it (M8b)
		assert((await expanded("rates-import-toggle")) === "false", "the import is folded");
		const folded = await page.$eval(by("rates-import"), (panel) => ({
			body: panel.querySelector(".sp-import-body").hidden,
			taken: Boolean(panel.querySelector('[data-testid="rates-import-taken"]').offsetParent),
		}));
		assert(folded.body && folded.taken, `folded, what was done in view: ${JSON.stringify(folded)}`);
		await page.evaluate(() => document.activeElement?.blur());
		return tallShot("wizard-rates-import-folded");
	});

	await check("the card reads back lazily: a line per profile, a zone once its profile opens, then its countries, method, days and tiers", async () => {
		await expectText("rates-summary", `It holds ${RATES.read.replace("The shipping rates hold ", "")}`);
		const toggles = await page.$$eval(by("rates-profile-toggle"), (found) =>
			found.map((button) => [button.getAttribute("aria-expanded"), button.innerText.replace(/\s+/g, " ").trim()])
		);
		assert(toggles.length === 4 && toggles.every(([open]) => open === "false"), `four profiles, closed: ${JSON.stringify(toggles)}`);
		assert(toggles[0][1] === RATES.petite, `profile 1 reads: ${toggles[0][1]}`);
		// the profile with a pattern of * takes whatever the others leave, and says so in words (M8h)
		assert(toggles[3][1].startsWith("4 Rest of the World Allowed Items Every other product. "), `profile 4 reads: ${toggles[3][1]}`);
		assert((await page.$$(by("rates-zone"))).length === 0, "no zone is drawn until its profile opens");
		await openProfile(2);
		const zones = await (await nth("rates-profile", 2)).$$eval(by("rates-zone-toggle"), (found) =>
			found.map((button) => button.innerText.replace(/\s+/g, " ").trim())
		);
		assert(zones.length === 6 && zones[0] === RATES.usa, `Dangerous Goods' zones: ${JSON.stringify(zones)}`);
		assert((await page.$$(by("rates-country-chip"))).length === 0, "no country is drawn until its zone opens");
		const usa = await openZone(2, "USA");
		const seen = await usa.evaluate((zone) => ({
			chips: [...zone.querySelectorAll('[data-testid="rates-country-chip"] .sp-token-text')].map((chip) => chip.textContent),
			method: zone.querySelector('[data-testid="rates-zone-method"]').value,
			days: zone.querySelector('[data-testid="rates-zone-days"]').value,
			heads: [...zone.querySelectorAll(".sp-tiers th")].map((head) => head.textContent),
			kind: [...zone.querySelectorAll('[data-testid="rates-zone-prices"]')].find((radio) => radio.checked)?.value,
			tiers: [...zone.querySelectorAll('[data-testid="rates-tier"]')].map((row) =>
				[...row.querySelectorAll("input")].map((input) => input.value)
			),
		}));
		assert(seen.chips.join() === "United States", `its countries: ${seen.chips}`);
		assert(seen.method === "International Express (4-7 days)" && seen.days === "7", `its method and days: ${seen.method}, ${seen.days}`);
		assert(seen.heads.join("|") === "Weight, in pounds|Price, in US dollars|Remove", `the ledger's heads: ${seen.heads}`);
		assert(seen.kind === "weight" && seen.tiers.length === RATES.usa_tiers, `${seen.kind}: ${seen.tiers.length} tiers`);
		const first = JSON.stringify(seen.tiers.slice(0, 2));
		assert(first === JSON.stringify([["0.1", "0.6", "35.00"], ["0.7", "2", "60.00"]]), `the first tiers: ${first}`);
		const count = await page.$eval("#sp-panel-rates", (panel) => panel.querySelectorAll("*").length);
		assert(count < 1500, `${count} elements on the Rates step`);
		await page.evaluate(() => document.activeElement?.blur());
		const file = await tallShot("wizard-rates-card");
		return `${count} elements with one zone open; ${file}`;
	});

	await check("a tier's price is edited where it stands: kept in cents, never below zero, never left blank", async () => {
		const usa = await zoneOfProfile(2, "USA");
		const price = async () => (await draftNow()).rate_card.profiles[2].zones[0].tiers[1].price;
		const problem = () =>
			usa.$$eval(by("rates-tier"), (rows) => rows[1].querySelector(".sp-tier-price .sp-problem").textContent);
		await typeInto(usa, "rates-tier-price", "61.5", 1);
		assert((await price()) === 61.5, `61.5: ${await price()}`);
		await typeOver(usa, "rates-tier-price", "-1", 1); // over the selected price: never blank on the way
		assert((await problem()) === "Type a price of 0 or more, such as 35.", await problem());
		assert((await price()) === 61.5, `a price below zero stays out of the draft: ${await price()}`);
		await typeOver(usa, "rates-tier-price", "60,5", 1);
		assert((await problem()) === "Use a period for decimals.", await problem());
		assert((await price()) === 60, `what was a number on the way is kept, 60: ${await price()}`);
		await typeInto(usa, "rates-tier-price", "60.126", 1);
		assert((await price()) === 60.13, `kept in cents: ${await price()}`);
		assert((await problem()) === "The ERP keeps cents, so this is $60.13.", await problem());
		await typeInto(usa, "rates-tier-price", "", 1);
		assert((await price()) === null, "blank is a missing price");
		assert((await problem()) === "Type the price, such as 35.", await problem());
		const missing = 'Tier 2 of zone 1 ("USA") of profile 3 ("Dangerous Goods") has a price that has to be a number from 0 to 1000000.';
		await expectText("rates-problems", missing, 15000);
		assert(!(await textOf("rates-problems")).includes("Rate card:"), "said without the words before it");
		await typeInto(usa, "rates-tier-price", "60", 1);
		assert((await price()) === 60 && (await problem()) === "", "60 again");
		await page.evaluate(() => document.activeElement?.blur());
		await page.waitForFunction(
			(missing) => !(document.querySelector('[data-testid="rates-problems"]')?.innerText || "").includes(missing),
			{ timeout: 15000 },
			missing
		);
		assert(JSON.stringify((await draftNow()).rate_card) === JSON.stringify(ratesCard), "the card is as it was imported");
	});

	await check("a country is in one zone of a profile only, and a code the ERP does not know is shown first and taken out", async () => {
		const zone = await openZone(3, "Worldwide (Allowed Items)");
		const seen = () =>
			zone.evaluate((li) => ({
				unknown: li.querySelector('[data-testid="rates-unknown-countries"]').innerText.replace(/\s+/g, " ").trim(),
				chips: li.querySelectorAll('.sp-token-box [data-testid="rates-country-chip"]').length,
				more: (({ hidden, textContent }) => (hidden ? "" : textContent))(li.querySelector('[data-testid="rates-countries-all"]')),
			}));
		let shown = await seen();
		if (RATES.unknown) {
			assert(
				shown.unknown === `Not a country in the ERP, so the card cannot be applied with it: ${RATES.unknown} ×`,
				`first, what the ERP does not know: ${shown.unknown}`
			);
		}
		if (RATES.worldwide > 30) {
			assert(shown.chips === 30 && shown.more === `Show all ${RATES.worldwide} countries`, `the first 30: ${JSON.stringify(shown)}`);
			await (await zone.$(by("rates-countries-all"))).click();
			shown = await seen();
			assert(shown.chips === RATES.worldwide && shown.more === "Show the first 30 countries only", `all of them: ${JSON.stringify(shown)}`);
			await (await zone.$(by("rates-countries-all"))).click();
		}
		// Bahrain is in the profile's GCC zone already: refused, naming that zone
		const before = JSON.stringify((await draftNow()).rate_card);
		const refusal = () => zone.$eval(by("rates-country-problem"), (line) => line.textContent);
		const refuse = async () => {
			await typeInto(zone, "rates-country-add", "Bahrain");
			await page.keyboard.press("Enter");
		};
		await refuse();
		const said = "Bahrain is already in the zone “GCC – all 6 (incl. BH & QA)” of this profile.";
		assert((await refusal()) === said, await refusal());
		assert(JSON.stringify((await draftNow()).rate_card) === before, "and it is not added");
		// the refusal goes once another country is typed, with the next change, and when the zone is closed (M1)
		await typeInto(zone, "rates-country-add", "Ba");
		assert((await refusal()) === "", `typing takes it away: ${await refusal()}`);
		await refuse();
		const gcc = await openZone(3, "GCC – all 6 (incl. BH & QA)");
		const days = await gcc.$eval(by("rates-zone-days"), (input) => input.value);
		await typeInto(gcc, "rates-zone-days", days === "4" ? "5" : "4");
		assert((await refusal()) === "", `the next change takes it away: ${await refusal()}`);
		await typeInto(gcc, "rates-zone-days", days);
		await (await gcc.$(by("rates-zone-toggle"))).click(); // the GCC zone closed again
		await refuse();
		await (await zone.$(by("rates-zone-toggle"))).click(); // closed
		await (await zone.$(by("rates-zone-toggle"))).click(); // and opened again
		assert((await refusal()) === "", `closing the zone takes it away: ${await refusal()}`);
		assert(JSON.stringify((await draftNow()).rate_card) === before, "nothing of this is in the card");
		if (!RATES.unknown) return "the sample has no code the ERP does not know";
		await expectText("rates-problems", `Country code ${RATES.unknown} in zone Worldwide (Allowed Items)`, 15000);
		await (await zone.$(`${by("rates-unknown-countries")} ${by("rates-country-remove")}`)).click();
		const countries = (await draftNow()).rate_card.profiles[3].zones[0].countries;
		assert(countries.length === RATES.worldwide && !countries.includes(RATES.unknown), `${countries.length} countries left`);
		assert((await seen()).unknown === "", "nothing the ERP does not know is left");
		await page.waitForFunction(() => document.querySelector('[data-testid="rates-problems"]')?.hidden, { timeout: 15000 });
		await expectText("rates-summary", RATES.after);
		return `${RATES.unknown} taken out: no problem left with the card`;
	});

	await check("product codes are chips, one with a brace is refused, and a new profile goes before the one that takes every item", async () => {
		const before = JSON.stringify((await draftNow()).rate_card);
		const names = async () => (await draftNow()).rate_card.profiles.map((profile) => profile.name).join("|");
		await openProfile(0);
		const profile = await nth("rates-profile", 0);
		const chips = () => profile.$$eval(`${by("rates-pattern-chip")} .sp-token-text`, (found) => found.map((chip) => chip.textContent).join("|"));
		const refusal = () => profile.$eval(by("rates-pattern-problem"), (line) => line.textContent);
		const refuse = async () => {
			await typeInto(profile, "rates-pattern-input", "{F-CR-XYZ-*}");
			await page.keyboard.press("Enter");
			assert((await refusal()) === "Write the product code without braces, such as F-CR-XYZ-*.", await refusal());
		};
		assert((await chips()) === "F-*-PT|F-CR-PC-*", `the patterns: ${await chips()}`);
		// how a pattern is written is said once for the whole card, folded, and each pattern field points to it
		assert((await page.$$(by("rates-pattern-help"))).length === 1, "the pattern help is there once");
		assert(!(await page.$eval(by("rates-pattern-help"), (details) => details.open)), "and folded");
		const second = await inCard("rates-profile", 1, "rates-profile-toggle");
		const closed = (await second.evaluate((button) => button.getAttribute("aria-expanded"))) !== "true";
		await openProfile(1);
		const described = await page.$$eval(by("rates-pattern-input"), (inputs) =>
			inputs.map((input) => input.getAttribute("aria-describedby").split(" ").includes("sp-pattern-help"))
		);
		assert(described.length >= 2 && described.every(Boolean), `each field is described by it: ${described}`);
		assert((await page.$$(by("rates-pattern-help"))).length === 1, "two profiles open, the help is still there once");
		if (closed) await second.click(); // as it was
		await refuse();
		assert(JSON.stringify((await draftNow()).rate_card) === before, "it is not added");
		// a refusal goes once the profile is closed and opened again (M1) ...
		await clickIn("rates-profile", 0, "rates-profile-toggle");
		await clickIn("rates-profile", 0, "rates-profile-toggle");
		assert((await refusal()) === "", `closing the profile takes it away: ${await refusal()}`);
		// ... once another pattern is typed ...
		await refuse();
		await (await profile.$(by("rates-pattern-input"))).type("x");
		assert((await refusal()) === "", `typing takes it away: ${await refusal()}`);
		// ... and once a pattern is added or removed
		await refuse();
		await typeInto(profile, "rates-pattern-input", "F-TEST-*");
		await page.keyboard.press("Enter");
		assert((await chips()) === "F-*-PT|F-CR-PC-*|F-TEST-*", `added: ${await chips()}`);
		assert((await refusal()) === "", "the refusal is gone");
		await refuse();
		await (await profile.$$(by("rates-pattern-remove")))[2].click();
		assert((await chips()) === "F-*-PT|F-CR-PC-*", `and removed: ${await chips()}`);
		assert((await refusal()) === "", `a removal takes it away: ${await refusal()}`);
		await typeInto(profile, "rates-pattern-input", "");
		// a new profile goes before the one that takes every item, which has to stay last
		await page.click(by("rates-profile-add"));
		assert(
			(await names()) === "Dangerous Goods Petite|Refill Service|Dangerous Goods|New profile|Rest of the World Allowed Items",
			await names()
		);
		assert((await focusedTestid()) === "rates-profile-name", `its name has the focus: ${await focusedTestid()}`);
		await clickIn("rates-profile", 3, "rates-profile-delete"); // it has no zone: nothing to ask
		assert((await names()) === "Dangerous Goods Petite|Refill Service|Dangerous Goods|Rest of the World Allowed Items", await names());
		// moved with the keyboard, and the move is said
		await (await inCard("rates-profile", 1, "rates-profile-down")).focus();
		await page.keyboard.press("Enter");
		assert((await names()) === "Dangerous Goods Petite|Dangerous Goods|Refill Service|Rest of the World Allowed Items", await names());
		await expectText("rates-live", "Profile 2 is now profile 3 of 4.");
		assert((await focusedTestid()) === "rates-profile-down", "the focus moves with the profile");
		await (await inCard("rates-profile", 2, "rates-profile-up")).focus();
		await page.keyboard.press("Enter");
		assert(JSON.stringify((await draftNow()).rate_card) === before, "the card is as it was");
		// the profile that takes every item moved above another: the server's sentence says why it has to be last
		await clickIn("rates-profile", 3, "rates-profile-up");
		assert((await names()) === "Dangerous Goods Petite|Refill Service|Rest of the World Allowed Items|Dangerous Goods", await names());
		const last =
			'Profile 3 ("Rest of the World Allowed Items") matches every item (a pattern of *) but is not the last profile, so the profiles after it would never be used.';
		await expectText("rates-problems", last, 15000);
		await clickIn("rates-profile", 2, "rates-profile-down");
		assert(JSON.stringify((await draftNow()).rate_card) === before, "back last, the card is as it was");
		await page.waitForFunction(() => document.querySelector('[data-testid="rates-problems"]')?.hidden, { timeout: 15000 });
		return "the profile with * moved up: the server's sentence, then gone once it is last again";
	});

	await check("a zone is added to a profile, priced by weight a tier at a time, and deleted after a question; days are whole numbers", async () => {
		const before = JSON.stringify((await draftNow()).rate_card);
		const zonesNow = async () => (await draftNow()).rate_card.profiles[1].zones;
		await openProfile(1);
		await (await (await nth("rates-profile", 1)).$(by("rates-zone-add"))).click();
		assert((await focusedTestid()) === "rates-zone-name", `the new zone's name has the focus: ${await focusedTestid()}`);
		let zones = await zonesNow();
		const fresh = { countries: [], days: null, method: "", name: "New zone", tiers: [{ from: null, price: null, to: null }] };
		assert(zones.length === 4 && JSON.stringify(zones[3]) === JSON.stringify(fresh), `a new zone: ${JSON.stringify(zones[3])}`);
		const zone = await zoneOfProfile(1, "New zone");
		const said = (selector) => zone.$eval(selector, (found) => found.textContent);
		assert((await said(".sp-tier-price .sp-problem")) === "Type the price, such as 35.", "one price at any weight, not typed yet");
		await typeInto(zone, "rates-tier-price", "25");
		let tiers = (await zonesNow())[3].tiers;
		assert(JSON.stringify(tiers) === JSON.stringify([{ from: null, price: 25, to: null }]), `$25 at any weight: ${JSON.stringify(tiers)}`);
		// by weight: the one price becomes the first tier, from 0 up to a weight still to be typed, its price kept (M10c)
		await (await zone.$(`${by("rates-zone-prices")}[value="weight"]`)).click();
		tiers = (await zonesNow())[3].tiers;
		assert(JSON.stringify(tiers) === JSON.stringify([{ from: 0, price: 25, to: null }]), `by weight, the price kept: ${JSON.stringify(tiers)}`);
		const kept = await zone.$eval(by("rates-tier-price"), (input) => input.value);
		assert(kept === "25.00", `the field shows it: ${kept}`);
		await typeInto(zone, "rates-tier-to", "2");
		await typeInto(zone, "rates-tier-price", "40");
		await (await zone.$(by("rates-tier-add"))).click();
		tiers = (await zonesNow())[3].tiers;
		const two = [{ from: 0, price: 40, to: 2 }, { from: 2.01, price: null, to: null }];
		assert(JSON.stringify(tiers) === JSON.stringify(two), `a tier from a hundredth of a pound on: ${JSON.stringify(tiers)}`);
		assert((await focusedTestid()) === "rates-tier-to", `its upper weight has the focus: ${await focusedTestid()}`);
		// one price waits for the other tiers to go, and says so only as the hint of that choice (M8g)
		const flat = await zone.evaluate((li) => {
			const radio = li.querySelector('[data-testid="rates-zone-prices"][value="flat"]');
			const hint = document.getElementById(radio.getAttribute("aria-describedby") || "");
			const box = hint?.getBoundingClientRect();
			return { disabled: radio.disabled, title: radio.closest("label").title, hint: hint?.textContent, seen: box ? box.width > 1 || box.height > 1 : null };
		});
		const why = "Remove the other weight tiers first.";
		assert(flat.disabled && flat.title === why && flat.hint === why && flat.seen === false, `the hint of the choice: ${JSON.stringify(flat)}`);
		await (await zone.$$(by("rates-tier-remove")))[1].click();
		assert((await zonesNow())[3].tiers.length === 1, "the second tier is removed");
		const back = await zone.evaluate((li) => {
			const radio = li.querySelector('[data-testid="rates-zone-prices"][value="flat"]');
			return { disabled: radio.disabled, title: radio.closest("label").hasAttribute("title"), described: radio.hasAttribute("aria-describedby") };
		});
		assert(!back.disabled && !back.title && !back.described, `one tier left, nothing to say: ${JSON.stringify(back)}`);
		// days: a whole number from 0 to 60, or none
		await typeInto(zone, "rates-zone-days", "abc");
		assert((await said(".sp-czone-method .sp-problem")) === "Type a whole number of days from 0 to 60, or leave it empty.", "days refused");
		assert((await zonesNow())[3].days === null, "and kept out of the draft");
		await typeInto(zone, "rates-zone-days", "5");
		assert((await zonesNow())[3].days === 5, "5 days");
		// it has a price now: the page asks before it deletes it
		await (await zone.$(by("rates-zone-delete"))).click();
		await answerDialog(true);
		assert(JSON.stringify((await draftNow()).rate_card) === before, "the card is as it was");
		assert((await focusedTestid()) === "rates-zone-toggle", `the focus goes to the zone in its place: ${await focusedTestid()}`);
	});

	await check("with the rate card as the price source the tape prices a USA order from the card, and the carrier's controls give way", async () => {
		await page.click(by("rates-switch-source"));
		assert((await draftNow()).price_source === "rate_card", "the price source is the rate card");
		await expectText("rates-import-taken", "The price source is the rate card.");
		assert((await focusedTestid()) === "price-source", `the focus is on the switch: ${await focusedTestid()}`);
		assert(await page.$eval(by("rates-not-used-note"), (note) => note.hidden), "the card is used now: no note");
		assert(await page.$eval('[data-block="carrier"]', (block) => block.hidden && !block.offsetParent), "the carrier's controls are hidden");
		await pickCountry("United States");
		await addTapeItem(ITEM);
		await addTapeItem(WATCH);
		await expectText("tape-total", "$100.00", 15000);
		const lines = await page.$$eval(`${by("tape-lines")} .sp-line`, (found) =>
			found.map((line) => line.innerText.replace(/\s+/g, " ").trim())
		);
		const want = [
			"Dangerous Goods, USA: 0.772 lb -> $60.00 (weight estimated) $60.00",
			"Rest of the World Allowed Items, Worldwide (Allowed Items): 0.772 lb -> $40.00 (weight estimated) $40.00",
		];
		assert(JSON.stringify(lines) === JSON.stringify(want), `the card's lines: ${JSON.stringify(lines)}`);
		await expectText("tape-lines", "International Express (4-7 days)");
		assert((await textOf("tape-total-label")) === "Customer would pay", "the draft is still off");
		await expectText("tape-lines", OFF_BY_CARD); // with the rate card, the note of a policy that is off is as it was
		return "$100.00: Dangerous Goods $60.00 and the catch-all $40.00, each unit 0.35 kg (0.772 lb): erp2 has no item weights";
	});

	await check("the Rules step says that rules are not used while the price source is the rate card", async () => {
		await page.click(by("step-tab-rules"));
		await expectText("rules-not-used-note", "Rules are not used while the price source is the rate card.");
		await page.click(by("step-tab-rates"));
	});

	await check("screenshots of the Rates step with the card, light and dark", async () => {
		await openZone(2, "USA");
		await page.evaluate(() => document.activeElement?.blur());
		const files = [await tallShot("wizard-rates-light")];
		await theme("dark");
		files.push(await tallShot("wizard-rates-dark"));
		const figure = await (await zoneOfProfile(2, "USA")).$eval(".sp-czone-figure", (found) => getComputedStyle(found).color);
		const free = await (await zoneOfProfile(2, "Bahrain (Local)")).$eval(".sp-czone-figure", (found) => getComputedStyle(found).color);
		await theme("light");
		assert(figure === "rgb(216, 178, 89)" && free === "rgb(116, 201, 160)", `brass and sage in the dark: ${figure}, ${free}`);
		return files.join(", ");
	});

	await check("Review: the card's own lines, then Apply writes the card, the price source and the Not delivered zone, switched on", async () => {
		await page.click(by("step-tab-review"));
		await applyReady();
		const summary = await textOf("review-summary");
		const first = "Prices come from the rate card: the carrier is not asked, and the rules and fallbacks below are not used.";
		assert(summary.startsWith(first), `the summary: ${summary.slice(0, 160)}`);
		const profiles = await textsOf("review-card-profile");
		assert(profiles.length === 4 && profiles[2].startsWith("3 Dangerous Goods Product codes F-*."), `the card's profiles: ${profiles[2]}`);
		assert(profiles[2].includes(RATES.usa_review), `a zone in words: ${profiles[2]}`);
		const card = await textOf("review-card");
		assert(
			card.includes("Delivery is not offered to Afghanistan") && card.includes("Russian Federation") && card.endsWith("their zone is not shipped to."),
			`the countries not delivered to: ${card.slice(-240)}`
		);
		const diff = await textOf("review-diff");
		for (const said of [
			"Zones added: Not delivered",
			"Rate card profiles added: Dangerous Goods, Dangerous Goods Petite, Refill Service and Rest of the World Allowed Items",
			"Price source: carrier → rate card",
		]) {
			assert(diff.includes(said), `what will change says "${said}": ${diff}`);
		}
		await page.click(by("policy-active"));
		await expectText("apply-button", "Apply and switch on");
		await applyReady();
		// with the rate card as the price source the website applies the policy: the words are as they were (P100)
		await expectText("policy-active-state", "Switched on: customers pay what this policy says");
		const why = await textOf("apply-hint");
		assert(why === "Customers pay what this policy says as soon as it is applied.", `beside Apply: ${why}`);
		await expectText("tape-total-label", "Customer pays");
		assert(!(await textOf("tape-lines")).includes("The website still charges"), "no note that the website charges the carrier");
		appliedCard = JSON.stringify((await draftNow()).rate_card);
		await page.click(by("apply-button"));
		await expectText("apply-done", "Applied and switched on. Customers see the new prices now.", 30000);
		const state = await wizardCall("wizard_state");
		assert(state.policy.active && state.policy.price_source === "rate_card", "the server agrees: on, from the rate card");
		assert(JSON.stringify(state.policy.rate_card) === appliedCard, "the saved card is the draft's, key order and all");
		assert(state.policy.zones.map((zone) => zone.name).join() === "Not delivered", "with the zone Not delivered");
	});

	await check("right after Apply, before any reload, the Rates step no longer says the rates are in the draft (I1)", async () => {
		await page.click(by("step-tab-rates"));
		const seen = await page.evaluate(() => {
			const panel = document.querySelector('[data-testid="rates-import"]');
			return {
				taken: panel.querySelector('[data-testid="rates-import-taken"]').childElementCount,
				switch: Boolean(document.querySelector('[data-testid="rates-switch-source"]')),
				said: panel.innerText.replace(/\s+/g, " ").trim(),
			};
		});
		assert(seen.taken === 0 && !seen.switch, `nothing of Use these rates is said: ${seen.said}`);
		assert(!seen.said.includes("in your draft now") && !seen.said.includes("Nothing is live"), `no word of a draft: ${seen.said}`);
		assert((await expanded("rates-import-toggle")) === "false", "the draft holds the applied card: the import stays a line");
		await expectText("rates-summary", RATES.after);
		assert(!(await store()).dirty, "the draft is the policy as applied");
		await page.evaluate(() => document.activeElement?.blur());
		return tallShot("wizard-rates-after-apply");
	});

	await check("after a reload the card is the one applied, the draft is clean, and the import is one line", async () => {
		await open();
		await page.click(by("step-tab-rates"));
		const saved = await store();
		assert(!saved.dirty, "nothing to apply");
		assert(JSON.stringify(saved.original.rate_card) === appliedCard, "the card as it was applied");
		assert(saved.original.price_source === "rate_card" && saved.original.active, "the price source, on");
		await expectText("rates-summary", RATES.after);
		assert((await expanded("rates-import-toggle")) === "false", "with a card, the import is one line");
		assert(await page.$eval(by("rates-not-used-note"), (note) => note.hidden), "the card is the price source");
		await pause(1500); // the server's word on the card
		assert(await page.$eval(by("rates-problems"), (box) => box.hidden), "and it has no problem");
	});

	await check("Test: three checks in their order, each with its status in words, and the verdict on the policy live on erp2", async () => {
		await page.click(by("step-tab-test"));
		const intro = await page.$eval("#sp-panel-test .sp-step-intro", (line) => line.textContent);
		assert(intro === "These checks use the saved policy and the live site, not your draft.", intro);
		// beside checks of what is live, the tape says that it prices the draft (M8a, ruling P93)
		const tapeTitle = () => page.$eval("#sp-tape-title", (title) => title.textContent);
		assert((await tapeTitle()) === "Try an order with your draft", `the tape beside the Test tab: ${await tapeTitle()}`);
		tested = 0;
		await intercept(true);
		try {
			holdTest = 2500; // a run takes a while: the spinner shows, and a second press sends nothing
			await page.click(by("test-run"));
			await pause(400);
			const running = await page.evaluate(() => {
				const verdict = document.querySelector('[data-testid="test-verdict"]');
				return {
					disabled: document.querySelector('[data-testid="test-run"]').getAttribute("aria-disabled"),
					spinner: !document.querySelector(".sp-spinner").hidden,
					label: document.querySelector(".sp-spinner").getAttribute("aria-label"),
					turning: getComputedStyle(document.querySelector(".sp-spinner")).animationName,
					status: document.querySelector(".sp-test-status").textContent,
					// the verdict's live region is there before its sentence comes, empty and not hidden (M2)
					verdict: { live: verdict?.getAttribute("aria-live"), said: verdict?.textContent, hidden: Boolean(verdict?.closest("[hidden]")) },
				};
			});
			assert(running.disabled === "true" && running.spinner && running.label === "Running the checks", JSON.stringify(running));
			assert(running.turning !== "none", `the desk's spinner turns: ${running.turning}`);
			assert(running.status === "Running the checks. A run takes 15 to 45 seconds, sometimes more.", running.status);
			assert(
				running.verdict.live === "polite" && running.verdict.said === "" && !running.verdict.hidden,
				`the verdict waits in its live region: ${JSON.stringify(running.verdict)}`
			);
			await page.click(by("test-run"));
			await page.waitForFunction(() => document.querySelector('[data-testid="test-verdict"]')?.textContent.trim(), { timeout: 90000 });
		} finally {
			await intercept(false);
		}
		assert(tested === 1, `${tested} requests for two presses`);
		const cards = await page.$$eval(".sp-checks > li", (found) =>
			found.map((card) => ({
				testid: card.dataset.testid,
				status: card.querySelector('[data-testid="test-check-status"]').textContent,
				name: card.querySelector(".sp-check-name").textContent,
				time: card.querySelector(".sp-check-time")?.textContent || "",
				summary: card.querySelector('[data-testid="test-check-summary"]').textContent,
				folded: card.querySelector("details")?.open === false,
			}))
		);
		assert(cards.map((card) => card.testid).join() === "test-check-erp-api,test-check-carrier,test-check-website", "in their order");
		assert(cards.map((card) => card.name).join("|") === "ERP price API|Carrier API|Website", "with their names");
		const [erp, carrier, website] = cards;
		assert(erp.status === "Passed" && erp.summary.includes("policy ON, price source rate card, 1 offer, $100.00."), JSON.stringify(erp));
		assert(/^\d[\d,]* ms$/.test(erp.time), `its time: ${erp.time}`);
		assert(carrier.status === "Passed" && carrier.summary.startsWith("The carrier returned"), JSON.stringify(carrier));
		assert(website.status === "Not asked" && website.summary.startsWith("The website was not asked."), JSON.stringify(website));
		assert(website.time === "", `a check that was not asked shows no time (M8e): ${website.time}`);
		assert(cards.every((card) => card.folded), "each check's details are folded");
		const verdict = await textOf("test-verdict");
		const want =
			"The ERP price API charges $100.00 from the rate card; the website was not asked because its address is not usable (see the third check).";
		assert(verdict === want, `the verdict: ${verdict}`);
		assert((await page.$eval(by("test-verdict"), (found) => found.getAttribute("aria-live"))) === "polite", "read out when it comes");
		// a check's details: the cleaned answer, as text
		await page.click(`${by("test-check-erp-api")} summary`);
		const detail = await page.$eval(`${by("test-check-erp-api")} pre`, (pre) => pre.textContent);
		assert(detail.includes('"policy_active": true') && detail.includes('"price_source": "rate_card"'), detail.slice(0, 200));
		// the details' toggle event comes a moment after the click
		await page.waitForFunction(
			() => document.querySelector('[data-testid="test-check-erp-api"] summary')?.textContent === "Hide details",
			{ timeout: 5000 }
		);
		// the sample the server used is there to be changed
		const sample = await page.evaluate(() => ({
			country: document.querySelector('[data-testid="test-country"]').value,
			city: document.querySelector('[data-testid="test-city"]').value,
			items: [...document.querySelectorAll('[data-testid="test-item"]')].map((line) => line.querySelector(".sp-item-name").title),
		}));
		assert(sample.country === "United States" && sample.city === "New York" && sample.items.join() === "10005020,10000097", JSON.stringify(sample));
		assert(!(await store()).dirty, "the run never touches the draft");
		liveRun = await page.evaluate(() => frappe.pages["shipping-policy-wizard"].wizard.ui.test.result);
		const files = [await tallShot("wizard-test-result")];
		await page.click(`${by("test-check-erp-api")} summary`);
		await page.evaluate(() => document.activeElement?.blur());
		files.push(await tallShot("wizard-test-light"));
		await theme("dark");
		files.push(await tallShot("wizard-test-dark"));
		await theme("light");
		// beside any other step the tape has its own title again
		await page.click(by("step-tab-rates"));
		assert((await tapeTitle()) === "Try an order", `the tape beside the Rates step: ${await tapeTitle()}`);
		return files.join(", ");
	});

	await check("a refused sample order is said in plain words, and a website that limits the route reads Rate limited, not as a failure", async () => {
		const refusal = JSON.stringify({ message: "The sample order can hold at most 10 items.", title: "Message", indicator: "red", raise_exception: 1 });
		await page.click(by("step-tab-test"));
		await intercept(true);
		try {
			fakeNext.test_integration = { status: 417, body: { exc_type: "ValidationError", _server_messages: JSON.stringify([refusal]) } };
			await page.click(by("test-run"));
			await expectText("test-error", "The checks could not run: The sample order can hold at most 10 items.");
			await pause(300); // the browser's own lines about the refused request
			expected(
				/^HTTP 417: POST .*shipping_policy_wizard\.test_integration/,
				/^console error: Failed to load resource: .*417.*shipping_policy_wizard\.test_integration/
			);
			assert((await page.$eval(by("test-error"), (box) => box.getAttribute("role"))) === "alert", "an alert");
			assert(await page.$eval(".sp-test-results", (box) => box.hidden), "a run clears the result before it");
			// the website check of a run, answered in the server's place: the site limits its route
			const check = (name, ok, status, summary, detail = {}) => ({ name, ok, status, ms: 120, summary, detail });
			const checks = [
				check("erp_price_api", true, 200, "The ERP answered in 120 ms: policy ON, price source rate card, 1 offer, $100.00."),
				check("carrier", true, null, "The carrier returned 1 rate: Aramex BH International PPX $3.68."),
				check(
					"website",
					false,
					429,
					"The website answered HTTP 429: it allows this route 10 requests a minute and is limiting this one. That is not a failure of the integration. Run the check again in a minute."
				),
			];
			const verdict =
				"The ERP price API charges $100.00 from the rate card, but the website limits this check to 10 requests a minute, so run it again in a minute.";
			const order = liveRun?.order || { country_code: "US", country: "United States", city: "New York", items: [], note: "" };
			fakeNext.test_integration = { status: 200, body: { message: { checks, verdict, order } } };
			await page.click(by("test-run"));
			await expectText("test-check-website", "Rate limited");
		} finally {
			await intercept(false);
		}
		assert((await textOf("test-error")) === "", "the refusal is gone");
		const look = await page.evaluate(() => {
			const card = document.querySelector('[data-testid="test-check-website"]');
			const status = card.querySelector('[data-testid="test-check-status"]');
			return {
				status: status.textContent,
				colour: getComputedStyle(status).color,
				quiet: getComputedStyle(document.querySelector("#sp-panel-test .sp-step-intro")).color,
				edge: getComputedStyle(card).borderLeftColor,
				said: card.innerText.replace(/\s+/g, " "),
			};
		});
		assert(look.status === "Rate limited" && look.colour === look.quiet, `neutral, in words: ${JSON.stringify(look)}`);
		assert(look.edge !== "rgb(168, 71, 58)", `not marked as a failure: ${look.edge}`);
		assert(look.said.includes("Rate limited: the site allows 10 asks a minute."), look.said);
		await expectText("test-verdict", "so run it again in a minute.");
	});

	await check("the policy is emptied and switched off again through the page's own Apply", async () => {
		await go(NEUTRAL);
		await page.evaluate((key) => window.localStorage.removeItem(key), DRAFT_KEY);
		await open();
		await page.evaluate((empty) => {
			frappe.pages["shipping-policy-wizard"].wizard.change((draft) => {
				for (const key of Object.keys(draft)) delete draft[key];
				Object.assign(draft, JSON.parse(JSON.stringify(empty)));
			});
		}, EMPTY);
		await page.click(by("step-tab-review"));
		await applyReady();
		const diff = await textOf("review-diff");
		for (const said of ["Zones removed: Not delivered", "Rate card profiles removed:", "Price source: rate card → carrier", "Active: on → off"]) {
			assert(diff.includes(said), `what will change says "${said}": ${diff}`);
		}
		await page.click(by("apply-button"));
		await expectText("apply-done", "Applied and switched off. Customers pay the carrier's price again.", 30000);
		const state = await wizardCall("wizard_state");
		assert(JSON.stringify(state.policy) === JSON.stringify(EMPTY), `erp2's policy: ${JSON.stringify(state.policy)}`);
		assert(state.version === version, `the version the run started from: ${state.version}`);
		await pause(500);
		assert((await stored()) === null, "no draft is left in storage");
	});
} finally {
	await check("erp2's policy is empty and switched off again after the Rates checks, whatever happened above", async () => {
		const end = await ensureEmptyPolicy();
		assert(end.version === version, `the version the run started from: ${end.version}`);
		return end.applied ? "it had to be emptied here" : "";
	});
}

// A card pasted as text, with a hostile profile name and a zone name the server's sentences quote (decision 4:
// what import_rate_card returns is plain text, and the page shows it as text everywhere).
const HOSTILE_RATES = [
	"Profile,Zone (recommended),Country count,Countries (ISO-2 - Name),Shipping method,Weight from (lb),Weight to (lb),Price (USD)",
	`${HOSTILE},${HOSTILE_ZONE},1,AE - United Arab Emirates,Express (2 days),,,25`,
	"Rest of the World Allowed Items,Worldwide,1,US - United States,Express (5 days),,,40",
].join("\n");

// The names of countries as the page has them from the ERP.
const countryNames = (codes) =>
	page.evaluate((codes) => {
		const names = new Map(frappe.pages["shipping-policy-wizard"].wizard.state.countries.map((country) => [country.code, country.name]));
		return codes.map((code) => names.get(code) || code);
	}, codes);

await check("a zone named Not delivered that is not Not shipped takes the countries, and the panel says customers there are still priced (M3)", async () => {
	const draft = { ...original, zones: [zoneOf("Not delivered", ["FR"])] };
	await openWithStored({ version, draft, step: "rates" });
	await paste("rates-paste-config", HOSTILE_RATES);
	await paste("rates-paste-notdelivered", "ISO-2,Country\nCU,Cuba\nKP,North Korea\n");
	await page.click(by("rates-preview"));
	await expectText("rates-import-read", "The shipping rates hold 2 profiles, 2 zones, 2 weight tiers and 2 countries.", 30000);
	// names that hold a comma are listed apart by semicolons (M8c)
	const names = await countryNames(["CU", "KP"]);
	await expectText("rates-import-not-delivered", `Not delivered 2 countries: ${names.join("; ")}.`);
	await page.click(by("rates-import-apply"));
	await expectText("rates-import-taken", "The zone “Not delivered” now lists 2 countries not delivered to.");
	await expectText(
		"rates-import-priced",
		"It is not marked Not shipped, so customers there are priced as in any other zone: turn on Not shipped in the Zones step."
	);
	const zone = (await draftNow()).zones[0];
	assert(zone.countries.join() === "CU,KP" && !zone.not_shipped && zone.enabled, `its countries replaced, nothing else: ${JSON.stringify(zone)}`);
	return names.join("; ");
});

await check("the countries not delivered leave out one already in another zone that is on, and say which", async () => {
	await openWithStored({ version, draft: { ...original, zones: [zoneOf("Closed", ["RU"])] }, step: "rates" });
	await paste("rates-paste-config", HOSTILE_RATES);
	await paste("rates-paste-notdelivered", "ISO-2,Country\nRU,Russia\nCU,Cuba\n");
	await page.click(by("rates-preview"));
	await expectText("rates-import-read", "The shipping rates hold 2 profiles, 2 zones, 2 weight tiers and 2 countries.", 30000);
	await page.click(by("rates-import-apply"));
	await expectText("rates-import-conflict", "Russian Federation is already in the zone “Closed”, so it was not added to “Not delivered”.");
	const zones = (await draftNow()).zones;
	assert(zones[0].countries.join() === "RU", "it stays where it is");
	const made = zones.find((zone) => zone.name === "Not delivered");
	assert(made?.countries.join() === "CU" && made.not_shipped, `the zone made: ${JSON.stringify(made)}`);
	await expectText("rates-import-taken", "The new Not shipped zone “Not delivered” lists 1 country not delivered to.");
	assert(!(await page.$(by("rates-import-priced"))), "a zone made Not shipped needs no word about prices");
});

await check("a card pasted with a hostile profile name and a zone named Gulf & <b>Co</b> reads as typed: the preview, the card and the Review", async () => {
	// read again: the draft has a card now, so the page asks before it replaces it. Use these rates folded the
	// import to its title line, so it is opened first
	assert((await expanded("rates-import-toggle")) === "false", "the import folded after Use these rates");
	await page.click(by("rates-import-toggle"));
	await page.click(by("rates-preview"));
	await expectText("rates-import-preview", "They replace the rate card in your draft, which has 2 profiles.", 30000);
	await page.click(`${by("rates-import-notes")} summary`);
	const preview = await textOf("rates-import-preview");
	for (const said of [
		`Profile "${HOSTILE}" is not one of the known profiles, so it has no product code patterns yet. It will not be used until patterns are added.`,
		`Profile 1 ("${HOSTILE}") has no product code patterns, so it will never be used.`,
	]) {
		assert(preview.includes(said), `the preview says "${said}": ${preview}`);
	}
	await page.click(by("rates-import-apply"));
	await answerDialog(false);
	assert((await focusedTestid()) === "rates-import-apply", `No keeps the card, and gives the focus back: ${await focusedTestid()}`);
	await page.click(by("rates-import-apply"));
	await answerDialog(true);
	await expectText("rates-import-taken", "These rates are in your draft now.");
	await openProfile(0);
	const profile = await nth("rates-profile", 0);
	assert((await profile.$eval(".sp-profile-title", (title) => title.textContent)) === HOSTILE, "the profile's name, as typed");
	assert((await profile.$eval(by("rates-profile-name"), (input) => input.value)) === HOSTILE, "in its field too");
	assert((await profile.$eval(".sp-czone-title", (title) => title.textContent)) === HOSTILE_ZONE, "and the zone's");
	await page.click(`${by("price-source")}[value="rate_card"]`);
	await page.click(by("step-tab-review"));
	await expectText("review-problems", `Rate card: Profile 1 ("${HOSTILE}") has no product code patterns, so it will never be used.`, 15000);
	const card = await textOf("review-card");
	assert(card.includes(`1 ${HOSTILE} No product codes yet, so it takes no item.`), `the Review's card: ${card}`);
	assert(card.includes(`${HOSTILE_ZONE}: United Arab Emirates. $25.00 at any weight, Express (2 days).`), `the zone in words: ${card}`);
	assert((await page.$$(".sp-wizard img, .sp-wizard b")).length === 0, "no element was made from a name");
	assert(!trouble.some((line) => line.startsWith("dialog")), "no dialog of the browser's opened");
	const file = await tallShot("wizard-rates-hostile");
	await page.evaluate((key) => window.localStorage.removeItem(key), DRAFT_KEY);
	return file;
});

await check("right after the draft is discarded, the Rates step says nothing of the rates that were in it, and the import is open (I1)", async () => {
	// the draft restored above, with the card Use these rates put into it, is still on the page
	await page.click(by("step-tab-rates"));
	await expectText("rates-import-taken", "These rates are in your draft now.");
	await page.click(by("step-tab-review"));
	await page.click(by("draft-discard"));
	await answerDialog(true);
	await page.waitForFunction((selector) => document.querySelector(selector)?.hidden, {}, by("draft-banner"));
	await page.click(by("step-tab-rates"));
	const seen = await page.evaluate(() => ({
		taken: document.querySelector('[data-testid="rates-import-taken"]').childElementCount,
		switch: Boolean(document.querySelector('[data-testid="rates-switch-source"]')),
		card: document.querySelector('[data-testid="rates-card"]').hidden,
	}));
	assert(seen.taken === 0 && !seen.switch, `nothing of Use these rates is said: ${JSON.stringify(seen)}`);
	assert(seen.card, "the saved policy has no card to show");
	assert((await expanded("rates-import-toggle")) === "true", "no card left: the import is open again");
	assert(!(await store()).dirty, "the draft is the saved policy");
	await page.evaluate(() => document.activeElement?.blur());
	return tallShot("wizard-rates-after-discard");
});

await check("a name of 300 characters from the file is shown cut to 140, the whole of it in a title, and kept whole in the draft (M7)", async () => {
	const long = Array(60).fill("Gulf").join(" "); // 299 characters
	const zone = Array(40).fill("Coast").join(" "); // 239 characters
	const cut = (name) => `${name.slice(0, 139)}…`;
	const rates = [HOSTILE_RATES.split("\n")[0], `${long},${zone},1,AE - United Arab Emirates,Express (2 days),,,25`].join("\n");
	await go(NEUTRAL);
	await page.evaluate((key) => window.localStorage.removeItem(key), DRAFT_KEY);
	await open();
	await page.click(by("step-tab-rates"));
	await paste("rates-paste-config", rates);
	await page.click(by("rates-preview"));
	await expectText("rates-import-read", "The shipping rates hold 1 profile", 30000);
	await page.click(by("rates-import-apply"));
	await expectText("rates-import-taken", "These rates are in your draft now.");
	assert((await draftNow()).rate_card.profiles[0].name === long, "the draft keeps the name whole");
	await openProfile(0);
	const profile = await nth("rates-profile", 0);
	const line = await profile.evaluate((li) => {
		const title = li.querySelector(".sp-profile-title");
		const zone = li.querySelector(".sp-czone-title");
		return { text: title.textContent, title: title.title, label: li.getAttribute("aria-label"), zone: zone.textContent, zone_title: zone.title };
	});
	assert(line.text === cut(long) && line.text.length === 140, `the profile's line: ${line.text.length} characters`);
	assert(line.title === long, "the whole name is its title");
	assert(line.label === `Profile 1: ${cut(long)}`, `its name for assistive technology: ${line.label.length} characters`);
	assert(line.zone === cut(zone) && line.zone_title === zone, `the zone's line: ${line.zone.length} characters`);
	assert((await profile.$eval(by("rates-profile-name"), (input) => input.value)) === long, "the name field holds it whole, to be fixed");
	await page.click(`${by("price-source")}[value="rate_card"]`);
	await page.click(by("step-tab-review"));
	await page.waitForFunction(() => document.querySelector('[data-testid="review-card-profile"]'), { timeout: 15000 });
	const review = await page.$eval(by("review-card-profile"), (article) => {
		const name = article.querySelector("h4 span:last-child");
		return { text: name.textContent, title: name.title, zone: article.querySelector("li").textContent };
	});
	assert(review.text === cut(long) && review.title === long, `the Review's card: ${review.text.length} characters`);
	assert(review.zone.startsWith(`${cut(zone)}: United Arab Emirates.`), `its zone: ${review.zone.slice(0, 160)}`);
	await expectText("review-diff", "Rate card profiles added:", 15000);
	const added = await page.$$eval(`${by("review-diff")} .sp-change`, (lines) =>
		lines
			.map((line) => ({ text: line.innerText.replace(/\s+/g, " ").trim(), title: line.querySelector(".sp-change-text").title }))
			.find((line) => line.text.startsWith("Rate card profiles added"))
	);
	assert(added?.text === `Rate card profiles added: ${cut(long)}` && added.title === long, `what will change: ${added?.text.length} characters`);
	await page.evaluate((key) => window.localStorage.removeItem(key), DRAFT_KEY);
	return "the profile, its zone, the Review's card and what will change: 140 characters each, whole in the title";
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

await check("at 760 px the fallbacks and the Review fit, their lines wrap and nothing scrolls sideways", async () => {
	const draft = { ...original, zones: EXAMPLE.zones, item_sets: builtSets(), rules: EXAMPLE.rules };
	await openWithStored({ version, draft, step: "fallback" });
	const fits = async (selector) =>
		page.evaluate((selector) => {
			const root = document.documentElement;
			const panel = document.querySelector(".sp-step").getBoundingClientRect();
			const parts = [...document.querySelectorAll(selector)];
			return {
				page: root.scrollWidth <= root.clientWidth,
				parts: parts.length > 0 && parts.every((part) => part.getBoundingClientRect().right <= panel.right + 1),
				inside: parts.every((part) => part.scrollWidth <= part.clientWidth + 1),
			};
		}, selector);
	const fallback = await fits('[data-testid="fallback-zone"], [data-testid="fallback-policy-default"], [data-testid="not-covered"]');
	const files = [await tallShot("wizard-fallback-760")];
	await page.click(by("step-tab-review"));
	await applyReady();
	const review = await fits(".sp-review-part, .sp-golive, [data-testid='review-zone']");
	files.push(await tallShot("wizard-review-760"));
	// the restored draft was not changed here, so nothing writes it back: the browser's storage is left empty
	await page.evaluate((key) => window.localStorage.removeItem(key), DRAFT_KEY);
	await pause(500);
	assert((await stored()) === null, "nothing is left in the browser's storage");
	assert(Object.values(fallback).every(Boolean), `fallbacks: ${JSON.stringify(fallback)}`);
	assert(Object.values(review).every(Boolean), `review: ${JSON.stringify(review)}`);
	return files.join(", ");
});

await check("at 760 px the Rates step and the Test tab fit, their lines wrap and nothing scrolls sideways", async () => {
	assert(ratesCard, "a card was imported earlier in the run");
	const draft = { ...original, price_source: "rate_card", rate_card: ratesCard };
	await openWithStored({ version, draft, step: "rates" });
	await openZone(2, "USA");
	const fits = async (selector) =>
		page.evaluate((selector) => {
			const root = document.documentElement;
			const panel = document.querySelector(".sp-step").getBoundingClientRect();
			const parts = [...document.querySelectorAll(selector)];
			return {
				page: root.scrollWidth <= root.clientWidth,
				parts: parts.length > 0 && parts.every((part) => part.getBoundingClientRect().right <= panel.right + 1),
				inside: parts.every((part) => part.scrollWidth <= part.clientWidth + 1),
			};
		}, selector);
	const rates = await fits(".sp-source, .sp-import, [data-testid='rates-profile'], .sp-tiers");
	await page.evaluate(() => document.activeElement?.blur());
	const files = [await tallShot("wizard-rates-760")];
	await page.click(by("step-tab-test"));
	await page.click(by("test-run"));
	await page.waitForFunction(() => document.querySelector('[data-testid="test-verdict"]')?.textContent.trim(), { timeout: 90000 });
	const test = await fits(".sp-test-order, .sp-check, .sp-test-total");
	files.push(await tallShot("wizard-test-760"));
	// the restored draft was not changed here, so nothing writes it back: the browser's storage is left empty
	await page.evaluate((key) => window.localStorage.removeItem(key), DRAFT_KEY);
	await pause(500);
	assert((await stored()) === null, "nothing is left in the browser's storage");
	assert(Object.values(rates).every(Boolean), `rates: ${JSON.stringify(rates)}`);
	assert(Object.values(test).every(Boolean), `test: ${JSON.stringify(test)}`);
	return files.join(", ");
});

// Try an order can send the server out to the carrier, once a carton: it takes POST only, as the page sends it, so
// a link opened by a signed-in System Manager (a GET) cannot start it (final review M5).
await check("every try_order the page sent was a POST, and a GET of it is refused", async () => {
	const verbs = [...tryVerbs];
	assert(priced > 0 && verbs.length === 1 && verbs[0] === "POST", `the verbs of the page's ${priced} try_order requests: ${verbs}`);
	const url = `/api/method/${METHOD}try_order?${new URLSearchParams({ draft: "{}", order: "{}" })}`;
	const status = await page.evaluate(async (url) => (await fetch(url, { headers: { Accept: "application/json" } })).status, url);
	assert(status === 403, `a GET of try_order answers HTTP ${status}`);
	await pause(300); // the browser's own lines about the refused request
	expected(
		/^HTTP 403: GET .*shipping_policy_wizard\.try_order/,
		/^console error: Failed to load resource: .*403.*shipping_policy_wizard\.try_order/
	);
	return `${priced - 1} try_order requests from the page, every one a POST; a GET answers HTTP 403`;
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
