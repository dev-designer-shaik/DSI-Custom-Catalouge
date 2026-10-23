"""Frappe-native tests for the shipping policy wizard's five methods (`dsi_catalogue.shipping_policy_wizard`).

They share the helpers of `test_shipping_policy` (the example policy, seeding and wiping it, real items, the
Guest session, the Error Log guard) and, like those tests, refuse to run on a site that already holds a
shipping policy, because they reset it: run them on erp2, the sandbox.

	bench --site erp.shaik.net run-tests --module dsi_catalogue.dsi_catalogue.doctype.shipping_policy.test_shipping_policy_wizard

Apply is all or nothing. The Apply tests compare a `snapshot()` of everything the policy is stored in (the
rows of the six tables, the single's values, the change stamps, the Version and Deleted Document rows)
before and after a refusal or a failure. The failures are real writes that the real database rolls back:
where a failure is injected, it is injected after the earlier steps have written.

Nothing here commits, and nothing writes to the Error Log (it is a MyISAM table, so a row would stay): the
loggers that a path may reach are mocks, and `error_log_entries` checks that none was written.

The rate card is covered too: `import_rate_card` (the CSV text is embedded below, because the sample files do
not reach erp2), Try an order and Apply with a price source and a card, and `test_integration`, whose HTTP
calls are always mocked (`requests.post` is replaced, the website's address and its name lookup are stand-ins),
so that no test reaches the network.
"""

import ast
import copy
import importlib.util
import inspect
import json
import os
import random
import string
import sys
import types
from contextlib import contextmanager, nullcontext
from unittest import mock

import frappe
import orjson
import requests
from filelock import FileLock
from frappe.handler import execute_cmd
from frappe.model.document import Document
from frappe.utils import flt, get_site_path

from dsi_catalogue import shipping_formula, shipping_policy
from dsi_catalogue import shipping_policy_wizard as wizard
from dsi_catalogue.dsi_catalogue.doctype.shipping_policy.test_shipping_policy import (
	CRASH_BOXES,
	EMPTY_POLICY,
	FORMULA_ERROR,
	FRAGILE,
	ONE_BOX,
	POLICY,
	QUOTE,
	ShippingPolicyTestCase,
	country_name,
	error_log_entries,
	find_items,
	first_amount,
	guest_price,
	load_example,
	one_of,
	policy_of,
	quote_of,
	rule_of,
	seed_policy,
	unserved_code,
	zone_of,
)
from dsi_catalogue.dsi_catalogue.doctype.shipping_policy.test_shipping_policy_ratecard import (
	CARD,
	FRAGRANCE,
	REFILL,
	WATCH,
	card_policy,
	recorded_queries,
	website_items,
	weigh,
)
from dsi_catalogue.shipping_engine import normalize_country, validate_policy
from dsi_catalogue.shipping_policy import load_policy
from dsi_catalogue.shipping_policy_data import response_from, snapshot_version
from dsi_catalogue.shipping_rate_card import RateCardError, parse_shopify_export

# These tests make their own zones and item sets. Without this, Frappe would first create test records for
# everything the doctypes link to, and leave them on the site.
IGNORE_TEST_RECORD_DEPENDENCIES = ["Shipping Zone", "Shipping Item Set"]

METHODS = ("wizard_state", "try_order", "check_formula", "preview_changes", "apply_policy")
RATE_CARD_METHODS = ("import_rate_card", "test_integration")
ALL_METHODS = METHODS + RATE_CARD_METHODS
SHAPE = "The draft is not in the expected shape."
BUSY = "Someone else is applying a change right now. Try again in a moment."
# What `preview_changes` says changes for a draft that is not a policy, and for one that changes nothing.
NO_CHANGES = {
	"zones": {"added": [], "removed": [], "changed": []},
	"item_sets": {"added": [], "removed": [], "changed": []},
	"rules": {"added": [], "removed": [], "changed": [], "order_changed": False},
	"rate_card": {"added": [], "removed": [], "changed": []},
	"settings": [],
}
TABLES = (
	"Shipping Zone",
	"Shipping Zone Country",
	"Shipping Item Set",
	"Shipping Item Set Group",
	"Shipping Item Set Item",
	"Shipping Policy Rule",
)
TWO_BOXES = [ONE_BOX, {"weight_kg": 2.0, "volume_l": 3.0}]
_MISSING = object()

# The rate card. These rows are a handful of the Shopify export, written the way the export writes them: a byte
# order mark, CRLF line ends, a zone name with an ampersand and a comma in it, a name with an accent and an
# apostrophe, an en dash. (The sample files are not on erp2, so the text is embedded.) Parsed, they give four
# profiles in this order: Dangerous Goods Petite, Refill Service, Dangerous Goods, Rest of the World Allowed
# Items; seven zones, eight tiers and nine countries.
HEADER = (
	"Profile,Zone (recommended),Country count,Countries (ISO-2 - Name),Shipping method,"
	"Weight from (lb),Weight to (lb),Price (USD)"
)
EXPRESS_ROW = "International Express (4-7 days)"
CONFIG_ROWS = [
	f"Dangerous Goods,USA,1,US - United States,{EXPRESS_ROW},0.1,0.6,35",
	f"Dangerous Goods,USA,1,US - United States,{EXPRESS_ROW},0.7,2,60",
	'Dangerous Goods,"Europe, UK & Select Asia",3,GB - United Kingdom; FR - France; '
	f"CI - C\u00f4te d'Ivoire,{EXPRESS_ROW},0.01,2,75",
	"Dangerous Goods,Bahrain (Local),1,BH - Bahrain,Free Shipping (1-2 days),,,0",
	f"Rest of the World Allowed Items,Worldwide (Allowed Items),2,AX - \u00c5land Islands; AU - Australia,{EXPRESS_ROW},0.01,2,40",
	"Rest of the World Allowed Items,GCC \u2013 all 6,2,BH - Bahrain; AE - United Arab Emirates,"
	"Free Shipping (1-3 days),,,0",
	"Refill Service,USA,1,US - United States,International Shipping (10-15 days),0.01,2,60",
	"Dangerous Goods Petite,GCC \u2013 KW,1,KW - Kuwait,International Express (1-3 days),,,20",
]


def csv_text(*rows, header=HEADER) -> str:
	"""CSV text as the export writes it: a byte order mark first, CRLF after every line."""
	return "\ufeff" + "\r\n".join([header, *rows]) + "\r\n"


CONFIG_CSV = csv_text(*CONFIG_ROWS)
NOT_DELIVERED_CSV = "\ufeffISO-2,Country\r\nRU,Russia\r\nKP,North Korea\r\n"
IMPORT_KEYS = {"card", "not_delivered", "not_delivered_missing", "summary", "notes", "problems"}
IMPORTED_SUMMARY = {"profiles": 4, "zones": 7, "tiers": 8, "countries": 9}
IMPORTED_PROFILES = [
	"Dangerous Goods Petite",
	"Refill Service",
	"Dangerous Goods",
	"Rest of the World Allowed Items",
]

# The integration test
PRICE_API = "/api/method/dsi_catalogue.shipping_policy.price_delivery"
RATES_ROUTE = "/api/webshop/shipping/rates"
WEBSITE = "https://shop.example.com"
PUBLIC_ADDRESS = "93.184.216.34"
CHECK_NAMES = ("erp_price_api", "carrier", "website")
CHECK_KEYS = {"name", "ok", "status", "ms", "summary", "detail"}
UNSAVABLE = "The rate card cannot be saved as it is (it is nested too deeply or holds values that are not plain data)."


# ---------------------------------------------------------------- helpers
def snapshot() -> dict:
	"""Everything the policy is stored in: compared before and after an Apply that must change nothing."""
	return {
		"policy": load_policy(),
		"rows": {doctype: frappe.db.count(doctype) for doctype in TABLES},
		"stamps": {
			doctype: frappe.get_all(doctype, fields=["name", "modified"], order_by="name asc")
			for doctype in ("Shipping Zone", "Shipping Item Set")
		},
		"rule rows": frappe.get_all(
			"Shipping Policy Rule",
			fields=["name", "idx", "label", "modified"],
			order_by="idx asc",
			parent_doctype=POLICY,
		),
		"single": frappe.db.sql(
			"select field, value from `tabSingles` where doctype = %s order by field", POLICY
		),
		"versions": frappe.db.count("Version", {"ref_doctype": ["like", "Shipping%"]}),
		"deleted": frappe.db.count("Deleted Document", {"deleted_doctype": ["like", "Shipping%"]}),
	}


def state() -> dict:
	return wizard.wizard_state()


def apply(draft, version=None) -> dict:
	"""Apply `draft` over the policy the wizard would load now (or over `version`)."""
	return wizard.apply_policy(draft, state()["version"] if version is None else version)


def messages(caught) -> list:
	"""The sentences of a refusal: the list it was thrown with, or its one sentence."""
	first = caught.exception.args[0] if caught.exception.args else ""
	return first if isinstance(first, list) else [str(first)]


def zone_named(policy, name) -> dict:
	return next(zone for zone in policy["zones"] if zone["name"] == name)


def rule_labelled(policy, label) -> dict:
	return next(rule for rule in policy["rules"] if rule["label"] == label)


def without_rule_names(policy) -> dict:
	"""The policy with its rules' row names blanked and its zones and item sets in name order."""
	policy = copy.deepcopy(policy)
	for rule in policy["rules"]:
		rule["name"] = None
	policy["zones"].sort(key=lambda zone: zone["name"])
	policy["item_sets"].sort(key=lambda item_set: item_set["name"])
	return policy


def sample(country="AE", items=(), boxes=(ONE_BOX,), quotes=(QUOTE,), **more) -> dict:
	"""A Try an order sample in the shape the page sends: one carton and one quote unless told otherwise."""
	order = {"country_code": country, "items": list(items), **more}
	order["boxes"] = None if boxes is None else list(boxes)
	order["quotes"] = None if quotes is None else list(quotes)
	return order


def tried(draft, order, **arguments) -> dict:
	"""`try_order`, and a check that the answer is plain data Frappe can send back."""
	answer = wizard.try_order(draft, order, **arguments)
	orjson.dumps(answer)
	return answer


def make_user(test, *roles) -> str:
	"""A user holding exactly `roles`, deleted when the test ends."""
	email = f"zz-wizard-{frappe.generate_hash(length=8)}@example.com"
	frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": "ZZ wizard test",
			"send_welcome_email": 0,
			"roles": [{"role": role} for role in roles],
		}
	).insert(ignore_permissions=True)
	test.addCleanup(
		frappe.delete_doc, "User", email, force=True, ignore_permissions=True, delete_permanently=True
	)
	return email


@contextmanager
def as_user(user):
	before = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(before)


def dispatched(user, method, verb="POST", **posted):
	"""`method` through Frappe's own request dispatcher: as `user`, over the HTTP verb `verb`, with the arguments
	as a form post sends them (objects as JSON text). This is the layer that checks the method is whitelisted,
	that a guest may call it and which verbs it takes."""
	request = types.SimpleNamespace(
		method=verb, path=f"/api/method/{wizard.__name__}.{method}", referrer=None
	)
	with (
		as_user(user),
		mock.patch.object(frappe.local, "request", request, create=True),
		mock.patch.dict(frappe.local.form_dict, posted),
	):
		return execute_cmd(f"{wizard.__name__}.{method}")


@contextmanager
def recording_history():
	"""Let Frappe keep Version rows. It keeps none while it is testing (`frappe.in_test`), but the history is
	part of what Apply writes, so a test that looks at it, or at it being rolled back, switches that off."""
	with mock.patch.object(frappe, "in_test", False):
		yield


@contextmanager
def modules(replacements: dict):
	"""Put stand-in modules into sys.modules under these names, and put back exactly what was there."""
	saved = {name: sys.modules.get(name, _MISSING) for name in replacements}
	sys.modules.update(replacements)
	try:
		yield
	finally:
		for name, module in saved.items():
			if module is _MISSING:
				sys.modules.pop(name, None)
			else:
				sys.modules[name] = module


@contextmanager
def fake_outbox(enqueue):
	"""`dsi_core.outbox` as a stand-in whose `enqueue` is the given mock: erp2 has no dsi_core."""
	package, outbox = types.ModuleType("dsi_core"), types.ModuleType("dsi_core.outbox")
	outbox.enqueue = enqueue
	package.outbox = outbox
	with modules({"dsi_core": package, "dsi_core.outbox": outbox}):
		yield


@contextmanager
def fake_packer(packed):
	"""`dsi_shipping.packing` as a stand-in whose `pack_delivery_note` returns `packed`: erp2 has no packer."""
	packing = types.ModuleType("dsi_shipping.packing")
	packing.pack_delivery_note = mock.Mock(return_value=packed)
	with modules({"dsi_shipping.packing": packing}):
		yield packing.pack_delivery_note


def carrier(**patch):
	"""Patch the carrier's rate call that Try an order asks: `side_effect=` or `return_value=`."""
	return mock.patch("dsi_shipping.api.get_shipping_rates", **patch)


def rates(*services) -> dict:
	"""A carrier reply quoting (service_id, name, cost) for one carton."""
	return {
		"success": True,
		"rates": [
			{"service_id": service_id, "service_name": name, "cost": cost, "currency": "USD"}
			for service_id, name, cost in services
		],
	}


@contextmanager
def writes():
	"""The saves, inserts and deletes of shipping documents inside the block, in order."""
	done = []
	real_save, real_insert, real_delete = Document.save, Document.insert, frappe.delete_doc

	def save(doc, *args, **kwargs):
		if doc.doctype.startswith("Shipping"):
			done.append(("save", doc.doctype, doc.name))
		return real_save(doc, *args, **kwargs)

	def insert(doc, *args, **kwargs):
		if doc.doctype.startswith("Shipping"):
			done.append(("insert", doc.doctype, doc.get("zone_name") or doc.get("set_name")))
		return real_insert(doc, *args, **kwargs)

	def delete(doctype=None, name=None, *args, **kwargs):
		if str(doctype).startswith("Shipping"):
			done.append(("delete", doctype, name))
		return real_delete(doctype, name, *args, **kwargs)

	with (
		mock.patch.object(Document, "save", save),
		mock.patch.object(Document, "insert", insert),
		mock.patch.object(frappe, "delete_doc", delete),
	):
		yield done


def perfume() -> str:
	[(code, _)] = find_items(watches=False, priced=True)
	return code


def watch() -> str:
	[(code, _)] = find_items(watches=True, priced=True)
	return code


def far_country(policy) -> str:
	"""A country a carrier serves that no zone of `policy` lists: it falls in the Everywhere-else zone."""
	zoned = {code for zone in policy["zones"] for code in zone["countries"]}
	return next(code for code in shipping_policy.served_country_codes() if code not in zoned)


# ---------------------------------------------------------------- the rate card and the integration test
def with_card(policy, card=None, source="rate_card") -> dict:
	"""A draft: `policy` with the price source and the rate card set (the sample card unless another is given)."""
	draft = copy.deepcopy(policy)
	draft["price_source"] = source
	draft["rate_card"] = copy.deepcopy(CARD if card is None else card)
	return draft


def profile_named(card, name) -> dict:
	return next(profile for profile in card["profiles"] if profile["name"] == name)


def imported(config=CONFIG_CSV, not_delivered=NOT_DELIVERED_CSV) -> dict:
	"""`import_rate_card`, and a check that the answer is plain data Frappe can send back."""
	answer = wizard.import_rate_card(config, not_delivered)
	orjson.dumps(answer)
	return answer


def sample_items() -> list[dict]:
	"""The items of the default sample order of `test_integration`: the first published full-size fragrance and
	the first published watch of the site."""
	[(fragrance, _)] = website_items(FRAGRANCE)
	[(timepiece, _)] = website_items(WATCH)
	return [{"item_code": fragrance, "qty": 1.0}, {"item_code": timepiece, "qty": 1.0}]


class Reply:
	"""A reply as the wizard reads one from `requests`: a status, a body that arrives in chunks, and close()."""

	def __init__(self, status=200, body=None, *, raw=None, chunks=None):
		self.status_code = status
		if chunks is None:
			data = raw if raw is not None else json.dumps(body).encode("utf-8")
			chunks = [data[start : start + 8192] for start in range(0, len(data), 8192)]
		self.chunks = chunks
		self.read = 0
		self.closed = False

	def iter_content(self, chunk_size=1):
		for chunk in self.chunks:
			self.read += 1
			yield chunk

	def close(self):
		self.closed = True


def posting(erp=None, site=None):
	"""A stand-in for `requests.post` that answers the ERP's price API with `erp` and the website's rates route
	with `site` (each a Reply, an exception to raise, or a function of the URL and the arguments), and fails the
	test on any other address. `post.calls` lists what was asked: the URL and the arguments."""
	calls = []

	def post(url, **arguments):
		calls.append({"url": url, **arguments})
		if url.endswith(PRICE_API):
			answer = erp
		elif url.endswith(RATES_ROUTE):
			answer = site
		else:
			raise AssertionError(f"a call to {url}")
		if callable(answer):
			answer = answer(url, arguments)
		if isinstance(answer, BaseException):
			raise answer
		return answer

	post.calls = calls
	return post


def lookup_of(*addresses):
	"""A stand-in for the lookup of a name: every name is at these addresses. It notes what it was asked."""

	def lookup(host, port, *rest, **more):
		lookup.asked.append((host, port))
		return [(2, 1, 6, "", (address, port)) for address in addresses]

	lookup.asked = []
	return lookup


public_lookup = lookup_of(PUBLIC_ADDRESS)


def stored(field):
	"""What the Shipping Policy single holds for a field right now, read from its table (through no cache)."""
	rows = frappe.db.sql("select value from `tabSingles` where doctype = %s and field = %s", (POLICY, field))
	return rows[0][0] if rows else None


@contextmanager
def the_network(erp=None, site=None, website=WEBSITE, lookup=public_lookup):
	"""Everything `test_integration` reaches out to, replaced: the HTTP calls, the website's address and the
	lookup of its name. Yields the stand-in for `requests.post`."""
	post = posting(erp, site)
	with (
		mock.patch.object(requests, "post", post),
		mock.patch.object(wizard, "_website_address", return_value=website),
		mock.patch.object(wizard, "_resolve", lookup),
	):
		yield post


def site_reply(amount=100.0, method_id="rate-card", name="International Express", status=200) -> Reply:
	"""The website's shipping rates route answering with one method."""
	method = {
		"id": method_id,
		"provider": "",
		"providerCode": "",
		"serviceName": name,
		"scope": "International",
		"estimatedDays": 7,
		"shippingAmount": amount,
		"currency": "USD",
		"codSupported": False,
		"collectionDate": None,
		"deliveryDate": None,
	}
	package = {"total_units": 2, "total_boxes": 0, "total_weight_kg": 0, "boxes": []}
	body = {
		"success": True,
		"package": package,
		"packingPlan": "rate-card",
		"methods": [method],
		"defaultMethodId": method_id,
	}
	return Reply(status, body)


def erp_reply(country, items, boxes=None, quotes=None) -> Reply:
	"""The ERP's price API answering over HTTP exactly as `price_delivery` answers in this process."""
	answer = shipping_policy.price_delivery(country, json.dumps(items), boxes, quotes)
	return Reply(200, {"message": answer})


# The pools a random edit draws from: countries of the ERP and names for new zones.
POOL_COUNTRIES = ["AE", "BH", "SA", "KW", "OM", "QA", "JO", "LB", "EG", "MA", "FR", "DE", "IN", "JP", "NZ"]
POOL_ZONES = ["Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Sigma", "Tau", "Omega"]
EDITS = (
	"add a zone",
	"drop a zone",
	"rename a zone",
	"move a country",
	"flip a zone flag",
	"list a country twice",
	"repeat a zone name in other capitals",
	"add a rule",
	"drop a rule",
	"reorder",
	"change a rule",
	"give a rule a name that is no row",
	"add an item set",
	"drop an item set",
	"rename an item set",
	"change the settings",
)


def random_edit(rng, draft, item) -> str:
	"""One random edit of `draft`, and its name: what a person does in the wizard, and some of what the checks
	have to refuse (a zone dropped while a rule still uses it, a country in two zones, a formula that cannot be
	used, a name that only changes its capitals, a negative fallback). A random edit that does not fit the draft
	as it is (nothing to drop, no free name left) changes nothing."""
	zones, rules, sets = draft["zones"], draft["rules"], draft["item_sets"]
	zone = rng.choice(zones) if zones else None
	rule = rng.choice(rules) if rules else None
	item_set = rng.choice(sets) if sets else None
	free = [name for name in POOL_ZONES if name not in {entry["name"] for entry in zones}]
	kind = rng.choice(EDITS)
	if kind == "add a zone" and free:
		countries = rng.sample(POOL_COUNTRIES, rng.randint(0, 3))
		fallback = {
			"method": rng.choice(["policy_default", "fixed", "free"]),
			"amount": rng.choice([0.0, 12.5]),
		}
		zones.append(zone_of(rng.choice(free), countries, enabled=rng.random() < 0.9, fallback=fallback))
	elif kind == "drop a zone" and zone:
		zones.remove(zone)
		if rng.random() < 0.8:  # its rules usually go with it
			rules[:] = [entry for entry in rules if entry["zone"] != zone["name"]]
	elif kind == "rename a zone" and zone and free:
		was, zone["name"] = zone["name"], rng.choice(free)
		for entry in rules:  # the rules usually follow
			if entry["zone"] == was and rng.random() < 0.85:
				entry["zone"] = zone["name"]
	elif kind == "move a country" and zone and zone["countries"]:
		rng.choice(zones)["countries"].append(zone["countries"].pop())
	elif kind == "flip a zone flag" and zone:
		flag = rng.choice(["enabled", "everywhere_else", "not_shipped"])
		zone[flag] = not zone[flag]
		if flag == "everywhere_else" and zone[flag] and rng.random() < 0.9:
			zone["countries"] = []
	elif kind == "list a country twice" and zone:
		zone["countries"].append(rng.choice(POOL_COUNTRIES))
	elif kind == "repeat a zone name in other capitals" and zone:
		zones.append(zone_of(zone["name"].swapcase()))
	elif kind == "add a rule" and zone:
		method = rng.choice(["carrier_price", "free", "formula"])
		rule_fields = {
			"label": f"Rule {rng.randint(1, 99)}",
			"item_set": rng.choice([None, *(entry["name"] for entry in sets)]),
			"max_order_value": rng.choice([0.0, 0.0, 500.0, 5.0]),
			"min_order_value": rng.choice([0.0, 0.0, 10.0, 100.0]),  # sometimes above the "up to": refused
			"method": method,
			"formula": rng.choice(["carrier_price * 1.1", "carrier_price ** 2"])
			if method == "formula"
			else "",
			"round_up": rng.random() < 0.5,
		}
		zone_name = zone["name"] if rng.random() < 0.9 else "Nowhere"
		rules.insert(rng.randint(0, len(rules)), rule_of(zone_name, **rule_fields))
	elif kind == "drop a rule" and rule:
		rules.remove(rule)
	elif kind == "reorder":
		rng.shuffle(rules)
		rng.shuffle(zones)
	elif kind == "change a rule" and rule:
		rule.update(label=f"Changed {rng.randint(1, 99)}", round_up=not rule["round_up"])
	elif kind == "give a rule a name that is no row" and rule:
		rule["name"] = rng.choice(["no-such-row", "abcdefghij"])
	elif kind == "add an item set":
		name = rng.choice(["Perfume", "Candles", "Mixed"])
		if name not in {entry["name"] for entry in sets}:
			sets.append({"name": name, "item_groups": rng.choice([[], ["WATCHES"]]), "items": [item]})
	elif kind == "drop an item set" and item_set:
		sets.remove(item_set)
		for entry in rules:
			if entry["item_set"] == item_set["name"] and rng.random() < 0.8:
				entry["item_set"] = None
	elif kind == "rename an item set" and item_set:
		was, item_set["name"] = item_set["name"], rng.choice(["Renamed", "Other"])
		for entry in rules:
			if entry["item_set"] == was and rng.random() < 0.9:
				entry["item_set"] = item_set["name"]
	elif kind == "change the settings":
		draft["active"] = not draft["active"]
		draft["not_covered"] = rng.choice(["pass_through", "no_delivery"])
		draft["default_fallback"] = {
			"method": rng.choice(["no_delivery", "fixed", "free"]),
			"amount": rng.choice([0.0, 20.0, -1.0]),
		}
	return kind


