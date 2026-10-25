// ERP2 only. Requires ERP_SESSION_FILE (private JSON with sid), PUPPETEER and CHROME.
// Exercises real UI and saves a temporary policy, restoring the original policy switched off afterward.
import { readFile, mkdir } from "node:fs/promises";
import assert from "node:assert/strict";
const BASE = "https://erp2.shaik.net";
const OUT = process.env.OUT_DIR || "/tmp/shipping-policy-browser";
await mkdir(OUT, { recursive: true });
const { default: puppeteer } = await import(process.env.PUPPETEER);
const browser = await puppeteer.launch({
	executablePath: process.env.CHROME,
	headless: true,
	args: ["--no-sandbox"],
});
const page = await browser.newPage();
await page.setViewport({ width: 1440, height: 1100 });
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));
page.on("dialog", (d) => d.accept());
await page.setCookie({
	name: "sid",
	value: JSON.parse(await readFile(process.env.ERP_SESSION_FILE, "utf8")).sid,
	domain: "erp2.shaik.net",
	path: "/",
	secure: true,
	httpOnly: true,
});
const button = async (text, scope = ".sp-page") =>
	page.evaluate(
		(text, scope) => {
			const b = [...document.querySelectorAll(scope + " button")].find((b) => b.textContent.trim() === text);
			if (!b) throw new Error("Button not found: " + text);
			b.click();
		},
		text,
		scope,
	);
const rpc = (method, args = {}) =>
	page.evaluate(
		async (method, args) =>
			(await frappe.call({ method: "dsi_catalogue.shipping_policy_wizard." + method, args, type: "POST" }))
				.message,
		method,
		args,
	);
