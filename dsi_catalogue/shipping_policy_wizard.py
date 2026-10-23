"""The shipping policy wizard's methods: what the wizard page reads, prices and, on Apply, writes.

Seven methods, for the System Manager role only. Each one checks the role before it does anything else.

	wizard_state     the saved policy, its version, and the lists the page needs
	try_order        prices a sample order with the draft, as if the draft were switched on
	check_formula    whether a formula can be used, and why not when it cannot
	preview_changes  what applying the draft would change, and what stops it from being applied
	apply_policy     writes the whole draft at once, or nothing at all
	import_rate_card reads the two Shopify files into a rate card and says what it found; writes nothing
	test_integration three read-only checks of what is live: the ERP's price API, the carrier, the website

The draft is a policy in the shape `shipping_engine` documents, as `wizard_state` hands it out and the page
edits it in the browser, with its price source and rate card. Only `apply_policy` writes. It takes the whole
draft in one request, refuses it when someone changed the policy after the wizard loaded it or when the draft
has a problem (the rate card included), and writes it inside one savepoint, so that a failure at any step
rolls back every step before it: a half-finished policy never goes live. Applies are taken one at a time.
`import_rate_card` only reads: the page puts the card it returns into the draft, and Apply is still the only
writer.

Before a draft is priced, checked, compared or written it is read as it will be stored: names and codes
without outer spaces, country codes in capitals, amounts rounded to cents and weights and volumes to the
three decimals their fields keep. The Review step therefore never shows rounding as a change, a band such as
2.0001 up to 2.0004 kg is judged as the 2 up to 2 it would be stored as, and what is checked is what is
written. A draft whose parts have the wrong types (a list given as text, a flag given as text, a number that
is not one) is not read at all: it is "not in the expected shape".

The desk shows a refusal as HTML, so the text people typed is escaped in the messages these methods raise.
Everything they return is plain text, for the page to show with textContent (never as HTML): the sentences of
`preview_changes`, `check_formula`, `import_rate_card` (which quote the file as it is, markup and all) and
`test_integration`, and every name and value in the answers.

`test_integration` is the one method that reaches out: it POSTs to the ERP's own price API and to the website
whose address is kept in Storefront Settings, and nowhere else. See its docstring for what it never does.
"""

import json
import math
import socket
import time
from urllib.parse import urlsplit

import frappe
import requests
import urllib3
from frappe import _
from frappe.utils import escape_html, flt, get_url, sbool
from frappe.utils.file_lock import LockTimeoutError
from frappe.utils.synchronization import filelock

from dsi_catalogue import shipping_formula, shipping_integration_check
from dsi_catalogue.shipping_engine import normalize_country, resolve, rule_previews, validate_policy
from dsi_catalogue.shipping_formula import FormulaError
from dsi_catalogue.shipping_policy import (
	PRICE_LIST,
	build_order,
	fallback_may_use_the_card,
	load_policy,
	price_delivery,
	served_country_codes,
)
from dsi_catalogue.shipping_policy_data import (
	MAX_BOXES,
	MAX_NUMBER,
	MAX_TEXT,
	InputError,
	clean_boxes,
	clean_items,
	clean_quotes,
	diff_policy,
	parse_json_arg,
	policy_from_rows,
	rows_from_policy,
	snapshot_version,
)
from dsi_catalogue.shipping_rate_card import (
	MAX_COUNTRIES,
	MAX_PROFILES,
	MAX_ZONES,
	RateCardError,
	card_summary,
	parse_shopify_export,
	validate_card,
)

POLICY = "Shipping Policy"
ZONE = "Shipping Zone"
ITEM_SET = "Shipping Item Set"

# One apply at a time, across every worker of the site: a second one waits this long for the first to
# finish, and is then refused.
APPLY_LOCK = "shipping_policy_apply"
APPLY_LOCK_SECONDS = 30
_SAVEPOINT = "shipping_policy_apply"

# Try an order packs at most this many units into cartons: packing takes longer with every unit, and every
# carton is one question to the carrier.
MAX_PACKED_UNITS = 1000

# `import_rate_card` reads files of up to this many characters (bytes: four times as many, as UTF-8 needs),
# and tells of at most this many problems and notes, then how many more there are.
MAX_CSV_CHARACTERS = 1_000_000
MAX_LISTED = 50

# A rate card is plain data nested about eight deep (card, profiles, a profile, zones, a zone, tiers, a tier,
# a value) and, at the most `validate_card` allows, some 250,000 values. A draft's card may hold more than the
# card needs, but not more than this.
MAX_CARD_DEPTH = 20
MAX_CARD_VALUES = 500_000

# `test_integration`: the sample order, the one sample parcel the carrier is asked about, and what is read
# of an answer. A call is given HTTP_TIMEOUT_SECONDS in all, and the answer is read at most up to MAX_BODY_BYTES.
MAX_SAMPLE_ITEMS = 10
SAMPLE_PARCEL = {
	"weight_kg": 1,
	"box": {"box_code": "BOX_S", "length_cm": 20, "width_cm": 15, "height_cm": 10},
}
SAMPLE_CARTON = {"weight_kg": 1.0, "volume_l": 3.0}  # sent to the price API when the carrier is the source
PRICE_API = "/api/method/dsi_catalogue.shipping_policy.price_delivery"
RATES_ROUTE = "/api/webshop/shipping/rates"
HTTP_TIMEOUT_SECONDS = shipping_integration_check.HTTP_TIMEOUT_SECONDS
MAX_BODY_BYTES = shipping_integration_check.MAX_BODY_BYTES
_resolve = socket.getaddrinfo  # looks up a host name for the guard on the website's address

SHAPE_PROBLEM = "The draft is not in the expected shape."
_CANNOT_APPLY = "The shipping policy cannot be applied"
_STALE = (
	"Someone changed the shipping policy after you opened the wizard. Reload the wizard to see the change, "
	"then apply again."
)
_BUSY = "Someone else is applying a change right now. Try again in a moment."
_UNSAVABLE_CARD = "The rate card cannot be saved as it is (it is nested too deeply or holds values that are not plain data)."

# The decimals each number is stored with: the precision of its doctype field.
_MONEY, _MEASURE = 2, 3
_LIMITS = {
	"min_order_value": _MONEY,
	"max_order_value": _MONEY,
	"min_weight_kg": _MEASURE,
	"max_weight_kg": _MEASURE,
	"min_volume_l": _MEASURE,
	"max_volume_l": _MEASURE,
}

# What each part of a draft holds. A policy has every one of its parts. Inside a fallback, a zone, an item
# set or a rule a part may be missing or None, which reads as empty; a part that is there has its type.
_NUMBER, _TEXTS = "a number", "a list of texts"
_SHAPES = {
	"policy": {
		"active": bool,
		"price_source": str,
		"rate_card": dict,
		"not_covered": str,
		"default_fallback": dict,
		"zones": list,
		"item_sets": list,
		"rules": list,
	},
	"fallback": {"method": str, "amount": _NUMBER},
	"zone": {
		"name": str,
		"enabled": bool,
		"countries": _TEXTS,
		"not_shipped": bool,
		"everywhere_else": bool,
		"customer_line": str,
		"not_offered_message": str,
		"fallback": dict,
	},
	"item set": {"name": str, "item_groups": _TEXTS, "items": _TEXTS},
	"rule": {
		"name": str,
		"enabled": bool,
		"label": str,
		"zone": str,
		"item_set": str,
		**dict.fromkeys(_LIMITS, _NUMBER),
		"method": str,
		"formula": str,
		"round_up": bool,
	},
}

