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

import { mkdir } from "node:fs/promises";
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

// The try_order requests the page sends, and a way to hold the next one back for a while.
let priced = 0;
let intercepting = false;
let holdNext = 0; // ms to hold the next try_order request before it goes out
page.on("request", (request) => {
	const isTry = request.url().includes("shipping_policy_wizard.try_order");
	if (isTry) priced += 1;
	if (!intercepting) return;
	if (isTry && holdNext) {
		const wait = holdNext;
		holdNext = 0;
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

async function shot(name) {
	await mkdir(OUT_DIR, { recursive: true });
	const file = path.resolve(OUT_DIR, `${name}.png`);
	await page.screenshot({ path: file, fullPage: true });
	return file;
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
// open there to tidy the storage as it unloads), then opens the wizard.
async function openWithStored(value) {
	await go(NEUTRAL);
	await page.evaluate((key, value) => window.localStorage.setItem(key, JSON.stringify(value)), DRAFT_KEY, value);
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

await check("each step shows a plain placeholder line until its body is built", async () => {
	for (const step of STEPS) {
		await page.click(by(`step-tab-${step}`));
		const panel = await page.$eval(`#sp-panel-${step}`, (element) => ({
			hidden: element.hidden,
			text: element.textContent.trim(),
			labelled: element.getAttribute("aria-labelledby"),
		}));
		assert(!panel.hidden && panel.text === "This step is built in the next stage.", `${step}: ${panel.text}`);
		assert(panel.labelled === `sp-tab-${step}`, `the ${step} panel is labelled by its tab`);
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

await check("screenshots of the initial state, light and dark (760 px wide comes last)", async () => {
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

await check("prefers-reduced-motion removes the receipt's transition", async () => {
	const duration = async () =>
		page.$eval(".sp-receipt", (receipt) => getComputedStyle(receipt).transitionDuration);
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
	const file = await shot("wizard-asked-the-carrier");
	return `the carrier said "${services}", the quote field holds ${quote}, the total reads ${await textOf("tape-total")}; ${file}`;
});

await check("removing the item takes it out of the order", async () => {
	await page.click(by("tape-item-remove"));
	assert((await page.$$(by("tape-item"))).length === 0, "no item lines left");
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

await check("no console errors or warnings and no failed requests", expectNoConsoleErrors);

await browser.close();

const failed = results.filter((result) => !result.ok).length;
console.log(`${results.length - failed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