class WizardTestCase(ShippingPolicyTestCase):
	"""The base of these tests: a clean site before and after each (the shared base), and no commit. The class
	rolls back at its end whatever its tests wrote; a commit would keep it, so any commit here fails the test.
	Nor can a test reach the network or write to the Error Log: both would stay whatever a rollback does."""

	def setUp(self):
		super().setUp()
		commit = mock.patch.object(
			frappe.db, "commit", side_effect=AssertionError("a test committed the transaction")
		)
		commit.start()
		self.addCleanup(commit.stop)
		# `requests.post` is replaced in every test that makes a call; this is for the one that forgets, so that
		# nothing here can reach the network (the ERP's own address, a website or anyone else).
		network = mock.patch.object(
			requests.adapters.HTTPAdapter, "send", side_effect=AssertionError("a test reached the network")
		)
		network.start()
		self.addCleanup(network.stop)
		# The Error Log is a MyISAM table: a row written by a test, or by code a test has broken, stays for good.
		# A test that expects a log entry replaces this with a mock of its own.
		logged = mock.patch.object(
			frappe, "log_error", side_effect=AssertionError("a test wrote to the Error Log")
		)
		logged.start()
		self.addCleanup(logged.stop)


# ---------------------------------------------------------------- who may use the wizard
class TestWizardAccess(WizardTestCase):
	def test_non_system_managers_are_refused(self):
		seed_policy(load_example())
		draft = load_policy()
		version = snapshot_version(draft)
		calls = {
			"wizard_state": lambda: wizard.wizard_state(),
			"try_order": lambda: wizard.try_order(draft, sample()),
			"check_formula": lambda: wizard.check_formula("carrier_price * 1.1"),
			"preview_changes": lambda: wizard.preview_changes(draft),
			# The saved policy as it is: nothing but the role check stands between this call and success.
			"apply_policy": lambda: wizard.apply_policy(draft, version),
			"import_rate_card": lambda: wizard.import_rate_card(CONFIG_CSV, NOT_DELIVERED_CSV),
			"test_integration": lambda: wizard.test_integration(),
		}
		self.assertEqual(tuple(calls), ALL_METHODS)
		# A draft that changes something is also refused by the document permissions, further in.
		calls["apply_policy with a change"] = lambda: wizard.apply_policy({**draft, "active": True}, version)
		before = snapshot()
		sales = make_user(self, "Sales User")
		# The integration test reaches out to the network: a refusal has to come before any call.
		with mock.patch.object(
			requests, "post", side_effect=AssertionError("the network was asked")
		) as asked:
			for user in ("Guest", sales):
				for method, call in calls.items():
					with self.subTest(user=user, method=method), as_user(user):
						with self.assertRaises(frappe.PermissionError):
							call()
		asked.assert_not_called()
		self.assertEqual(snapshot(), before, "nothing was written")
		# The control: the same user once it holds the System Manager role.
		frappe.get_doc("User", sales).add_roles("System Manager")
		with as_user(sales):
			self.assertEqual(wizard.wizard_state()["version"], version)
			self.assertTrue(wizard.check_formula("carrier_price * 1.1")["ok"])
			self.assertEqual(
				wizard.import_rate_card(CONFIG_CSV, NOT_DELIVERED_CSV)["summary"], IMPORTED_SUMMARY
			)

	def test_each_method_checks_the_role_before_anything_else(self):
		tree = ast.parse(inspect.getsource(wizard))
		functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
		for method in ALL_METHODS:
			with self.subTest(method=method):
				body = functions[method].body
				if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
					body = body[1:]  # the docstring
				self.assertEqual(ast.unparse(body[0]), "frappe.only_for('System Manager')")

	def test_the_methods_are_whitelisted_for_users_and_none_for_guests(self):
		public = {
			name: function
			for name, function in vars(wizard).items()
			if inspect.isfunction(function)
			and function.__module__ == wizard.__name__
			and not name.startswith("_")
		}
		self.assertEqual({name for name, f in public.items() if f in frappe.whitelisted}, set(ALL_METHODS))
		self.assertEqual({name for name, f in public.items() if f in frappe.guest_methods}, set())
		signatures = {name: str(inspect.signature(getattr(wizard, name))) for name in ALL_METHODS}
		self.assertEqual(
			signatures,
			{
				"wizard_state": "() -> dict",
				"try_order": "(draft, order, ask_carrier=False, city=None) -> dict",
				"check_formula": "(source) -> dict",
				"preview_changes": "(draft) -> dict",
				"apply_policy": "(draft, expected_version) -> dict",
				"import_rate_card": "(config_csv, not_delivered_csv=None) -> dict",
				"test_integration": "(order=None) -> dict",
			},
		)
		# Apply writes, and the integration test makes HTTP calls: neither takes a GET request (a link cannot
		# apply a policy or send the server out to the network).
		allowed = frappe.allowed_http_methods_for_whitelisted_func
		self.assertEqual(allowed[wizard.apply_policy], ["POST"])
		self.assertEqual(allowed[wizard.test_integration], ["POST"])
		self.assertIn("GET", allowed[wizard.wizard_state])

	def test_the_methods_work_through_the_request_dispatcher_with_text_arguments(self):
		seed_policy(load_example())
		saved = wizard.wizard_state()
		draft = json.dumps(saved["policy"])
		posted = {
			"wizard_state": {},
			"try_order": {"draft": draft, "order": json.dumps(sample()), "ask_carrier": "false"},
			"check_formula": {"source": "carrier_price * 1.1"},
			"preview_changes": {"draft": draft},
			"apply_policy": {"draft": draft, "expected_version": saved["version"]},
		}
		self.assertEqual(tuple(posted), METHODS)
		sales = make_user(self, "Sales User")
		before = snapshot()
		for user in ("Guest", sales):
			for method, arguments in posted.items():
				with self.subTest(user=user, method=method), self.assertRaises(frappe.PermissionError):
					dispatched(user, method, **arguments)
		answers = {method: dispatched("Administrator", method, **args) for method, args in posted.items()}
		self.assertEqual(answers["wizard_state"]["version"], saved["version"])
		self.assertEqual(answers["check_formula"], {"ok": True, "error": None})
		self.assertEqual(answers["preview_changes"], {"changes": NO_CHANGES, "problems": []})
		self.assertEqual(first_amount(answers["try_order"]["result"]), 39.0)
		self.assertEqual(answers["apply_policy"], {"version": saved["version"], "changes": NO_CHANGES})
		self.assertEqual(snapshot(), before, "the saved policy was applied as it is: nothing changed")
		# Only Apply writes, and it takes no GET; the readers do.
		with self.assertRaises(frappe.PermissionError):
			dispatched("Administrator", "apply_policy", "GET", **posted["apply_policy"])
		self.assertEqual(dispatched("Administrator", "wizard_state", "GET")["version"], saved["version"])


# ---------------------------------------------------------------- wizard_state
class TestWizardState(WizardTestCase):
	def test_wizard_state_gives_the_saved_policy_its_version_and_the_lists_the_page_needs(self):
		empty = wizard.wizard_state()
		self.assertEqual(empty["policy"], EMPTY_POLICY)
		self.assertEqual(empty["version"], snapshot_version(EMPTY_POLICY))
		seed_policy(load_example(), active=True)
		answer = wizard.wizard_state()
		orjson.dumps(answer)  # plain data, nothing Frappe could not send back
		self.assertEqual(
			set(answer), {"policy", "version", "countries", "served_codes", "price_list", "carrier_available"}
		)
		self.assertEqual(answer["policy"], load_policy())
		self.assertEqual(answer["version"], snapshot_version(load_policy()))
		self.assertNotEqual(answer["version"], empty["version"])
		rows = frappe.get_all("Country", fields=["name", "code"], order_by="name asc")
		expected = [
			{"code": normalize_country(row["code"]), "name": row["name"]}
			for row in rows
			if normalize_country(row["code"])
		]
		self.assertEqual(answer["countries"], expected, "every Country with a code, by name")
		self.assertIn({"code": "AE", "name": country_name("AE")}, answer["countries"])
		self.assertEqual(answer["served_codes"], shipping_policy.served_country_codes())
		self.assertEqual(answer["price_list"], "USD - Online")
		self.assertIs(answer["carrier_available"], True, "erp2 has enabled Aramex services")

	def test_the_carrier_is_available_only_with_an_enabled_service_of_an_enabled_provider(self):
		def available():
			return wizard.wizard_state()["carrier_available"]

		self.assertTrue(available())
		providers = frappe.get_all("Shipping Provider", {"enabled": 1}, pluck="name")
		for name in providers:
			self.addCleanup(frappe.db.set_value, "Shipping Provider", name, "enabled", 1)
			frappe.db.set_value("Shipping Provider", name, "enabled", 0)
		self.assertFalse(available(), "every provider is off")
		frappe.db.set_value("Shipping Provider", providers[0], "enabled", 1)
		services = frappe.get_all(
			"Shipping Service", {"enabled": 1, "shipping_provider": providers[0]}, pluck="name"
		)
		for name in services:
			self.addCleanup(frappe.db.set_value, "Shipping Service", name, "enabled", 1)
			frappe.db.set_value("Shipping Service", name, "enabled", 0)
		self.assertFalse(available(), "the provider is on, its services are off")
		frappe.db.set_value("Shipping Service", services[0], "enabled", 1)
		self.assertTrue(available())
		with mock.patch.object(wizard, "_carrier_doctypes_exist", return_value=False):
			self.assertFalse(available(), "dsi_shipping is not installed")


# ---------------------------------------------------------------- check_formula
class TestCheckFormula(WizardTestCase):
	def test_check_formula_reports_a_plain_error(self):
		for good in ("max(carrier_price * 1.1, 45)", "weight_kg * 6 + 8", "  carrier_price  \n"):
			with self.subTest(good=good):
				self.assertEqual(wizard.check_formula(good), {"ok": True, "error": None})
		bad = [
			("carrier_price ** 2", "That part of the formula is not allowed."),
			("", "The formula is empty."),
			("carrier_price +", "The formula is not written correctly"),
			("__import__('os')", "Unknown function '__import__'."),
			("foo * 2", "Unknown name 'foo'."),
			("10 / (weight_kg - 2.4)", "The formula tries to divide by zero."),  # 0 at the sample weight
			("carrier_price > 5", "A yes/no answer"),
			("1" * 100_000, "The formula is too long or complex."),
			("a\x00b", "The formula is not written correctly"),
			(None, "The formula must be written as text."),
			(5, "The formula must be written as text."),
			(["carrier_price"], "The formula must be written as text."),
			({"formula": "carrier_price"}, "The formula must be written as text."),
		]
		with mock.patch.object(frappe, "log_error") as logged, error_log_entries(FORMULA_ERROR) as new:
			for source, start in bad:
				with self.subTest(source=str(source)[:40]):
					answer = wizard.check_formula(source)
					self.assertEqual(set(answer), {"ok", "error"})
					self.assertIs(answer["ok"], False)
					self.assertTrue(answer["error"].startswith(start), answer["error"])
					self.assertTrue(answer["error"].endswith("."), "a plain sentence")
					self.assertNotIn("Traceback", answer["error"])
			self.assertEqual(new(), [])
		logged.assert_not_called()  # the wizard checks on every keystroke: a bad formula is not an error of ours

	def test_check_formula_agrees_with_the_check_made_when_the_policy_is_saved(self):
		for source in ("carrier_price * 1.1", "carrier_price ** 2", "10 / (weight_kg - 2.4)", "max(1)"):
			with self.subTest(source=source):
				saved = validate_policy(
					policy_of([zone_of("Gulf", ["AE"])], [rule_of("Gulf", method="formula", formula=source)])
				)
				self.assertEqual(wizard.check_formula(source)["ok"], not saved, saved)

	def test_check_formula_never_raises_even_when_the_checker_fails_unexpectedly(self):
		with mock.patch.object(shipping_formula, "check_formula", side_effect=RecursionError("deep")):
			answer = wizard.check_formula("carrier_price")
		self.assertEqual(answer["ok"], False)
		self.assertTrue(answer["error"].endswith("."), answer)