# A draft may leave out the two settings of the rate card (or give None): a missing price source is the carrier
# and a missing card is no card, as the engine reads them. The other parts of a policy have to be there.
_MAY_BE_LEFT_OUT = {"policy": ("price_source", "rate_card")}

# The child tables of a zone and of an item set, each with the field that holds its value.
_CHILD_TABLES = {ZONE: {"countries": "country"}, ITEM_SET: {"item_groups": "item_group", "items": "item"}}
_CARRIER_DOCTYPES = ("Shipping Service", "Shipping Provider")


# ---------------------------------------------------------------- the methods
@frappe.whitelist()
def wizard_state() -> dict:
	"""What the wizard starts from: {"policy", "version", "countries": [{"code", "name"}], "served_codes",
	"price_list", "carrier_available"}.

	`policy` is the saved policy as `load_policy` reads it and `version` names it (Apply gives it back, so a
	change made meanwhile is noticed). `countries` is every Country with a code, by name, for the zone
	chips; `served_codes` the countries some carrier service delivers to; `price_list` the list an order's
	value comes from; `carrier_available` whether Try an order can ask the carrier. Plain data only.
	"""
	frappe.only_for("System Manager")
	policy = load_policy()
	return {
		"policy": policy,
		"version": snapshot_version(policy),
		"countries": _country_list(),
		"served_codes": served_country_codes(),
		"price_list": PRICE_LIST,
		"carrier_available": _carrier_available(),
	}


@frappe.whitelist()
def try_order(draft, order, ask_carrier=False, city=None) -> dict:
	"""Price a sample order with the draft, not with the saved policy: {"result", "previews", "order"}.

	The draft is priced as if it were switched on, so that it can be tried before it goes live; the page
	reads on and off from the draft itself. `order` is {"country_code", "items": [{"item_code", "qty"}],
	"order_value", "boxes", "quotes"} (a dict or JSON text). What is not typed comes from the ERP: the items'
	groups and their value in USD - Online, and the cartons dsi_shipping packs them into. A typed value,
	typed cartons and typed quotes (an empty list too) win. With `ask_carrier` and no typed quotes the
	carrier is asked for each carton (it needs a `city`), and any failure of the carrier means no quotes.

	When the draft's price source is the rate card only the country and the items are read, as the guest
	method does: the order is priced from the card, using each item's product code and unit weight (an item
	without a weight is priced at the card's default weight, which the receipt says), and a typed value,
	typed cartons, typed quotes and `ask_carrier` are ignored (they are not even read, so bad ones cannot
	fail it). A card that cannot price the order (`too_heavy`, `not_covered`) is an answer, not an error. The
	item facts are read for a draft that prices from the carrier too, when its fallback may use the card (see
	`shipping_policy.fallback_may_use_the_card`), so that the preview and the live price read the same facts.

	`result` is the engine's whole answer, the trace of each offer included; `previews` says, rule by rule,
	what it would charge this order; `order` is the order as it was priced. Input that cannot be read is
	refused with a plain sentence. Nothing is written, and a formula that fails is not logged: the page
	prices the draft on every keystroke.
	"""
	frappe.only_for("System Manager")
	policy = _read_draft(draft)
	if policy is None:
		frappe.throw(_(SHAPE_PROBLEM))
	sample = _sample_order(order, _is_yes(ask_carrier), city, policy)
	as_if_on = {**policy, "active": True}
	return {"result": resolve(as_if_on, sample), "previews": rule_previews(as_if_on, sample), "order": sample}


@frappe.whitelist()
def check_formula(source) -> dict:
	"""{"ok": True, "error": None} for a formula a rule can use, otherwise {"ok": False, "error": <why, in a
	plain sentence>}. The same check a rule's formula gets when the policy is saved. It never raises,
	whatever it is given, and nothing is logged."""
	frappe.only_for("System Manager")
	try:
		shipping_formula.check_formula(source)
	except FormulaError as error:
		return {"ok": False, "error": str(error)}
	except Exception:  # the checker raises nothing else; if it ever did, the page still gets an answer
		return {"ok": False, "error": "The formula could not be checked."}
	return {"ok": True, "error": None}


@frappe.whitelist()
def preview_changes(draft) -> dict:
	"""What applying the draft would change, and what stops it from being applied: {"changes", "problems"}.

	`changes` is `diff_policy(saved policy, draft as it will be stored)` (see its docstring for the shape;
	the price source and the rate card are in it too). `problems` are plain sentences: `validate_policy`'s
	(the rate card's own problems among them), one for each country code of the zones or of the rate card that
	is not a Country of the ERP, a rate card that cannot be saved as text, and those the doctypes' own checks
	would raise on saving. Apply is refused while there is one. It never raises on a malformed draft: one that
	is not a policy gives the single problem "The draft is not in the expected shape." and no changes. A draft
	may leave out `price_source` and `rate_card` (the carrier, and no card). Nothing is written.
	"""
	frappe.only_for("System Manager")
	policy = _read_draft(draft)
	if policy is None:
		return {"changes": diff_policy({}, {}), "problems": [SHAPE_PROBLEM]}
	countries, saved = _country_names(), load_policy()
	problems = _problems(policy, countries, saved)
	# A card that cannot be saved cannot be compared either (it is not written out, to see what it would read
	# back as): there are no changes to show, as for a draft that is not a policy.
	if _UNSAVABLE_CARD in problems:
		return {"changes": diff_policy({}, {}), "problems": problems}
	return {"changes": diff_policy(saved, _as_loaded(policy, countries)), "problems": problems}


@frappe.whitelist(methods=["POST"])
def apply_policy(draft, expected_version) -> dict:
	"""Write the whole draft, or nothing: {"version", "changes"}.

	Refused, before anything is written, when `expected_version` is not the version of the saved policy
	(someone changed it after the wizard loaded it: `frappe.TimestampMismatchError`), and when the draft has
	any of the problems `preview_changes` reports. Otherwise written inside one savepoint, in this order:

	1. the item sets, inserted or updated;
	2. every saved zone the draft drops is switched off, so that the policy's own check in step 4 finds it
	   inert (a country it lists may now be in another zone);
	3. the draft's zones, inserted or updated, one by one, without the zone form's checks across zones (the
	   draft was checked as a whole: one at a time, a country moving between two zones would be refused);
	4. the policy's settings (the price source and the rate card among them) and rules, in one save that the
	   policy's own check validates;
	5. the zones and item sets the draft drops are deleted: no rule points at them any more;
	6. the saved policy is read back and checked once more.

	A failure at any step rolls everything back and is raised. A zone or item set that would not change is
	left alone, so the history of each document records only real changes. Afterwards the website is asked
	to refresh through dsi_core's outbox, when the site has it. Returns the new version and the changes
	(`diff_policy(policy before, draft)`).

	Only one apply runs at a time: a second waits for the first, and is refused if that takes over 30 seconds.
	It is the one method that writes, so it takes POST requests only (a link cannot apply a policy).
	"""
	frappe.only_for("System Manager")
	try:
		with filelock(APPLY_LOCK, timeout=APPLY_LOCK_SECONDS):
			return _apply(draft, expected_version)
	except LockTimeoutError:
		frappe.throw(_(_BUSY), title=_(_CANNOT_APPLY))


