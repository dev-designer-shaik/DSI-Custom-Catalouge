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
"""

import ast
import copy
import importlib.util
import inspect
import json
import os
import random
import sys
import types
from contextlib import contextmanager, nullcontext
from unittest import mock

import frappe
import orjson
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
from dsi_catalogue.shipping_engine import normalize_country, validate_policy
from dsi_catalogue.shipping_policy import load_policy
from dsi_catalogue.shipping_policy_data import response_from, snapshot_version

# These tests make their own zones and item sets. Without this, Frappe would first create test records for
# everything the doctypes link to, and leave them on the site.
IGNORE_TEST_RECORD_DEPENDENCIES = ["Shipping Zone", "Shipping Item Set"]

METHODS = ("wizard_state", "try_order", "check_formula", "preview_changes", "apply_policy")
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
	rolls back at its end whatever its tests wrote; a commit would keep it, so any commit here fails the test."""

	def setUp(self):
		super().setUp()
		commit = mock.patch.object(
			frappe.db, "commit", side_effect=AssertionError("a test committed the transaction")
		)
		commit.start()
		self.addCleanup(commit.stop)


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
		}
		self.assertEqual(tuple(calls), METHODS)
		# A draft that changes something is also refused by the document permissions, further in.
		calls["apply_policy with a change"] = lambda: wizard.apply_policy({**draft, "active": True}, version)
		before = snapshot()
		sales = make_user(self, "Sales User")
		for user in ("Guest", sales):
			for method, call in calls.items():
				with self.subTest(user=user, method=method), as_user(user):
					with self.assertRaises(frappe.PermissionError):
						call()
		self.assertEqual(snapshot(), before, "nothing was written")
		# The control: the same user once it holds the System Manager role.
		frappe.get_doc("User", sales).add_roles("System Manager")
		with as_user(sales):
			self.assertEqual(wizard.wizard_state()["version"], version)
			self.assertTrue(wizard.check_formula("carrier_price * 1.1")["ok"])

	def test_each_method_checks_the_role_before_anything_else(self):
		tree = ast.parse(inspect.getsource(wizard))
		functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
		for method in METHODS:
			with self.subTest(method=method):
				body = functions[method].body
				if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
					body = body[1:]  # the docstring
				self.assertEqual(ast.unparse(body[0]), "frappe.only_for('System Manager')")

	def test_the_five_methods_are_whitelisted_for_users_and_none_for_guests(self):
		public = {
			name: function
			for name, function in vars(wizard).items()
			if inspect.isfunction(function)
			and function.__module__ == wizard.__name__
			and not name.startswith("_")
		}
		self.assertEqual({name for name, f in public.items() if f in frappe.whitelisted}, set(METHODS))
		self.assertEqual({name for name, f in public.items() if f in frappe.guest_methods}, set())
		signatures = {name: str(inspect.signature(getattr(wizard, name))) for name in METHODS}
		self.assertEqual(
			signatures,
			{
				"wizard_state": "() -> dict",
				"try_order": "(draft, order, ask_carrier=False, city=None) -> dict",
				"check_formula": "(source) -> dict",
				"preview_changes": "(draft) -> dict",
				"apply_policy": "(draft, expected_version) -> dict",
			},
		)
		# Apply is the only method that writes: it takes no GET request (a link cannot apply a policy).
		allowed = frappe.allowed_http_methods_for_whitelisted_func
		self.assertEqual(allowed[wizard.apply_policy], ["POST"])
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