# ---------------------------------------------------------------- try_order
class TestTryOrder(WizardTestCase):
	def test_try_order_prices_the_draft_not_the_saved_policy(self):
		seed_policy(load_example(), active=True)
		item = perfume()
		draft = wizard.wizard_state()["policy"]
		gulf = rule_labelled(draft, "Gulf at the carrier price")
		gulf.update(method="formula", formula="carrier_price + 5", round_up=False)
		before = snapshot()
		answer = tried(draft, sample(items=one_of(item)))
		self.assertEqual(set(answer), {"result", "previews", "order"})
		result = answer["result"]
		self.assertEqual(
			(result["zone"], first_amount(result)), ("GCC", 43.2), "38.20 + 5 by the draft's formula"
		)
		self.assertEqual(
			first_amount(guest_price("AE", items=one_of(item))), 39.0, "the saved policy prices as before"
		)
		self.assertEqual(snapshot(), before, "nothing was written")
		trace = result["offers"][0]["trace"]
		self.assertEqual([step["kind"] for step in trace], ["carrier", "rule", "formula", "result"])
		self.assertEqual(result["rule"], 2, "the third rule of the draft")
		# One preview per rule of the draft: what each would charge this order.
		previews = answer["previews"]
		self.assertEqual([preview["index"] for preview in previews], [0, 1, 2, 3])
		self.assertEqual(set(previews[0]), {"index", "in_zone", "matches", "amount"})
		self.assertEqual(
			[(p["in_zone"], p["matches"], p["amount"]) for p in previews],
			[(False, True, None), (True, False, None), (True, True, 43.2), (False, True, None)],
		)
		# A rule the saved policy does not have at all.
		draft["rules"].insert(0, rule_of("GCC", label="Everything free in the Gulf", method="free"))
		self.assertEqual(first_amount(tried(draft, sample(items=one_of(item)))["result"]), 0.0)
		self.assertEqual(snapshot(), before)

	def test_try_order_prices_an_inactive_draft_as_if_it_were_on(self):
		seed_policy(load_example(), active=False)
		draft = wizard.wizard_state()["policy"]
		self.assertFalse(draft["active"])
		answer = tried(draft, sample(items=one_of(perfume())))
		self.assertEqual((answer["result"]["zone"], first_amount(answer["result"])), ("GCC", 39.0))
		self.assertTrue(
			answer["result"]["policy_active"], "priced as if on; the page reads on and off from the draft"
		)
		self.assertFalse(draft["active"], "the page's draft is not changed")
		self.assertEqual(answer["previews"][2]["amount"], 39.0)
		# A draft nobody has saved yet is priced the same way.
		frappe.db.delete("Shipping Policy Rule", {"parent": POLICY})
		self.assertEqual(first_amount(tried(draft, sample(items=one_of(perfume())))["result"]), 39.0)

	def test_try_order_and_price_delivery_give_the_same_answer_for_the_saved_policy(self):
		seed_policy(load_example(), active=True)
		draft = wizard.wizard_state()["policy"]
		item, timepiece = perfume(), watch()
		far = far_country(draft)
		whole = quote_of(38.2, service_id="whole", name="Whole", boxes_quoted=2)
		short = quote_of(10.0, service_id="short", name="Short", boxes_quoted=1)
		orders = {
			"Gulf at the carrier price": ("AE", one_of(item), [ONE_BOX], [QUOTE]),
			"watches ship free in the Gulf": ("AE", one_of(timepiece), [ONE_BOX], [QUOTE]),
			"a mixed order is not all watches": ("AE", one_of(timepiece) + one_of(item), [ONE_BOX], [QUOTE]),
			"free in Bahrain": ("BH", one_of(item, 3), [ONE_BOX], [QUOTE]),
			"the international minimum": (far, one_of(item), [ONE_BOX], [quote_of(20.0)]),
			"ten percent on top": (far, one_of(item), [ONE_BOX], [quote_of(100.0)]),
			"not shipped": ("RU", one_of(item), [ONE_BOX], [QUOTE]),
			"no quote: the Gulf fallback": ("AE", one_of(item), [ONE_BOX], []),
			"a partial quote is dropped": ("AE", one_of(item), TWO_BOXES, [short, whole]),
			"nobody serves it": (unserved_code(), one_of(item), [ONE_BOX], [QUOTE]),
			"no cartons, a country written loosely": (
				" ae ",
				one_of(item),
				[],
				[quote_of(38.2, boxes_quoted=0)],
			),
		}
		amounts = set()
		for what, (country, items, boxes, quotes) in orders.items():
			with self.subTest(what):
				live = guest_price(country, items=items, boxes=boxes, quotes=quotes)
				answer = tried(draft, sample(country, items, boxes, quotes))
				self.assertEqual(response_from(answer["result"], lambda text: text), live)
				amounts |= {offer["amount"] for offer in live["offers"]}
		self.assertGreaterEqual(len(amounts), 5, f"at least five different amounts: {sorted(amounts)}")

	def test_try_order_fills_the_order_from_the_erp_unless_it_is_typed(self):
		seed_policy(load_example(), active=True)
		draft = wizard.wizard_state()["policy"]
		[(item, price)] = find_items(watches=False, priced=True)
		[(timepiece, watch_price)] = find_items(watches=True, priced=True)
		items = one_of(item, 2) + one_of(timepiece)
		order = tried(draft, sample(" ae ", items))["order"]
		self.assertEqual(set(order), {"country_code", "items", "order_value", "boxes", "quotes", "served"})
		self.assertEqual(order["country_code"], " ae ", "as it was typed: the engine reads it")
		self.assertEqual(order["order_value"], flt(2 * price + watch_price, 2), "from USD - Online")
		self.assertEqual([line["qty"] for line in order["items"]], [2.0, 1.0])
		self.assertIn("WATCHES", order["items"][1]["groups"])
		self.assertEqual(order["items"][1]["groups"][-1], "All Item Groups")
		self.assertEqual((order["boxes"], order["served"]), ([ONE_BOX], True))
		self.assertEqual(order["quotes"], [QUOTE])
		for typed, expected in ((0, 0.0), (0.0, 0.0), (125.5, 125.5), (499.999, 500.0)):
			with self.subTest(order_value=typed):
				typed_order = tried(draft, sample(items=items, order_value=typed))["order"]
				self.assertEqual(typed_order["order_value"], expected, "a typed value wins, in cents")
		# Typed cartons win over packed ones, and typed quotes over the carrier's: neither is asked.
		with (
			fake_packer({"boxes": []}) as packer,
			carrier(return_value=rates(("A", "Express", 10.0))) as asked,
		):
			answer = tried(draft, sample(items=items), ask_carrier=True, city="Dubai")
		packer.assert_not_called()
		asked.assert_not_called()
		self.assertEqual((answer["order"]["boxes"], answer["order"]["quotes"]), ([ONE_BOX], [QUOTE]))
		# A typed empty list is typed too: no quotes, the carrier is not asked, the fallback prices it.
		with carrier(return_value=rates(("A", "Express", 10.0))) as asked:
			answer = tried(draft, sample(items=items, quotes=[]), ask_carrier=True, city="Dubai")
		asked.assert_not_called()
		self.assertEqual(
			(answer["result"]["reason"], first_amount(answer["result"])), ("fallback_offer", 35.0)
		)
		# No quotes typed and nobody asked: nothing to price, so the fallback.
		self.assertEqual(tried(draft, sample(quotes=None))["order"]["quotes"], [])

	def test_try_order_packs_the_items_when_no_cartons_are_typed(self):
		seed_policy(load_example(), active=True)
		draft = wizard.wizard_state()["policy"]
		item, timepiece = perfume(), watch()
		items = one_of(item, 2) + one_of(timepiece)
		packed = {
			"boxes": [
				{
					"box_code": "BOX_M",
					"gross_weight_kg": 1.25,
					"dimensions": {"length_cm": 30, "width_cm": 20, "height_cm": 12},
				},
				{
					"box_code": "BOX_S",
					"gross_weight_kg": 0.55,
					"dimensions": {"length_cm": 20, "width_cm": 15, "height_cm": 10},
				},
			]
		}
		with fake_packer(packed) as packer:
			answer = tried(draft, sample(items=items, boxes=None))
		[(note,), _] = packer.call_args
		self.assertIsInstance(note, frappe._dict)
		self.assertEqual(
			note["items"], [{"item_code": item, "qty": 2.0}, {"item_code": timepiece, "qty": 1.0}]
		)
		self.assertEqual(
			answer["order"]["boxes"],
			[{"weight_kg": 1.25, "volume_l": 7.2}, {"weight_kg": 0.55, "volume_l": 3.0}],
			"each carton's gross weight, and its volume in litres",
		)
		# Asking the carrier rates the same cartons, each with its box code and size.
		asked = []

		def reply(**arguments):
			asked.append(arguments)
			return rates(("A", "Express", 10.0))

		with fake_packer(packed), carrier(side_effect=reply):
			answer = tried(
				draft, sample(items=items, boxes=None, quotes=None), ask_carrier=True, city=" Dubai "
			)
		self.assertEqual(
			[json.loads(call["dimensions"]) for call in asked],
			[
				{"box_code": "BOX_M", "length_cm": 30, "width_cm": 20, "height_cm": 12},
				{"box_code": "BOX_S", "length_cm": 20, "width_cm": 15, "height_cm": 10},
			],
		)
		self.assertEqual([call["weight_kg"] for call in asked], [1.25, 0.55])
		for call in asked:
			self.assertEqual(
				{key: call[key] for key in ("country_code", "city", "cod_amount", "currency")},
				{"country_code": "AE", "city": "Dubai", "cod_amount": 0, "currency": "USD"},
			)
		self.assertEqual(
			answer["order"]["quotes"],
			[{"service_id": "A", "name": "Express", "amount": 20.0, "boxes_quoted": 2}],
		)
		# Without a packer (erp2 has none), or without items, there are no cartons.
		if importlib.util.find_spec("dsi_shipping.packing") is None:
			self.assertEqual(tried(draft, sample(items=items, boxes=None))["order"]["boxes"], [])
		with fake_packer(packed) as packer:
			self.assertEqual(tried(draft, sample(items=[], boxes=None))["order"]["boxes"], [])
		packer.assert_not_called()
		with modules({"dsi_shipping.packing": None}):  # not importable, whatever is installed
			self.assertEqual(tried(draft, sample(items=items, boxes=None))["order"]["boxes"], [])

	def test_try_order_packs_a_bounded_order_only(self):
		seed_policy(load_example(), active=True)
		draft = wizard.wizard_state()["policy"]
		carton = {
			"box_code": "BOX_S",
			"gross_weight_kg": 0.5,
			"dimensions": {"length_cm": 1, "width_cm": 1, "height_cm": 1},
		}
		with fake_packer({"boxes": [carton] * 3}) as packer:
			with self.assertRaises(frappe.ValidationError) as caught:
				wizard.try_order(draft, sample(items=one_of(perfume(), 1001), boxes=None))
		packer.assert_not_called()
		self.assertTrue(messages(caught)[0].endswith("."), messages(caught))
		with fake_packer({"boxes": [carton] * 51}), self.assertRaises(frappe.ValidationError) as caught:
			wizard.try_order(draft, sample(items=one_of(perfume()), boxes=None))
		self.assertIn("50", messages(caught)[0])

	def test_try_order_asks_the_carrier_for_each_carton_and_adds_up_each_service(self):
		if not frappe.conf.get("shipping_mock_mode"):
			self.skipTest("the carrier is not in mock mode on this site, and a test must not ask Aramex")
		from dsi_shipping.api import get_shipping_rates

		seed_policy(load_example(), active=True)
		draft = wizard.wizard_state()["policy"]
		cartons = [{"weight_kg": 1.5, "volume_l": 1.0}, {"weight_kg": 2.0, "volume_l": 1.0}]
		expected = {}
		for carton in cartons:
			reply = get_shipping_rates(
				country_code="AE",
				city="Dubai",
				weight_kg=carton["weight_kg"],
				dimensions="{}",
				cod_amount=0,
				currency="USD",
			)
			for rate in reply["rates"]:
				entry = expected.setdefault(
					rate["service_id"],
					{
						"service_id": rate["service_id"],
						"name": rate["service_name"],
						"amount": 0.0,
						"boxes_quoted": 0,
					},
				)
				entry["amount"] += rate["cost"]
				entry["boxes_quoted"] += 1
		quotes = sorted(
			({**entry, "amount": flt(entry["amount"], 3)} for entry in expected.values()),
			key=lambda quote: quote["amount"],
		)
		self.assertTrue(quotes, "erp2's Aramex GCC service quotes the Emirates")
		answer = tried(
			draft, sample(items=one_of(perfume()), boxes=cartons, quotes=None), ask_carrier=True, city="Dubai"
		)
		self.assertEqual(answer["order"]["quotes"], quotes)
		self.assertTrue(all(quote["boxes_quoted"] == 2 for quote in quotes))
		typed = tried(draft, sample(items=one_of(perfume()), boxes=cartons, quotes=quotes))
		self.assertEqual(answer["result"], typed["result"], "priced as if those quotes were typed")
		# A typed carton is sent with a size that holds its volume, so the carrier sees its volumetric weight.
		asked = []

		def reply(**arguments):
			asked.append(arguments)
			return rates(("A", "Express", 10.0))

		with carrier(side_effect=reply):
			tried(
				draft,
				sample(boxes=[{"weight_kg": 0.5, "volume_l": 6.0}], quotes=None),
				ask_carrier=True,
				city="Dubai",
			)
		[sent] = [json.loads(call["dimensions"]) for call in asked]
		self.assertIsNone(sent["box_code"])
		self.assertAlmostEqual(sent["length_cm"] * sent["width_cm"] * sent["height_cm"], 6000.0, places=6)
		self.assertNotIn(
			"postcode", asked[0], "erp2's dsi_shipping takes no postcode; the live one's default is empty"
		)

	def test_try_order_carrier_error_means_no_quotes(self):
		seed_policy(load_example(), active=True)
		draft = wizard.wizard_state()["policy"]

		def throws(**arguments):
			frappe.throw("Aramex SOAP HTTP 500 at /RateCalculator: <soap:Fault>secret</soap:Fault>")

		good = rates(("A", "Express", 10.0))
		failures = {
			"it raises": {"side_effect": RuntimeError("connection reset")},
			"it throws a message": {"side_effect": throws},
			"it says it failed": {"return_value": {"success": False, "rates": []}},
			"it answers nonsense": {"return_value": "nope"},
			"a rate has no price": {"return_value": {"success": True, "rates": [{"service_id": "A"}]}},
			"a price below zero": {"return_value": rates(("A", "Express", -1.0))},
			"a price below zero for one carton only": {
				"side_effect": [rates(("A", "Express", 10.0)), rates(("A", "Express", -1.0))]
			},
			"a service quoted twice for one carton": {
				"return_value": rates(("A", "E", 1.0), ("A", "E", 2.0))
			},
			"only the second carton fails": {"side_effect": [good, RuntimeError("timeout")]},
			"only the second carton is refused": {"side_effect": [good, {"success": False, "rates": []}]},
		}
		with mock.patch.object(frappe, "log_error") as logged:
			for what, behaviour in failures.items():
				with self.subTest(what), carrier(**behaviour):
					shown = len(frappe.local.message_log)
					answer = tried(
						draft,
						sample(items=one_of(perfume()), boxes=TWO_BOXES, quotes=None),
						ask_carrier=True,
						city="Dubai",
					)
					self.assertEqual(answer["order"]["quotes"], [], "no quotes at all, never a part of them")
					self.assertEqual(
						(answer["result"]["reason"], first_amount(answer["result"])), ("fallback_offer", 35.0)
					)
					self.assertEqual(
						len(frappe.local.message_log), shown, "the carrier's own message is not passed on"
					)
		logged.assert_not_called()

	def test_asking_the_carrier_needs_a_city_a_country_and_a_carton_with_weight(self):
		seed_policy(load_example(), active=True)
		draft = wizard.wizard_state()["policy"]
		refused = {
			"no city": (sample(quotes=None), None),
			"a blank city": (sample(quotes=None), "   "),
			"a city that is not text": (sample(quotes=None), ["Dubai"]),
			"a city that is too long": (sample(quotes=None), "D" * 141),
			"a city with a control character": (sample(quotes=None), "Dub\x00ai"),
			"no country": (sample("", quotes=None), "Dubai"),
			"a country that is not two letters": (sample("UAE", quotes=None), "Dubai"),
			"no carton": (sample(boxes=[], quotes=None), "Dubai"),
			"a carton that weighs nothing": (
				sample(boxes=[{"weight_kg": 0, "volume_l": 1.0}], quotes=None),
				"Dubai",
			),
		}
		with carrier(return_value=rates(("A", "Express", 10.0))) as asked:
			for what, (order, city) in refused.items():
				with self.subTest(what), self.assertRaises(frappe.ValidationError) as caught:
					wizard.try_order(draft, order, ask_carrier=True, city=city)
				[message] = messages(caught)
				self.assertTrue(message.endswith("."), message)
				self.assertNotIn("Dub\x00ai", message)
			# Typed quotes need none of it: the carrier is not asked.
			self.assertEqual(first_amount(tried(draft, sample(boxes=[]), ask_carrier=True)["result"]), 39.0)
		asked.assert_not_called()
		# "Ask the carrier" as a form post sends it: only a yes asks.
		for no in (False, 0, "0", "false", "", None, "no"):
			with self.subTest(ask_carrier=no), carrier(return_value=rates(("A", "Express", 10.0))) as asked:
				self.assertEqual(tried(draft, sample(quotes=None), ask_carrier=no)["order"]["quotes"], [])
				asked.assert_not_called()
		for yes in (True, 1, "1", "true", "True"):
			with self.subTest(ask_carrier=yes), carrier(return_value=rates(("A", "Express", 10.0))) as asked:
				answer = tried(draft, sample(quotes=None), ask_carrier=yes, city="Dubai")
				self.assertEqual([quote["service_id"] for quote in answer["order"]["quotes"]], ["A"])
				asked.assert_called_once()  # one carton, one question

	def test_try_order_refuses_bad_input_with_plain_sentences(self):
		seed_policy(load_example(), active=True)
		draft = wizard.wizard_state()["policy"]
		refused = {
			"a draft that is not a policy": (5, sample()),
			"a draft that is not JSON": ("{", sample()),
			"a draft with rules that are not a list": ({**draft, "rules": {}}, sample()),
			"an order that is not an object": (draft, ["AE"]),
			"an order that is not JSON": (draft, "{"),
			"a negative quantity": (draft, sample(items=[{"item_code": "X", "qty": -1}])),
			"a carton with no volume": (draft, sample(boxes=[{"weight_kg": 1.0}])),
			"a quote below zero": (draft, sample(quotes=[quote_of(-1.0)])),
			"an order value below zero": (draft, sample(order_value=-1)),
			"an order value as text": (draft, sample(order_value="250")),
			"an order value that is yes": (draft, sample(order_value=True)),
			"an order value over a billion": (draft, sample(order_value=1e10)),
			"an order value that is not a number": (draft, sample(order_value=float("nan"))),
			"an item code that is not plain text": (
				draft,
				sample(items=[{"item_code": "A\ud800", "qty": 1}]),
			),
		}
		for what, (bad_draft, order) in refused.items():
			with self.subTest(what), self.assertRaises(frappe.ValidationError) as caught:
				wizard.try_order(bad_draft, order)
			[message] = messages(caught)
			self.assertTrue(message.endswith("."), message)
			self.assertNotIn("Traceback", message)
		with self.assertRaises(frappe.ValidationError) as caught:
			wizard.try_order({"zones": []}, sample())
		self.assertEqual(messages(caught), [SHAPE])
		# Both may come as JSON text, as a form post sends them.
		order = sample(items=one_of(perfume()))
		self.assertEqual(tried(json.dumps(draft), json.dumps(order)), tried(draft, order))
		# A country that cannot be sent back is no country: nobody serves it.
		answer = tried(draft, sample("A\ud800"))
		self.assertEqual((answer["result"]["reason"], answer["order"]["country_code"]), ("not_served", ""))

	def test_previews_of_a_country_nobody_serves_have_no_amount(self):
		far = unserved_code()
		seed_policy(
			policy_of([zone_of("Far", [far])], [rule_of("Far"), rule_of("Far", method="free")]), active=True
		)
		draft = wizard.wizard_state()["policy"]
		answer = tried(draft, sample(far))
		self.assertEqual((answer["result"]["reason"], answer["result"]["offers"]), ("not_served", []))
		self.assertFalse(answer["order"]["served"])
		self.assertEqual([preview["amount"] for preview in answer["previews"]], [None, None])
		self.assertEqual([preview["in_zone"] for preview in answer["previews"]], [True, True])

	def test_the_wizard_never_logs_a_formula_error(self):
		seed_policy(
			policy_of(
				[zone_of("Gulf", ["AE"], fallback={"method": "fixed", "amount": 35.0})],
				[rule_of("Gulf", method="formula", formula=FRAGILE)],
			),
			active=True,
		)
		draft = wizard.wizard_state()["policy"]
		with mock.patch.object(frappe, "log_error") as logged, error_log_entries(FORMULA_ERROR) as new:
			answer = tried(draft, sample(boxes=CRASH_BOXES))
			self.assertEqual(
				(answer["result"]["reason"], first_amount(answer["result"])), ("formula_error", 35.0)
			)
			self.assertEqual(answer["previews"][0]["amount"], None)
			self.assertEqual(new(), [], "a draft is priced on every keystroke: its failures are not logged")
		logged.assert_not_called()
		used = {
			node.attr
			for node in ast.walk(ast.parse(inspect.getsource(wizard)))
			if isinstance(node, ast.Attribute)
		}
		self.assertNotIn("_log_formula_error", used)


# ---------------------------------------------------------------- preview_changes
class TestPreviewChanges(WizardTestCase):
	def test_preview_changes_reports_the_diff_and_the_problems(self):
		seed_policy(load_example(), active=False)
		saved = load_policy()
		names = {rule["label"]: rule["name"] for rule in saved["rules"]}
		draft = wizard.wizard_state()["policy"]
		draft["active"] = True
		draft["zones"].append(zone_of("Levant", ["JO", "LB"]))
		draft["item_sets"] = []
		draft["rules"] = [rule for rule in draft["rules"] if rule["item_set"] is None]
		rule_labelled(draft, "Gulf at the carrier price")["round_up"] = False
		draft["rules"].append(rule_of("Levant", label="Levant at the carrier price"))
		before = snapshot()
		preview = wizard.preview_changes(draft)
		orjson.dumps(preview)
		self.assertEqual(set(preview), {"changes", "problems"})
		self.assertEqual(preview["problems"], [])
		self.assertEqual(
			preview["changes"],
			{
				"zones": {"added": ["Levant"], "removed": [], "changed": []},
				"item_sets": {"added": [], "removed": ["Watches"], "changed": []},
				"rules": {
					"added": [
						{"index": 3, "name": None, "label": "Levant at the carrier price", "zone": "Levant"}
					],
					"removed": [
						{
							"index": 1,
							"name": names["Free watches in the Gulf"],
							"label": "Free watches in the Gulf",
							"zone": "GCC",
						}
					],
					"changed": [
						{
							"index": 1,
							"name": names["Gulf at the carrier price"],
							"label": "Gulf at the carrier price",
							"zone": "GCC",
						}
					],
					"order_changed": False,
				},
				"rate_card": {"added": [], "removed": [], "changed": []},
				"settings": ["active"],
			},
		)
		self.assertEqual(snapshot(), before, "nothing was written")
		# What stops it from being applied: the engine's sentences and the countries the ERP does not know.
		zz = "ZZ"
		self.assertFalse(frappe.db.exists("Country", {"code": zz.lower()}))
		draft["zones"].append(zone_of("GCC", ["IQ"]))
		zone_named(draft, "Levant")["countries"].append(zz)
		draft["rules"].append(
			rule_of("Levant", label="Squared", method="formula", formula="carrier_price ** 2")
		)
		problems = wizard.preview_changes(draft)["problems"]
		self.assertEqual(
			problems,
			[
				'More than one zone is called "GCC". Zone names have to be different.',
				f"Rule 5 has a formula that cannot be used. {wizard.check_formula('carrier_price ** 2')['error']}",
				"Country code ZZ is not a country in the ERP.",
			],
		)
		self.assertEqual(snapshot(), before)
		# A draft that changes nothing.
		self.assertEqual(
			wizard.preview_changes(wizard.wizard_state()["policy"]), {"changes": NO_CHANGES, "problems": []}
		)
		# The draft may come as JSON text.
		self.assertEqual(wizard.preview_changes(json.dumps(draft)), wizard.preview_changes(draft))

	def test_preview_changes_never_raises_on_a_malformed_draft(self):
		seed_policy(load_example())
		draft = wizard.wizard_state()["policy"]
		zone, rule = draft["zones"][0], draft["rules"][0]
		malformed = {
			"nothing": None,
			"a number": 5,
			"text that is not JSON": "{",
			"JSON of a list": "[1, 2]",
			"a list": [draft],
			"no zones at all": {key: value for key, value in draft.items() if key != "zones"},
			"zones that are text": {**draft, "zones": "GCC"},
			"rules that are an object": {**draft, "rules": {}},
			"item sets that are None": {**draft, "item_sets": None},
			"a default fallback that is text": {**draft, "default_fallback": "free"},
			"active that is text": {**draft, "active": "yes"},
			"not covered that is a number": {**draft, "not_covered": 1},
			"a zone that is a number": {**draft, "zones": [5]},
			"countries that are text": {**draft, "zones": [{**zone, "countries": "BH"}]},
			"a country that is a number": {**draft, "zones": [{**zone, "countries": [48]}]},
			"a zone flag that is text": {**draft, "zones": [{**zone, "enabled": "no"}]},
			"a fallback amount that is text": {
				**draft,
				"zones": [{**zone, "fallback": {"method": "fixed", "amount": "35"}}],
			},
			"a fallback amount that is not a number": {
				**draft,
				"zones": [{**zone, "fallback": {"method": "free", "amount": float("nan")}}],
			},
			"a name nobody can write": {**draft, "zones": [{**zone, "name": "\ud800"}]},
			"a rule that is a list": {**draft, "rules": [[rule]]},
			"a rule flag that is a number": {**draft, "rules": [{**rule, "enabled": 1}]},
			"a limit that is text": {**draft, "rules": [{**rule, "max_weight_kg": "5"}]},
			"a limit too big for a float": {**draft, "rules": [{**rule, "max_weight_kg": 10**400}]},
			"a limit over a billion": {**draft, "rules": [{**rule, "max_order_value": 1e10}]},
			"a limit that is yes": {**draft, "rules": [{**rule, "min_volume_l": True}]},
			"a rule name that is a number": {**draft, "rules": [{**rule, "name": 7}]},
			"item groups that are text": {
				**draft,
				"item_sets": [{"name": "Watches", "item_groups": "WATCHES", "items": []}],
			},
		}
		for what, bad in malformed.items():
			with self.subTest(what):
				answer = wizard.preview_changes(bad)
				self.assertEqual(answer, {"changes": NO_CHANGES, "problems": [SHAPE]})
				orjson.dumps(answer)
		# A part that is missing or None inside a zone, item set or rule reads as empty: that is not malformed.
		loose = {
			**draft,
			"zones": [{"name": zone["name"], "countries": zone["countries"]}, *draft["zones"][1:]],
		}
		self.assertNotEqual(wizard.preview_changes(loose)["problems"], [SHAPE])

	def test_a_draft_that_leaves_out_what_is_empty_reads_like_one_that_spells_it_out(self):
		seed_policy(load_example())
		before = snapshot()
		draft = wizard.wizard_state()["policy"]
		bahrain, free = zone_named(draft, "Bahrain"), rule_labelled(draft, "Free in Bahrain")
		loose_zone = {
			"name": "Bahrain",
			"enabled": True,
			"countries": ["BH"],
			"customer_line": bahrain["customer_line"],
			"fallback": bahrain["fallback"],
		}
		loose_rule = {
			"name": free["name"],
			"enabled": True,
			"label": "Free in Bahrain",
			"zone": "Bahrain",
			"method": "free",
		}
		loose = {
			**draft,
			"zones": [loose_zone if zone is bahrain else zone for zone in draft["zones"]],
			"item_sets": [{"name": "Watches", "item_groups": draft["item_sets"][0]["item_groups"]}],
			"rules": [loose_rule if rule is free else rule for rule in draft["rules"]],
		}
		self.assertEqual(wizard.preview_changes(loose), {"changes": NO_CHANGES, "problems": []})
		self.assertEqual(apply(loose)["changes"], NO_CHANGES)
		self.assertEqual(snapshot(), before, "nothing differs, so nothing is written")

	def test_drafts_are_rounded_to_the_stored_precision_before_they_are_checked(self):
		for doctype, fieldname, places in (
			("Shipping Zone", "fallback_amount", 2),
			("Shipping Policy", "default_fallback_amount", 2),
			("Shipping Policy Rule", "min_order_value", 2),
			("Shipping Policy Rule", "max_order_value", 2),
			("Shipping Policy Rule", "min_weight_kg", 3),
			("Shipping Policy Rule", "max_weight_kg", 3),
			("Shipping Policy Rule", "min_volume_l", 3),
			("Shipping Policy Rule", "max_volume_l", 3),
		):
			with self.subTest(field=fieldname):
				self.assertEqual(int(frappe.get_meta(doctype).get_field(fieldname).precision), places)
		seed_policy(load_example())
		draft = wizard.wizard_state()["policy"]
		zone_named(draft, "GCC")["fallback"]["amount"] = 35.004  # 35.00 once stored: no change
		self.assertEqual(wizard.preview_changes(draft), {"changes": NO_CHANGES, "problems": []})
		gulf = rule_labelled(draft, "Gulf at the carrier price")
		gulf.update(min_weight_kg=2.0001, max_weight_kg=2.0004)  # both 2.000 once stored: an empty band
		preview = wizard.preview_changes(draft)
		self.assertEqual(
			preview["problems"],
			[
				'Rule 3 has its limits on weight the wrong way round: from 2 up to 2. "Up to" has to be higher '
				'than "from".'
			],
		)
		gulf.update(
			min_weight_kg=2.0004, max_weight_kg=5.0006, min_order_value=99.999, max_order_value=500.004
		)
		draft["default_fallback"] = {"method": "fixed", "amount": 12.345}
		applied = apply(draft)
		saved = rule_labelled(load_policy(), "Gulf at the carrier price")
		self.assertEqual(
			[saved[key] for key in ("min_weight_kg", "max_weight_kg", "min_order_value", "max_order_value")],
			[2.0, 5.001, 100.0, 500.0],
		)
		self.assertEqual(zone_named(load_policy(), "GCC")["fallback"]["amount"], 35.0)
		self.assertEqual(load_policy()["default_fallback"], {"method": "fixed", "amount": flt(12.345, 2)})
		self.assertEqual(applied["changes"]["zones"]["changed"], [], "rounding is not a change")
		self.assertEqual(applied["version"], wizard.wizard_state()["version"])

	def test_an_unknown_country_code_is_refused_and_nothing_is_written(self):
		seed_policy(load_example())
		before = snapshot()
		draft = wizard.wizard_state()["policy"]
		self.assertFalse(frappe.db.exists("Country", {"code": "zz"}))
		zone_named(draft, "GCC")["countries"] += [" zz ", "ZZ"]
		sentence = "Country code ZZ is not a country in the ERP."
		self.assertEqual(wizard.preview_changes(draft)["problems"], [sentence])
		with self.assertRaises(frappe.ValidationError) as caught:
			apply(draft)
		self.assertEqual(messages(caught), [sentence])
		self.assertEqual(snapshot(), before)
		# A code that is not two letters gets the engine's sentence, not this one as well.
		zone_named(draft, "GCC")["countries"] = ["AE", "<b>"]
		with self.assertRaises(frappe.ValidationError) as caught:
			apply(draft)
		self.assertEqual(
			messages(caught),
			["Zone &quot;GCC&quot; lists &quot;&lt;b&gt;&quot;, which is not a two-letter country code."],
			"the sentence quotes what was typed, escaped",
		)
		self.assertEqual(snapshot(), before)

	def test_a_country_that_is_not_two_plain_letters_is_never_turned_into_one(self):
		seed_policy(load_example())
		before = snapshot()
		draft = wizard.wizard_state()["policy"]
		# Upper-casing would turn "ß" into the code SS (South Sudan) and "ﬁ" into FI (Finland).
		self.assertEqual(("ß".upper(), "ﬁ".upper()), ("SS", "FI"))
		for typed in ("ß", "ﬁ", "\u0131", "A E", "<b>"):  # \u0131 is the dotless i, which upper-cases to I
			zone_named(draft, "GCC")["countries"] = ["AE", typed]
			sentence = f'Zone "GCC" lists "{typed}", which is not a two-letter country code.'
			with self.subTest(typed=typed):
				self.assertEqual(wizard.preview_changes(draft)["problems"], [sentence])
				with self.assertRaises(frappe.ValidationError) as caught:
					apply(draft)
				self.assertEqual(messages(caught), [frappe.utils.escape_html(sentence)])
		self.assertEqual(snapshot(), before)
		# A code written loosely is read as the code it is.
		zone_named(draft, "GCC")["countries"] = [" ae ", "sa", "Kw", "OM", "qa"]
		self.assertEqual(wizard.preview_changes(draft), {"changes": NO_CHANGES, "problems": []})

	def test_names_that_differ_only_in_capital_letters_are_one_name_to_the_erp(self):
		seed_policy(load_example())
		before = snapshot()
		draft = wizard.wizard_state()["policy"]
		twice = copy.deepcopy(draft)
		twice["zones"].append(zone_of("gcc", ["JO"]))
		twice["item_sets"].append({"name": "WATCHES", "item_groups": [], "items": [perfume()]})
		renamed = copy.deepcopy(draft)
		zone_named(renamed, "GCC")["name"] = "Gcc"
		renamed["item_sets"][0]["name"] = "watches"
		for rule in renamed["rules"]:
			rule["zone"] = "Gcc" if rule["zone"] == "GCC" else rule["zone"]
			rule["item_set"] = "watches" if rule["item_set"] == "Watches" else rule["item_set"]
		refusals = {
			"two names in one draft": (
				twice,
				[
					'Zones "GCC" and "gcc" differ only in capital letters, which the ERP reads as one name. '
					"Zone names have to be different.",
					'Item sets "Watches" and "WATCHES" differ only in capital letters, which the ERP reads as one '
					"name. Item set names have to be different.",
				],
			),
			"a saved name written with other capital letters": (
				renamed,
				[
					'Zone "Gcc" is the saved zone "GCC" with other capital letters. The ERP reads them as one '
					'name, so keep "GCC" or choose another name.',
					'Item set "watches" is the saved item set "Watches" with other capital letters. The ERP '
					'reads them as one name, so keep "Watches" or choose another name.',
				],
			),
		}
		for what, (bad, sentences) in refusals.items():
			with self.subTest(what):
				self.assertEqual(wizard.preview_changes(bad)["problems"], sentences)
				with self.assertRaises(frappe.ValidationError) as caught:
					apply(bad)
				self.assertEqual(messages(caught), [frappe.utils.escape_html(s) for s in sentences])
				shown = frappe.local.message_log[-1]  # what the desk is sent: one item for each sentence
				self.assertEqual((bool(shown.get("as_list")), len(shown["message"])), (True, len(sentences)))
				self.assertEqual(snapshot(), before)
		# The same name twice (here with spaces round one of them) stays the engine's sentence alone.
		exactly = copy.deepcopy(draft)
		exactly["zones"].append(zone_of(" GCC ", ["JO"]))
		self.assertEqual(
			wizard.preview_changes(exactly)["problems"],
			['More than one zone is called "GCC". Zone names have to be different.'],
		)

	def test_a_draft_with_absurdly_deep_extra_content_is_read_like_any_other(self):
		seed_policy(load_example())
		draft = wizard.wizard_state()["policy"]
		deep = []
		for _level in range(900):
			deep = [deep]
		zone_named(draft, "GCC")["extra"] = deep
		for what, given in (("a dict", draft), ("JSON text", json.dumps(draft))):
			with self.subTest(what):
				self.assertEqual(wizard.preview_changes(given), {"changes": NO_CHANGES, "problems": []})
				self.assertEqual(first_amount(tried(given, sample())["result"]), 39.0)
		# JSON that Python itself will not read is not a policy.
		too_deep = "[" * 100_000 + "]" * 100_000
		self.assertEqual(wizard.preview_changes(too_deep), {"changes": NO_CHANGES, "problems": [SHAPE]})

	def test_a_disabled_everywhere_else_zone_cannot_list_countries(self):
		seed_policy(load_example())
		before = snapshot()
		draft = wizard.wizard_state()["policy"]
		draft["zones"].append(zone_of("Far & <away>", ["FR"], enabled=False, everywhere_else=True))
		sentence = 'Zone "Far & <away>" is an Everywhere else zone, so it cannot list countries.'
		self.assertEqual(wizard.preview_changes(draft)["problems"], [sentence])
		with self.assertRaises(frappe.ValidationError) as caught:
			apply(draft)
		self.assertEqual(messages(caught), [frappe.utils.escape_html(sentence)])
		self.assertNotIn("<", " ".join(messages(caught)))
		self.assertEqual(snapshot(), before)
		# An enabled one gets the engine's own sentence, once.
		zone_named(draft, "Far & <away>")["enabled"] = True
		problems = wizard.preview_changes(draft)["problems"]
		self.assertEqual(len(problems), 2, problems)
		self.assertTrue(problems[0].startswith('Zone "Far & <away>" is set to Everywhere else'), problems)
		self.assertTrue(
			problems[1].startswith("More than one enabled zone is set to Everywhere else"), problems
		)

	def test_an_amount_below_zero_that_the_zone_does_not_charge_still_cannot_be_saved(self):
		seed_policy(load_example())
		before = snapshot()
		draft = wizard.wizard_state()["policy"]
		zone_named(draft, "GCC")["fallback"] = {"method": "free", "amount": -5.0}
		draft["default_fallback"] = {"method": "no_delivery", "amount": -1.0}
		expected = [
			"The policy default has an amount below zero for when the carrier can't quote. Make it 0 or more.",
			'Zone "GCC" has an amount below zero for when the carrier can\'t quote. Make it 0 or more.',
		]
		self.assertEqual(wizard.preview_changes(draft)["problems"], expected)
		with self.assertRaises(frappe.ValidationError):
			apply(draft)
		self.assertEqual(snapshot(), before)