@frappe.whitelist()
def import_rate_card(config_csv, not_delivered_csv=None) -> dict:
	"""Read the two files exported from Shopify into a rate card and say what was found. It writes nothing.

	`config_csv` is the shipping rates file (Profile, Zone (recommended), Country count, Countries (ISO-2 -
	Name), Shipping method, Weight from (lb), Weight to (lb), Price (USD)) and `not_delivered_csv` the optional
	list of countries that get no delivery (ISO-2, Country), each as text of up to 1,000,000 characters
	(bytes are read as UTF-8, with or without a byte order mark; an empty second file is none). Anything else
	is refused with a plain sentence.

	Returns {"card", "not_delivered": [ISO-2], "not_delivered_missing": [ISO-2], "summary": {"profiles",
	"zones", "tiers", "countries"}, "notes": [str], "problems": [str]}. `card` is the rate card (None when the
	rates file cannot be used at all, which is then the one problem). `notes` are what was skipped or is odd in
	the files, `problems` what stops the card from being applied: the card's own (`validate_card`), then a
	sentence for each country code that is not a Country of the ERP, in the first zone of the card that lists it
	and in the not-delivered list. `not_delivered_missing` lists those of the not-delivered countries. Each list
	shows at most 50 sentences and then "And N more.". The sentences quote the files as they are and are plain
	text: the page shows them with textContent, never as HTML.

	The draft is not changed: the page takes the card (and the not-delivered countries, as a Not shipped zone)
	into its draft, and Apply is the only writer.
	"""
	frappe.only_for("System Manager")
	config = _csv_text(config_csv, "rates file")
	not_delivered = (
		None if _is_blank(not_delivered_csv) else _csv_text(not_delivered_csv, "not-delivered file")
	)
	try:
		read = parse_shopify_export(config, not_delivered)
	except RateCardError as error:
		return _imported(None, [], [], [], [str(error)])
	card, codes = read["card"], _country_names()
	missing = [code for code in read["not_delivered"] if code not in codes]
	problems = [
		*validate_card(card),
		*_unknown_card_countries(card, codes),
		*(f"Country code {code} in the not-delivered list is not a country in the ERP." for code in missing),
	]
	return _imported(card, read["not_delivered"], missing, read["notes"], problems)


@frappe.whitelist(methods=["POST"])
def test_integration(order=None) -> dict:
	"""Three read-only checks of what is live, not of the draft: {"checks": [...], "verdict", "order"}.

	Each check is {"name", "ok", "status", "ms", "summary", "detail"} (see `shipping_integration_check`), in this
	order, and each is made whatever happened to the one before:

	1. `erp_price_api`: POST of the sample order to this site's own `price_delivery` over HTTP, as the website
	   calls it (items only when the price source is the rate card; with one sample carton and no quotes when it
	   is the carrier), compared with the amount `price_delivery` gives in this process for the saved policy. The
	   address is the site's `host_name` (site_config.json): the check relies on it. A site without one is called
	   at its own name, never at the Host or X-Forwarded-Proto of the request, which the caller controls.
	2. `carrier`: `dsi_shipping.api.get_shipping_rates` asked in this process for one sample parcel. Whatever the
	   carrier says is reported in plain words: its own error text, which can hold what its server sent back,
	   is never passed on.
	3. `website`: POST of the same cart to `/api/webshop/shipping/rates` of the website kept in Storefront
	   Settings (Current Website), and a comparison of its first amount with check 1's when the rate card is the
	   price source. The address is called only if it is the website's own and public: https (http only for
	   `localhost`), a host name, no user name, port 80 or 443, nothing after the host, and every address its
	   name resolves to is public. A redirect is not followed.

	`order` may change the sample, as a dict or JSON text: `country_code` (or `country`, a name or a code),
	`city` and `items` ([{"item_code", "qty"}], at most 10); what it leaves out (or leaves empty) is the
	default, the first published full-size fragrance and the first published watch to New York in the United
	States. The answer's `order` is the sample used, with a `note` when the default had to make do. Anything
	else in `order` is ignored: no other address is ever called.

	A call has 15 seconds in all, is sent with no cookie, no credential and no header but its content type,
	and has at most 64 KB of the answer read. Only the fields the summaries name, and `detail` (the answer
	cut to 4 KB with every key that looks like a secret dropped), come back. A call that fails or gives
	nonsense is an answer, not an error. The `verdict` says in one sentence what customers are charged and
	whether the website follows the policy. It takes POST requests only: it sends this server out to the
	network.

	It writes no document. It can leave rows in the Error Log, a table that nothing rolls back, and these are
	all there is:
	- the callee `price_delivery` (check 1, over HTTP and in this process) writes its throttled "Shipping
	  policy formula error" row when a rule's formula fails for the sample order, as for any customer;
	- the callee `dsi_shipping.api.get_shipping_rates` (check 2) writes "get_shipping_rates: <service> failed,
	  skipping" for each service whose carrier call fails, and the Aramex adapter writes "Aramex
	  CalculateRate Failed": about two rows for each Aramex service that serves the destination while the
	  account is blocked. The check then says the carrier returned no rates and points at those rows; their
	  text, which can hold what the provider sent back, is never passed on;
	- an unexpected exception in a check is written once, titled "shipping policy integration test failed",
	  with Frappe's usual copy of the request's form data, which here is only the optional `order` override.
	"""
	frappe.only_for("System Manager")
	sample = _integration_sample(order)
	erp = _run_check("erp_price_api", _check_erp, sample)
	carrier = _run_check("carrier", _check_carrier, sample)
	website = _run_check("website", _check_website, sample, erp)
	checks = [erp, carrier, website]
	return {"checks": checks, "verdict": shipping_integration_check.verdict_for(checks), "order": sample}


# ---------------------------------------------------------------- reading a draft
def _is_text(value) -> bool:
	"""Text that can be stored and sent back: JSON can carry a lone surrogate, which nothing can write."""
	if not isinstance(value, str):
		return False
	try:
		value.encode("utf-8")
	except UnicodeEncodeError:
		return False
	return True


def _is_number(value) -> bool:
	"""A real, finite number of at most a billion either way: an int or a float, and not a bool."""
	if isinstance(value, bool) or not isinstance(value, int | float):
		return False
	try:
		return math.isfinite(value) and abs(value) <= MAX_NUMBER
	except OverflowError:  # an int too big for a float
		return False


def _holds(value, kind) -> bool:
	if kind == _NUMBER:
		return _is_number(value)
	if kind == _TEXTS:
		return isinstance(value, list) and all(_is_text(entry) for entry in value)
	if kind is str:
		return _is_text(value)
	return isinstance(value, kind)


def _fits(entry, shape, *, whole=False) -> bool:
	"""Is `entry` a dict whose parts have the types of `shape`? With `whole`, every part has to be there."""
	if not isinstance(entry, dict):
		return False
	for part, kind in _SHAPES[shape].items():
		value = entry.get(part)
		if value is None:
			if whole and part not in _MAY_BE_LEFT_OUT.get(shape, ()):
				return False
		elif not _holds(value, kind):
			return False
	return True


def _is_policy(value) -> bool:
	if not _fits(value, "policy", whole=True):
		return False
	entries = [
		*((zone, "zone") for zone in value["zones"]),
		*((item_set, "item set") for item_set in value["item_sets"]),
		*((rule, "rule") for rule in value["rules"]),
	]
	if not all(_fits(entry, shape) for entry, shape in entries):
		return False
	fallbacks = [value["default_fallback"], *(zone.get("fallback") for zone in value["zones"])]
	return all(fallback is None or _fits(fallback, "fallback") for fallback in fallbacks)


def _trimmed(text):
	return text.strip() if isinstance(text, str) else text


def _rounded(value, places):
	return flt(value, places) if _is_number(value) else value


def _code(text):
	"""A country code as two upper-case letters. What is not one stays as it was typed (without outer spaces),
	for `validate_policy` to say so: upper-casing first would turn "\u00df" into SS, South Sudan."""
	return normalize_country(text) or text.strip()