let original;
try {
	await page.goto(BASE + "/desk/shipping-policy-wizard", { waitUntil: "networkidle2", timeout: 60000 });
	await page.waitForSelector(".sp-cards");
	// Frappe stores the instance on the page wrapper.
	await page.evaluate(() => {
		window.testWizard = document.querySelector(".sp-page").closest(".page-container").wizard;
	});
	assert(await page.evaluate(() => !!window.testWizard), "page instance");
	original = (await rpc("wizard_state")).policy;
	await page.evaluate(() => {
		const w = window.testWizard;
		w.draft = {
			active: false,
			fallback: { method: "no_delivery", amount: 0 },
			zones: [],
			item_sets: [],
			rules: [],
		};
		w.render();
		w.changed();
	});
	await button("+ Add product group");
	await page.waitForSelector("dialog[open] input");
	await page.type("dialog input", "Browser sample");
	await button("Add group", "dialog");
	await page.waitForSelector('dialog input[aria-label="Search products"]');
	await page.type('dialog input[aria-label="Search products"]', "10005020");
	await page.waitForFunction(() =>
		document.querySelector("dialog .sp-product-result")?.textContent.includes("10005020"),
	);
	assert.equal(await page.$$eval("dialog .sp-product-result", (els) => els.length), 1);
	await page.click("dialog .sp-product-result input");
	await button("Use 1 selected products", "dialog");
	assert.equal(await page.evaluate(() => window.testWizard.draft.item_sets[0].items[0]), "10005020");
	console.log("PASS dynamic product picker and manual selection");
	// The row is created through the page, then edited through its price controls.
	await button("+ Add country group", ".sp-card");
	await page.waitForSelector(".sp-row-editor");
	await button("One price", ".sp-row-editor");
	const price = await page.$(".sp-pricing input");
	await price.click({ clickCount: 3 });
	await price.type("12.50");
	await button("Done", ".sp-row-editor");
	assert.match(await page.$eval(".sp-row-summary", (e) => e.textContent), /12.50/);
	await button("Review and save");
	await page.waitForFunction(() =>
		[...document.querySelectorAll("dialog button")].some((b) => b.textContent === "Save policy"),
	);
	await button("Save policy", "dialog");
	await page.waitForFunction(
		() => !document.querySelector("dialog[open]") && !document.querySelector(".sp-savebar:not([hidden])"),
	);
	const saved = (await rpc("wizard_state")).policy;
	assert.equal(saved.rules[0].price_table[0].price, 12.5);
	assert.equal(saved.active, false);
	console.log("PASS review and real save; policy remains off");
	await page.evaluate(() => {
		const w = window.testWizard;
		w.sample = { country_code: "BH", city: "Manama", items: [{ item_code: "10005020", qty: 1 }] };
		w.renderPreview();
	});
	await page.waitForFunction(() => document.querySelector(".sp-receipt")?.textContent.includes("$12.50"));
	console.log("PASS draft preview uses ERP item facts");
	// Live filter is a persisted group/pattern, not the search result list.
	await button("Edit products", ".sp-card");
	await page.waitForSelector('dialog input[placeholder="e.g. ABC-*"]');
	await page.type('dialog input[placeholder="e.g. ABC-*"]', "F-*-PT");
	await page.waitForFunction(
		() =>
			![...document.querySelectorAll("dialog button")].find((b) => b.textContent === "Follow this filter")
				?.disabled,
	);
	await button("Follow this filter", "dialog");
	assert.deepEqual(await page.evaluate(() => window.testWizard.draft.item_sets[0].patterns), ["F-*-PT"]);
	console.log("PASS live code-pattern filter");
	await page.evaluate(() => {
		const b = document.querySelector(".sp-row-summary");
		if (b.getAttribute("aria-expanded") !== "true") b.click();
	});
	await page.waitForSelector(".sp-row-editor");
	await button("Formula", ".sp-row-editor");
	await page.type(".sp-row-editor textarea", "carrier_price * 1.1 + 5");
	await page.waitForFunction(() =>
		document.querySelector(".sp-pricing")?.textContent.includes("Formula is valid."),
	);
	assert.match(await page.$eval(".sp-services", (e) => e.textContent), /All available provider services/);
	const counts = await page.evaluate(() => ({
		shown: document.querySelectorAll(".sp-services input[type=checkbox]").length,
		known: window.testWizard.state.services.length,
	}));
	assert.equal(counts.shown, counts.known);
	console.log("PASS formula validation and every ERP service displayed");
	await page.evaluate(() => {
		$(".modal").modal("hide");
		$(".alert-container").hide();
	});
	await new Promise((resolve) => setTimeout(resolve, 6000));
	await page.screenshot({ path: OUT + "/editor-light.png", fullPage: true });
	await page.evaluate(() => document.documentElement.setAttribute("data-theme", "dark"));
	await page.screenshot({ path: OUT + "/editor-dark.png", fullPage: true });
	await button("Done", ".sp-row-editor");
	await page.evaluate(() => document.documentElement.setAttribute("data-theme", "light"));
	await page.setViewport({ width: 390, height: 844 });
	await page.mouse.click(365, 180);
	await new Promise(resolve=>setTimeout(resolve,500));
	await page.screenshot({ path: OUT + "/mobile.png", fullPage: true });
	assert(
		await page.evaluate(
			() => document.querySelector(".sp-page").getBoundingClientRect().right <= window.innerWidth + 2,
		),
		"mobile page fits",
	);
	console.log("PASS light, dark and mobile renders");
	// Never save the formula test; report what the actual sandbox carrier returned.
	await page.waitForFunction(
		() => !document.querySelector(".sp-receipt")?.textContent.includes("Calculating"),
		{ timeout: 60000 },
	);
	console.log("LIVE_PREVIEW", await page.$eval(".sp-receipt", (e) => e.textContent));
	assert.deepEqual(errors, []);
} finally {
	if (original) {
		const state = await rpc("wizard_state");
		original.active = false;
		await rpc("apply_policy", { draft: JSON.stringify(original), expected_version: state.version });
		await page.evaluate(() => {
			localStorage.removeItem("dsi_catalogue.shipping_policy.draft.v2");
		});
		console.log("RESTORED original policy, switched off");
	}
	await browser.close();
}