# ---------------------------------------------------------------- apply_policy
class TestApplyPolicy(WizardTestCase):
	def test_apply_writes_zones_sets_and_rules_in_one_go(self):
		self.assertEqual(load_policy(), EMPTY_POLICY)
		example = load_example()
		result = apply(
			example, snapshot_version(EMPTY_POLICY)
		)  # and it commits nothing: the base class sees to that
		self.assertEqual(set(result), {"version", "changes"})
		after = wizard.wizard_state()
		self.assertEqual(result["version"], after["version"])
		self.assertEqual(result["version"], snapshot_version(load_policy()))
		names = [rule["name"] for rule in after["policy"]["rules"]]
		self.assertTrue(all(names) and len(set(names)) == len(names), "every rule has a row of its own")
		self.assertEqual(without_rule_names(after["policy"]), example)
		self.assertEqual(
			result["changes"],
			{
				"zones": {
					"added": ["Bahrain", "GCC", "Not shipped", "Rest of world"],
					"removed": [],
					"changed": [],
				},
				"item_sets": {"added": ["Watches"], "removed": [], "changed": []},
				"rules": {
					"added": [
						{"index": index, "name": None, "label": rule["label"], "zone": rule["zone"]}
						for index, rule in enumerate(example["rules"])
					],
					"removed": [],
					"changed": [],
					"order_changed": False,
				},
				"rate_card": {"added": [], "removed": [], "changed": []},
				"settings": ["active"],
			},
		)
		self.assertEqual(first_amount(guest_price("AE", items=one_of(perfume()))), 39.0, "live at once")
		# Applying what is saved writes nothing (not even a change stamp), and every rule keeps its row.
		kept = snapshot()
		again = apply(after["policy"], after["version"])
		self.assertEqual(again, {"version": after["version"], "changes": NO_CHANGES})
		self.assertEqual(snapshot(), kept)
		self.assertEqual([rule["name"] for rule in load_policy()["rules"]], names)

	def test_apply_with_one_bad_rule_writes_nothing(self):
		seed_policy(load_example())
		before = snapshot()
		draft = wizard.wizard_state()["policy"]
		draft["active"] = True
		draft["zones"].append(zone_of("Levant", ["JO", "LB"]))
		draft["item_sets"].append({"name": "Perfume", "item_groups": [], "items": [perfume()]})
		draft["rules"].append(rule_of("Levant", label="Levant perfume", item_set="Perfume"))
		draft["rules"].append(
			rule_of("Levant", label="Squared <price>", method="formula", formula="carrier_price ** 2")
		)
		with self.assertRaises(frappe.ValidationError) as caught:
			apply(draft)
		[message] = messages(caught)
		self.assertTrue(message.startswith("Rule 6 has a formula that cannot be used."), message)
		self.assertEqual(snapshot(), before)
		self.assertEqual(frappe.db.count("Shipping Zone"), 4)
		self.assertEqual(frappe.db.count("Shipping Item Set"), 1)
		# Without the bad rule it goes through.
		draft["rules"].pop()
		apply(draft)
		self.assertEqual(frappe.db.count("Shipping Zone"), 5)
		self.assertEqual(frappe.db.count("Shipping Item Set"), 2)

	def test_apply_refuses_a_stale_version(self):
		seed_policy(load_example())
		opened = wizard.wizard_state()
		draft = copy.deepcopy(opened["policy"])
		draft["active"] = True
		frappe.db.set_value("Shipping Zone", "GCC", "customer_line", "Changed by someone else")
		before = snapshot()
		for version in (opened["version"], None, "", 5, "0" * 16):
			with self.subTest(version=version), self.assertRaises(frappe.TimestampMismatchError) as caught:
				wizard.apply_policy(draft, version)
			[message] = messages(caught)
			self.assertTrue(message.endswith("."), message)
		self.assertEqual(snapshot(), before)
		# Stale first: a stale wizard is told so even when its draft also has problems.
		with self.assertRaises(frappe.TimestampMismatchError):
			wizard.apply_policy({**draft, "zones": "broken"}, opened["version"])
		# After a reload it goes through.
		fresh = wizard.wizard_state()
		apply(draft, fresh["version"])
		self.assertTrue(load_policy()["active"])

	def test_apply_drops_a_zone_that_rules_no_longer_use_in_the_right_order(self):
		seed_policy(load_example())
		draft = wizard.wizard_state()["policy"]
		# The old rules use Bahrain and the Watches set; the new ones do not.
		draft["zones"] = [zone for zone in draft["zones"] if zone["name"] != "Bahrain"]
		draft["rules"] = [
			rule for rule in draft["rules"] if rule["zone"] != "Bahrain" and not rule["item_set"]
		]
		draft["item_sets"] = [{"name": "Perfume", "item_groups": [], "items": [perfume()]}]
		zone_named(draft, "GCC")["customer_line"] = "Gulf delivery at the carrier's price."
		draft["zones"].append(zone_of("Levant", ["JO"]))
		with writes() as done:
			apply(draft)
		self.assertEqual(
			done,
			[
				("insert", "Shipping Item Set", "Perfume"),  # 1. item sets
				("save", "Shipping Zone", "Bahrain"),  # 2. the zone the draft drops is switched off
				("save", "Shipping Zone", "GCC"),  # 3. the draft's zones (the unchanged ones are left alone)
				("insert", "Shipping Zone", "Levant"),
				("save", "Shipping Policy", "Shipping Policy"),  # 4. the settings and rules, once
				("delete", "Shipping Zone", "Bahrain"),  # 5. what no rule points at any more
				("delete", "Shipping Item Set", "Watches"),
			],
		)
		self.assertFalse(frappe.db.exists("Shipping Zone", "Bahrain"))
		self.assertFalse(frappe.db.exists("Shipping Item Set", "Watches"))
		saved = load_policy()
		self.assertEqual(
			[rule["label"] for rule in saved["rules"]],
			["Gulf at the carrier price", "International with a minimum"],
		)
		self.assertEqual(
			[zone["name"] for zone in saved["zones"]], ["GCC", "Levant", "Not shipped", "Rest of world"]
		)
		self.assertEqual(
			frappe.db.count("Version", {"ref_doctype": "Shipping Zone", "docname": "Not shipped"}), 0
		)
		# A dropped zone or item set that a rule of the draft still uses is a problem, not a broken link.
		still_used = copy.deepcopy(saved)
		still_used["zones"] = [zone for zone in still_used["zones"] if zone["name"] != "Levant"]
		still_used["rules"].append(rule_of("Levant"))
		still_used["item_sets"] = []
		still_used["rules"].append(rule_of("GCC", item_set="Perfume"))
		before = snapshot()
		with self.assertRaises(frappe.ValidationError) as caught:
			apply(still_used)
		self.assertEqual(
			messages(caught),
			[
				"Rule 3 is for the zone &quot;Levant&quot;, which does not exist.",
				"Rule 4 uses the item set &quot;Perfume&quot;, which does not exist.",
			],
		)
		self.assertEqual(snapshot(), before)

	def test_apply_refuses_duplicate_zone_names_and_a_country_in_two_zones(self):
		seed_policy(load_example())
		before = snapshot()
		draft = wizard.wizard_state()["policy"]
		twice = copy.deepcopy(draft)
		twice["zones"].append(zone_of("GCC", ["JO"]))
		spaced = copy.deepcopy(draft)
		spaced["zones"].append(zone_of(" GCC ", ["JO"]))
		clash = copy.deepcopy(draft)
		clash["zones"].append(zone_of("Emirates", ["AE"]))
		refusals = {
			"the same name twice": (
				twice,
				"More than one zone is called &quot;GCC&quot;. Zone names have to be different.",
			),
			"the same name with spaces": (
				spaced,
				"More than one zone is called &quot;GCC&quot;. Zone names have to be different.",
			),
			"a country in two enabled zones": (
				clash,
				"The country AE is in more than one enabled zone (&quot;GCC&quot; and &quot;Emirates&quot;). "
				"A country can be in only one enabled zone.",
			),
		}
		for what, (bad, sentence) in refusals.items():
			with self.subTest(what), self.assertRaises(frappe.ValidationError) as caught:
				apply(bad)
			self.assertEqual(messages(caught), [sentence])
			self.assertEqual(snapshot(), before)
		# A disabled zone may repeat a country.
		zone_named(clash, "Emirates")["enabled"] = False
		apply(clash)
		self.assertIn("AE", zone_named(load_policy(), "Emirates")["countries"])

	def test_apply_moves_a_country_and_renames_a_zone(self):
		seed_policy(load_example(), active=True)
		draft = wizard.wizard_state()["policy"]
		gulf = zone_named(draft, "GCC")
		gulf["countries"].remove("AE")
		# The Emirates come before the Gulf: saved one by one, AE would be in two enabled zones for a moment.
		draft["zones"].insert(0, zone_of("Emirates", ["AE"]))
		draft["rules"].insert(0, rule_of("Emirates", label="Emirates at the carrier price"))
		rest = zone_named(draft, "Rest of world")
		rest["name"] = "International"
		rule_labelled(draft, "International with a minimum")["zone"] = "International"
		result = apply(draft)
		saved = load_policy()
		self.assertEqual(
			[zone["name"] for zone in saved["zones"]],
			["Bahrain", "Emirates", "GCC", "International", "Not shipped"],
		)
		self.assertEqual(zone_named(saved, "Emirates")["countries"], ["AE"])
		self.assertNotIn("AE", zone_named(saved, "GCC")["countries"])
		self.assertTrue(zone_named(saved, "International")["everywhere_else"])
		self.assertEqual(rule_labelled(saved, "International with a minimum")["zone"], "International")
		self.assertEqual(
			result["changes"]["zones"],
			{"added": ["Emirates", "International"], "removed": ["Rest of world"], "changed": ["GCC"]},
		)
		item = perfume()
		self.assertEqual(
			first_amount(guest_price("AE", items=one_of(item))), 38.2, "the Emirates rule, no rounding"
		)
		self.assertEqual(first_amount(guest_price("SA", items=one_of(item))), 39.0, "still the Gulf rule")
		far = far_country(saved)
		self.assertEqual(guest_price(far, quotes=[quote_of(20.0)])["zone"], "International")

	def test_names_and_codes_typed_with_spaces_around_them_are_stored_without_them(self):
		seed_policy(load_example())
		item = perfume()
		draft = wizard.wizard_state()["policy"]
		draft["zones"].append(zone_of("  Levant ", [" jo ", "LB"]))
		draft["item_sets"].append(
			{"name": " Perfume ", "item_groups": [" WATCHES "], "items": [f" {item} ", " "]}
		)
		draft["rules"].append(rule_of(" Levant ", label="Levant", item_set=" Perfume "))
		result = apply(draft)
		saved = load_policy()
		self.assertEqual(zone_named(saved, "Levant")["countries"], ["JO", "LB"])
		perfume_set = next(item_set for item_set in saved["item_sets"] if item_set["name"] == "Perfume")
		self.assertEqual(perfume_set, {"name": "Perfume", "item_groups": ["WATCHES"], "items": [item]})
		rule = rule_labelled(saved, "Levant")
		self.assertEqual((rule["zone"], rule["item_set"]), ("Levant", "Perfume"))
		self.assertEqual(result["changes"]["zones"]["added"], ["Levant"])

	def test_the_everywhere_else_flag_and_a_country_can_swap_between_two_saved_zones(self):
		seed_policy(
			policy_of(
				[zone_of("Near", ["AE"]), zone_of("Far", everywhere_else=True)],
				[rule_of("Near"), rule_of("Far")],
			)
		)
		draft = wizard.wizard_state()["policy"]
		draft["active"] = True
		# Saved one at a time, "Far" goes first: AE would be in two enabled zones, and so would the flag.
		zone_named(draft, "Far").update(everywhere_else=False, countries=["AE"])
		zone_named(draft, "Near").update(everywhere_else=True, countries=[])
		result = apply(draft)
		saved = load_policy()
		self.assertEqual(
			{zone["name"]: (zone["everywhere_else"], zone["countries"]) for zone in saved["zones"]},
			{"Far": (False, ["AE"]), "Near": (True, [])},
		)
		self.assertEqual(result["changes"]["zones"], {"added": [], "removed": [], "changed": ["Far", "Near"]})
		self.assertEqual(guest_price("AE")["zone"], "Far")
		self.assertEqual(guest_price(far_country(saved), quotes=[quote_of(20.0)])["zone"], "Near")

	def test_a_system_manager_who_is_not_the_administrator_can_apply_and_the_history_names_them(self):
		seed_policy(load_example())
		manager = make_user(self, "System Manager")
		draft = wizard.wizard_state()["policy"]
		zone_named(draft, "GCC")["countries"].append("EG")
		draft["zones"] = [zone for zone in draft["zones"] if zone["name"] != "Bahrain"]
		draft["zones"].append(zone_of("Levant", ["JO"]))
		draft["rules"] = [
			rule for rule in draft["rules"] if rule["zone"] != "Bahrain" and not rule["item_set"]
		]
		draft["rules"].append(rule_of("Levant", label="Levant at the carrier price"))
		draft["item_sets"] = []
		known = set(frappe.get_all("Version", {"ref_doctype": ["like", "Shipping%"]}, pluck="name"))
		with as_user(manager), recording_history():
			applied = apply(draft)
		self.assertEqual(applied["version"], wizard.wizard_state()["version"])
		saved = load_policy()
		self.assertEqual(
			[zone["name"] for zone in saved["zones"]], ["GCC", "Levant", "Not shipped", "Rest of world"]
		)
		self.assertEqual(saved["item_sets"], [])
		new = [
			row
			for row in frappe.get_all(
				"Version",
				{"ref_doctype": ["like", "Shipping%"]},
				["name", "ref_doctype", "docname", "owner", "data"],
			)
			if row["name"] not in known
		]
		self.assertTrue(new)
		self.assertEqual({row["owner"] for row in new}, {manager}, "each change is recorded with who made it")
		history = {(row["ref_doctype"], row["docname"]): json.loads(row["data"]) for row in new}
		# The zone that only gained a country says so: the countries it kept are not removed and added again.
		gulf = history[("Shipping Zone", "GCC")]
		self.assertEqual(
			[(table, row["country"]) for table, row in gulf["added"]], [("countries", country_name("EG"))]
		)
		self.assertEqual((gulf["removed"], gulf["changed"], gulf["row_changed"]), ([], [], []))
		self.assertEqual(history[("Shipping Zone", "Bahrain")]["changed"], [["enabled", 1, 0]])
		self.assertIn(("Shipping Policy", "Shipping Policy"), history)

	def test_a_rule_keeps_its_row_only_when_its_name_is_one_of_the_saved_rows(self):
		seed_policy(load_example())
		saved = load_policy()
		rows = {rule["label"]: rule["name"] for rule in saved["rules"]}
		draft = copy.deepcopy(saved)
		bahrain = rule_labelled(draft, "Free in Bahrain")
		bahrain["label"] = "Free in Bahrain, renamed"
		stranger = rule_of("GCC", name="no-such-row", label="A rule with a name that is not a row")
		copied = {
			**rule_labelled(draft, "Gulf at the carrier price"),
			"label": "A copy carrying the same row name",
		}
		draft["rules"] = [bahrain, stranger, rule_labelled(draft, "Gulf at the carrier price"), copied]
		apply(draft)
		after = load_policy()
		self.assertEqual(
			[rule["label"] for rule in after["rules"]],
			[
				"Free in Bahrain, renamed",
				"A rule with a name that is not a row",
				"Gulf at the carrier price",
				"A copy carrying the same row name",
			],
			"no rule is lost",
		)
		names = [rule["name"] for rule in after["rules"]]
		self.assertEqual(names[0], rows["Free in Bahrain"], "updated in its own row")
		self.assertEqual(names[2], rows["Gulf at the carrier price"])
		self.assertNotIn(names[1], ("no-such-row", *rows.values()), "a name that is not a row is a new row")
		self.assertNotIn(names[3], rows.values(), "a row name used twice is a new row the second time")
		self.assertEqual(len(set(names)), 4)
		self.assertEqual(frappe.db.count("Shipping Policy Rule"), 4)
		self.assertFalse(frappe.db.exists("Shipping Policy Rule", rows["Free watches in the Gulf"]))

	def test_a_failure_at_any_step_of_the_write_leaves_everything_as_it_was(self):
		seed_policy(load_example())
		before = snapshot()
		draft = wizard.wizard_state()["policy"]
		draft["active"] = True
		draft["zones"] = [zone for zone in draft["zones"] if zone["name"] != "Bahrain"]
		draft["rules"] = [rule for rule in draft["rules"] if rule["zone"] != "Bahrain"]
		draft["zones"].append(zone_of("Levant", ["JO", "LB"]))
		draft["item_sets"].append({"name": "Perfume", "item_groups": [], "items": [perfume()]})
		draft["rules"].append(rule_of("Levant", item_set="Perfume", method="free"))
		rule_labelled(draft, "Gulf at the carrier price")["round_up"] = False
		real_validate = wizard.validate_policy

		def guard_finds_a_problem(policy):
			# The first call checks the draft; the second is the final check of what was written.
			guard_finds_a_problem.calls += 1
			return real_validate(policy) if guard_finds_a_problem.calls == 1 else ["Injected <problem>."]

		guard_finds_a_problem.calls = 0
		unknown_group = copy.deepcopy(draft)
		unknown_group["item_sets"].append(
			{"name": "Nothing", "item_groups": ["ZZ No Such Group"], "items": []}
		)
		unnameable = copy.deepcopy(draft)
		unnameable["zones"].append(
			zone_of("Shipping Zone", ["FR"])
		)  # Frappe will not name a zone after its doctype
		validate = (
			"dsi_catalogue.dsi_catalogue.doctype.shipping_policy.shipping_policy.ShippingPolicy.validate"
		)
		# (the draft to apply, what makes it fail, the error that comes out, a word of its message, and whether
		# Version rows and Deleted Document rows had been written by the time it failed)
		failures = {
			"1, halfway through the item sets: a group the ERP does not have": (
				unknown_group,
				None,
				frappe.LinkValidationError,
				"ZZ No Such Group",
				(False, False),
			),
			"3, halfway through the zones: a name Frappe refuses": (
				unnameable,
				None,
				frappe.NameError,
				"cannot be Shipping Zone",
				(True, False),
			),
			"4, the policy's save fails": (
				draft,
				mock.patch(validate, side_effect=frappe.ValidationError("disk full")),
				frappe.ValidationError,
				"disk full",
				(True, False),
			),
			"5, a delete fails": (
				draft,
				mock.patch.object(frappe, "delete_doc", side_effect=RuntimeError("disk full")),
				RuntimeError,
				"disk full",
				(True, False),
			),
			"6, the final check finds a problem": (
				draft,
				mock.patch.object(wizard, "validate_policy", side_effect=guard_finds_a_problem),
				frappe.ValidationError,
				"Injected",
				(True, True),
			),
		}
		self.assertFalse(frappe.db.exists("Item Group", "ZZ No Such Group"))
		for what, (bad, failure, error, word, written) in failures.items():
			with self.subTest(step=what):
				at_rollback = []
				real_rollback = frappe.db.rollback

				def spy(**arguments):
					# What the database holds at the moment Apply gives up: the writes are really there.
					at_rollback.append(snapshot())
					return real_rollback(**arguments)

				with (
					failure or nullcontext(),
					recording_history(),
					mock.patch.object(frappe.db, "rollback", spy),
					writes() as done,
					self.assertRaises(error) as caught,
				):
					apply(bad)
				self.assertIn(word, str(caught.exception.args))
				self.assertTrue(done, "the failure came after something was written")
				[held] = at_rollback
				self.assertEqual(
					(held["versions"] > before["versions"], held["deleted"] > before["deleted"]), written
				)
				self.assertNotEqual(held, before, "rolled back from a changed database")
				self.assertEqual(snapshot(), before)
		self.assertEqual(
			messages(caught), ["Injected &lt;problem&gt;."], "the final check's sentence, escaped"
		)
		# Nothing was left half-open: the same draft applies cleanly afterwards.
		apply(draft)
		self.assertEqual(without_rule_names(load_policy()), without_rule_names(draft))

	def test_the_final_check_reads_back_what_was_written(self):
		seed_policy(load_example())
		before = snapshot()
		draft = wizard.wizard_state()["policy"]
		draft["zones"].append(zone_of("Levant", ["JO"]))
		real_write = wizard._write

		def write_then_spoil(*arguments):
			real_write(*arguments)
			# Something besides the draft lands in the database: Levant also lists a country of the Gulf.
			levant = frappe.get_doc("Shipping Zone", "Levant")
			levant.flags.policy_draft_validated = True
			levant.append("countries", {"country": country_name("AE")})
			levant.save()

		with (
			mock.patch.object(wizard, "_write", write_then_spoil),
			self.assertRaises(frappe.ValidationError) as caught,
		):
			apply(draft)
		self.assertEqual(
			messages(caught),
			[
				"The country AE is in more than one enabled zone (&quot;GCC&quot; and &quot;Levant&quot;). "
				"A country can be in only one enabled zone."
			],
		)
		self.assertEqual(snapshot(), before)

	def test_applies_are_taken_one_at_a_time(self):
		seed_policy(load_example())
		before = snapshot()
		draft = wizard.wizard_state()["policy"]
		draft["active"] = True
		path = os.path.abspath(get_site_path("locks", "shipping_policy_apply.lock"))
		self.assertEqual(wizard.APPLY_LOCK, "shipping_policy_apply")
		self.assertEqual(wizard.APPLY_LOCK_SECONDS, 30)
		held = FileLock(path, timeout=0)
		with (
			held,
			mock.patch.object(wizard, "APPLY_LOCK_SECONDS", 0.2),
			mock.patch.object(frappe, "log_error") as logged,
		):
			with self.assertRaises(frappe.ValidationError) as caught:
				apply(draft)
		self.assertEqual(messages(caught), [BUSY])
		logged.assert_called_once()  # Frappe's own note that the lock was busy, kept out of the Error Log
		self.assertEqual(snapshot(), before)
		apply(draft)  # once the other apply is done
		self.assertTrue(load_policy()["active"])

	def test_apply_asks_the_website_to_refresh_through_the_outbox(self):
		seed_policy(load_example())
		draft = wizard.wizard_state()["policy"]
		draft["active"] = True
		enqueue = mock.Mock()
		with fake_outbox(enqueue):
			result = apply(draft)
			with self.assertRaises(frappe.TimestampMismatchError):
				wizard.apply_policy(draft, "stale")
		enqueue.assert_called_once_with(
			"website-revalidate",
			{"tags": ["shipping-policy"]},
			dedup_key=f"shipping-policy|{result['version']}",
			source=("Shipping Policy", "Shipping Policy"),
		)

	def test_an_outbox_that_fails_never_fails_the_apply(self):
		seed_policy(load_example())
		draft = wizard.wizard_state()["policy"]
		draft["active"] = True
		with fake_outbox(mock.Mock(side_effect=RuntimeError("outbox table locked"))):
			with mock.patch.object(frappe, "log_error") as logged:
				result = apply(draft)
		self.assertTrue(load_policy()["active"], "the apply stands")
		self.assertEqual(result["version"], wizard.wizard_state()["version"])
		logged.assert_called_once()
		self.assertEqual(logged.call_args.kwargs["title"], "shipping policy revalidate failed")
		self.assertIn("outbox table locked", logged.call_args.kwargs["message"])

	def test_without_dsi_core_the_refresh_is_skipped_silently(self):
		seed_policy(load_example())
		draft = wizard.wizard_state()["policy"]
		draft["active"] = True
		with mock.patch.object(frappe, "log_error") as logged:
			if importlib.util.find_spec("dsi_core") is None:  # erp2 as it is
				apply(draft)
			draft["active"] = False
			with modules({"dsi_core": None}):  # not importable, whatever is installed
				apply(draft)
		logged.assert_not_called()
		self.assertFalse(load_policy()["active"])

	def test_apply_refuses_a_malformed_draft_and_takes_json_text(self):
		seed_policy(load_example())
		before = snapshot()
		draft = wizard.wizard_state()["policy"]
		for bad in (None, 5, "{", [], {**draft, "zones": "GCC"}, {**draft, "rules": [{"enabled": "yes"}]}):
			with self.subTest(bad=str(bad)[:40]), self.assertRaises(frappe.ValidationError) as caught:
				apply(bad)
			self.assertEqual(messages(caught), [SHAPE])
		self.assertEqual(snapshot(), before)
		draft["active"] = True
		apply(json.dumps(draft))
		self.assertTrue(load_policy()["active"])

	def test_random_drafts_are_applied_whole_or_refused_whole_and_the_preview_agrees(self):
		seed_policy(load_example())
		rng, item = random.Random(20261022), perfume()
		applied = refused = 0
		for number in range(30):
			opened = wizard.wizard_state()
			draft = copy.deepcopy(opened["policy"])
			edits = [random_edit(rng, draft, item) for _ in range(rng.randint(1, 3))]
			where = f"draft {number}: {', '.join(edits)}"
			before = snapshot()
			if wizard.preview_changes(draft)["problems"]:
				refused += 1
				with self.assertRaises(frappe.ValidationError, msg=where):
					wizard.apply_policy(draft, opened["version"])
				self.assertEqual(snapshot(), before, where)
				continue
			applied += 1
			result = wizard.apply_policy(draft, opened["version"])  # a draft the preview passes is applied
			now = wizard.wizard_state()
			self.assertEqual(result["version"], now["version"], where)
			self.assertEqual(without_rule_names(now["policy"]), without_rule_names(draft), where)
			kept = snapshot()
			self.assertEqual(wizard.apply_policy(now["policy"], now["version"])["changes"], NO_CHANGES, where)
			self.assertEqual(snapshot(), kept, where)
		self.assertGreaterEqual(min(applied, refused), 8, f"applied {applied}, refused {refused}")