def _tidy_card(card):
	"""The rate card with the numbers of each tier rounded as they are kept: prices to cents, weights to three
	decimals. What is not a card (None) and the parts of a card that are not in the shape of one are left as
	they are, for `validate_card` to say so, and nothing else in the card is looked into."""
	if not isinstance(card, dict) or not isinstance(card.get("profiles"), list):
		return card
	decimals = {"from": _MEASURE, "to": _MEASURE, "price": _MONEY}

	def tier(entry):
		if not isinstance(entry, dict):
			return entry
		return {
			**entry,
			**{key: _rounded(entry[key], places) for key, places in decimals.items() if key in entry},
		}

	def zone(entry):
		if not isinstance(entry, dict) or not isinstance(entry.get("tiers"), list):
			return entry
		return {**entry, "tiers": [tier(each) for each in entry["tiers"]]}

	def profile(entry):
		if not isinstance(entry, dict) or not isinstance(entry.get("zones"), list):
			return entry
		return {**entry, "zones": [zone(each) for each in entry["zones"]]}

	return {**card, "profiles": [profile(each) for each in card["profiles"]]}


def _tidied(policy) -> dict:
	"""The draft as the doctypes will hold it: names and codes without outer spaces, empty entries left out,
	country codes in capitals, and every number rounded to the decimals of its field (those of the rate card
	too). The draft itself is left as it is, and nothing the draft holds beyond the parts read here is looked
	into."""

	def fallback(entry):
		return None if entry is None else {**entry, "amount": _rounded(entry.get("amount"), _MONEY)}

	def stripped(entries):
		return [entry.strip() for entry in entries or [] if entry.strip()]

	return {
		**policy,
		"rate_card": _tidy_card(policy.get("rate_card")),
		"default_fallback": fallback(policy["default_fallback"]),
		"zones": [
			{
				**zone,
				"name": _trimmed(zone.get("name")),
				"countries": [_code(code) for code in zone.get("countries") or [] if code.strip()],
				"fallback": fallback(zone.get("fallback")),
			}
			for zone in policy["zones"]
		],
		"item_sets": [
			{
				**item_set,
				"name": _trimmed(item_set.get("name")),
				"item_groups": stripped(item_set.get("item_groups")),
				"items": stripped(item_set.get("items")),
			}
			for item_set in policy["item_sets"]
		],
		"rules": [
			{
				**rule,
				"zone": _trimmed(rule.get("zone")),
				"item_set": _trimmed(rule.get("item_set")) or None,
				**{limit: _rounded(rule.get(limit), places) for limit, places in _LIMITS.items()},
			}
			for rule in policy["rules"]
		],
	}


def _read_draft(draft) -> dict | None:
	"""The draft (a dict or JSON text) as a policy read the way it will be stored, or None when it is not a
	policy in the expected shape."""
	try:
		value = parse_json_arg(draft, None)
	except InputError:
		return None
	return _tidied(value) if _is_policy(value) else None


def _country_list() -> list[dict]:
	"""[{"code", "name"}] for every Country with a two-letter code, by name."""
	rows = frappe.get_all("Country", fields=["name", "code"], order_by="name asc")
	return [{"code": code, "name": row["name"]} for row in rows if (code := normalize_country(row["code"]))]


def _country_names() -> dict[str, str]:
	"""{ISO code: Country name}."""
	return {country["code"]: country["name"] for country in _country_list()}


def _rows_and_loaded(policy, countries) -> tuple[dict, dict]:
	"""The rows the draft is written as, and the policy `load_policy` will read back from them (zones and
	item sets in the draft's order)."""
	rows = rows_from_policy(policy, countries)
	codes = {name: code for code, name in countries.items()}
	return rows, policy_from_rows(rows["policy"], rows["zones"], rows["item_sets"], codes)


def _as_loaded(policy, countries) -> dict:
	return _rows_and_loaded(policy, countries)[1]


# ---------------------------------------------------------------- what stops a draft
def _problems(policy, countries, saved) -> list[str]:
	"""Everything that stops the draft from being applied, as plain sentences: `validate_policy`'s (the rate
	card's own problems among them), then the countries the ERP does not know (those of the zones, then those
	of the rate card), then what the doctypes would refuse to save although the engine has no quarrel with it,
	a rate card that cannot be saved included. `saved` is the policy as it is in the ERP now."""
	card = policy.get("rate_card")
	return [
		*validate_policy(policy),
		*_unknown_countries(policy, countries),
		*_unknown_card_countries(card, countries),
		*_unsavable_card(card),
		*_saving_problems(policy, saved),
	]


def _short(text, limit=60) -> str:
	"""A name a sentence quotes: cut to `limit` characters, with anything unprintable shown as ?."""
	if not isinstance(text, str):
		return ""
	text = "".join(char if char.isprintable() else "?" for char in text[: limit * 2]).strip()
	return text if len(text) <= limit else text[: limit - 3] + "..."


def _unknown_card_countries(card, countries) -> list[str]:
	"""One sentence for each country code of the rate card that is no Country of the ERP, in the first zone of
	the card that lists it ("Country code ZZ in zone Tier 2 (Dangerous Goods) is not a country in the ERP.").
	A code that is not two capital letters gets the card's own sentence, from `validate_card`. Only as much of
	the card as `validate_card` looks at is read."""
	found = {}
	profiles = card.get("profiles") if isinstance(card, dict) else None
	for number, profile in enumerate(profiles[:MAX_PROFILES] if isinstance(profiles, list) else [], start=1):
		zones = profile.get("zones") if isinstance(profile, dict) else None
		for position, zone in enumerate(zones[:MAX_ZONES] if isinstance(zones, list) else [], start=1):
			listed = zone.get("countries") if isinstance(zone, dict) else None
			for code in listed[:MAX_COUNTRIES] if isinstance(listed, list) else []:
				if code in found or not isinstance(code, str) or code != normalize_country(code):
					continue
				if code not in countries:
					zone_name = _short(zone.get("name")) or f"number {position}"
					profile_name = _short(profile.get("name")) or f"number {number}"
					found[code] = (
						f"Country code {code} in zone {zone_name} ({profile_name}) is not a country in the ERP."
					)
	return list(found.values())


def _is_plain_data(value) -> bool:
	"""Is `value` plain data of a size a rate card has: dicts with text keys, lists, texts, numbers, True, False
	and None, nested at most MAX_CARD_DEPTH deep and made of at most MAX_CARD_VALUES values in all? It looks
	with a loop of its own, not by recursion, so a value nested a hundred thousand deep or one that holds
	itself is answered, not tripped over; the first value it finds out of bounds ends it.

	The values still to be looked at are counted before they are queued, so a list or dict of millions of
	values is refused by its size, without being copied onto the stack or looked through."""
	stack, seen = [(value, 1)], 0
	while stack:
		current, depth = stack.pop()
		seen += 1
		if seen > MAX_CARD_VALUES or depth > MAX_CARD_DEPTH:
			return False
		if isinstance(current, dict):
			children = current.values()
		elif isinstance(current, list):
			children = current
		elif current is None or isinstance(current, str | int | float | bool):
			continue
		else:
			return False
		if seen + len(stack) + len(children) > MAX_CARD_VALUES:
			return False
		if isinstance(current, dict) and not all(isinstance(key, str) for key in current):
			return False
		stack.extend((item, depth + 1) for item in children)
	return True