# ---------------------------------------------------------------- import_rate_card
class TestImportRateCard(WizardTestCase):
	def test_import_rate_card_previews_without_writing(self):
		seed_policy(load_example(), active=True)
		before, errors = snapshot(), frappe.db.count("Error Log")
		with recorded_queries() as queries:
			answer = imported()
		self.assertEqual(set(answer), IMPORT_KEYS)
		card = answer["card"]
		self.assertEqual([profile["name"] for profile in card["profiles"]], IMPORTED_PROFILES)
		self.assertEqual(answer["summary"], IMPORTED_SUMMARY)
		self.assertEqual((answer["not_delivered"], answer["not_delivered_missing"]), (["RU", "KP"], []))
		self.assertEqual(answer["problems"], [])
		# The export's quirks are said as notes, with the numbers of the rows.
		self.assertTrue(
			any("gap between 0.6 lb and 0.7 lb" in note for note in answer["notes"]), answer["notes"]
		)
		# What was read is data, as the file has it: names are not escaped, the BOM and CRLF are gone.
		dangerous = profile_named(card, "Dangerous Goods")
		self.assertEqual(
			[zone["name"] for zone in dangerous["zones"]],
			["USA", "Europe, UK & Select Asia", "Bahrain (Local)"],
		)
		europe = dangerous["zones"][1]
		self.assertEqual(
			(europe["countries"], europe["method"], europe["days"], europe["tiers"]),
			(["GB", "FR", "CI"], EXPRESS_ROW, 7, [{"from": 0.01, "to": 2.0, "price": 75.0}]),
		)
		petite = profile_named(card, "Dangerous Goods Petite")["zones"][0]
		self.assertEqual(
			(petite["name"], petite["tiers"]), ("GCC \u2013 KW", [{"from": None, "to": None, "price": 20.0}])
		)
		self.assertEqual(profile_named(card, "Refill Service")["match"], ["F-*-RF"])
		# It writes nothing: no document, no stamp, no Error Log row, and the database is only read.
		self.assertEqual(snapshot(), before)
		self.assertEqual(frappe.db.count("Error Log"), errors)
		other = [query for query in queries if not query.lstrip("( \n\t").lower().startswith("select")]
		self.assertEqual(other, [], "only SELECTs ran")
		# Bytes are read as UTF-8 text, and the file may come without a BOM and with plain line ends.
		self.assertEqual(imported(CONFIG_CSV.encode("utf-8"), NOT_DELIVERED_CSV.encode("utf-8")), answer)
		plain = CONFIG_CSV.replace("\ufeff", "").replace("\r\n", "\n")
		self.assertEqual(imported(plain, NOT_DELIVERED_CSV)["card"], card)
		# The second file is optional: none, an empty one, and one of blanks all mean none was given.
		for nothing in (None, "", "  \r\n "):
			alone = imported(CONFIG_CSV, nothing)
			self.assertEqual((alone["card"], alone["notes"]), (card, answer["notes"]), repr(nothing))
			self.assertEqual((alone["not_delivered"], alone["not_delivered_missing"]), ([], []))
		self.assertEqual(snapshot(), before)

	def test_import_refuses_oversized_or_malformed_files_in_plain_words(self):
		before = snapshot()
		# Refused outright (an error, not a list of problems): not text, too much text, text nobody can store.
		refused = {
			"a number": (5, None),
			"a list": ([CONFIG_CSV], None),
			"a dict": ({"Profile": 1}, None),
			"nothing": (None, None),
			"bytes that are not UTF-8": (b"\xff\xfe\x00bad", None),
			"a lone surrogate": ("Profile,Zone\ud800", None),
			"a file over a million characters": ("a" * 1_000_001, None),
			"bytes over four million": (b"a" * 4_000_001, None),
			"a second file that is not text": (CONFIG_CSV, 5),
			"a second file over a million characters": (CONFIG_CSV, "a" * 1_000_001),
			"a second file with a lone surrogate": (CONFIG_CSV, "ISO-2\ud800"),
		}
		for what, (config, not_delivered) in refused.items():
			with self.subTest(what), self.assertRaises(frappe.ValidationError) as caught:
				wizard.import_rate_card(config, not_delivered)
			[message] = messages(caught)
			self.assertTrue(message.endswith("."), message)
			self.assertNotIn("Traceback", message)
		for config, not_delivered, sentence in (
			("a" * 1_000_001, None, "The rates file is longer than 1,000,000 characters."),
			(CONFIG_CSV, "a" * 1_000_001, "The not-delivered file is longer than 1,000,000 characters."),
			(5, None, "The rates file has to be text."),
			(CONFIG_CSV, 5, "The not-delivered file has to be text."),
		):
			with self.assertRaises(frappe.ValidationError) as caught:
				wizard.import_rate_card(config, not_delivered)
			self.assertEqual(messages(caught), [sentence])
		# A file of exactly a million characters is read, and is a file that cannot be used.
		answer = imported("a" * 1_000_000, None)
		self.assertIsNone(answer["card"])
		# A file that is read but cannot be used is not an error: the answer says why, as a problem.
		nothing_usable = {"profiles": 0, "zones": 0, "tiers": 0, "countries": 0}
		malformed = {
			"an empty file": "",
			"only blanks": "  \r\n\r\n ",
			"no known columns": "a,b,c\r\n1,2,3\r\n",
			"a header and nothing else": HEADER + "\r\n",
			"rows that cannot be used": csv_text(",,,,,,,5"),
			"an unclosed quote": 'Profile,"Zone\r\nx',
			"plain rubbish": "\x00\x01\x02" * 10,
		}
		for what, text in malformed.items():
			with self.subTest(what):
				with self.assertRaises(RateCardError) as raised:
					parse_shopify_export(text)
				self.assertEqual(
					imported(text),
					{
						"card": None,
						"not_delivered": [],
						"not_delivered_missing": [],
						"summary": nothing_usable,
						"notes": [],
						"problems": [frappe.utils.escape_html(str(raised.exception))],
					},
				)
		self.assertTrue(imported("")["problems"][0].startswith("The rates file is empty"))
		self.assertTrue(
			imported("a,b,c\r\n")["problems"][0].startswith("The rates file is missing these columns")
		)
		# A second file that cannot be used only costs its countries: a note says so.
		answer = imported(CONFIG_CSV, "A,B\r\n1,2\r\n")
		self.assertEqual((answer["not_delivered"], answer["problems"]), ([], []))
		self.assertIn(
			frappe.utils.escape_html(
				'The not-delivered file has no "ISO-2" column, so no country was marked as not delivered.'
			),
			answer["notes"],
		)
		self.assertEqual(snapshot(), before)

	def test_import_marks_countries_missing_from_the_erp(self):
		self.assertFalse(frappe.db.exists("Country", {"code": "zz"}), "this site has no country ZZ")
		before = snapshot()
		config = csv_text(*CONFIG_ROWS, f"Dangerous Goods,Tier 2,1,ZZ - Nowhere,{EXPRESS_ROW},0.01,2,90")
		answer = imported(config, "ISO-2,Country\r\nRU,Russia\r\nZZ,Nowhere\r\nKP,North Korea\r\n")
		self.assertEqual(answer["not_delivered"], ["RU", "ZZ", "KP"])
		self.assertEqual(answer["not_delivered_missing"], ["ZZ"])
		self.assertEqual(
			answer["problems"],
			[
				"Country code ZZ in zone Tier 2 (Dangerous Goods) is not a country in the ERP.",
				"Country code ZZ in the not-delivered list is not a country in the ERP.",
			],
		)
		self.assertEqual(answer["summary"], {**IMPORTED_SUMMARY, "zones": 8, "tiers": 9, "countries": 10})
		# A code that several zones list is one problem, in the first zone of the card that lists it.
		twice = csv_text(
			*CONFIG_ROWS,
			f"Dangerous Goods,Tier 2,1,ZZ - Nowhere,{EXPRESS_ROW},0.01,2,90",
			f"Refill Service,Tier 3,1,ZZ - Nowhere,{EXPRESS_ROW},0.01,2,90",
		)
		self.assertEqual(
			imported(twice, None)["problems"],
			["Country code ZZ in zone Tier 3 (Refill Service) is not a country in the ERP."],
		)
		# The problems of the card itself come first, in the words the policy uses for them.
		no_catch_all = csv_text(
			CONFIG_ROWS[0], f"Dangerous Goods,Tier 2,1,ZZ - Nowhere,{EXPRESS_ROW},0.01,2,90"
		)
		problems = imported(no_catch_all, None)["problems"]
		self.assertEqual(len(problems), 2, problems)
		self.assertTrue(problems[0].startswith("No profile matches every item"), problems)
		self.assertTrue(problems[1].startswith("Country code ZZ in zone Tier 2"), problems)
		self.assertEqual(snapshot(), before)

	def test_import_lists_at_most_fifty_problems_and_notes_and_says_how_many_more(self):
		codes = {code.upper() for code in frappe.get_all("Country", pluck="code") if code}
		unknown = [
			a + b for a in string.ascii_uppercase for b in string.ascii_uppercase if a + b not in codes
		]
		unknown = unknown[:60]
		self.assertEqual(len(unknown), 60)
		profiles = ("Dangerous Goods Petite", "Refill Service", "Dangerous Goods")
		rows = [
			f"{profiles[number % 3]},Zone {code},1,{code} - Nowhere,{EXPRESS_ROW},,,10"
			for number, code in enumerate(unknown)
		]
		rows.append(f"Rest of the World Allowed Items,Worldwide,1,US - United States,{EXPRESS_ROW},0.01,2,40")
		answer = imported(csv_text(*rows), None)
		self.assertEqual(len(answer["problems"]), 51)
		pattern = (
			r"Country code [A-Z]{2} in zone Zone [A-Z]{2} "
			r"\((Dangerous Goods Petite|Refill Service|Dangerous Goods)\) is not a country in the ERP\."
		)
		for sentence in answer["problems"][:50]:
			self.assertRegex(sentence, pattern)
		self.assertEqual(len(set(answer["problems"][:50])), 50, "each code once")
		self.assertEqual(answer["problems"][50], "And 10 more.")
		# The same for the notes: a file of rows that cannot be used would otherwise give a note for each.
		junk = csv_text(*[",,,,,,,5"] * 120, CONFIG_ROWS[0], CONFIG_ROWS[4])
		every_note = parse_shopify_export(junk)["notes"]
		self.assertGreater(len(every_note), 120)
		answer = imported(junk, None)
		self.assertEqual(len(answer["notes"]), 51)
		self.assertEqual(answer["notes"][:50], every_note[:50])
		self.assertEqual(answer["notes"][50], f"And {len(every_note) - 50} more.")

	def test_import_escapes_what_the_file_says_in_the_sentences_it_returns(self):
		name = '<b>Gulf & "co"</b>'
		quoted = '"<b>Gulf & ""co""</b>"'
		config = csv_text(
			f"Dangerous Goods,{quoted},1,ZZ - Nowhere,{EXPRESS_ROW},,,10",
			f"Dangerous Goods,Odd,1,US - United States,{EXPRESS_ROW},,,<i>",
			f"Rest of the World Allowed Items,Worldwide,1,US - United States,{EXPRESS_ROW},0.01,2,40",
		)
		answer = imported(config, None)
		# The card is data and keeps the name as it was written; the sentences about it are escaped.
		self.assertEqual(answer["card"]["profiles"][0]["zones"][0]["name"], name)
		self.assertEqual(
			answer["problems"],
			[
				frappe.utils.escape_html(
					f"Country code ZZ in zone {name} (Dangerous Goods) is not a country in the ERP."
				)
			],
		)
		self.assertIn(
			frappe.utils.escape_html('Row 3 was skipped: the price "<i>" is not a number.'), answer["notes"]
		)
		for sentence in [*answer["problems"], *answer["notes"]]:
			self.assertNotIn("<", sentence)
			self.assertNotIn(">", sentence)
		self.assertIn("&lt;b&gt;Gulf &amp; &quot;co&quot;&lt;/b&gt;", answer["problems"][0])