def _unsavable_card(card) -> list[str]:
	"""The rate card cannot be saved as it is: it is nested too deeply or is too big, holds a loop, keys that are
	not text, values that are not plain data, or a number that cannot be written. The Python that runs here
	writes JSON far deeper than the one the tests were first made on, and writes the indented text of a card
	nested a thousand deep as a megabyte, so the card is looked at first (`_is_plain_data`) and only a card
	of sensible size is written out to see that it can be. Written as empty text it would erase the card that is
	stored, so the draft is refused instead."""
	if card is None:
		return []
	if _is_plain_data(card) and rows_from_policy({"rate_card": card}, {})["policy"]["rate_card"]:
		return []
	return [_UNSAVABLE_CARD]


def _unknown_countries(policy, countries) -> list[str]:
	"""One sentence for each two-letter code that is no Country of the ERP. (A code that is not two letters
	gets the engine's own sentence.)"""
	unknown = dict.fromkeys(
		code
		for zone in policy["zones"]
		for code in zone["countries"]
		if normalize_country(code) and code not in countries
	)
	return [f"Country code {code} is not a country in the ERP." for code in unknown]


def _capital_letter_clashes(kind, names, saved_names) -> list[str]:
	"""One sentence for each name the ERP would take for another although the text is not the same, because it
	ignores capital letters in a name: two names of the draft, or a name of the draft and the saved document of
	that name which the draft drops (it is still there when the new one is written). Names that are exactly the
	same are the engine's to report."""
	problems, first = [], {}
	for name in filter(None, names):
		earlier = first.setdefault(name.casefold(), name)
		if earlier != name:
			problems.append(
				f'{kind}s "{earlier}" and "{name}" differ only in capital letters, which the ERP reads as one '
				f"name. {kind} names have to be different."
			)
	for name in saved_names:
		held = first.get(name.casefold())
		if held and held != name and name not in names:
			problems.append(
				f'{kind} "{held}" is the saved {kind.lower()} "{name}" with other capital letters. The ERP '
				f'reads them as one name, so keep "{name}" or choose another name.'
			)
	return problems


def _saving_problems(policy, saved) -> list[str]:
	"""What the zone and policy doctypes would refuse to save although `validate_policy` has no quarrel with it,
	said now instead of half way through the write: names the ERP reads as one, a disabled Everywhere else zone
	that lists countries, and a fallback amount below zero that no fixed charge uses."""
	problems = [
		*_capital_letter_clashes(
			"Zone", [zone.get("name") for zone in policy["zones"]], [zone["name"] for zone in saved["zones"]]
		),
		*_capital_letter_clashes(
			"Item set",
			[item_set.get("name") for item_set in policy["item_sets"]],
			[item_set["name"] for item_set in saved["item_sets"]],
		),
	]
	fallbacks = [("The policy default", policy["default_fallback"])]
	for position, zone in enumerate(policy["zones"], start=1):
		ref = f'"{zone["name"]}"' if zone.get("name") else f"number {position}"
		# The zone form refuses countries on any Everywhere else zone; the engine looks at enabled zones only.
		if zone.get("everywhere_else") and not zone.get("enabled") and zone["countries"]:
			problems.append(f"Zone {ref} is an Everywhere else zone, so it cannot list countries.")
		fallbacks.append((f"Zone {ref}", zone.get("fallback")))
	for subject, fallback in fallbacks:
		amount = (fallback or {}).get("amount")
		# A fixed amount below zero is the engine's to report. Any other is not used, but no field takes it.
		if (fallback or {}).get("method") != "fixed" and _is_number(amount) and amount < 0:
			problems.append(
				f"{subject} has an amount below zero for when the carrier can't quote. Make it 0 or more."
			)
	return problems


# ---------------------------------------------------------------- the sample order
def _is_yes(value) -> bool:
	"""A yes or no as a JSON body or a form post sends it: True, 1, "1" and "true" are yes."""
	if isinstance(value, str):
		value = sbool(value.strip())
	return value is True or (type(value) is int and value == 1)


def _order_value(value):
	"""A typed order value in cents, or None to take the value of the items."""
	if value is None:
		return None
	if not _is_number(value) or value < 0:
		raise InputError(f"The order value has to be a number from 0 to {MAX_NUMBER}.")
	return flt(value, _MONEY)


def _country(value) -> str:
	"""The sample's country as it was typed, for the engine to read loosely (" ae " is AE). Text that could
	not be sent back is no country."""
	return value if _is_text(value) and len(value) <= MAX_TEXT else ""


def _price_source(policy) -> str:
	"""The draft's price source as the engine reads it: "rate_card", or "carrier" for anything else. (A source
	the engine does not know prices nothing, whichever way the order is made.)"""
	return "rate_card" if policy.get("price_source") == "rate_card" else "carrier"


def _may_fall_back_to_the_card(policy) -> bool:
	"""`shipping_policy.fallback_may_use_the_card` for a draft, in which a part may be left out: what is
	missing is read as the engine reads it (a zone that is not enabled is off, no method is no method)."""
	view = {
		"default_fallback": {"method": (policy.get("default_fallback") or {}).get("method")},
		"zones": [
			{
				"enabled": bool(zone.get("enabled")),
				"fallback": {"method": (zone.get("fallback") or {}).get("method")},
			}
			for zone in policy["zones"]
		],
	}
	return fallback_may_use_the_card(view)


def _sample_order(order, ask, city, policy) -> dict:
	"""The sample, cleaned and completed as `build_order` completes an order. For the carrier, with the cartons
	and the carrier's quotes filled in when they are not typed; for the rate card, from the items alone."""
	by_card = _price_source(policy) == "rate_card"
	try:
		order = parse_json_arg(order, None)
		if not isinstance(order, dict):
			raise InputError("The sample order is not in the expected shape.")
		items = clean_items(order.get("items"))
		if not by_card:
			boxes = None if order.get("boxes") is None else clean_boxes(order["boxes"])
			quotes = None if order.get("quotes") is None else clean_quotes(order["quotes"])
			value = _order_value(order.get("order_value"))
	except InputError as error:
		frappe.throw(str(error), frappe.ValidationError)
	country = _country(order.get("country_code"))
	if by_card:
		return build_order(country, items, [], [], item_facts=True)
	cartons = _packed_cartons(items) if boxes is None else [{**box, "size": _cube(box)} for box in boxes]
	if quotes is None:
		quotes = _carrier_quotes(country, city, cartons) if ask else []
	shipped = [{"weight_kg": carton["weight_kg"], "volume_l": carton["volume_l"]} for carton in cartons]
	return build_order(
		country, items, shipped, quotes, order_value=value, item_facts=_may_fall_back_to_the_card(policy)
	)


def _cube(box) -> dict:
	"""The size a typed carton is sent to the carrier with: a cube that holds exactly its volume. The carrier
	works out a volumetric weight from length x width x height, so it sees the volume that was typed."""
	side = (box["volume_l"] * 1000) ** (1 / 3)
	return {"box_code": None, "length_cm": side, "width_cm": side, "height_cm": side}