# ---------------------------------------------------------------- Try an order, Review and Apply with a rate card
class TestRateCardDrafts(WizardTestCase):
	def assert_the_same_answers(self, draft, orders, wanted=5):
		"""For each order, Try an order (with the draft, which is the saved policy) and the guest method give one
		answer. Returns the distinct outcomes seen, and asserts that there are at least `wanted`."""
		outcomes = set()
		for what, (country, items, boxes, quotes) in orders.items():
			with self.subTest(what):
				live = guest_price(country, items=items, boxes=boxes, quotes=quotes)
				answer = tried(draft, sample(country, items, boxes, quotes))
				self.assertEqual(response_from(answer["result"], lambda text: text), live)
				outcomes.add(
					(live["offered"], live["reason"], tuple(offer["amount"] for offer in live["offers"]))
				)
		self.assertGreaterEqual(
			len(outcomes), wanted, f"at least {wanted} different outcomes: {sorted(outcomes, key=str)}"
		)
		return outcomes

	def test_wizard_state_carries_the_price_source_and_the_card_inside_the_policy(self):
		empty = wizard.wizard_state()
		self.assertEqual((empty["policy"]["price_source"], empty["policy"]["rate_card"]), ("carrier", None))
		seed_policy(card_policy(), active=True)
		answer = wizard.wizard_state()
		orjson.dumps(answer)
		self.assertEqual(
			(answer["policy"]["price_source"], answer["policy"]["rate_card"]), ("rate_card", CARD)
		)
		self.assertEqual(
			set(answer), {"policy", "version", "countries", "served_codes", "price_list", "carrier_available"}
		)
		self.assertEqual(answer["version"], snapshot_version(load_policy()))
		self.assertNotEqual(answer["version"], empty["version"])
		# The version follows the card: another price is another version.
		cheaper = copy.deepcopy(CARD)
		profile_named(cheaper, "Refill Service")["zones"][0]["tiers"][0]["price"] = 59.0
		frappe.get_doc(POLICY).update({"rate_card": json.dumps(cheaper)}).save()
		self.assertNotEqual(wizard.wizard_state()["version"], answer["version"])

	def test_try_order_in_rate_card_mode_prices_a_draft(self):
		seed_policy(load_example(), active=False)
		[(fragrance, _), (second, _)] = website_items(FRAGRANCE, 2)
		[(timepiece, _)] = website_items(WATCH)
		draft = with_card(wizard.wizard_state()["policy"])
		untouched = copy.deepcopy(draft)
		self.assertFalse(draft["active"])
		before = snapshot()
		# A fragrance and a watch to the USA: 60 and 40, each at the weight it defaults to (no item has one here).
		items = one_of(fragrance) + one_of(timepiece)
		with carrier(return_value=rates(("A", "Express", 10.0))) as asked:
			answer = tried(
				draft,
				# Typed cartons, quotes and value that carrier mode would refuse: this mode does not use them.
				sample("US", items, boxes=[{"weight_kg": -1}], quotes=[quote_of(-1.0)], order_value=-5),
				ask_carrier=True,
				city="Dubai",
			)
		asked.assert_not_called()
		self.assertEqual(set(answer), {"result", "previews", "order"})
		result = answer["result"]
		self.assertEqual(
			(
				result["policy_active"],
				result["price_source"],
				result["zone"],
				result["offered"],
				result["reason"],
			),
			(True, "rate_card", "Rest of world", True, None),  # the zone of the policy the USA falls in
		)
		[offer] = result["offers"]
		self.assertEqual(
			(offer["service_id"], offer["name"], offer["source"], offer["amount"], offer["estimated_days"]),
			("rate-card", EXPRESS_ROW, "rate_card", 100.0, 7),
		)
		self.assertEqual([step["kind"] for step in offer["trace"]], ["rate_card", "rate_card", "result"])
		self.assertIn("weight estimated", offer["trace"][0]["text"])
		order = answer["order"]
		self.assertEqual([line["item_code"] for line in order["items"]], [fragrance, timepiece])
		for line in order["items"]:
			self.assertTrue(line["index_key"].startswith("{"), line)
			self.assertEqual(line["weight_kg"], 0.0)
		self.assertEqual((order["boxes"], order["quotes"]), ([], []), "typed cartons and quotes are not used")
		self.assertNotEqual(order["order_value"], -5, "nor is a typed value")
		self.assertEqual([preview["index"] for preview in answer["previews"]], [0, 1, 2, 3])
		self.assertEqual(snapshot(), before, "nothing was written")
		self.assertEqual(draft, untouched, "the page's draft is not changed")
		# An item with a weight is priced by it: 1 kg is 2.205 lb, the 2.01 to 4 lb tier.
		weigh(self, second, 1.0)
		heavier = tried(draft, sample("US", one_of(second)))["result"]
		self.assertEqual(first_amount(heavier), 80.0)
		self.assertNotIn("weight estimated", heavier["offers"][0]["trace"][0]["text"])
		# A card that cannot price the order is an answer, not an error.
		refusals = {
			"too heavy": (
				sample("US", one_of(fragrance, 40)),
				"too_heavy",
				"This order is heavier than our delivery rates cover.",
			),
			"not covered": (
				sample("FR", one_of(fragrance)),
				"not_covered",
				"No delivery option applies to this order.",
			),
			"not shipped": (
				sample("RU", one_of(fragrance)),
				"not_shipped",
				zone_named(draft, "Not shipped")["not_offered_message"],  # the zone's own words
			),
			"nothing to price": (
				sample("US", []),
				"not_offered",
				"Delivery is not available for this order right now.",
			),
		}
		for what, (order_, reason, message) in refusals.items():
			with self.subTest(what):
				result = tried(draft, order_)["result"]
				self.assertEqual((result["offered"], result["reason"], result["offers"]), (False, reason, []))
				self.assertEqual(result["message"], message)
		# No card in the draft, and a price source nobody knows, offer nothing.
		bare = {**draft, "rate_card": None}
		result = tried(bare, sample("US", items))["result"]
		self.assertEqual((result["offered"], result["reason"]), (False, "not_covered"))
		result = tried({**draft, "price_source": "Gold"}, sample("US", items))["result"]
		self.assertEqual((result["offered"], result["reason"]), (False, "not_covered"))
		self.assertEqual(snapshot(), before)

	def test_the_carrier_is_still_asked_in_carrier_mode_and_typed_quotes_still_have_to_be_valid(self):
		seed_policy(card_policy(price_source="carrier"), active=True)
		draft = wizard.wizard_state()["policy"]
		[(fragrance, _)] = website_items(FRAGRANCE)
		with carrier(return_value=rates(("A", "Express", 10.0))) as asked:
			answer = tried(
				draft, sample("AE", one_of(fragrance), quotes=None), ask_carrier=True, city="Dubai"
			)
		asked.assert_called_once()
		self.assertEqual([quote["service_id"] for quote in answer["order"]["quotes"]], ["A"])
		with self.assertRaises(frappe.ValidationError):
			wizard.try_order(draft, sample("AE", one_of(fragrance), quotes=[quote_of(-1.0)]))

	def test_try_order_and_price_delivery_give_the_same_answer_for_a_rate_card_policy(self):
		seed_policy(card_policy(), active=True)
		draft = wizard.wizard_state()["policy"]
		[(fragrance, _), (weighed, _)] = website_items(FRAGRANCE, 2)
		[(timepiece, _)] = website_items(WATCH)
		[(refill, _)] = website_items(REFILL)
		weigh(self, weighed, 1.0)
		plain = ([ONE_BOX], [QUOTE])
		orders = {
			"a fragrance at the weight it defaults to": ("US", one_of(fragrance), *plain),
			"two of them": ("US", one_of(fragrance, 2), *plain),
			"a fragrance with a weight": ("US", one_of(weighed), *plain),
			"a mixed cart": ("US", one_of(fragrance) + one_of(timepiece), [], []),
			"a refill": ("US", one_of(refill), *plain),
			"a watch": ("US", one_of(timepiece), *plain),
			"free in the Gulf": ("AE", one_of(timepiece), *plain),
			"a flat price in the Gulf": ("AE", one_of(fragrance), *plain),
			"free in Bahrain": ("BH", one_of(fragrance), *plain),
			"a country the card does not cover": ("FR", one_of(fragrance), *plain),
			"too heavy": ("US", one_of(fragrance, 40), *plain),
			"a Not shipped zone": ("RU", one_of(fragrance), *plain),
			"nothing to price": ("US", [], *plain),
			"nobody serves it": ("UAE", one_of(fragrance), *plain),
			"a country written loosely": (" us ", one_of(fragrance), *plain),
			"an item the ERP does not know": ("US", one_of("NO-SUCH-ITEM-ZZ"), *plain),
			"cartons and quotes that carrier mode would refuse": (
				"US",
				one_of(fragrance),
				[{"weight_kg": -1}],
				[quote_of(-1.0)],
			),
		}
		outcomes = self.assert_the_same_answers(draft, orders, wanted=8)
		amounts = {amount for _, _, offered in outcomes for amount in offered}
		self.assertTrue({35.0, 40.0, 60.0, 80.0, 100.0} <= amounts, sorted(amounts))
		self.assertTrue(
			{"too_heavy", "not_covered", "not_shipped", "not_offered", "not_served"}
			<= {reason for _, reason, _ in outcomes}
		)

	def test_try_order_and_price_delivery_agree_when_a_zone_falls_back_to_the_rate_card(self):
		policy = card_policy(price_source="carrier")
		for name in ("GCC", "Rest of world"):
			zone_named(policy, name)["fallback"] = {"method": "rate_card", "amount": 0.0}
		seed_policy(policy, active=True)
		draft = wizard.wizard_state()["policy"]
		[(fragrance, _)] = website_items(FRAGRANCE)
		[(timepiece, _)] = website_items(WATCH)
		short = quote_of(10.0, service_id="short", name="Short", boxes_quoted=1)
		orders = {
			"the carrier cannot quote, a fragrance in the Gulf": ("AE", one_of(fragrance), [ONE_BOX], []),
			"the carrier cannot quote, a watch in the Gulf": ("AE", one_of(timepiece), [ONE_BOX], []),
			"the carrier cannot quote, a mixed cart": (
				"SA",
				one_of(fragrance) + one_of(timepiece),
				[ONE_BOX],
				[],
			),
			"a partial quote is dropped": ("AE", one_of(fragrance), TWO_BOXES, [short]),
			"the carrier cannot quote, the USA": ("US", one_of(fragrance), [ONE_BOX], []),
			"the carrier cannot quote, too heavy": ("US", one_of(fragrance, 40), [ONE_BOX], []),
			"the carrier cannot quote, not on the card": ("FR", one_of(fragrance), [ONE_BOX], []),
			"the carrier cannot quote, Bahrain keeps the policy default": (
				"BH",
				one_of(fragrance),
				[ONE_BOX],
				[],
			),
			"a quote is there, so the carrier's price is used": ("AE", one_of(fragrance), [ONE_BOX], [QUOTE]),
			"not shipped": ("RU", one_of(fragrance), [ONE_BOX], []),
		}
		outcomes = self.assert_the_same_answers(draft, orders, wanted=6)
		self.assertIn((True, "fallback_offer", (35.0,)), outcomes, "the card priced the Gulf order")
		self.assertIn((True, "fallback_offer", (0.0,)), outcomes, "and the watch, which it gives free")
		self.assertIn((True, "fallback_offer", (60.0,)), outcomes)
		self.assertIn((False, "too_heavy", ()), outcomes)

	def test_try_order_and_price_delivery_agree_when_the_policy_default_falls_back_to_the_rate_card(self):
		policy = card_policy(price_source="carrier", default_fallback={"method": "rate_card", "amount": 0.0})
		zone_named(policy, "GCC")["fallback"] = {"method": "policy_default", "amount": 0.0}
		seed_policy(policy, active=True)
		draft = wizard.wizard_state()["policy"]
		[(fragrance, _)] = website_items(FRAGRANCE)
		[(timepiece, _)] = website_items(WATCH)
		orders = {
			"the carrier cannot quote, a fragrance in the Gulf": ("AE", one_of(fragrance), [ONE_BOX], []),
			"the carrier cannot quote, a watch in the Gulf": ("AE", one_of(timepiece), [ONE_BOX], []),
			"the carrier cannot quote, the USA": ("US", one_of(fragrance) + one_of(timepiece), [ONE_BOX], []),
			"the carrier cannot quote, Bahrain": ("BH", one_of(fragrance), [ONE_BOX], []),
			"not on the card": ("FR", one_of(fragrance), [ONE_BOX], []),
			"a quote is there": ("AE", one_of(fragrance), [ONE_BOX], [QUOTE]),
			"not shipped": ("RU", one_of(fragrance), [ONE_BOX], []),
		}
		outcomes = self.assert_the_same_answers(draft, orders, wanted=5)
		self.assertIn((True, "fallback_offer", (100.0,)), outcomes)

	def test_try_order_reads_the_item_facts_only_when_the_draft_may_price_from_the_card(self):
		seed_policy(card_policy(price_source="carrier"), active=True)
		saved = wizard.wizard_state()["policy"]
		[(fragrance, _)] = website_items(FRAGRANCE)

		def facts_read(draft):
			with mock.patch.object(
				shipping_policy, "item_facts_for", wraps=shipping_policy.item_facts_for
			) as read:
				tried(draft, sample(items=one_of(fragrance)))
			return read.call_count

		draft = copy.deepcopy(saved)
		self.assertEqual(
			facts_read(draft), 0, "a carrier draft with no rate-card fallback reads no item facts"
		)
		draft["default_fallback"] = {"method": "rate_card", "amount": 0.0}
		self.assertEqual(facts_read(draft), 1, "the policy default is the card")
		draft["default_fallback"] = {"method": "no_delivery", "amount": 0.0}
		self.assertEqual(facts_read(draft), 0)
		zone_named(draft, "GCC")["fallback"] = {"method": "rate_card", "amount": 0.0}
		self.assertEqual(facts_read(draft), 1, "an enabled zone's fallback is the card")
		zone_named(draft, "GCC")["enabled"] = False
		self.assertEqual(facts_read(draft), 0, "a zone that is off does not count")
		self.assertEqual(facts_read(with_card(saved)), 1, "the card as the price source always reads them")
		# It is the rule the live method uses, which the wizard asks of the draft; the saved policy has no such fallback.
		self.assertFalse(shipping_policy.fallback_may_use_the_card(saved))
		with_default = {**saved, "default_fallback": {"method": "rate_card", "amount": 0.0}}
		self.assertTrue(shipping_policy.fallback_may_use_the_card(with_default))

	def test_try_order_reads_a_draft_that_leaves_out_what_is_empty_when_it_decides_about_the_facts(self):
		seed_policy(card_policy(price_source="carrier"), active=True)
		saved = wizard.wizard_state()["policy"]
		[(fragrance, _)] = website_items(FRAGRANCE)
		loose = copy.deepcopy(saved)
		loose["default_fallback"] = {}
		loose["zones"] = [
			{"name": "GCC", "countries": ["AE"]},
			{"name": "Bahrain", "countries": ["BH"], "enabled": True, "fallback": None},
			{"name": "Far", "enabled": True, "fallback": {"amount": 5}},
			{
				"name": "Everywhere",
				"everywhere_else": True,
				"enabled": True,
				"fallback": {"method": "rate_card"},
			},
		]
		for what, draft in (("a loose draft", loose), ("the same as JSON text", json.dumps(loose))):
			with self.subTest(what):
				answer = tried(draft, sample("US", one_of(fragrance), quotes=[]))
				self.assertEqual(answer["result"]["reason"], "fallback_offer")
				self.assertEqual(
					first_amount(answer["result"]), 60.0, "the Everywhere-else zone falls back to the card"
				)

	def test_preview_changes_reports_the_price_source_and_the_card(self):
		seed_policy(
			card_policy(price_source="carrier"), active=True
		)  # a card is stored; the carrier is the source
		saved = wizard.wizard_state()["policy"]
		self.assertEqual((saved["price_source"], saved["rate_card"]), ("carrier", CARD))
		before = snapshot()
		self.assertEqual(wizard.preview_changes(saved), {"changes": NO_CHANGES, "problems": []})
		# Only the price source changes.
		draft = copy.deepcopy(saved)
		draft["price_source"] = "rate_card"
		self.assertEqual(
			wizard.preview_changes(draft),
			{"changes": {**NO_CHANGES, "settings": ["price_source"]}, "problems": []},
		)
		# Only the card changes: a price, a profile less, a profile more.
		changed = copy.deepcopy(saved)
		profile_named(changed["rate_card"], "Refill Service")["zones"][0]["tiers"][0]["price"] = 61.0
		self.assertEqual(
			wizard.preview_changes(changed),
			{
				"changes": {
					**NO_CHANGES,
					"rate_card": {"added": [], "removed": [], "changed": ["Refill Service"]},
					"settings": ["rate_card"],
				},
				"problems": [],
			},
		)
		fewer = copy.deepcopy(saved)
		fewer["rate_card"]["profiles"] = [
			profile for profile in fewer["rate_card"]["profiles"] if profile["name"] != "Refill Service"
		]
		self.assertEqual(
			wizard.preview_changes(fewer)["changes"]["rate_card"],
			{"added": [], "removed": ["Refill Service"], "changed": []},
		)
		more = copy.deepcopy(saved)
		extra = copy.deepcopy(profile_named(more["rate_card"], "Refill Service"))
		extra["name"], extra["match"] = "Straps", ["S-*"]
		more["rate_card"]["profiles"].insert(3, extra)
		self.assertEqual(
			wizard.preview_changes(more)["changes"]["rate_card"],
			{"added": ["Straps"], "removed": [], "changed": []},
		)
		# Rounding is not a change: prices go to cents and tier weights to three decimals, as they are stored.
		rounded = copy.deepcopy(saved)
		tier = profile_named(rounded["rate_card"], "Refill Service")["zones"][0]["tiers"][0]
		tier.update({"from": 0.01004, "to": 2.0004, "price": 60.004})
		self.assertEqual(wizard.preview_changes(rounded), {"changes": NO_CHANGES, "problems": []})
		self.assertEqual(tier["price"], 60.004, "the draft itself is left as it is")
		# A draft that leaves the two keys out is a policy with the carrier and no card.
		without = {k: v for k, v in saved.items() if k not in ("price_source", "rate_card")}
		self.assertEqual(
			wizard.preview_changes(without)["changes"],
			{
				**NO_CHANGES,
				"rate_card": {
					"added": [],
					"removed": sorted(p["name"] for p in CARD["profiles"]),
					"changed": [],
				},
				"settings": ["rate_card"],
			},
			"no card in the draft is a card taken away",
		)
		# The keys have to be of the right kinds.
		for what, bad in (
			("a price source that is a number", {**saved, "price_source": 5}),
			("a price source that is a list", {**saved, "price_source": ["rate_card"]}),
			("a card that is a list", {**saved, "rate_card": []}),
			("a card that is text", {**saved, "rate_card": "x"}),
			("a card that is a number", {**saved, "rate_card": 5}),
		):
			with self.subTest(what):
				self.assertEqual(wizard.preview_changes(bad), {"changes": NO_CHANGES, "problems": [SHAPE]})
		self.assertEqual(snapshot(), before)

	def test_preview_changes_names_what_is_wrong_with_the_card(self):
		seed_policy(load_example(), active=True)
		draft = wizard.wizard_state()["policy"]
		# The rate card as the price source, and no card.
		bare = {**draft, "price_source": "rate_card"}
		self.assertEqual(
			wizard.preview_changes(bare)["problems"],
			["The price source is the rate card, but there is no rate card yet."],
		)
		# A card that breaks the card's own rules, in the policy's words.
		broken = with_card(draft)
		broken["rate_card"]["profiles"] = [
			p for p in broken["rate_card"]["profiles"] if p["name"] != "Rest of the World Allowed Items"
		]
		self.assertEqual(
			wizard.preview_changes(broken)["problems"],
			[
				"Rate card: No profile matches every item (a pattern of *), so items that match no profile would "
				"be left out of the price."
			],
		)
		# A country the ERP does not know, in the first zone of the card that lists it.
		self.assertFalse(frappe.db.exists("Country", {"code": "zz"}))
		unknown = with_card(draft)
		profile_named(unknown["rate_card"], "Dangerous Goods")["zones"][0]["countries"].append("ZZ")
		profile_named(unknown["rate_card"], "Refill Service")["zones"][0]["countries"].append("ZZ")
		self.assertEqual(
			wizard.preview_changes(unknown)["problems"],
			["Country code ZZ in zone USA refill (Refill Service) is not a country in the ERP."],
		)
		# The zones of the policy are checked as before.
		zone_named(unknown, "GCC")["countries"].append("QQ")
		self.assertEqual(
			wizard.preview_changes(unknown)["problems"],
			[
				"Country code QQ is not a country in the ERP.",
				"Country code ZZ in zone USA refill (Refill Service) is not a country in the ERP.",
			],
		)

	def test_apply_writes_price_source_and_the_card_atomically_and_refuses_a_broken_card(self):
		seed_policy(load_example(), active=True)
		before = snapshot()
		opened = wizard.wizard_state()
		self.assertEqual((opened["policy"]["price_source"], opened["policy"]["rate_card"]), ("carrier", None))
		draft = with_card(opened["policy"])
		preview = wizard.preview_changes(draft)
		self.assertEqual(preview["problems"], [])
		self.assertEqual(preview["changes"]["settings"], ["price_source", "rate_card"])
		self.assertEqual(
			preview["changes"]["rate_card"],
			{"added": sorted(p["name"] for p in CARD["profiles"]), "removed": [], "changed": []},
		)
		self.assertEqual(snapshot(), before, "a preview writes nothing")
		# A card that is broken, a country the ERP does not know and a price source with no card: refused whole.
		catch_all_less = with_card(opened["policy"])
		catch_all_less["rate_card"]["profiles"].pop()
		unknown = with_card(opened["policy"])
		profile_named(unknown["rate_card"], "Dangerous Goods")["zones"][0]["countries"].append("ZZ")
		no_card = {**opened["policy"], "price_source": "rate_card"}
		refusals = {
			"a card with no catch-all profile": (
				catch_all_less,
				"Rate card: No profile matches every item (a pattern of *), so items that match no profile would "
				"be left out of the price.",
			),
			"a country that is not in the ERP": (
				unknown,
				"Country code ZZ in zone USA (Dangerous Goods) is not a country in the ERP.",
			),
			"the rate card as the source, and no card": (
				no_card,
				"The price source is the rate card, but there is no rate card yet.",
			),
		}
		for what, (bad, sentence) in refusals.items():
			with self.subTest(what), self.assertRaises(frappe.ValidationError) as caught:
				apply(bad)
			self.assertEqual(messages(caught), [frappe.utils.escape_html(sentence)])
			self.assertEqual(snapshot(), before, "nothing was written")
		# A failure while the policy is saved takes the price source and the card back with everything else.
		validate = (
			"dsi_catalogue.dsi_catalogue.doctype.shipping_policy.shipping_policy.ShippingPolicy.validate"
		)
		with (
			mock.patch(validate, side_effect=frappe.ValidationError("disk full")),
			self.assertRaises(frappe.ValidationError),
		):
			apply(draft)
		self.assertEqual(snapshot(), before)
		self.assertIn(stored("rate_card"), (None, ""))
		# The same, when the last check of what was written finds a problem: by then the card has been saved.
		real_validate = wizard.validate_policy
		calls = []

		def refuses_the_second_look(policy):
			calls.append(policy)
			return real_validate(policy) if len(calls) == 1 else ["Injected problem."]

		at_rollback = []
		real_rollback = frappe.db.rollback

		def spy(**arguments):
			at_rollback.append(load_policy())
			return real_rollback(**arguments)

		with (
			mock.patch.object(wizard, "validate_policy", side_effect=refuses_the_second_look),
			mock.patch.object(frappe.db, "rollback", spy),
			self.assertRaises(frappe.ValidationError),
		):
			apply(draft)
		[held] = at_rollback
		self.assertEqual(
			(held["price_source"], held["rate_card"]), ("rate_card", CARD), "it had been written"
		)
		self.assertEqual(snapshot(), before, "and was taken back")
		# Now it is applied, whole.
		result = apply(draft)
		saved = load_policy()
		self.assertEqual((saved["price_source"], saved["rate_card"]), ("rate_card", CARD))
		self.assertEqual(stored("price_source"), "Rate card", "stored as the label")
		self.assertEqual(json.loads(stored("rate_card")), CARD)
		self.assertEqual(result["changes"]["settings"], ["price_source", "rate_card"])
		self.assertEqual(result["version"], wizard.wizard_state()["version"])
		# Live at once: a fragrance and a watch to the USA are priced from the card.
		priced = guest_price("US", items=sample_items())
		self.assertEqual(
			(
				priced["policy_active"],
				priced["price_source"],
				priced["offers"][0]["service_id"],
				first_amount(priced),
			),
			(True, "rate_card", "rate-card", 100.0),
		)
		# Applying what is saved writes nothing.
		kept = snapshot()
		again = wizard.wizard_state()
		self.assertEqual(
			wizard.apply_policy(again["policy"], again["version"]),
			{"version": again["version"], "changes": NO_CHANGES},
		)
		self.assertEqual(snapshot(), kept)

	def test_a_draft_that_changes_only_the_price_source_or_only_the_card_is_applied(self):
		seed_policy(card_policy(price_source="carrier"), active=True)
		[(refill, _)] = website_items(REFILL)
		opened = wizard.wizard_state()
		stamps = snapshot()["stamps"]
		draft = copy.deepcopy(opened["policy"])
		draft["price_source"] = "rate_card"
		result = wizard.apply_policy(draft, opened["version"])
		self.assertEqual(result["changes"], {**NO_CHANGES, "settings": ["price_source"]})
		self.assertEqual(stored("price_source"), "Rate card")
		self.assertEqual(load_policy()["rate_card"], CARD)
		self.assertEqual(snapshot()["stamps"], stamps, "no zone or item set was written")
		self.assertEqual(first_amount(guest_price("US", items=one_of(refill))), 60.0)
		# Only the card: one price of the Refill Service.
		opened = wizard.wizard_state()
		draft = copy.deepcopy(opened["policy"])
		profile_named(draft["rate_card"], "Refill Service")["zones"][0]["tiers"][0]["price"] = 61.0
		result = wizard.apply_policy(draft, opened["version"])
		self.assertEqual(
			result["changes"],
			{
				**NO_CHANGES,
				"rate_card": {"added": [], "removed": [], "changed": ["Refill Service"]},
				"settings": ["rate_card"],
			},
		)
		saved = profile_named(load_policy()["rate_card"], "Refill Service")
		self.assertEqual(saved["zones"][0]["tiers"][0]["price"], 61.0)
		self.assertEqual(first_amount(guest_price("US", items=one_of(refill))), 61.0, "live at once")
		self.assertEqual(result["version"], wizard.wizard_state()["version"])
		# And back to the carrier, which keeps the card for later.
		opened = wizard.wizard_state()
		draft = {**opened["policy"], "price_source": "carrier"}
		self.assertEqual(
			wizard.apply_policy(draft, opened["version"])["changes"],
			{**NO_CHANGES, "settings": ["price_source"]},
		)
		self.assertEqual(stored("price_source"), "Carrier")
		self.assertEqual(
			profile_named(load_policy()["rate_card"], "Refill Service")["zones"][0]["tiers"][0]["price"], 61.0
		)

	def test_prices_and_weights_of_a_card_are_stored_to_the_cent_and_the_gram(self):
		seed_policy(load_example(), active=True)
		draft = with_card(wizard.wizard_state()["policy"])
		tiers = profile_named(draft["rate_card"], "Dangerous Goods")["zones"][0]["tiers"]
		tiers[0].update({"from": 0.10004, "to": 0.60006, "price": 35.504})
		tiers[1]["price"] = 59.996
		tiers[2]["price"] = 12.347  # to the cent, not to the dollar
		apply(draft)
		saved_tiers = profile_named(load_policy()["rate_card"], "Dangerous Goods")["zones"][0]["tiers"]
		self.assertEqual(saved_tiers[0], {"from": 0.1, "to": 0.6, "price": 35.5})
		self.assertEqual(saved_tiers[1]["price"], 60.0)
		self.assertEqual(saved_tiers[2]["price"], 12.35)
		self.assertEqual(tiers[0]["price"], 35.504, "the draft itself is left as it is")
		text = stored("rate_card")
		self.assertNotIn("35.504", text)
		self.assertNotIn("0.10004", text)

	def test_a_stored_empty_price_source_reads_as_the_carrier(self):
		seed_policy(
			policy_of([zone_of("Gulf", ["AE"])], [rule_of("Gulf")]), active=False
		)  # no price source given
		opened = wizard.wizard_state()
		self.assertEqual((opened["policy"]["price_source"], opened["policy"]["rate_card"]), ("", None))
		before = snapshot()
		spelled = copy.deepcopy(opened["policy"])
		spelled["price_source"] = "carrier"
		bare = {
			key: value for key, value in opened["policy"].items() if key not in ("price_source", "rate_card")
		}
		for what, draft in (("spelled out", spelled), ("left out", bare), ("as it is", opened["policy"])):
			with self.subTest(what):
				self.assertEqual(wizard.preview_changes(draft), {"changes": NO_CHANGES, "problems": []})
				self.assertEqual(
					wizard.apply_policy(draft, opened["version"]),
					{"version": opened["version"], "changes": NO_CHANGES},
				)
				self.assertEqual(snapshot(), before, "nothing is written")
				self.assertEqual(
					first_amount(tried(draft, sample())["result"]), 38.2, "priced as the carrier"
				)
		# A change that is not about the source keeps what is stored.
		draft = copy.deepcopy(opened["policy"])
		draft["active"] = True
		wizard.apply_policy(draft, opened["version"])
		self.assertEqual(wizard.wizard_state()["policy"]["price_source"], "")

	def test_a_rate_card_that_cannot_be_saved_is_refused_and_the_stored_one_stays(self):
		seed_policy(card_policy(), active=True)
		opened = wizard.wizard_state()
		before = snapshot()
		deep = []
		for _level in range(1000):
			deep = [deep]
		looped = copy.deepcopy(CARD)
		looped["loop"] = looped
		mixed = copy.deepcopy(CARD)
		mixed[1] = "a number as a key"
		cannot_be_saved = {
			"a key nested 1,000 levels deep": {**CARD, "extra": deep},
			"a card that holds itself": looped,
			"keys of two kinds": mixed,
			"a key that is not text": {**CARD, "extra": {1: "a"}},
			"a number of 5,000 digits": {**CARD, "extra": 10**5000},
			"a value that is not data": {**CARD, "extra": {1, 2}},
			"more values than any card has": {**CARD, "extra": [0] * 600_000},
		}
		for what, card in cannot_be_saved.items():
			draft = {**opened["policy"], "rate_card": card}
			with self.subTest(what):
				self.assertEqual(wizard.preview_changes(draft)["problems"], [UNSAVABLE])
				with self.assertRaises(frappe.ValidationError) as caught:
					apply(draft)
				self.assertEqual(messages(caught), [UNSAVABLE])
				self.assertEqual(snapshot(), before)
				self.assertEqual(load_policy()["rate_card"], CARD, "the stored card was not erased")
		# Nothing is compared with such a card: it is the one problem and no changes.
		draft = {**opened["policy"], "rate_card": {**CARD, "extra": deep}}
		self.assertEqual(wizard.preview_changes(draft), {"changes": NO_CHANGES, "problems": [UNSAVABLE]})
		# The card is looked at before anything is written out: a card nested a hundred thousand deep would be
		# written as text of gigabytes by the Python of this site, and one 20,000 deep as 400 MB.
		hundred_thousand = []
		for _level in range(100_000):
			hundred_thousand = [hundred_thousand]
		with mock.patch.object(wizard, "rows_from_policy", side_effect=AssertionError("it was written out")):
			for card in ({**CARD, "extra": hundred_thousand}, looped):
				self.assertEqual(
					wizard.preview_changes({**opened["policy"], "rate_card": card}),
					{"changes": NO_CHANGES, "problems": [UNSAVABLE]},
				)
				with self.assertRaises(frappe.ValidationError):
					apply({**opened["policy"], "rate_card": card})
		self.assertEqual(snapshot(), before)
		# A card with more in it than it needs is fine while it is of a size a card has.
		ordinary = {
			**opened["policy"],
			"rate_card": {**CARD, "note": "kept", "more": {"a": [1, 2, None, True, "x"]}},
		}
		self.assertEqual(wizard.preview_changes(ordinary)["problems"], [])
		# Trying an order is not saving: it prices with such a card all the same.
		[(fragrance, _)] = website_items(FRAGRANCE)
		answer = tried(
			{**opened["policy"], "rate_card": {**CARD, "extra": deep}}, sample("US", one_of(fragrance))
		)
		self.assertEqual(first_amount(answer["result"]), 60.0)