def _packed_cartons(items) -> list[dict]:
	"""The cartons dsi_shipping packs the items into, the same cartons a real order is rated and booked in
	(its packer reads each item's weight per unit and package size, with its own fallbacks). Each carton is
	{"weight_kg": its gross weight, "volume_l", "size": {"box_code", "length_cm", "width_cm", "height_cm"}}.
	No cartons without items, or on a site without the packer.
	"""
	if not items:
		return []
	try:
		from dsi_shipping.packing import pack_delivery_note
	except ImportError:
		return []
	if sum(item["qty"] for item in items) > MAX_PACKED_UNITS:
		frappe.throw(
			_("Try an order packs at most {0} units into cartons. Type the cartons instead.").format(
				MAX_PACKED_UNITS
			)
		)
	note = frappe._dict(items=[{"item_code": item["item_code"], "qty": item["qty"]} for item in items])
	cartons = []
	for box in pack_delivery_note(note).get("boxes") or []:
		size = box.get("dimensions") or {}
		length, width, height = (flt(size.get(side)) for side in ("length_cm", "width_cm", "height_cm"))
		cartons.append(
			{
				"weight_kg": flt(box.get("gross_weight_kg")),
				"volume_l": length * width * height / 1000,
				"size": {
					"box_code": box.get("box_code"),
					"length_cm": length,
					"width_cm": width,
					"height_cm": height,
				},
			}
		)
	if len(cartons) > MAX_BOXES:
		frappe.throw(_("The items fill more than {0} cartons. Type the cartons instead.").format(MAX_BOXES))
	return cartons


class _CarrierFailed(Exception):
	"""The carrier's answer is not a list of prices."""


def _carrier_quotes(country, city, cartons) -> list[dict]:
	"""The carrier's quotes for the cartons, asked one carton at a time as the website asks: each service's
	prices added up, with the number of cartons it quoted, cheapest first. When the carrier fails in any way
	(an error, an answer that says it failed, or one that is not a list of prices) there are no quotes at
	all, never some of them."""
	code = normalize_country(country)
	if not code:
		frappe.throw(_("Asking the carrier needs a country."))
	place = city.strip() if isinstance(city, str) else ""
	if not place:
		frappe.throw(_("Asking the carrier needs a city."))
	if len(place) > MAX_TEXT or not place.isprintable():
		frappe.throw(_("The city has to be plain text of at most {0} characters.").format(MAX_TEXT))
	if not any(carton["weight_kg"] > 0 for carton in cartons):
		frappe.throw(_("Asking the carrier needs a carton that weighs more than 0 kg."))
	try:
		from dsi_shipping.api import get_shipping_rates
	except ImportError:
		return []
	shown = len(frappe.local.message_log)
	try:
		return _quoted(get_shipping_rates, code, place, cartons)
	except Exception:
		del frappe.local.message_log[shown:]  # the carrier's own message (a SOAP fault, say) is not passed on
		return []


def _quoted(get_shipping_rates, code, city, cartons) -> list[dict]:
	# No postcode is sent: the wizard has none, the carrier's default is an empty one, and erp2's older
	# dsi_shipping takes no such argument at all.
	totals = {}
	for carton in cartons:
		reply = get_shipping_rates(
			country_code=code,
			city=city,
			weight_kg=carton["weight_kg"],
			dimensions=json.dumps(carton["size"]),
			cod_amount=0,
			currency="USD",
		)
		rates = reply.get("rates") if isinstance(reply, dict) and reply.get("success") else None
		if not isinstance(rates, list):
			raise _CarrierFailed
		quoted = set()
		for rate in rates:
			service_id = rate.get("service_id") if isinstance(rate, dict) else None
			cost = rate.get("cost") if isinstance(rate, dict) else None
			if not isinstance(service_id, str) or service_id in quoted or not _is_number(cost) or cost < 0:
				raise _CarrierFailed
			quoted.add(service_id)
			name = rate.get("service_name") or service_id
			total = totals.setdefault(
				service_id, {"service_id": service_id, "name": name, "amount": 0.0, "boxes_quoted": 0}
			)
			total["amount"] += cost
			total["boxes_quoted"] += 1
	# Added up to three decimals, as the website adds them, then cleaned like any quote.
	quotes = [{**total, "amount": flt(total["amount"], 3)} for total in totals.values()]
	return clean_quotes(sorted(quotes, key=lambda quote: quote["amount"]))


def _carrier_doctypes_exist() -> bool:
	found = frappe.get_all("DocType", filters={"name": ["in", list(_CARRIER_DOCTYPES)]}, pluck="name")
	return len(found) == len(_CARRIER_DOCTYPES)


def _carrier_available() -> bool:
	"""Can Try an order ask the carrier? Only where dsi_shipping is installed and an enabled service of an
	enabled provider exists."""
	if not _carrier_doctypes_exist():
		return False
	service, provider = (frappe.qb.DocType(name) for name in _CARRIER_DOCTYPES)
	found = (
		frappe.qb.from_(service)
		.join(provider)
		.on(provider.name == service.shipping_provider)
		.select(service.name)
		.where((service.enabled == 1) & (provider.enabled == 1))
		.limit(1)
		.run()
	)
	return bool(found)


# ---------------------------------------------------------------- Apply
def _refuse(problems):
	"""Refuse the apply with these sentences, escaped: the desk shows them as HTML, and they quote names,
	labels, formulas and codes people typed."""
	frappe.throw([escape_html(problem) for problem in problems], title=_(_CANNOT_APPLY), as_list=True)


def _apply(draft, expected_version) -> dict:
	before = load_policy()
	if expected_version != snapshot_version(before):
		frappe.throw(_(_STALE), frappe.TimestampMismatchError, title=_(_CANNOT_APPLY))
	policy = _read_draft(draft)
	if policy is None:
		_refuse([SHAPE_PROBLEM])
	countries = _country_names()
	if problems := _problems(policy, countries, before):
		_refuse(problems)
	rows, loaded = _rows_and_loaded(policy, countries)
	frappe.db.savepoint(_SAVEPOINT)
	try:
		_write(rows, loaded, before)
		written = load_policy()
		if found := validate_policy(written):
			_refuse(found)
	except BaseException:
		frappe.db.rollback(save_point=_SAVEPOINT)
		raise
	frappe.db.release_savepoint(_SAVEPOINT)
	version = snapshot_version(written)
	_refresh_the_website(version)
	return {"version": version, "changes": diff_policy(before, loaded)}


def _write(rows, loaded, before):
	"""Steps 1 to 5 of `apply_policy`. `rows` are the draft's rows, `loaded` the same draft as the loader will
	read it back, `before` the saved policy."""
	zones_before = {zone["name"]: zone for zone in before["zones"]}
	sets_before = {item_set["name"]: item_set for item_set in before["item_sets"]}
	dropped_zones = sorted(zones_before.keys() - {zone["name"] for zone in loaded["zones"]})
	dropped_sets = sorted(sets_before.keys() - {item_set["name"] for item_set in loaded["item_sets"]})
	for row, item_set in zip(rows["item_sets"], loaded["item_sets"], strict=True):
		_upsert(ITEM_SET, row, item_set, sets_before)
	for name in dropped_zones:
		_switch_off(name)
	for row, zone in zip(rows["zones"], loaded["zones"], strict=True):
		_upsert(ZONE, row, zone, zones_before)
	_save_policy(rows["policy"], loaded, before)
	for name in dropped_zones:
		frappe.delete_doc(ZONE, name)
	for name in dropped_sets:
		frappe.delete_doc(ITEM_SET, name)


def _upsert(doctype, row, entry, saved):
	"""Insert the zone or item set `row`, or update the saved one of the same name. One that would not change
	is left alone. A zone is saved with the flag that leaves its checks across zones to the draft's check."""
	name = entry["name"]
	if name in saved and saved[name] == entry:
		return
	if name in saved:
		doc = frappe.get_doc(doctype, name)
		doc.update(_keeping_row_names(doc, row))
		write = doc.save
	else:
		doc = frappe.get_doc({"doctype": doctype, **row})
		write = doc.insert
	if doctype == ZONE:
		doc.flags.policy_draft_validated = True
	write()