# ---------------------------------------------------------------- test_integration
SECRET = "hunter2-SECRET"
THE_CARRIER = ("Aramex BH International PPX", "Aramex BH International PPX", 38.2)


def secrets_of_the_site() -> list[str]:
	"""Values that must never appear in an answer: what the site's configuration keeps secret."""
	found = [
		str(frappe.conf.get(key)) for key in ("db_password", "encryption_key", "secret_key", "jwt_secret")
	]
	return [value for value in found if value and value != "None"]


class TestTestIntegration(WizardTestCase):
	def test_test_integration_runs_all_three_checks_with_mocked_http(self):
		seed_policy(card_policy(), active=True)  # the rate card is the price source
		items = sample_items()
		answer = shipping_policy.price_delivery("US", json.dumps(items))
		amount = first_amount(answer)
		self.assertEqual(
			amount, 100.0, "a fragrance and a watch to the USA, each at the weight it defaults to"
		)
		erp, site = Reply(200, {"message": answer}), site_reply(amount)
		before, errors, shown = snapshot(), frappe.db.count("Error Log"), len(frappe.local.message_log)
		with (
			the_network(erp, site) as post,
			carrier(return_value=rates(THE_CARRIER)) as asked,
			recorded_queries() as queries,
		):
			result = wizard.test_integration()
		orjson.dumps(result)
		self.assertEqual(set(result), {"checks", "verdict", "order"})
		checks = result["checks"]
		self.assertEqual([check["name"] for check in checks], list(CHECK_NAMES))
		for check in checks:
			self.assertEqual(set(check), CHECK_KEYS)
			self.assertIs(check["ok"], True, check)
			self.assertIs(type(check["ms"]), int)
			self.assertGreaterEqual(check["ms"], 0)
			self.assertIsInstance(check["detail"], dict)
		self.assertEqual([check["status"] for check in checks], [200, None, 200])
		erp_check, carrier_check, website_check = checks
		self.assertEqual(
			erp_check["summary"],
			f"The ERP answered in {erp_check['ms']} ms: policy ON, price source rate card, 1 offer, $100.00.",
		)
		self.assertEqual(erp_check["detail"]["message"]["offers"][0]["amount"], 100.0)
		self.assertEqual(
			carrier_check["summary"], "The carrier returned 1 rate: Aramex BH International PPX $38.20."
		)
		self.assertEqual(
			website_check["summary"],
			f"The website answered in {website_check['ms']} ms with 1 delivery method: "
			"International Express $100.00. Its first amount matches the ERP's $100.00.",
		)
		self.assertEqual(website_check["detail"]["methods"][0]["id"], "rate-card")
		self.assertEqual(
			result["verdict"],
			"The ERP price API and the website agree: customers are charged $100.00 from the rate card, "
			"and the carrier is not asked.",
		)
		self.assertEqual(
			result["order"],
			{
				"country_code": "US",
				"country": "United States",
				"city": "New York",
				"items": items,
				"note": "",
			},
		)
		# What was sent: the ERP's own address and the configured website, nothing else.
		erp_call, site_call = post.calls
		self.assertEqual(erp_call["url"], frappe.utils.get_url() + PRICE_API)
		self.assertEqual(site_call["url"], WEBSITE + RATES_ROUTE)
		self.assertEqual(
			json.loads(erp_call["data"]), {"country_code": "US", "items": json.dumps(items)}
		)  # a rate-card policy needs no cartons and no quotes
		self.assertEqual(
			json.loads(site_call["data"]),
			{
				"cartItems": items,
				"shippingAddress": {"city": "New York", "country": "United States"},
				"currency": "USD",
			},
		)
		for call in post.calls:
			self.assertEqual(call["headers"], {"Content-Type": "application/json"}, "no other header")
			self.assertEqual(call["timeout"], 15)
			self.assertIs(call["allow_redirects"], False)
			self.assertIs(call["stream"], True)
			self.assertEqual(
				set(call), {"url", "data", "headers", "timeout", "allow_redirects", "stream", "auth"}
			)
			request = types.SimpleNamespace(headers={})
			self.assertIs(call["auth"](request), request, "no credentials, and none from a .netrc either")
			self.assertEqual(request.headers, {})
		for reply in (erp, site):
			self.assertTrue(reply.closed, "the connection is let go")
		# The carrier was asked once, for one sample parcel, in this process and without a postcode.
		asked.assert_called_once()
		self.assertNotIn("postcode", asked.call_args.kwargs)
		parcel = dict(asked.call_args.kwargs)
		dimensions = json.loads(parcel.pop("dimensions"))
		self.assertEqual(
			parcel,
			{"country_code": "US", "city": "New York", "weight_kg": 1, "cod_amount": 0, "currency": "USD"},
		)
		self.assertEqual(dimensions, {"box_code": "BOX_S", "length_cm": 20, "width_cm": 15, "height_cm": 10})
		# It is read only, and says nothing that is secret.
		self.assertEqual(snapshot(), before)
		self.assertEqual(frappe.db.count("Error Log"), errors)
		self.assertEqual(len(frappe.local.message_log), shown)
		other = [query for query in queries if not query.lstrip("( \n\t").lower().startswith("select")]
		self.assertEqual(other, [], "only SELECTs ran")
		text = json.dumps(result)
		for word in [*secrets_of_the_site(), "Content-Type", "Cookie", "cookie", "Authorization"]:
			self.assertNotIn(word, text)

	def test_test_integration_sends_a_sample_carton_and_no_quotes_when_the_carrier_is_the_price_source(self):
		seed_policy(card_policy(price_source="carrier"), active=True)
		items = sample_items()
		boxes = json.dumps([{"weight_kg": 1.0, "volume_l": 3.0}])
		answer = shipping_policy.price_delivery("US", json.dumps(items), boxes, "[]")
		self.assertEqual(
			(answer["offered"], answer["reason"]), (False, "not_offered"), "no quote, no fallback"
		)
		website = site_reply(38.2, "Aramex BH GCC PPX", "Aramex BH GCC PPX")
		with (
			the_network(Reply(200, {"message": answer}), website) as post,
			carrier(return_value=rates(THE_CARRIER)),
		):
			result = wizard.test_integration()
		erp_call, _ = post.calls
		self.assertEqual(
			json.loads(erp_call["data"]),
			{"country_code": "US", "items": json.dumps(items), "boxes": boxes, "quotes": "[]"},
		)
		erp_check, _, website_check = result["checks"]
		self.assertEqual(
			erp_check["summary"],
			f"The ERP answered in {erp_check['ms']} ms: policy ON, price source carrier, "
			"no delivery offered (not offered).",
		)
		self.assertTrue(all(check["ok"] for check in result["checks"]), result["checks"])
		self.assertNotIn(
			"matches", website_check["summary"], "nothing to compare with when the carrier prices"
		)
		self.assertEqual(
			result["verdict"],
			"The price source is the carrier, so customers pay the carrier's price through the policy's rules.",
		)

	def test_the_verdict_follows_what_the_three_checks_found(self):
		seed_policy(card_policy(), active=True)
		items = sample_items()
		answer = shipping_policy.price_delivery("US", json.dumps(items))
		no_rates = {"return_value": {"success": True, "rates": []}}
		good = {"return_value": rates(THE_CARRIER)}

		def erp():
			return Reply(200, {"message": answer})

		def disagreeing():
			return Reply(200, {"message": {**answer, "offers": [{**answer["offers"][0], "amount": 90.0}]}})

		cases = {
			"the website still asks the carrier": (
				erp,
				lambda: site_reply(38.2, "Aramex BH GCC PPX", "Aramex BH GCC PPX"),
				good,
				"The website still asks the carrier: it returned the carrier's methods, not the rate card's.",
			),
			"the website asks the carrier and the carrier gives it nothing": (
				erp,
				lambda: Reply(
					200,
					{
						"success": True,
						"package": {"total_units": 2, "total_boxes": 2, "boxes": []},
						"packingPlan": "volume_desc|min_delta_chargeable|lowest_chargeable",
						"methods": [],
						"defaultMethodId": None,
					},
				),
				no_rates,
				"The website still asks the carrier, and the carrier gave it no delivery method, so customers see none.",
			),
			"the website charges another price": (
				erp,
				lambda: site_reply(90.0),
				good,
				"The website charges $90.00 but the ERP price API charges $100.00 from the rate card: they disagree.",
			),
			"the website is limiting requests": (
				erp,
				lambda: Reply(429, {"error": "Too many requests"}),
				no_rates,
				"The ERP price API charges $100.00 from the rate card, but the website limits this check to "
				"10 requests a minute, so run it again in a minute.",
			),
			"the website is down": (
				erp,
				requests.exceptions.ConnectionError,
				good,
				"The ERP price API charges $100.00 from the rate card, but the website did not give a usable "
				"answer (see the third check).",
			),
			"the ERP does not answer": (
				requests.exceptions.ReadTimeout,
				lambda: site_reply(100.0),
				good,
				"The ERP price API did not give a usable answer, so the website cannot get delivery prices from "
				"the policy (see the first check).",
			),
			"the ERP disagrees with the policy": (
				disagreeing,
				lambda: site_reply(100.0),
				good,
				"The public ERP endpoint and the policy disagree about the price of the sample order, so fix "
				"that before trusting the website (see the first check).",
			),
		}
		for what, (erp_side, site_side, carrier_behaviour, verdict) in cases.items():
			with self.subTest(what), the_network(erp_side(), site_side()), carrier(**carrier_behaviour):
				self.assertEqual(wizard.test_integration()["verdict"], verdict)
		# Without the rate card as the price source the carrier is what matters. Nothing is sent but the cart.
		carton = json.dumps([{"weight_kg": 1.0, "volume_l": 3.0}])
		frappe.get_doc(POLICY).update({"active": 0}).save()
		own = shipping_policy.price_delivery("US", json.dumps(items), carton, "[]")
		with the_network(Reply(200, {"message": own}), site_reply(38.2)), carrier(**no_rates):
			result = wizard.test_integration()
		self.assertEqual(
			result["verdict"],
			"The shipping policy is switched off and the carrier returns no rates, so customers see no delivery method.",
		)
		with the_network(Reply(200, {"message": own}), site_reply(38.2)), carrier(**good):
			self.assertEqual(
				wizard.test_integration()["verdict"],
				"The shipping policy is switched off, so customers pay the carrier's own prices.",
			)
		frappe.get_doc(POLICY).update({"active": 1, "price_source": "Carrier"}).save()
		own = shipping_policy.price_delivery("US", json.dumps(items), carton, "[]")
		with the_network(Reply(200, {"message": own}), site_reply(38.2)), carrier(**no_rates) as asked:
			result = wizard.test_integration()
		asked.assert_called_once()
		self.assertEqual(
			result["verdict"],
			"The carrier returns no rates, so while the price source is Carrier customers see no delivery method.",
		)
		self.assertEqual(
			result["checks"][1]["summary"],
			"The carrier returned no rates (the carrier account may be blocked or may not serve this country). "
			"See the Error Log entries titled get_shipping_rates.",
		)
		self.assertFalse(result["checks"][1]["ok"])

	def test_test_integration_only_calls_the_configured_website(self):
		seed_policy(card_policy(), active=True)
		items = sample_items()
		erp = Reply(200, {"message": shipping_policy.price_delivery("US", json.dumps(items))})
		erp_url = frappe.utils.get_url() + PRICE_API
		never = AssertionError("the website was called")
		# Addresses that must never be called: each gives a website check that says it was not asked.
		hostile = [
			"http://169.254.169.254/latest/meta-data/",
			"https://user:pw@shop.example.com/",
			"file:///etc/passwd",
			"ftp://shop.example.com/",
			"https://[::1]/",
			"https://10.0.0.5/",
			"https://127.0.0.1/",
			"https://shop.example.com:8443/",
			"https://shop.example.com/shop",
			"https://shop.example.com/?x=1",
			"http://shop.example.com/",
			"https://intranet/",
			"https://" + "a" * 300 + ".com/",
			"",
			"   ",
			None,
			5,
		]
		for address in hostile:
			lookup = lookup_of(PUBLIC_ADDRESS)
			with (
				self.subTest(address=str(address)[:40]),
				the_network(erp, never, website=address, lookup=lookup) as post,
			):
				result = wizard.test_integration()
			site = result["checks"][2]
			self.assertEqual((site["name"], site["ok"], site["status"]), ("website", False, None))
			self.assertEqual(site["detail"], {"problem": "not_asked"})
			self.assertTrue(site["summary"].startswith("The website was not asked. "), site["summary"])
			self.assertEqual([call["url"] for call in post.calls], [erp_url], "only the ERP was called")
			self.assertEqual(lookup.asked, [], "and no name was looked up")
			for word in ("user:pw", "169.254", "etc/passwd", "10.0.0.5"):
				self.assertNotIn(word, json.dumps(result))
			self.assertTrue(result["verdict"].endswith("."))
		# A name that points at a private address is looked up and refused.
		for private in ("127.0.0.1", "10.1.2.3", "192.168.0.7", "169.254.169.254", "::1", "fd00::1"):
			lookup = lookup_of(private)
			with self.subTest(resolves_to=private), the_network(erp, never, lookup=lookup) as post:
				result = wizard.test_integration()
			self.assertEqual(lookup.asked, [("shop.example.com", 443)])
			self.assertEqual(
				result["checks"][2]["summary"],
				"The website was not asked. The website address resolves to a private or local network address.",
			)
			self.assertEqual([call["url"] for call in post.calls], [erp_url])
		# The address that is configured is the one that is called, whatever else the sample order says.
		lookup = lookup_of(PUBLIC_ADDRESS)
		sample_order = {
			"country_code": "GB",
			"website": "https://evil.example.com/",
			"url": "https://evil.example.com",
		}
		with the_network(
			erp, site_reply(100.0), website="  https://shop.example.com/ \n", lookup=lookup
		) as post:
			result = wizard.test_integration(sample_order)
		self.assertEqual(
			[call["url"] for call in post.calls],
			[erp_url, "https://shop.example.com/api/webshop/shipping/rates"],
		)
		self.assertEqual(lookup.asked, [("shop.example.com", 443)])
		self.assertNotIn("evil", json.dumps(result))
		# The address is written out once, in the one way every parser reads it: the scheme and the host in lower
		# case, no dot after the host, nothing else.
		for typed in (
			"HTTPS://Shop.Example.COM./ \n",
			"https://shop.example.com:443",
			"https://SHOP.example.com/",
		):
			lookup = lookup_of(PUBLIC_ADDRESS)
			with (
				self.subTest(typed=typed),
				the_network(erp, site_reply(100.0), website=typed, lookup=lookup) as post,
			):
				wizard.test_integration()
			port = ":443" if ":443" in typed else ""
			self.assertEqual(post.calls[1]["url"], f"https://shop.example.com{port}{RATES_ROUTE}")
			self.assertEqual(lookup.asked, [("shop.example.com", 443)])
		# A redirect is reported, never followed.
		with the_network(erp, Reply(302, raw=b"")) as post:
			result = wizard.test_integration()
		site = result["checks"][2]
		self.assertEqual((site["ok"], site["status"]), (False, 302))
		self.assertIn("redirect", site["summary"])
		self.assertEqual(len(post.calls), 2)
		self.assertTrue(all(call["allow_redirects"] is False for call in post.calls))

	def test_a_body_that_arrives_too_slowly_is_given_up_on_at_the_deadline(self):
		clock = [1000.0]

		def drip():
			for _ in range(100):
				clock[0] += 4.0  # a byte every four seconds: each wait is within the timeout of the call
				yield b" "

		reply = Reply(200, chunks=drip())
		with (
			mock.patch.object(requests, "post", return_value=reply),
			mock.patch.object(wizard.time, "monotonic", lambda: clock[0]),
		):
			status, ms, body, problem = wizard._post_json(WEBSITE + RATES_ROUTE, {})
		self.assertEqual((status, body, problem), (200, None, "timeout"))
		self.assertEqual(ms, 16000)
		self.assertEqual(reply.read, 4, "the deadline is looked at between bytes: 15 seconds, then no more")
		self.assertTrue(reply.closed)
		# A body that is in time is read to its end, and one that is not JSON says so.
		clock[0] = 1000.0
		quick = Reply(200, raw=b'{"a": 1}')
		with mock.patch.object(requests, "post", return_value=quick):
			self.assertEqual(wizard._post_json(WEBSITE + RATES_ROUTE, {})[::2], (200, {"a": 1}))

	def test_the_websites_address_is_the_one_storefront_settings_keeps(self):
		with mock.patch.object(frappe.db, "exists", return_value=False):
			self.assertEqual(
				wizard._website_address(), "", "a site without Storefront Settings has no address"
			)
		if not frappe.db.exists("DocType", "Storefront Settings"):
			self.assertEqual(wizard._website_address(), "", "erp2 has none")
		for stored, expected in (
			(WEBSITE, WEBSITE),
			(" https://a.example.com/ ", " https://a.example.com/ "),
			(None, ""),
			(5, ""),
			("", ""),
		):
			with (
				self.subTest(stored=stored),
				mock.patch.object(frappe.db, "exists", return_value=True),
				mock.patch.object(
					frappe, "get_single", return_value=frappe._dict(active_domain=stored)
				) as read,
			):
				self.assertEqual(wizard._website_address(), expected)
			read.assert_called_once_with("Storefront Settings")
		with (
			mock.patch.object(frappe.db, "exists", return_value=True),
			mock.patch.object(frappe, "get_single", side_effect=RuntimeError(SECRET)),
		):
			self.assertEqual(wizard._website_address(), "")

	def test_test_integration_survives_timeouts_and_garbage(self):
		seed_policy(card_policy(), active=True)
		items = sample_items()
		answer = shipping_policy.price_delivery("US", json.dumps(items))
		errors = frappe.db.count("Error Log")
		shown = len(frappe.local.message_log)

		def broken_half_way():
			yield b'{"message": {"poli'
			raise requests.exceptions.ChunkedEncodingError(SECRET)

		failures = {
			"a connection timeout": (
				lambda: requests.exceptions.ConnectTimeout(SECRET),
				"did not answer within 15 seconds",
			),
			"a read timeout": (
				lambda: requests.exceptions.ReadTimeout(SECRET),
				"did not answer within 15 seconds",
			),
			"a refused connection": (
				lambda: requests.exceptions.ConnectionError(SECRET),
				"could not be reached",
			),
			"a bad certificate": (lambda: requests.exceptions.SSLError(SECRET), "could not be reached"),
			"an address requests will not use": (
				lambda: requests.exceptions.InvalidURL(SECRET),
				"could not be reached",
			),
			"too many redirects": (
				lambda: requests.exceptions.TooManyRedirects(SECRET),
				"could not be reached",
			),
			"html from a proxy": (
				lambda: Reply(502, raw=f"<html>Bad Gateway {SECRET}</html>".encode()),
				"HTTP 502",
			),
			"an empty reply": (lambda: Reply(200, raw=b""), "but not with JSON"),
			"binary rubbish": (lambda: Reply(200, raw=bytes(range(256)) * 4), "but not with JSON"),
			"json nested too deep": (lambda: Reply(200, raw=b"[" * 60_000), "but not with JSON"),
			"a reply that breaks half way": (
				lambda: Reply(200, chunks=broken_half_way()),
				"but not with JSON",
			),
			"a reply over 64 KB": (lambda: Reply(200, chunks=[b" " * 8192] * 100), "more than 64 KB"),
			"json that is a list": (lambda: Reply(200, raw=b"[1, 2, 3]"), None),
			"json that is text": (lambda: Reply(200, raw=b'"hello"'), None),
			"json with the wrong shape": (lambda: Reply(200, {"message": "x"}), None),
			"not a number": (lambda: Reply(200, raw=b'{"message": NaN}'), "but not with JSON"),
			"an error with a secret in it": (
				lambda: Reply(500, {"exc_type": "Error", "password": SECRET, "api_key": SECRET}),
				"HTTP 500",
			),
		}
		for where in ("the ERP", "the website"):
			for what, (failure, words) in failures.items():
				bad = failure()
				erp_side = bad if where == "the ERP" else Reply(200, {"message": answer})
				site_side = bad if where == "the website" else site_reply(100.0)
				with (
					self.subTest(where=where, what=what),
					the_network(erp_side, site_side) as post,
					carrier(return_value=rates(THE_CARRIER)),
					mock.patch.object(frappe, "log_error") as logged,
				):
					result = wizard.test_integration()
				checks = result["checks"]
				broken, other = (checks[0], checks[2]) if where == "the ERP" else (checks[2], checks[0])
				self.assertEqual([check["name"] for check in checks], list(CHECK_NAMES))
				for check in checks:
					self.assertEqual(set(check), CHECK_KEYS)
				self.assertIs(broken["ok"], False)
				if words:
					self.assertIn(words, broken["summary"])
				self.assertIs(other["ok"], True, "the other call was not spoilt by it")
				self.assertTrue(checks[1]["ok"])
				self.assertTrue(result["verdict"].endswith("."))
				text = json.dumps(result)
				self.assertNotIn(SECRET, text)
				self.assertNotIn("Traceback", text)
				logged.assert_not_called()  # a failing call is an answer, not an error of ours
				if isinstance(bad, Reply):
					self.assertTrue(bad.closed)
					self.assertLessEqual(bad.read, 9, "no more than 64 KB is read")
				self.assertEqual(len(post.calls), 2)
		self.assertEqual(frappe.db.count("Error Log"), errors)
		self.assertEqual(len(frappe.local.message_log), shown)

	def test_the_carrier_check_survives_what_the_shipping_module_can_do(self):
		seed_policy(card_policy(), active=True)
		answer = shipping_policy.price_delivery("US", json.dumps(sample_items()))
		errors, shown = frappe.db.count("Error Log"), len(frappe.local.message_log)

		def throws(**arguments):
			frappe.throw(f"Aramex SOAP HTTP 500 at /RateCalculator: <soap:Fault>{SECRET}</soap:Fault>")

		cases = {
			"no rates (the module swallows what its carriers say)": (
				{"return_value": {"success": True, "rates": []}},
				"The carrier returned no rates (the carrier account may be blocked or may not serve this country). "
				"See the Error Log entries titled get_shipping_rates.",
			),
			"an error that says the account is blocked": (
				{"side_effect": RuntimeError(f"Account is blocked {SECRET}")},
				"The carrier says its account is blocked.",
			),
			"any other error": (
				{"side_effect": ValueError(SECRET)},
				"The carrier could not be asked: the call stopped with an error.",
			),
			"a message thrown by the module": (
				{"side_effect": throws},
				"The carrier could not be asked: the call stopped with an error.",
			),
			"an answer that is not rates": (
				{"return_value": "nope"},
				"The carrier did not answer with a list of rates.",
			),
			"a failure it reports": (
				{"return_value": {"success": False, "rates": []}},
				"The carrier did not answer with a list of rates.",
			),
		}
		for what, (behaviour, sentence) in cases.items():
			with (
				self.subTest(what),
				the_network(Reply(200, {"message": answer}), site_reply(100.0)),
				carrier(**behaviour),
				mock.patch.object(frappe, "log_error") as logged,
			):
				result = wizard.test_integration()
			check = result["checks"][1]
			self.assertEqual(
				(check["name"], check["ok"], check["status"], check["summary"]),
				("carrier", False, None, sentence),
			)
			self.assertNotIn(SECRET, json.dumps(result))
			self.assertNotIn("SOAP", json.dumps(result))
			self.assertEqual(
				len(frappe.local.message_log), shown, "the module's own message is not passed on"
			)
			self.assertTrue(result["checks"][0]["ok"] and result["checks"][2]["ok"])
			logged.assert_not_called()
		# The shipping module may not be there at all.
		with (
			the_network(Reply(200, {"message": answer}), site_reply(100.0)),
			modules({"dsi_shipping.api": None}),
		):
			check = wizard.test_integration()["checks"][1]
		self.assertEqual(
			(check["ok"], check["summary"]), (False, "The shipping module is not installed on this site.")
		)
		self.assertEqual(frappe.db.count("Error Log"), errors)

	def test_an_unexpected_error_is_logged_once_and_reported_plainly(self):
		seed_policy(card_policy(), active=True)
		answer = shipping_policy.price_delivery("US", json.dumps(sample_items()))
		real_post = wizard._post_json

		def breaks_on_the_website(url, payload):
			if url.endswith(RATES_ROUTE):
				frappe.msgprint(f"a message for the desk {SECRET}")
				raise ValueError(f"boom {SECRET}")
			return 200, 5, {"message": answer}, None

		shown = len(frappe.local.message_log)

		with (
			mock.patch.object(wizard, "_post_json", breaks_on_the_website),
			mock.patch.object(wizard, "_website_address", return_value=WEBSITE),
			mock.patch.object(wizard, "_resolve", public_lookup),
			mock.patch.object(frappe, "log_error") as logged,
			carrier(return_value=rates(THE_CARRIER)),
		):
			result = wizard.test_integration()
		self.assertIsNot(real_post, breaks_on_the_website)
		erp_check, carrier_check, website_check = result["checks"]
		self.assertEqual(
			website_check["summary"],
			"This check stopped because of an unexpected error, which was written to the Error Log.",
		)
		self.assertEqual(
			(website_check["ok"], website_check["status"], website_check["detail"]),
			(False, None, {"problem": "error"}),
		)
		self.assertTrue(erp_check["ok"] and carrier_check["ok"], "the checks before it ran")
		logged.assert_called_once()
		self.assertEqual(logged.call_args.kwargs["title"], "shipping policy integration test failed")
		self.assertIn("boom", logged.call_args.kwargs["message"], "the traceback is for the Error Log")
		self.assertNotIn("boom", json.dumps(result))
		self.assertNotIn(SECRET, json.dumps(result))
		self.assertEqual(
			len(frappe.local.message_log), shown, "what the failing check left for the desk is not passed on"
		)
		self.assertEqual(
			result["verdict"],
			"The ERP price API charges $100.00 from the rate card, but the website did not give a usable answer "
			"(see the third check).",
		)
		# Even the Error Log failing does not fail the answer.
		with (
			mock.patch.object(wizard, "_post_json", side_effect=ValueError("boom")),
			mock.patch.object(wizard, "_website_address", return_value=WEBSITE),
			mock.patch.object(wizard, "_resolve", public_lookup),
			mock.patch.object(frappe, "log_error", side_effect=RuntimeError("log is full")),
			carrier(return_value=rates(THE_CARRIER)),
		):
			self.assertEqual(len(wizard.test_integration()["checks"]), 3)

	def test_the_sample_order_comes_from_the_site_and_can_be_changed(self):
		items = sample_items()
		[(fragrance, _)] = website_items(FRAGRANCE)

		def ordered(order=None):
			"""The sample order of a run in which nothing answers."""
			dead = requests.exceptions.ConnectionError()
			with the_network(dead, dead), carrier(return_value=rates(THE_CARRIER)):
				return wizard.test_integration(order)["order"]

		default = {
			"country_code": "US",
			"country": "United States",
			"city": "New York",
			"items": items,
			"note": "",
		}
		self.assertEqual(ordered(), default)
		self.assertEqual(ordered(None), default)
		self.assertEqual(ordered({}), default)
		# The country, the city and the items can each be changed; JSON text is read like a dict.
		changed = ordered(
			{"country_code": "gb", "city": " London ", "items": [{"item_code": fragrance, "qty": 2}]}
		)
		self.assertEqual(
			changed,
			{
				"country_code": "GB",
				"country": "United Kingdom",
				"city": "London",
				"items": [{"item_code": fragrance, "qty": 2.0}],
				"note": "",
			},
		)
		self.assertEqual(
			ordered(json.dumps({"country_code": "gb", "city": " London ", "items": changed["items"]})),
			changed,
		)
		self.assertEqual(ordered({"city": "Paris"}), {**default, "city": "Paris"})
		by_name = ordered({"country": "France", "items": [{"item_code": "NO-SUCH-ITEM-ZZ", "qty": 1}]})
		self.assertEqual(
			(by_name["country_code"], by_name["country"], by_name["city"]), ("FR", "France", "New York")
		)
		self.assertEqual(
			by_name["items"], [{"item_code": "NO-SUCH-ITEM-ZZ", "qty": 1.0}], "a diagnostic: any code"
		)
		self.assertEqual(
			ordered({"country": " fr "})["country_code"], "FR", "a code in the country field is a code"
		)
		# What is left empty is not given: the default.
		for blank in (None, "", "  "):
			self.assertEqual(
				ordered({"city": blank, "country_code": blank, "country": blank, "items": []}), default
			)
		# What the site has decides the default: without a published fragrance or watch, others stand in.
		rows = [
			{"name": "WEB-ITM-0002", "item_code": "B-ITEM", "index_key": "{S-CR-ECS-RC}", "published": 0},
			{"name": "WEB-ITM-0001", "item_code": "A-ITEM", "index_key": "{S-CR-ECS-WC}", "published": 1},
		]
		with mock.patch.object(wizard, "_website_items", return_value=rows):
			stand_in = ordered()
		self.assertEqual([item["item_code"] for item in stand_in["items"]], ["A-ITEM", "B-ITEM"])
		self.assertTrue(stand_in["note"].endswith("."), stand_in["note"])
		with mock.patch.object(wizard, "_website_items", return_value=[]):
			empty = ordered()
		self.assertEqual((empty["items"], empty["country_code"]), ([], "US"))
		self.assertTrue(empty["note"])
		# The default is read with a single query of Website Items.
		with recorded_queries() as queries:
			ordered()
		self.assertEqual(len([q for q in queries if "tabWebsite Item" in q]), 1)

	def test_a_sample_order_that_cannot_be_read_is_refused_in_plain_words_before_anything_is_asked(self):
		before = snapshot()
		refused = {
			"text that is not JSON": "{",
			"a list": [{"city": "Paris"}],
			"a number": 5,
			"a country code that is not two letters": {"country_code": "UAE"},
			"a country code that is a number": {"country_code": 5},
			"a country the ERP does not know": {"country": "Atlantis"},
			"a country code the ERP does not know": {"country_code": "ZZ"},
			"a country that is a list": {"country": ["France"]},
			"a city that is too long": {"city": "C" * 141},
			"a city with a control character": {"city": "Par\x00is"},
			"a city that is a list": {"city": ["Paris"]},
			"items that are text": {"items": "A"},
			"items that are not objects": {"items": ["A"]},
			"a quantity below zero": {"items": [{"item_code": "A", "qty": -1}]},
			"an item code that is empty": {"items": [{"item_code": " ", "qty": 1}]},
			"more than ten items": {"items": [{"item_code": f"I{number}", "qty": 1} for number in range(11)]},
		}
		with mock.patch.object(requests, "post", side_effect=AssertionError("the network was asked")) as post:
			with carrier(side_effect=AssertionError("the carrier was asked")) as asked:
				for what, order in refused.items():
					with self.subTest(what), self.assertRaises(frappe.ValidationError) as caught:
						wizard.test_integration(order)
					[message] = messages(caught)
					self.assertTrue(message.endswith("."), message)
					self.assertNotIn("Traceback", message)
		post.assert_not_called()
		asked.assert_not_called()
		self.assertEqual(snapshot(), before)
		with self.assertRaises(frappe.ValidationError) as caught:
			wizard.test_integration(
				{"items": [{"item_code": f"I{number}", "qty": 1} for number in range(11)]}
			)
		self.assertEqual(messages(caught), ["The sample order can hold at most 10 items."])

	def test_the_new_methods_work_through_the_request_dispatcher(self):
		sales = make_user(self, "Sales User")
		posted = {
			"import_rate_card": {"config_csv": CONFIG_CSV, "not_delivered_csv": NOT_DELIVERED_CSV},
			"test_integration": {"order": json.dumps({"country_code": "GB", "city": "London"})},
		}
		self.assertEqual(tuple(posted), RATE_CARD_METHODS)
		dead = requests.exceptions.ConnectionError()
		with the_network(dead, dead) as post, carrier(return_value=rates(THE_CARRIER)):
			for user in ("Guest", sales):
				for method, arguments in posted.items():
					with self.subTest(user=user, method=method), self.assertRaises(frappe.PermissionError):
						dispatched(user, method, **arguments)
			self.assertEqual(post.calls, [], "a refusal comes before any call")
			read = dispatched("Administrator", "import_rate_card", **posted["import_rate_card"])
			self.assertEqual(read["summary"], IMPORTED_SUMMARY)
			ran = dispatched("Administrator", "test_integration", **posted["test_integration"])
			self.assertEqual((ran["order"]["country_code"], ran["order"]["city"]), ("GB", "London"))
			self.assertEqual(len(post.calls), 2)
			# It sends the server out to the network: a link (GET) cannot start it.
			with self.assertRaises(frappe.PermissionError):
				dispatched("Administrator", "test_integration", "GET", **posted["test_integration"])
			self.assertEqual(len(post.calls), 2)