def _keeping_row_names(doc, row) -> dict:
	"""`row` with each child row given the name of a saved row of `doc` that holds the same value, so that the
	document's history shows what changed, not every row removed and added again. Only names of rows the
	document has now are used: Frappe takes a named row for one to update."""
	row = dict(row)
	for table, field in _CHILD_TABLES[doc.doctype].items():
		free = {}
		for child in doc.get(table):
			free.setdefault(child.get(field), []).append(child.name)
		row[table] = [
			{**child, "name": free[child[field]].pop(0)} if free.get(child[field]) else child
			for child in row[table]
		]
	return row


def _switch_off(name):
	doc = frappe.get_doc(ZONE, name)
	if doc.enabled:
		doc.enabled = 0
		doc.flags.policy_draft_validated = True
		doc.save()


def _is_unchanged(key, loaded, before) -> bool:
	"""Is this setting, as the loader will read it back, the one that is saved? A price source that is not
	said (stored empty) is the carrier, as the engine and the diff read it: writing "Carrier" over it is no
	change."""
	if key == "price_source":
		return (loaded[key] or "carrier") == (before[key] or "carrier")
	return loaded[key] == before[key]


def _save_policy(row, loaded, before):
	"""Save the settings (the price source and the rate card among them) and the rules in one save of the
	single (none when none of them changes).

	A rule keeps its row when its name is the name of a saved row (the first rule to claim it); every other
	rule is a new row. Frappe takes a named row for one to update, so a name that is not a saved row's
	would lose the rule.
	"""
	settings = ("active", "price_source", "rate_card", "not_covered", "default_fallback", "rules")
	if all(_is_unchanged(key, loaded, before) for key in settings):
		return
	doc = frappe.get_doc(POLICY)
	free = {rule.name for rule in doc.rules}
	rules = []
	for rule in row["rules"]:
		if rule.get("name") in free:
			free.discard(rule["name"])
			rules.append(rule)
		else:
			rules.append({key: value for key, value in rule.items() if key != "name"})
	doc.update({**row, "rules": rules})
	doc.save()


def _refresh_the_website(version):
	"""Ask the website to refresh the pages that show the policy, through dsi_core's outbox when the site has
	it, the way `api.notify_revalidate` does for products. It never fails the apply: a failure is logged."""
	try:
		from dsi_core import outbox
	except ImportError:
		return
	try:
		outbox.enqueue(
			"website-revalidate",
			{"tags": ["shipping-policy"]},
			dedup_key=f"shipping-policy|{version}",
			source=(POLICY, POLICY),
		)
	except Exception:
		frappe.log_error(title="shipping policy revalidate failed", message=frappe.get_traceback())


# ---------------------------------------------------------------- importing a rate card
def _is_blank(value) -> bool:
	"""No file given: None, or text of nothing but white space (what a page sends for a file it was not given)."""
	return value is None or (isinstance(value, str) and not value.strip())


def _csv_text(value, what) -> str:
	"""The text of a file the page sends. Text is used as it is and bytes are read as UTF-8 (a byte order
	mark is dropped); more than MAX_CSV_CHARACTERS characters, anything that is not text, and text that cannot
	be written as UTF-8 (a lone surrogate, which JSON can carry) are refused in plain words."""
	if isinstance(value, bytes | bytearray):
		if len(value) > MAX_CSV_CHARACTERS * 4:  # UTF-8 takes at most four bytes for a character
			frappe.throw(_("The {0} is longer than {1} characters.").format(what, f"{MAX_CSV_CHARACTERS:,}"))
		try:
			value = bytes(value).decode("utf-8-sig")
		except UnicodeDecodeError:
			frappe.throw(_("The {0} is not UTF-8 text.").format(what))
	if not isinstance(value, str):
		frappe.throw(_("The {0} has to be text.").format(what))
	if len(value) > MAX_CSV_CHARACTERS:
		frappe.throw(_("The {0} is longer than {1} characters.").format(what, f"{MAX_CSV_CHARACTERS:,}"))
	if not _is_text(value):
		frappe.throw(_("The {0} has to be plain text.").format(what))
	return value


def _cut(sentences) -> list[str]:
	"""At most MAX_LISTED sentences, as they are (plain text), and then how many more there are."""
	shown = list(sentences[:MAX_LISTED])
	if len(sentences) > MAX_LISTED:
		shown.append(f"And {len(sentences) - MAX_LISTED} more.")
	return shown


def _imported(card, not_delivered, missing, notes, problems) -> dict:
	return {
		"card": card,
		"not_delivered": not_delivered,
		"not_delivered_missing": missing,
		"summary": card_summary(card),
		"notes": _cut(notes),
		"problems": _cut(problems),
	}


# ---------------------------------------------------------------- the integration test
def _website_address() -> str:
	"""The address of the website as Storefront Settings keeps it ("Current Website"), exactly as stored. It is
	"" when there is none, or when this site has no such settings."""
	try:
		if not frappe.db.exists("DocType", "Storefront Settings"):
			return ""
		address = frappe.get_single("Storefront Settings").active_domain
	except Exception:
		return ""
	return address if isinstance(address, str) else ""


def _website_items() -> list[dict]:
	"""The Website Items as `pick_sample_order` reads them: {"name", "item_code", "index_key", "published"}."""
	rows = frappe.get_all(
		"Website Item", fields=["name", "item_code", "custom_index_key", "published"], order_by="name asc"
	)
	return [
		{
			"name": row["name"],
			"item_code": row["item_code"],
			"index_key": row["custom_index_key"],
			"published": row["published"],
		}
		for row in rows
	]


def _known_country(value, names) -> tuple[str, str]:
	"""(ISO code, Country name) of a country typed as a code or as a name; refused if it is neither."""
	if not isinstance(value, str):
		frappe.throw(_("The country has to be text."))
	code = normalize_country(value)
	if not code:
		code = next(
			(known for known, name in names.items() if name.casefold() == value.strip().casefold()), ""
		)
		if not code:
			frappe.throw(
				_("The country has to be a country code such as GB, or the name of a country of the ERP.")
			)
	elif code not in names:
		frappe.throw(_("The country code {0} is not a country in the ERP.").format(code))
	return code, names[code]


def _integration_sample(order) -> dict:
	"""The order the three checks use: {"country_code", "country", "city", "items", "note"}. What `order` does
	not give (or leaves empty) is the default: New York in the United States and what `pick_sample_order`
	chooses from the Website Items. Refused in plain words, before anything is asked, when `order` cannot be
	read."""
	try:
		given = parse_json_arg(order, None)
		if given is None:
			given = {}
		if not isinstance(given, dict):
			raise InputError("The sample order is not in the expected shape.")
		items = clean_items(given.get("items"))
	except InputError as error:
		frappe.throw(str(error), frappe.ValidationError)
	if len(items) > MAX_SAMPLE_ITEMS:
		frappe.throw(_("The sample order can hold at most {0} items.").format(MAX_SAMPLE_ITEMS))
	home, names = shipping_integration_check.SAMPLE_DESTINATION, _country_names()
	typed = [given[key] for key in ("country_code", "country") if not _is_blank(given.get(key))]
	if typed:
		code, country = _known_country(typed[0], names)
	else:
		code, country = home["country_code"], names.get(home["country_code"], home["country"])
	city = given.get("city")
	if _is_blank(city):
		city = home["city"]
	elif not isinstance(city, str):
		frappe.throw(_("The city has to be text."))
	else:
		city = city.strip()
		if len(city) > MAX_TEXT or not city.isprintable():
			frappe.throw(_("The city has to be plain text of at most {0} characters.").format(MAX_TEXT))
	note = ""
	if not items:
		picked = shipping_integration_check.pick_sample_order(_website_items())
		items, note = clean_items(picked["items"]), picked["note"]
	return {"country_code": code, "country": country, "city": city, "items": items, "note": note}


def _milliseconds(started) -> int:
	return int((time.monotonic() - started) * 1000)


def _no_credentials(request):
	"""An `auth` that adds nothing to the request. Giving `requests` one stops it from looking for a .netrc entry
	for the host, which would send somebody's credentials to an address it was never meant for."""
	return request


def _not_a_number(constant):
	raise ValueError(constant)  # NaN and Infinity are not JSON


def _is_a_read_timeout(error) -> bool:
	"""Is this error the answer having stopped coming? Reading the body of an answer that stalls for longer than
	the timeout, `requests` re-raises urllib3's ReadTimeoutError as a ConnectionError that holds it as its first
	argument (not a Timeout), so the error, what it holds and what it was raised from or while handling are
	looked at, a few steps deep, for a timeout; failing that, whether it says so in words."""
	seen, pending = [], [error]
	while pending and len(seen) < 8:
		current = pending.pop()
		if any(current is known for known in seen):
			continue
		seen.append(current)
		if isinstance(current, urllib3.exceptions.TimeoutError | TimeoutError):
			return True
		pending.extend(
			argument for argument in getattr(current, "args", ()) if isinstance(argument, BaseException)
		)
		pending.extend(link for link in (current.__cause__, current.__context__) if link is not None)
	return "read timed out" in str(error).casefold()


def _read_capped(response, deadline) -> tuple[bytes | None, str | None]:
	"""(the body, problem): at most MAX_BODY_BYTES of it, read before `deadline`. A body over the cap is
	"too_large", one not finished in time or that stalls for longer than the timeout "timeout", one that breaks
	off "unreadable".

	It is read a byte at a time. The timeout of a call only limits each wait for the server, so a server that
	sends a byte every 14 seconds would keep the call for as long as it liked; this way the deadline is looked
	at between the bytes. (A server that sends its headers that slowly still can: that is `requests`.)
	"""
	chunks, size = [], 0
	try:
		for chunk in response.iter_content(chunk_size=1):
			size += len(chunk)
			if size > MAX_BODY_BYTES:
				return None, "too_large"
			chunks.append(chunk)
			if time.monotonic() > deadline:
				return None, "timeout"
	except requests.exceptions.Timeout:
		return None, "timeout"
	except requests.exceptions.RequestException as error:
		return None, "timeout" if _is_a_read_timeout(error) else "unreadable"
	return b"".join(chunks), None


def _post_json(url, payload) -> tuple[int | None, int, object, str | None]:
	"""POST `payload` as JSON to `url` and read the answer: (status, milliseconds, body, problem).

	The call has no cookie and no credential, only a content type for header, follows no redirect, and has
	HTTP_TIMEOUT_SECONDS in all. `body` is the answer parsed as JSON, or None. `problem` says why there is no
	answer to read: "timeout", "unreachable" (no answer at all), "too_large", or "unreadable" (a status 200
	that is not JSON, or an answer that breaks off); any other status without JSON has none, the status says
	what there is to say. The answer is closed as soon as it is read.
	"""
	started = time.monotonic()
	response, status, body, problem = None, None, None, None
	try:
		response = requests.post(
			url,
			data=json.dumps(payload),
			headers={"Content-Type": "application/json"},
			timeout=HTTP_TIMEOUT_SECONDS,
			allow_redirects=False,
			stream=True,
			auth=_no_credentials,
		)
		status = response.status_code
		raw, problem = _read_capped(response, started + HTTP_TIMEOUT_SECONDS)
		if problem is None:
			try:
				body = json.loads(raw.decode("utf-8"), parse_constant=_not_a_number)
			except (ValueError, RecursionError):
				# not UTF-8, not JSON, an integer of over 4,300 digits, or nested too deep
				problem = "unreadable" if status == 200 else None
	except requests.exceptions.Timeout:
		problem = "timeout"
	except requests.exceptions.RequestException:
		problem = "unreachable"
	finally:
		if response is not None:
			response.close()
	return status, _milliseconds(started), body, problem


def _first_amount(answer):
	"""The first offer's amount in an answer of `price_delivery`, or None when it offers nothing."""
	offers = answer.get("offers") or []
	return offers[0].get("amount") if offers else None


def _check_erp(sample) -> dict:
	"""The first check: this site's price API over HTTP against `price_delivery` in this process, for the saved
	policy. With the rate card as the price source the call carries the items alone, like the website's; with
	the carrier, one sample carton and no quotes."""
	arguments = {"country_code": sample["country_code"], "items": json.dumps(sample["items"])}
	if load_policy()["price_source"] != "rate_card":
		arguments.update(boxes=json.dumps([SAMPLE_CARTON]), quotes="[]")
	status, ms, body, problem = _post_json(get_url(allow_header_override=False) + PRICE_API, arguments)
	own = _first_amount(price_delivery(**arguments))  # the same input, answered in this process
	return shipping_integration_check.summarize_policy_response(status, ms, body, own, problem=problem)


def _check_carrier(sample) -> dict:
	"""The second check: the shipping module asked in this process for one sample parcel. Its messages are not
	passed on, and nor is the text of any error it raises."""
	started, shown = time.monotonic(), len(frappe.local.message_log)
	try:
		from dsi_shipping.api import get_shipping_rates

		reply = get_shipping_rates(
			country_code=sample["country_code"],
			city=sample["city"],
			weight_kg=SAMPLE_PARCEL["weight_kg"],
			dimensions=json.dumps(SAMPLE_PARCEL["box"]),
			cod_amount=0,
			currency="USD",
		)
	except Exception as error:
		reply = error
	finally:
		del frappe.local.message_log[shown:]
	return shipping_integration_check.summarize_carrier_response(reply, ms=_milliseconds(started))


def _origin(address) -> str:
	"""The address the guard let through, written the one way any parser reads it: scheme, host and port."""
	parts = urlsplit(address.strip())
	port = f":{parts.port}" if parts.port else ""
	return f"{parts.scheme.lower()}://{(parts.hostname or '').rstrip('.')}{port}"


def _check_website(sample, erp) -> dict:
	"""The third check: the sample cart posted to the rates route of the website in Storefront Settings, when
	that address passes the guard, and its first amount compared with the one the ERP's check found."""
	address = _website_address()
	allowed, reason = shipping_integration_check.check_website_url(address, resolver=_resolve)
	if not allowed:
		return shipping_integration_check.website_not_asked(reason)
	payload = {
		"cartItems": sample["items"],
		"shippingAddress": {"city": sample["city"], "country": sample["country"]},
		"currency": "USD",
	}
	status, ms, body, problem = _post_json(_origin(address) + RATES_ROUTE, payload)
	expected = shipping_integration_check.rate_card_amount(erp)
	return shipping_integration_check.summarize_website_response(status, ms, body, expected, problem=problem)


def _run_check(name, check, *arguments) -> dict:
	"""One check; an unexpected error in it is written to the Error Log and reported without its text (nor is
	any message it left for the desk passed on)."""
	started, shown = time.monotonic(), len(frappe.local.message_log)
	try:
		return check(*arguments)
	except Exception:
		del frappe.local.message_log[shown:]
		try:
			frappe.log_error(title="shipping policy integration test failed", message=frappe.get_traceback())
		except Exception:  # the Error Log failing does not fail the answer
			pass
		return shipping_integration_check.unexpected_failure(name, _milliseconds(started))
