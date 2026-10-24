"""Pins the Shipping Policy doctypes, read straight from the files the desk will load.

Seven standard doctypes in the dsi_catalogue module: Shipping Zone, Shipping Item Set and the Shipping Policy
single, with four child tables between them (zone countries, item set groups, item set items, policy rules).
Nothing here needs Frappe or bench: the doctypes are JSON, and the tests check what the engine and the data
module rely on (field names, Select strings, defaults), who may edit them, and that the asterisk sentence is on
the form. The wizard page is pinned the same way: who may open it, that its script never writes text as HTML,
and that its style stays on the page.

The second half pins the way these files reach erp2: tools/shipping_policy_files.txt names every file, and
tools/erp2_push.sh copies only those, refuses anything outside its allow-list, and reloads the doctypes in
dependency order. It is run here against stand-ins for ssh and rsync, so nothing leaves the machine.
"""

import ast
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from dsi_catalogue import shipping_integration_check
from dsi_catalogue.shipping_engine import LABELS, _fallback_choice, resolve, validate_policy
from dsi_catalogue.shipping_policy_data import diff_policy, policy_from_rows, rows_from_policy
from dsi_catalogue.shipping_rate_card import card_summary, lb_text, parse_shopify_export, validate_card

ROOT = Path(__file__).resolve().parents[2]
DOCTYPE_DIR = ROOT / "dsi_catalogue" / "dsi_catalogue" / "doctype"
FIXTURE = Path(__file__).parent / "fixtures" / "shipping_policy_example.json"

# The sentence the plan's Global Constraints fix word for word.
ASTERISK = (
	"* A rule for selected items applies only when every item in the order is one of them. "
	"A rule for every item applies to every order."
)

ZONE, ITEM_SET, POLICY = "Shipping Zone", "Shipping Item Set", "Shipping Policy"
ZONE_COUNTRY, SET_GROUP, SET_ITEM, RULE = (
	"Shipping Zone Country",
	"Shipping Item Set Group",
	"Shipping Item Set Item",
	"Shipping Policy Rule",
)
TOP_LEVEL = (ZONE, ITEM_SET, POLICY)
CHILDREN = (ZONE_COUNTRY, SET_GROUP, SET_ITEM, RULE)
DOCTYPES = TOP_LEVEL + CHILDREN

# Every limit a rule can set, with the number of decimals it is kept to.
BANDS = {
	"min_order_value": "2",
	"max_order_value": "2",
	"min_weight_kg": "3",
	"max_weight_kg": "3",
	"min_volume_l": "3",
	"max_volume_l": "3",
}

# The smallest rate card `validate_card` accepts: one profile that takes every item, with one flat-price zone.
TINY_CARD = {
	"version": 1,
	"currency": "USD",
	"unit": "lb",
	"profiles": [
		{
			"name": "Everything",
			"match": ["*"],
			"zones": [
				{
					"name": "Gulf",
					"countries": ["AE"],
					"method": "Express (1-3 days)",
					"days": 3,
					"tiers": [{"from": None, "to": None, "price": 20.0}],
				}
			],
		}
	],
}

# The Select fields, and the engine's label table whose keys are their options, in order.
SELECTS = {
	(ZONE, "fallback_method"): "zone_fallback",
	(POLICY, "default_fallback_method"): "default_fallback",
	(POLICY, "not_covered"): "not_covered",
	(POLICY, "price_source"): "price_source",
	(RULE, "method"): "method",
}


def folder_of(name):
	return name.lower().replace(" ", "_")


def doctype(name):
	return json.loads((DOCTYPE_DIR / folder_of(name) / f"{folder_of(name)}.json").read_text(encoding="utf-8"))


def fields_of(name):
	return {field["fieldname"]: field for field in doctype(name)["fields"]}


def example():
	return json.loads(FIXTURE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- what kind of doctypes they are
@pytest.mark.parametrize("name", DOCTYPES)
def test_every_doctype_is_standard_and_in_the_module(name):
	data = doctype(name)
	assert data["doctype"] == "DocType"
	assert data["name"] == name
	assert data["module"] == "dsi_catalogue"
	assert "custom" not in data, "a standard doctype that ships with the app, not a Custom DocType"
	folder = DOCTYPE_DIR / folder_of(name)
	assert (folder / "__init__.py").is_file()
	controller = (folder / f"{folder_of(name)}.py").read_text(encoding="utf-8")
	assert f"class {name.replace(' ', '')}(Document)" in controller


@pytest.mark.parametrize("name", TOP_LEVEL)
def test_every_top_level_doctype_has_a_native_test_module(name):
	assert (DOCTYPE_DIR / folder_of(name) / f"test_{folder_of(name)}.py").is_file()


def test_the_policy_is_a_single_and_four_doctypes_are_child_tables():
	assert doctype(POLICY).get("issingle") == 1
	for name in (ZONE, ITEM_SET, *CHILDREN):
		assert not doctype(name).get("issingle"), name
	for name in CHILDREN:
		assert doctype(name).get("istable") == 1, name
	for name in TOP_LEVEL:
		assert not doctype(name).get("istable"), name


@pytest.mark.parametrize("name", DOCTYPES)
def test_nothing_can_be_renamed_and_the_lists_are_newest_first(name):
	data = doctype(name)
	assert data["allow_rename"] == 0
	assert data["sort_field"] == "modified"
	assert data["sort_order"] == "DESC"


@pytest.mark.parametrize("name", DOCTYPES)
def test_changes_are_tracked_on_the_three_top_level_doctypes_only(name):
	assert doctype(name).get("track_changes", 0) == (1 if name in TOP_LEVEL else 0)


def test_zones_and_item_sets_are_named_by_their_own_name_field():
	for name, field in ((ZONE, "zone_name"), (ITEM_SET, "set_name")):
		data = doctype(name)
		assert data["autoname"] == f"field:{field}"
		assert data["naming_rule"] == "By fieldname"
		assert fields_of(name)[field]["fieldtype"] == "Data"
		assert fields_of(name)[field]["reqd"] == 1
		assert fields_of(name)[field]["unique"] == 1
	for name in (POLICY, *CHILDREN):
		assert not doctype(name).get("autoname"), name


@pytest.mark.parametrize("name", DOCTYPES)
def test_field_order_lists_every_field_once(name):
	data = doctype(name)
	names = [field["fieldname"] for field in data["fields"]]
	assert len(names) == len(set(names)), "a field name is used twice"
	assert data["field_order"] == names


# ---------------------------------------------------------------- who may edit
RIGHTS = {
	ZONE: {"read", "write", "create", "delete"},
	ITEM_SET: {"read", "write", "create", "delete"},
	POLICY: {"read", "write"},
}


@pytest.mark.parametrize("name", TOP_LEVEL)
def test_only_system_manager_has_permissions(name):
	rows = doctype(name)["permissions"]
	assert [row["role"] for row in rows] == ["System Manager"]
	granted = {right for right, value in rows[0].items() if right != "role" and value}
	assert granted == RIGHTS[name]


@pytest.mark.parametrize("name", CHILDREN)
def test_child_doctypes_have_no_permissions_of_their_own(name):
	data = doctype(name)
	assert data["permissions"] == []
	assert data["editable_grid"] == 1


# ---------------------------------------------------------------- the Select strings and defaults
def test_select_options_match_the_engine_labels():
	found = {
		(name, fieldname)
		for name in DOCTYPES
		for fieldname, field in fields_of(name).items()
		if field["fieldtype"] == "Select"
	}
	assert found == set(SELECTS), "every Select field has a label table in the engine"
	for (name, fieldname), kind in SELECTS.items():
		field = fields_of(name)[fieldname]
		assert field["options"].split("\n") == list(LABELS[kind]), (name, fieldname)
		assert field["default"] == next(iter(LABELS[kind])), "the first label is the default"


def test_every_select_still_matches_the_engine_labels():
	"""The rate card added a Select and a choice to two others: all of it still matches the engine.

	Each Select lists exactly the labels of its table, in the engine's order, with no stray blanks. Each of
	the engine's tables is the options of one Select, so a table the doctypes do not offer (or a Select the
	engine has no table for) fails here. "Use the rate card" is the last choice of both fallbacks.
	"""
	selects = {
		(name, fieldname): field
		for name in DOCTYPES
		for fieldname, field in fields_of(name).items()
		if field["fieldtype"] == "Select"
	}
	assert set(selects) == set(SELECTS)
	assert sorted(SELECTS.values()) == sorted(LABELS), "each label table is the options of exactly one Select"
	for key, field in selects.items():
		options = field["options"].split("\n")
		assert options == list(LABELS[SELECTS[key]]), key
		assert all(option == option.strip() and option for option in options), key
		assert field["default"] == options[0], key
	for key in ((ZONE, "fallback_method"), (POLICY, "default_fallback_method")):
		assert selects[key]["options"].split("\n")[-2:] == ["Free", "Use the rate card"], key
		assert LABELS[SELECTS[key]]["Use the rate card"] == "rate_card"


def test_new_rule_rows_start_disabled():
	enabled = fields_of(RULE)["enabled"]
	assert enabled["fieldtype"] == "Check"
	assert enabled["default"] == "0"


def test_new_zones_start_enabled_and_as_ordinary_zones():
	fields = fields_of(ZONE)
	assert fields["enabled"]["default"] == "1"
	for name in ("not_shipped", "everywhere_else"):
		assert fields[name]["fieldtype"] == "Check"
		assert fields[name].get("default", "0") == "0"


def test_a_zones_texts_say_where_the_website_shows_them():
	"""Ruling P105 (final review I1, its residual): the website's checkout shows a zone's message only while the
	policy is on with the rate card as the price source (with the carrier it does not apply the policy yet). The
	zone form and the Zones step say so, true whatever the policy's state."""
	message = fields_of(ZONE)["not_offered_message"]
	assert message["description"] == (
		"Shown at checkout when delivery is not offered here (a Not shipped zone, a country the rate card does not "
		"cover, or a fallback that declines), once the policy is on with the rate card as the price source. Optional."
	)
	zones = (PAGE_DIR / "step_zones.js").read_text(encoding="utf-8")
	assert (
		"\"Delivery is not offered to the zone's countries. Once the policy is on with the rate card as the price "
		"source, checkout shows them the zone's message instead.\""
	) in zones
	assert "Checkout shows them the zone's message instead." not in served_page_script()
	# The zone's line is meant for the FAQ and the Shipping page, which the website does not show yet: it never
	# calls public_summary, in any mode (ruling P105).
	line = fields_of(ZONE)["customer_line"]
	assert (line["label"], line["description"]) == (
		"Line for customers",
		"One plain line meant for the FAQ and the Shipping page; the website does not show it yet. Optional.",
	)
	assert (
		"\"Each zone's line is meant for the FAQ and the Shipping page; the website does not show it yet. It is "
		'optional."'
	) in zones
	for label in (
		'"A zone\'s line"',
		'"A line about delivery to this zone"',
		'"The message when delivery is not offered here"',
	):
		assert label in zones, label
	served = served_page_script()
	for claim in ("The FAQ and the Shipping page show", "What customers read", "Customers read:"):
		assert claim not in served, claim


def test_policy_defaults_ship_the_policy_off():
	fields = fields_of(POLICY)
	assert fields["active"]["fieldtype"] == "Check"
	assert fields["active"]["default"] == "0"
	# On, the policy sets the price from the rate card; with the carrier as the price source the website does not
	# apply it yet, so customers still pay the carrier's own prices (ruling P100, final review I1).
	assert fields["active"]["description"] == (
		"Off: delivery costs stay exactly what the carrier quotes. On, with the rate card as the price source: "
		"customers pay from the rate card. On, with the carrier: the rules below are kept, but for now the "
		"website still charges the carrier's own prices."
	)
	assert fields["not_covered"]["default"] == "Pass the carrier price through"
	assert fields["default_fallback_method"]["default"] == "Don't offer delivery"


def test_policy_has_price_source_and_rate_card_fields_with_safe_defaults():
	data, fields = doctype(POLICY), fields_of(POLICY)
	source, card = fields["price_source"], fields["rate_card"]
	# The price source: the engine's two labels, and the carrier unless somebody chooses the card.
	assert (source["fieldtype"], source["label"]) == ("Select", "Price source")
	assert source["options"] == "Carrier\nRate card" == "\n".join(LABELS["price_source"])
	assert source["default"] == "Carrier"
	assert source["description"] == (
		"Carrier: the carrier's price with the rules below, but for now the website applies the policy only with "
		"the rate card, so customers still pay the carrier's own prices. "
		"Rate card: customers pay from the interim rate card and the carrier is not asked."
	)
	# The rate card: JSON text that the form shows and nobody types, there is none until the wizard saves one.
	assert (card["fieldtype"], card["label"], card["options"]) == ("Code", "Rate card", "JSON")
	assert card["read_only"] == 1
	assert card["description"] == "Edited in the wizard (Rates step); this is the stored copy."
	for field in (source, card):
		assert not field.get("reqd") and not field.get("hidden"), field["fieldname"]
	assert "default" not in card
	# Both sit right after Active, before the sections for the rules.
	order = data["field_order"]
	assert order[order.index("active") + 1 : order.index("active") + 3] == ["price_source", "rate_card"]
	assert order.index("rate_card") < order.index("sec_defaults")
	# What a policy that has never been saved reads as (the doctype's own defaults): off, the carrier, no
	# card, nothing to complain about. A policy saved before these fields existed has neither stored, and
	# reads the same.
	defaults = {name: field["default"] for name, field in fields.items() if "default" in field}
	fresh = policy_from_rows(defaults, [], [], {})
	assert (fresh["active"], fresh["price_source"], fresh["rate_card"]) == (False, "carrier", None)
	assert validate_policy(fresh) == []
	older = {key: value for key, value in defaults.items() if key not in ("price_source", "rate_card")}
	quotes = [{"service_id": "A", "name": "A", "amount": 12.5}]
	answer = resolve(
		{**policy_from_rows(older, [], [], {}), "active": True},
		{"country_code": "AE", "served": True, "boxes": [], "quotes": quotes},
	)
	assert (answer["price_source"], answer["offered"], answer["offers"][0]["amount"]) == (
		"carrier",
		True,
		12.5,
	)


def test_the_fixed_amount_shows_only_when_the_choice_is_a_fixed_amount():
	assert "Charge a fixed amount" in LABELS["zone_fallback"]
	assert "Charge a fixed amount" in LABELS["default_fallback"]
	zone = fields_of(ZONE)["fallback_amount"]
	policy = fields_of(POLICY)["default_fallback_amount"]
	assert zone["depends_on"] == "eval:doc.fallback_method == 'Charge a fixed amount'"
	assert policy["depends_on"] == "eval:doc.default_fallback_method == 'Charge a fixed amount'"


def test_a_formula_is_required_only_for_a_custom_formula_and_round_up_is_hidden_for_free():
	assert "Custom formula" in LABELS["method"]
	assert "Free" in LABELS["method"]
	fields = fields_of(RULE)
	assert fields["formula"]["fieldtype"] == "Small Text"
	assert fields["formula"]["depends_on"] == "eval:doc.method == 'Custom formula'"
	assert fields["formula"]["mandatory_depends_on"] == "eval:doc.method == 'Custom formula'"
	assert fields["round_up"]["depends_on"] == "eval:doc.method != 'Free'"


# ---------------------------------------------------------------- the asterisk sentence
def test_asterisk_sentence_is_on_the_form_and_the_item_set_field():
	note = fields_of(POLICY)["rules_note"]
	assert note["fieldtype"] == "HTML"
	assert ASTERISK in note["options"], "the policy form carries the sentence word for word"
	assert note["options"].startswith("<p ") and 'class="text-muted' in note["options"], (
		"in a muted paragraph"
	)
	assert ASTERISK in fields_of(RULE)["item_set"]["description"], "and so does the rule's item set field"


# ---------------------------------------------------------------- the fields the data module uses
def test_links_and_tables_point_at_the_right_doctypes():
	links = {
		(ZONE_COUNTRY, "country"): "Country",
		(SET_GROUP, "item_group"): "Item Group",
		(SET_ITEM, "item"): "Item",
		(RULE, "zone"): ZONE,
		(RULE, "item_set"): ITEM_SET,
	}
	for (name, fieldname), target in links.items():
		field = fields_of(name)[fieldname]
		assert (field["fieldtype"], field["options"]) == ("Link", target), (name, fieldname)
	tables = {
		(ZONE, "countries"): ZONE_COUNTRY,
		(ITEM_SET, "item_groups"): SET_GROUP,
		(ITEM_SET, "items"): SET_ITEM,
		(POLICY, "rules"): RULE,
	}
	for (name, fieldname), child in tables.items():
		field = fields_of(name)[fieldname]
		assert (field["fieldtype"], field["options"]) == ("Table", child), (name, fieldname)
	assert fields_of(RULE)["zone"]["reqd"] == 1
	for name, fieldname in ((ZONE_COUNTRY, "country"), (SET_GROUP, "item_group"), (SET_ITEM, "item")):
		assert fields_of(name)[fieldname]["reqd"] == 1, "a blank row means nothing"


def test_flags_are_checks_and_limits_are_floats_that_cannot_be_negative():
	checks = {
		(ZONE, "enabled"),
		(ZONE, "not_shipped"),
		(ZONE, "everywhere_else"),
		(POLICY, "active"),
		(RULE, "enabled"),
		(RULE, "round_up"),
	}
	for name, fieldname in checks:
		assert fields_of(name)[fieldname]["fieldtype"] == "Check", (name, fieldname)
	floats = {(ZONE, "fallback_amount"): "2", (POLICY, "default_fallback_amount"): "2"}
	floats.update({(RULE, fieldname): precision for fieldname, precision in BANDS.items()})
	for (name, fieldname), precision in floats.items():
		field = fields_of(name)[fieldname]
		assert field["fieldtype"] == "Float", (name, fieldname)
		assert field["non_negative"] == 1, (name, fieldname)
		assert field["precision"] == precision, (name, fieldname)


class Reads(dict):
	"""A row that notes every key the data module asks it for."""

	def __init__(self, row, seen):
		super().__init__(row)
		self.seen = seen

	def get(self, key, default=None):
		self.seen.add(key)
		return super().get(key, default)


def test_every_field_the_data_module_reads_exists():
	policy = example()
	policy["item_sets"][0]["items"] = ["WATCH-001"]  # so that the item rows are read as well
	names = {"BH": "Bahrain", "AE": "United Arab Emirates", "SA": "Saudi Arabia", "KW": "Kuwait"}
	names.update({"OM": "Oman", "QA": "Qatar", "RU": "Russian Federation", "BY": "Belarus"})
	rows = rows_from_policy(policy, names)
	seen = {kind: set() for kind in ("policy", "rule", "zone", "country", "set", "group", "item")}
	policy_row = Reads(
		{**rows["policy"], "rules": [Reads(rule, seen["rule"]) for rule in rows["policy"]["rules"]]},
		seen["policy"],
	)
	zone_rows = [
		Reads({**zone, "countries": [Reads(c, seen["country"]) for c in zone["countries"]]}, seen["zone"])
		for zone in rows["zones"]
	]
	set_rows = [
		Reads(
			{
				**item_set,
				"item_groups": [Reads(g, seen["group"]) for g in item_set["item_groups"]],
				"items": [Reads(i, seen["item"]) for i in item_set["items"]],
			},
			seen["set"],
		)
		for item_set in rows["item_sets"]
	]
	policy_from_rows(policy_row, zone_rows, set_rows, {name: code for code, name in names.items()})

	doctypes = {
		"policy": POLICY,
		"rule": RULE,
		"zone": ZONE,
		"country": ZONE_COUNTRY,
		"set": ITEM_SET,
		"group": SET_GROUP,
		"item": SET_ITEM,
	}
	for kind, name in doctypes.items():
		assert seen[kind], f"the data module read nothing from a {name} row"
		# The `name` of a rule row is the row's own name in the database, not a field.
		missing = seen[kind] - set(fields_of(name)) - ({"name"} if kind == "rule" else set())
		assert not missing, f"{name} has no field {sorted(missing)}, which policy_from_rows reads"
	written = {
		POLICY: {key for key in rows["policy"] if key != "rules"},
		RULE: {key for rule in rows["policy"]["rules"] for key in rule} - {"name"},
		ZONE: {key for zone in rows["zones"] for key in zone},
		ZONE_COUNTRY: {key for zone in rows["zones"] for c in zone["countries"] for key in c},
		ITEM_SET: {key for item_set in rows["item_sets"] for key in item_set},
		SET_GROUP: {key for item_set in rows["item_sets"] for g in item_set["item_groups"] for key in g},
		SET_ITEM: {key for item_set in rows["item_sets"] for i in item_set["items"] for key in i},
	}
	for name, keys in written.items():
		missing = keys - set(fields_of(name))
		assert not missing, f"{name} has no field {sorted(missing)}, which rows_from_policy writes"


def test_the_data_module_reads_the_new_fields():
	"""`price_source` and `rate_card` are asked of the policy row, written back, and are fields of the
	doctype, and the new choice of the two fallbacks is a label the doctypes list."""
	assert validate_card(TINY_CARD) == []
	policy = {
		"active": True,
		"price_source": "rate_card",
		"rate_card": TINY_CARD,
		"not_covered": "pass_through",
		"default_fallback": {"method": "rate_card", "amount": 0.0},
		"zones": [
			{
				"name": "Gulf",
				"enabled": True,
				"countries": ["AE"],
				"not_shipped": False,
				"everywhere_else": False,
				"customer_line": "",
				"not_offered_message": "",
				"fallback": {"method": "rate_card", "amount": 0.0},
			}
		],
		"item_sets": [],
		"rules": [],
	}
	rows = rows_from_policy(policy, {"AE": "United Arab Emirates"})
	row, fields = rows["policy"], fields_of(POLICY)
	assert row["price_source"] == "Rate card"
	assert json.loads(row["rate_card"]) == TINY_CARD, "the stored text is the card, as JSON"
	assert {"price_source", "rate_card"} <= set(fields)
	assert row["price_source"] in fields["price_source"]["options"].split("\n")
	assert row["default_fallback_method"] == "Use the rate card"
	assert row["default_fallback_method"] in fields["default_fallback_method"]["options"].split("\n")
	assert rows["zones"][0]["fallback_method"] == "Use the rate card"
	assert rows["zones"][0]["fallback_method"] in fields_of(ZONE)["fallback_method"]["options"].split("\n")

	seen = set()
	read = policy_from_rows(
		Reads(row, seen), rows["zones"], rows["item_sets"], {"United Arab Emirates": "AE"}
	)
	assert {"price_source", "rate_card"} <= seen, "policy_from_rows asks the row for both"
	assert (read["price_source"], read["rate_card"]) == ("rate_card", TINY_CARD)
	assert read["default_fallback"]["method"] == read["zones"][0]["fallback"]["method"] == "rate_card"
	assert read == policy, "and the whole policy comes back as it went"


# ---------------------------------------------------------------- the wizard page
PAGE_DIR = ROOT / "dsi_catalogue" / "dsi_catalogue" / "page" / "shipping_policy_wizard"
PAGE_SCRIPT = PAGE_DIR / "shipping_policy_wizard.js"
FORM_SCRIPT = DOCTYPE_DIR / "shipping_policy" / "shipping_policy.js"
# Ways a script could put text on the page as HTML (the desk's own dialogs among them: they show their message
# as HTML). The wizard builds its elements from text instead.
HTML_WRITERS = (
	"innerHTML",
	"outerHTML",
	"insertAdjacentHTML",
	"document.write",
	"createContextualFragment",
	"parseHTML",
	"$(",
	".html(",
	".append(`",
	".prepend(",
	".before(",
	".after(",
	".replaceWith(",
	"frappe.msgprint(",
	"frappe.show_alert(",
	"frappe.confirm(",
)
# The only calls of those the page may make, each with a fixed sentence of its own (never text from the server
# or from a person), and why it is safe.
ALLOWED_HTML_WRITERS = {
	'frappe.confirm(__("Discard your unsaved draft and start again from the saved policy?")': "a fixed question",
	'frappe.confirm(__("Delete this rule?")': "a fixed question",
	(
		"frappe.confirm(__(\"Remove this zone's countries, so that it takes every country that is in no other "
		'zone?")'
	): "a fixed question",
	'frappe.confirm(__("Discard your draft and load the policy as it is saved now?")': "a fixed question",
	'frappe.confirm(__("Replace the rate card in your draft with these rates?")': "a fixed question",
	'frappe.confirm(__("Delete this profile, with its zones and prices?")': "a fixed question",
	'frappe.confirm(__("Delete this zone, with its countries and prices?")': "a fixed question",
}
# The pattern of an include directive in Frappe's render_include (frappe/model/utils/__init__.py), which the
# desk runs on a page's script when it loads the page (Page.load_assets).
INCLUDE_DIRECTIVE = re.compile(r"""{% include\s['"](.*)['"]\s%}""")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed here")


def page_file(suffix):
	return (PAGE_DIR / f"shipping_policy_wizard{suffix}").read_text(encoding="utf-8")


def page_parts():
	"""The part files of the page script: every .js file of the page's folder but the entry file."""
	return sorted(path for path in PAGE_DIR.glob("*.js") if path != PAGE_SCRIPT)


def served_page_script():
	"""The page script as the desk serves it: render_include done the same way, at most five rounds, each
	directive replaced by the file it names (the path is read from the app's folder)."""
	content = PAGE_SCRIPT.read_text(encoding="utf-8")
	for _ in range(5):
		if "{% include" not in content:
			break
		paths = INCLUDE_DIRECTIVE.findall(content)
		assert paths, "text that starts a directive but is not one: render_include refuses the page"
		for path in paths:
			app, app_path = path.split("/", 1)
			included = (ROOT / app / app_path).resolve()
			assert app == "dsi_catalogue" and included.parent == PAGE_DIR, f"{path} is not in the page folder"
			text = included.read_text(encoding="utf-8").replace("\\", "\\\\")
			content = re.sub(rf"""{{% include\s['"]{path}['"]\s%}}""", text, content)
	return content


def css_selectors(css):
	"""Every selector of the rules in `css`, the rules inside an @media block included."""
	text, selectors, start = css, [], 0
	while (comment := text.find("/*")) != -1:
		text = text[:comment] + text[text.index("*/", comment) + 2 :]
	for position, character in enumerate(text):
		if character in "{}":
			if character == "{":
				prelude = text[start:position].strip()
				if not prelude.startswith("@"):
					selectors += [selector.strip() for selector in prelude.split(",")]
			start = position + 1
	return selectors


def test_the_wizard_page_is_a_standard_page_for_system_managers_only():
	page = json.loads(page_file(".json"))
	assert page["doctype"] == "Page"
	assert page["name"] == page["page_name"] == "shipping-policy-wizard"
	assert (page["module"], page["standard"], page["title"]) == (
		"dsi_catalogue",
		"Yes",
		"Shipping policy wizard",
	)
	assert page["roles"] == [{"role": "System Manager"}], "one role row: System Manager"
	assert page_file(".py").startswith("# Copyright"), "the Python file is the license stub"
	assert "def " not in page_file(".py")


def test_the_wizard_methods_that_write_or_reach_out_take_post_only():
	"""The page's seven methods, read from the wizard module's decorators, and the three that take POST only, so
	that a link opened by a signed-in System Manager (a GET) cannot start them: apply_policy writes, try_order asks
	the carrier once a carton (up to 50) when told to, and test_integration calls the ERP's own price API and the
	website (final review M5). The page sends every call as a POST (frappe.call)."""
	tree = ast.parse((ROOT / "dsi_catalogue" / "shipping_policy_wizard.py").read_text(encoding="utf-8"))
	whitelisted, post_only = set(), set()
	for function in (node for node in tree.body if isinstance(node, ast.FunctionDef)):
		for decorator in function.decorator_list:
			call = decorator if isinstance(decorator, ast.Call) else None
			if ast.unparse(call.func if call else decorator) != "frappe.whitelist":
				continue
			whitelisted.add(function.name)
			given = {
				keyword.arg: ast.literal_eval(keyword.value) for keyword in (call.keywords if call else [])
			}
			assert set(given) <= {"methods"}, f"{function.name}: {given}"
			if given.get("methods") == ["POST"]:
				post_only.add(function.name)
	assert whitelisted == {
		"wizard_state",
		"try_order",
		"check_formula",
		"preview_changes",
		"apply_policy",
		"import_rate_card",
		"test_integration",
	}
	assert post_only == {"try_order", "apply_policy", "test_integration"}


def test_the_page_script_is_its_parts_stitched_in_by_the_desk():
	entry = PAGE_SCRIPT.read_text(encoding="utf-8")
	directives = [line.strip() for line in entry.splitlines() if "{% include" in line]
	named = []
	for line in directives:
		found = INCLUDE_DIRECTIVE.fullmatch(line)
		assert found, f"one directive per line, and nothing else on it: {line}"
		named.append(found.group(1))
	assert len(named) == len(set(named)), "each part is stitched in once"
	assert sorted(Path(path).name for path in named) == [path.name for path in page_parts()], (
		"every .js file of the folder is a part, and every part is stitched in"
	)
	served = served_page_script()
	assert "{% include" not in served
	for part in page_parts():
		assert part.read_text(encoding="utf-8") in served, part.name
	css = page_file(".css")
	assert "{% include" not in css and "@import" not in css, "the desk reads the style as it is: one file"


@needs_node
def test_the_page_script_as_served_is_valid_javascript(tmp_path):
	served = tmp_path / "shipping_policy_wizard.served.js"
	served.write_text(served_page_script(), encoding="utf-8")
	result = subprocess.run(["node", "--check", str(served)], capture_output=True, text=True, timeout=60)
	assert result.returncode == 0, result.stderr


# The served script run under node, with stand-ins for the few desk globals it touches while it loads (and a
# tiny document for sp.el), so that its pure helpers can be called. `calls` is the body of a function of
# `sp` that returns plain data.
NODE_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
globalThis.__ = (text, args) => (args ? text.replace(/\{(\d+)\}/g, (_, i) => String(args[i])) : text);
globalThis.frappe = {
	provide(name) {
		globalThis[name] = globalThis[name] || {};
	},
	pages: { "shipping-policy-wizard": {} },
	session: { user: "wizard-test@dsi.local" },
	utils: {},
};
globalThis.document = {
	createElement(tag) {
		return {
			tag,
			attributes: {},
			listeners: {},
			children: [],
			setAttribute(key, value) {
				this.attributes[key] = value;
			},
			addEventListener(key, listener) {
				this.listeners[key] = listener;
			},
			append(...children) {
				this.children.push(...children);
			},
		};
	},
};
vm.runInThisContext(fs.readFileSync(process.argv[2], "utf8"), { filename: "shipping_policy_wizard.js" });
const calls = new Function("sp", fs.readFileSync(process.argv[3], "utf8"));
console.log(JSON.stringify(calls(globalThis.dsi_catalogue.sp)));
"""


def run_page_helpers(tmp_path, calls):
	served, harness, body = tmp_path / "served.js", tmp_path / "harness.js", tmp_path / "calls.js"
	served.write_text(served_page_script(), encoding="utf-8")
	harness.write_text(NODE_HARNESS, encoding="utf-8")
	body.write_text(calls, encoding="utf-8")
	command = ["node", str(harness), str(served), str(body)]
	result = subprocess.run(command, capture_output=True, text=True, timeout=60)
	assert result.returncode == 0, result.stderr
	return json.loads(result.stdout)


@needs_node
def test_a_typed_number_takes_commas_only_between_thousands(tmp_path):
	"""1,5 kg is never read as 15: a comma anywhere but between groups of thousands is refused (comma is set,
	for the tape to say "Use a period for decimals."), and the other refusals stay as they were."""
	read = {  # what is typed: [value, ok] or [value, ok, comma]
		"38.20": [38.2, True],
		"$38.20": [38.2, True],
		".5": [0.5, True],
		"38.": [38, True],
		"1,234.50": [1234.5, True],
		"1,234,567": [1234567, True],
		"1000000000": [1000000000, True],
		"": [None, True],
		"  ": [None, True],
		"1,5": [None, False, True],
		"12,34": [None, False, True],
		",5": [None, False, True],
		"1,2345": [None, False, True],
		"1,234,56": [None, False, True],
		"-5": [None, False],
		"1e5": [None, False],
		"Infinity": [None, False],
		"abc": [None, False],
		"1000000001": [None, False],
	}
	texts = json.dumps(list(read))
	got = run_page_helpers(
		tmp_path,
		f"return Object.fromEntries({texts}.map((text) => {{ const r = sp.read_number(text); "
		"return [text, r.comma ? [r.value, r.ok, true] : [r.value, r.ok]]; }));",
	)
	assert got == read
	above_zero = run_page_helpers(
		tmp_path, 'return ["0", "0.5", "2,5"].map((text) => sp.read_number(text, { above_zero: true }));'
	)
	assert above_zero == [
		{"value": None, "ok": False},
		{"value": 0.5, "ok": True},
		{"value": None, "ok": False, "comma": True},
	]


def test_the_wizard_page_puts_text_on_the_page_only_as_text():
	served = served_page_script()
	assert 'frappe.pages["shipping-policy-wizard"].on_page_load' in served
	for allowed in ALLOWED_HTML_WRITERS:
		assert served.count(allowed) == 1, f"the allowed call is there once: {allowed}"
	files = [("the served script", served)]
	files += [(path.name, path.read_text(encoding="utf-8")) for path in (PAGE_SCRIPT, *page_parts())]
	for name, text in files:
		for allowed in ALLOWED_HTML_WRITERS:
			text = text.replace(allowed, "")
		for writer in HTML_WRITERS:
			assert writer not in text, f"{name} uses {writer}"


@needs_node
def test_new_entries_of_the_draft_follow_the_servers_key_order(tmp_path):
	"""dirty is a JSON compare, key order included: a zone, rule or item set the wizard adds must have the keys
	the server reads back, in the same order, or an addition taken back would leave the draft dirty."""
	got = run_page_helpers(
		tmp_path,
		'return {zone: sp.new_zone("Gulf"), rule: sp.new_rule("Gulf"), item_set: sp.new_item_set("Watches")};',
	)
	example = json.loads(FIXTURE.read_text(encoding="utf-8"))
	read_back = policy_from_rows(
		{"rules": [{"zone": "Gulf", "method": "Carrier price"}]},  # a new row as the doctypes default it
		[{"zone_name": "Gulf", "enabled": 1, "fallback_method": "Use the policy default"}],
		[{"set_name": "Watches"}],
		{},
	)
	for kind, entries in (("zone", "zones"), ("rule", "rules"), ("item_set", "item_sets")):
		assert list(got[kind]) == list(example[entries][0]) == list(read_back[entries][0]), kind
	assert list(got["zone"]["fallback"]) == list(example["zones"][0]["fallback"])
	assert got["zone"] == read_back["zones"][0], "a new zone is what the server reads back for a new row"
	assert got["item_set"] == read_back["item_sets"][0]
	assert got["rule"] == {**read_back["rules"][0], "enabled": True}, "a rule added in the wizard is on"


@needs_node
def test_names_are_compared_as_the_erp_compares_them(tmp_path):
	"""The ERP's database reads names that differ only in capital letters or accents as one name, so the Zones and
	Rules steps compare zone and item set names so, and a new zone is named so that no other zone has its name."""
	got = run_page_helpers(
		tmp_path,
		"""
		return {
			same: [sp.fold_name(" Gùlf "), sp.fold_name("GULF"), sp.fold_name("gulf")],
			other: sp.fold_name("Gulf 2"),
			blank: [sp.fold_name(null), sp.fold_name(undefined)],
			fresh: sp.unique_name("New zone", ["Bahrain"]),
			second: sp.unique_name("New zone", ["new ZONE", "Bahrain"]),
			third: sp.unique_name("New zone", ["New zone", "Néw zone 2"]),
			lists: [sp.and_list([]), sp.and_list(["2"]), sp.and_list(["2", "3"]), sp.and_list(["2", "3", "4"])],
		};
		""",
	)
	assert got == {
		"same": ["gulf", "gulf", "gulf"],
		"other": "gulf 2",
		"blank": ["", ""],
		"fresh": "New zone",
		"second": "New zone 2",
		"third": "New zone 3",
		"lists": ["", "2", "2 and 3", "2, 3 and 4"],
	}


@needs_node
def test_a_rule_reads_as_one_sentence_for_any_step(tmp_path):
	"""sp.rule_words and sp.rule_text (wizard_rule_words.js) give a rule's words as plain text: the Rules step's
	cards show them in chips, and the Review step prints them."""
	rules = example()["rules"]
	limited = {**rules[2], "min_order_value": 100, "max_order_value": 500, "max_weight_kg": 2.5}
	got = run_page_helpers(
		tmp_path,
		f"""
		const rules = {json.dumps([*rules, limited, {**rules[0], "zone": "", "method": "formula"}])};
		return {{ texts: rules.map((rule) => sp.rule_text(rule)), words: sp.rule_words(rules[4]) }};
		""",
	)
	assert got["texts"] == [
		"In Bahrain for every item charge nothing (free)",
		"In GCC for Watches charge nothing (free)",
		"In GCC for every item charge the carrier's price, rounded up",
		"In Rest of world for every item charge max(carrier_price * 1.1, 45), rounded up",
		"In GCC for every item charge the carrier's price, rounded up "
		"(only when: order value from $100.00 up to $500.00 and weight up to 2.5 kg)",
		"In choose a zone for every item charge a formula",
	]
	assert got["words"] == {
		"zone": "GCC",
		"items": "every item",
		"charge": "the carrier's price",
		"rounded": True,
		"limits": "order value from $100.00 up to $500.00 and weight up to 2.5 kg",
	}


@needs_node
def test_the_fallback_step_offers_the_engines_choices_in_its_words(tmp_path):
	"""A zone's fallback, the policy default and "Anything not covered" offer the engine's choices (LABELS), in
	the order of the doctypes' Select options: the policy default has all of a zone's but the first."""
	got = run_page_helpers(
		tmp_path, "return {zone: sp.fallback_choices, not_covered: sp.not_covered_choices};"
	)
	zone = [[choice["label"], choice["method"]] for choice in got["zone"]]
	assert zone == [list(pair) for pair in LABELS["zone_fallback"].items()]
	assert zone[1:] == [list(pair) for pair in LABELS["default_fallback"].items()]
	not_covered = [[choice["label"], choice["method"]] for choice in got["not_covered"]]
	assert not_covered == [list(pair) for pair in LABELS["not_covered"].items()]


@needs_node
def test_a_fallback_is_said_as_the_engine_works_it_out(tmp_path):
	"""sp.fallback_outcome mirrors the engine's _fallback_choice for every way a draft can hold a fallback, a
	missing or broken one included, and sp.fallback_sentence says each outcome in one sentence."""
	cases = [  # [the policy default, the zone's fallback]
		[{"method": "no_delivery", "amount": 0}, {"method": "policy_default", "amount": 0}],
		[{"method": "fixed", "amount": 20}, {"method": "policy_default", "amount": 0}],
		[{"method": "free", "amount": 0}, {"method": "policy_default", "amount": 0}],
		[{"method": "rate_card", "amount": 0}, None],
		[{"method": "fixed", "amount": None}, {}],
		[{"method": "bogus"}, {"method": "policy_default"}],
		[None, {"method": "policy_default"}],
		[{"method": "no_delivery", "amount": 0}, {"method": "fixed", "amount": 35}],
		[{"method": "no_delivery", "amount": 0}, {"method": "fixed", "amount": 0}],
		[{"method": "no_delivery", "amount": 0}, {"method": "fixed", "amount": None}],
		[{"method": "no_delivery", "amount": 0}, {"method": "fixed", "amount": -1}],
		[{"method": "no_delivery", "amount": 0}, {"method": "free", "amount": 0}],
		[{"method": "no_delivery", "amount": 0}, {"method": "rate_card", "amount": 0}],
		[{"method": "fixed", "amount": 20}, {"method": "no_delivery", "amount": 0}],
		[{"method": "free", "amount": 0}, {"method": "bogus", "amount": 5}],
	]
	got = run_page_helpers(
		tmp_path,
		f"""
		return {json.dumps(cases)}.map(([fallback_default, own]) => {{
			const policy = {{ default_fallback: fallback_default }};
			const zone = {{ name: "Gulf", fallback: own }};
			return {{
				zone: sp.fallback_outcome(policy, zone),
				default: sp.fallback_outcome(policy, null),
				said: sp.fallback_sentence(policy, zone),
				default_said: sp.fallback_sentence(policy, null),
			}};
		}});
		""",
	)
	engine = {"fixed": "fixed", "free": "free", "rate_card": "rate_card", "none": "none", "missing": "none"}
	for (fallback_default, own), answer in zip(cases, got, strict=True):
		policy = {"default_fallback": fallback_default}
		for outcome, zone in ((answer["zone"], {"fallback": own}), (answer["default"], {"fallback": {}})):
			kind, amount = _fallback_choice(policy, zone)
			assert (engine[outcome["kind"]], outcome["amount"] or 0.0) == (kind, amount), (
				policy,
				zone,
				outcome,
			)
	sentences = [answer["said"] for answer in got]
	assert sentences[0] == "If the carrier cannot quote, the policy default applies: delivery is not offered."
	assert (
		sentences[1]
		== "If the carrier cannot quote, the policy default applies: customers pay a fixed $20.00."
	)
	assert sentences[2] == "If the carrier cannot quote, the policy default applies: delivery is free."
	assert sentences[3] == (
		"If the carrier cannot quote, the policy default applies: customers pay what the rate card charges."
	)
	assert sentences[4] == (
		"If the carrier cannot quote, the policy default applies: customers pay a fixed amount that is not typed yet."
	)
	assert sentences[7] == "If the carrier cannot quote, customers here pay a fixed $35.00."
	assert (
		sentences[9]
		== "If the carrier cannot quote, customers here pay a fixed amount that is not typed yet."
	)
	assert sentences[11] == "If the carrier cannot quote, delivery here is free."
	assert sentences[12] == "If the carrier cannot quote, customers here pay what the rate card charges."
	assert sentences[13] == "If the carrier cannot quote, delivery is not offered here."
	assert got[1]["default_said"] == (
		"When a zone uses the policy default and the carrier cannot quote, customers pay a fixed $20.00."
	)
	assert got[0]["default_said"] == (
		"When a zone uses the policy default and the carrier cannot quote, delivery is not offered."
	)


@needs_node
def test_what_will_change_is_said_in_short_plain_lines(tmp_path):
	"""sp.change_lines says diff_policy's answer (what preview_changes and apply_policy return) in the Review
	step's words, a line for each list and setting that changed, and nothing when nothing did."""
	empty = {
		"active": False,
		"price_source": "carrier",
		"rate_card": None,
		"not_covered": "pass_through",
		"default_fallback": {"method": "no_delivery", "amount": 0.0},
		"zones": [],
		"item_sets": [],
		"rules": [],
	}
	saved = example()
	for number, rule in enumerate(saved["rules"], start=1):
		rule["name"] = f"row-{number}"
	draft = json.loads(json.dumps(saved))
	draft["zones"] = [zone for zone in draft["zones"] if zone["name"] != "Not shipped"]
	draft["zones"][1]["customer_line"] = "Gulf delivery, changed."
	draft["item_sets"][0]["items"] = ["10000137"]
	draft["rules"][2]["label"] = "Gulf at the carrier price, edited"
	draft["rules"] = [
		draft["rules"][1],
		draft["rules"][0],
		draft["rules"][2],
		{**draft["rules"][0], "name": None},
	]
	draft["rules"][3]["label"] = ""
	draft.update(
		price_source="rate_card",
		rate_card=TINY_CARD,
		not_covered="no_delivery",
		default_fallback={"method": "fixed", "amount": 20.0},
	)
	cases = [
		[diff_policy(empty, {**saved, "active": True}), empty, {**saved, "active": True}],
		[diff_policy(saved, draft), saved, draft],
		[diff_policy(saved, saved), saved, saved],
		[
			diff_policy(
				{**saved, "rate_card": TINY_CARD}, {**saved, "rate_card": {**TINY_CARD, "version": 2}}
			),
			saved,
			saved,
		],
	]
	got = run_page_helpers(
		tmp_path,
		f"""
		return {json.dumps(cases)}.map(([changes, before, after]) =>
			sp.change_lines(changes, before, after).map((line) => sp.change_text(line)));
		""",
	)
	assert got[0] == [
		"Zones added: Bahrain, GCC, Not shipped and Rest of world",
		"Item sets added: Watches",
		"Rules added: Rule 1 (Free in Bahrain), Rule 2 (Free watches in the Gulf), Rule 3 (Gulf at the carrier "
		"price) and Rule 4 (International with a minimum)",
		"Active: off → on",
	]
	assert got[1] == [
		"Zones removed: Not shipped",
		"Zones changed: GCC",
		"Item sets changed: Watches",
		"Rules added: Rule 4 (in Bahrain)",
		"Rules removed: Rule 4 (International with a minimum)",
		"Rules changed: Rule 3 (Gulf at the carrier price, edited)",
		"The rules were reordered.",
		"Rate card profiles added: Everything",
		"The policy default: don't offer delivery → charge a fixed $20.00",
		"Anything not covered: pass the carrier price through → don't offer delivery",
		"Price source: carrier → rate card",
	]
	assert got[2] == [], "nothing changed: no line"
	assert got[3] == ["Rate card: changed"], "a change the profile lines cannot show"


@needs_node
def test_the_review_reads_the_policy_zone_by_zone_in_plain_words(tmp_path):
	"""sp.policy_summary says the whole draft: where prices start, then each zone (who is in it, what customers
	read, its rules in their order as sp.rule_statement says them, what happens when the carrier cannot quote),
	then what happens to an order no rule fits."""
	policy = example()
	policy["zones"].append(
		{
			"name": "Levant",
			"enabled": False,
			"countries": ["JO"],
			"not_shipped": False,
			"everywhere_else": False,
			"customer_line": "Levant delivery.",
			"not_offered_message": "",
			"fallback": {"method": "free", "amount": 0.0},
		}
	)
	policy["rules"][3]["enabled"] = False
	names = {
		"BH": "Bahrain",
		"AE": "United Arab Emirates",
		"SA": "Saudi Arabia",
		"KW": "Kuwait",
		"OM": "Oman",
	}
	names |= {"QA": "Qatar", "RU": "Russian Federation", "BY": "Belarus", "JO": "Jordan"}
	by_card = {**example(), "price_source": "rate_card", "zones": example()["zones"][:1]}
	got = run_page_helpers(
		tmp_path,
		f"""
		const names = {json.dumps(names)};
		const name = (code) => names[code] || code;
		return [sp.policy_summary({json.dumps(policy)}, name), sp.policy_summary({json.dumps(by_card)}, name),
			sp.policy_summary({{ zones: [], rules: [] }}, name)];
		""",
	)
	summary, card, empty = got
	# with the carrier as the price source the website does not apply the policy yet (ruling P100): said first
	assert summary["source"] == (
		"Prices start from the carrier's quote for each order. For now the website applies the policy only with "
		"the rate card as the price source, so customers still pay the carrier's own prices."
	)
	assert empty["source"] == summary["source"], (
		"a policy that does not name its price source prices by the carrier"
	)
	bahrain, gcc, rest, closed, levant = summary["zones"]
	assert bahrain == {
		"name": "Bahrain",
		"states": [],
		"where": "Bahrain.",
		"read": [
			"Line for the FAQ and the Shipping page, not shown on the website yet: “Free delivery in Bahrain.”"
		],
		"off": "",
		"never": "",
		"rules": ["Rule 1 (Free in Bahrain): In Bahrain, for every item, charge nothing (free)."],
		"no_rules": "",
		"fallback": "If the carrier cannot quote, the policy default applies: delivery is not offered.",
	}
	# apart by semicolons: a country's own name can hold a comma or an "and" (sp.country_list)
	assert gcc["where"] == "United Arab Emirates; Saudi Arabia; Kuwait; Oman; Qatar."
	assert gcc["rules"] == [
		"Rule 2 (Free watches in the Gulf): In GCC, for orders made up only of items in Watches, charge nothing (free).",
		"Rule 3 (Gulf at the carrier price): In GCC, for every item, charge the carrier's price, rounded up.",
	]
	assert gcc["fallback"] == "If the carrier cannot quote, customers here pay a fixed $35.00."
	assert rest["states"] == [{"kind": "everywhere_else", "text": "Everywhere else"}]
	assert rest["where"] == "Every other country."
	assert rest["rules"] == [
		"Rule 4 (International with a minimum) is off: In Rest of world, for every item, charge "
		"max(carrier_price * 1.1, 45), rounded up."
	]
	assert closed["states"] == [{"kind": "not_shipped", "text": "Not shipped"}]
	# the website's checkout shows a zone's message only while the policy is on with the rate card (ruling P105)
	assert closed["read"] == [
		"Line for the FAQ and the Shipping page, not shown on the website yet: “We do not deliver to these countries.”",
		"Once the policy is on with the rate card as the price source, checkout shows them: “We don't deliver to "
		"this country.”",
	]
	assert (closed["never"], closed["rules"], closed["fallback"]) == (
		"Delivery is never offered here.",
		[],
		"",
	)
	assert levant["states"] == [{"kind": "off", "text": "Off"}]
	assert levant["off"] == "This zone is off: its countries are priced as if it were not there."
	assert levant["read"] == [], "a zone that is off is shown to nobody"
	assert (levant["rules"], levant["no_rules"], levant["fallback"]) == ([], "", "")
	assert summary["closing"] == ["When no rule fits an order, customers pay the carrier's price."]
	# ruling P89: with the rate card as the price source the carrier is not asked; the summary says so first, and
	# still says the rules and the fallbacks as they stand in the draft, for the page to show them quietly
	assert card["source"] == (
		"Prices come from the rate card: the carrier is not asked, and the rules and fallbacks below are not used."
	)
	assert (card["unused"], summary["unused"]) == (True, False)
	assert card["zones"][0]["rules"] == [
		"Rule 1 (Free in Bahrain): In Bahrain, for every item, charge nothing (free)."
	]
	assert card["zones"][0]["fallback"] == (
		"If the carrier cannot quote, the policy default applies: delivery is not offered."
	)
	assert card["closing"] == ["When no zone or no rule fits an order, customers pay the carrier's price."]
	assert empty["closing"] == [
		"There is no zone yet.",
		"When no zone or no rule fits an order, customers pay the carrier's price.",
	]


@needs_node
def test_the_review_says_a_rule_with_commas_and_a_sets_condition(tmp_path):
	"""Ruling P90: the Review says a rule in the words of its card (sp.rule_words) with commas, and a rule for an
	item set says what the Rules step's asterisk says, that it is only for orders made up of items in the set.
	The Rules step's cards keep their own sentence (sp.rule_text)."""
	rules = example()["rules"]
	limited = {**rules[2], "min_order_value": 100, "max_order_value": 500, "max_weight_kg": 2.5}
	got = run_page_helpers(
		tmp_path,
		f"""
		const rules = {json.dumps([*rules, limited, {**rules[0], "zone": " ", "method": "formula"}])};
		return {{ said: rules.map((rule) => sp.rule_statement(rule)), cards: rules.map((rule) => sp.rule_text(rule)) }};
		""",
	)
	assert got["said"] == [
		"In Bahrain, for every item, charge nothing (free)",
		"In GCC, for orders made up only of items in Watches, charge nothing (free)",
		"In GCC, for every item, charge the carrier's price, rounded up",
		"In Rest of world, for every item, charge max(carrier_price * 1.1, 45), rounded up",
		"In GCC, for every item, charge the carrier's price, rounded up "
		"(only when: order value from $100.00 up to $500.00 and weight up to 2.5 kg)",
		"In choose a zone, for every item, charge a formula",
	]
	assert got["cards"][:4] == [
		"In Bahrain for every item charge nothing (free)",
		"In GCC for Watches charge nothing (free)",
		"In GCC for every item charge the carrier's price, rounded up",
		"In Rest of world for every item charge max(carrier_price * 1.1, 45), rounded up",
	], "the cards keep their words"


@needs_node
def test_the_review_reads_names_as_the_erp_keeps_them_without_outer_spaces(tmp_path):
	"""The ERP trims a zone's name, the zone a rule names, and an item set's name before it checks or writes them
	(shipping_policy_wizard._tidied), and the Zones step writes a name as it is typed, spaces and all, to the zone
	and its rules: the Review still finds a zone's rules, and says the names as they will be kept."""
	policy = example()
	policy["zones"][1]["name"] = "GCC "  # as typed in the Zones step, which writes it to the zone's rules too
	policy["rules"][1]["zone"] = policy["rules"][2]["zone"] = "GCC "
	policy["zones"][0]["name"] = " Bahrain"  # and a rule may name the zone without the spaces
	policy["item_sets"][0]["name"] = " Watches "
	policy["rules"][1]["item_set"] = " Watches "
	assert not validate_policy(
		{
			**policy,
			"zones": [{**zone, "name": zone["name"].strip()} for zone in policy["zones"]],
			"item_sets": [{**item_set, "name": item_set["name"].strip()} for item_set in policy["item_sets"]],
			"rules": [
				{**rule, "zone": rule["zone"].strip(), "item_set": (rule["item_set"] or "").strip() or None}
				for rule in policy["rules"]
			],
		}
	), "trimmed as the ERP trims them, the policy has no problem"
	got = run_page_helpers(tmp_path, f"return sp.policy_summary({json.dumps(policy)}, (code) => code);")
	bahrain, gcc = got["zones"][:2]
	assert (bahrain["name"], gcc["name"]) == ("Bahrain", "GCC")
	assert bahrain["rules"] == [
		"Rule 1 (Free in Bahrain): In Bahrain, for every item, charge nothing (free)."
	]
	assert gcc["rules"] == [
		"Rule 2 (Free watches in the Gulf): In GCC, for orders made up only of items in Watches, charge nothing (free).",
		"Rule 3 (Gulf at the carrier price): In GCC, for every item, charge the carrier's price, rounded up.",
	]
	assert (bahrain["no_rules"], gcc["no_rules"]) == ("", "")


@pytest.fixture
def wizard_module(monkeypatch):
	"""dsi_catalogue.shipping_policy_wizard, imported with a stand-in for frappe that has only what the module and
	shipping_policy need to load (the decorator, the translation function, a few names from frappe.utils): the
	functions used from it here are pure. The stand-in and the two modules are gone again after the test."""
	import importlib
	import sys
	import types

	pytest.importorskip("requests")
	pytest.importorskip("urllib3")
	fake = types.ModuleType("frappe")
	fake._ = lambda text: text
	fake.whitelist = lambda *args, **kwargs: lambda function: function
	utils = types.ModuleType("frappe.utils")
	for name in ("escape_html", "get_url", "sbool", "add_to_date", "getdate", "now_datetime"):
		setattr(utils, name, lambda *args, **kwargs: None)
	utils.flt = lambda value, places=None: (
		float(value or 0) if places is None else round(float(value or 0), places)
	)
	file_lock = types.ModuleType("frappe.utils.file_lock")
	file_lock.LockTimeoutError = type("LockTimeoutError", (Exception,), {})
	synchronization = types.ModuleType("frappe.utils.synchronization")
	synchronization.filelock = lambda *args, **kwargs: None
	model = types.ModuleType("frappe.model")
	base_document = types.ModuleType("frappe.model.base_document")
	base_document.BaseDocument = type("BaseDocument", (), {})
	fake.utils, fake.model = utils, model
	stand_ins = {
		"frappe": fake,
		"frappe.utils": utils,
		"frappe.utils.file_lock": file_lock,
		"frappe.utils.synchronization": synchronization,
		"frappe.model": model,
		"frappe.model.base_document": base_document,
	}
	for name, module in stand_ins.items():
		monkeypatch.setitem(sys.modules, name, module)
	loaded = ("dsi_catalogue.shipping_policy_wizard", "dsi_catalogue.shipping_policy")
	package = importlib.import_module("dsi_catalogue")
	try:
		yield importlib.import_module(loaded[0])
	finally:
		for name in loaded:
			sys.modules.pop(name, None)
			if hasattr(package, name.rsplit(".", 1)[1]):
				delattr(package, name.rsplit(".", 1)[1])


@needs_node
def test_a_problem_links_to_the_step_that_owns_it(tmp_path, wizard_module, monkeypatch):
	"""The Review step's "Fix it" goes to the step that owns a problem: sp.problem_step reads the way the engine's
	and the wizard's sentences start. Here are the engine's real sentences (validate_policy) and the wizard
	module's own (made by the functions preview_changes and apply_policy use), each made, not copied."""

	def problems(**policy):
		return validate_policy({"zones": [], "item_sets": [], "rules": [], **policy})

	zone = {"name": "A", "enabled": True, "countries": ["AE"], "fallback": {"method": "policy_default"}}
	rule = {"enabled": True, "zone": "A", "method": "carrier_price"}
	engine = {
		"fallback": [
			*problems(not_covered="bogus"),
			*problems(default_fallback={"method": "bogus"}),
			*problems(default_fallback={"method": "rate_card"}),
			*problems(default_fallback={"method": "fixed", "amount": "x"}),
			*problems(default_fallback={"method": "fixed", "amount": -5}),
			*problems(zones=[{**zone, "fallback": {"method": "bogus"}}]),
			*problems(zones=[{**zone, "fallback": {"method": "rate_card"}}]),
			*problems(zones=[{**zone, "fallback": {"method": "fixed", "amount": "x"}}]),
			*problems(zones=[{**zone, "fallback": {"method": "fixed", "amount": -1}}]),
			*problems(
				zones=[zone, {**zone, "name": "", "countries": [], "fallback": {"method": "rate_card"}}]
			)[1:],
		],
		"rates": [
			*problems(price_source="bogus"),
			*problems(price_source="rate_card"),
			*problems(rate_card={"profiles": "x"}),
		],
		"zones": [
			*problems(zones=[{**zone, "name": ""}]),
			*problems(zones=[zone, {**zone, "countries": ["SA"]}]),
			*problems(zones=[{**zone, "countries": ["XYZ"]}]),
			*problems(zones=[{**zone, "everywhere_else": True}]),
			*problems(zones=[zone, {**zone, "name": "B"}]),
			*problems(
				zones=[
					{**zone, "countries": [], "everywhere_else": True},
					{**zone, "name": "B", "countries": [], "everywhere_else": True},
				]
			),
		],
		"rules": [
			*problems(item_sets=[{"name": "", "items": ["X"]}]),
			*problems(item_sets=[{"name": "S", "items": []}]),
			*problems(item_sets=[{"name": "S", "items": ["X"]}, {"name": "S", "items": ["Y"]}]),
			*problems(zones=[zone], rules=[{**rule, "zone": ""}]),
			*problems(zones=[zone], rules=[{**rule, "zone": "Z"}]),
			*problems(zones=[zone], rules=[{**rule, "item_set": "Missing"}]),
			*problems(zones=[zone], rules=[{**rule, "method": "bogus"}]),
			*problems(zones=[zone], rules=[{**rule, "method": "formula", "formula": ""}]),
			*problems(zones=[zone], rules=[{**rule, "method": "formula", "formula": "carrier_price +"}]),
			*problems(zones=[zone], rules=[{**rule, "min_weight_kg": 5, "max_weight_kg": 2}]),
			*problems(zones=[zone], rules=[{**rule, "min_order_value": -1}]),
		],
	}
	assert all(engine.values()), "each kind of problem was made"
	wizard = wizard_module
	empty = {"zones": [], "item_sets": [], "default_fallback": None}
	saved = {"zones": [{"name": "Gulf"}], "item_sets": [{"name": "W"}]}
	card = {
		**TINY_CARD,
		"profiles": [
			{
				**TINY_CARD["profiles"][0],
				"zones": [{**TINY_CARD["profiles"][0]["zones"][0], "countries": ["ZZ"]}],
			}
		],
	}
	# A disabled zone of a draft whose countries have no two-letter code: one look in the Country list names
	# them (the database compares codes without case); a code no Country has is shown as it is.
	asked = []

	def get_all(doctype, filters=None, fields=None, **more):
		asked.append((doctype, filters))
		return [{"name": "United Kingdom", "code": "gbr"}]

	monkeypatch.setattr(wizard.frappe, "get_all", get_all, raising=False)
	parked = wizard._unknown_countries(
		{"zones": [{"name": "A", "enabled": False, "countries": ["GBR", "XYZ", "AE"]}]}, {"AE": "UAE"}
	)
	assert parked == [
		'Zone "A" is off but lists United Kingdom (code "gbr") and "XYZ", which have no two-letter code in '
		"the Country list, so the zone cannot be saved as it is. Remove them from the zone, remove the zone, "
		"or give them two-letter codes in the Country list."
	]
	assert asked == [("Country", {"code": ["in", ["GBR", "XYZ"]]})], "one look, at the codes that are wrong"
	made = {
		"zones": [
			*wizard._unknown_countries({"zones": [{"countries": ["ZZ"]}]}, {}),
			*parked,
			*wizard._saving_problems({**empty, "zones": [{"name": "Gulf"}, {"name": "gulf"}]}, empty),
			*wizard._saving_problems({**empty, "zones": [{"name": "gulf"}]}, saved),
		],
		"rates": [
			*wizard._unknown_card_countries(card, {}),
			*wizard._unsavable_card({"profiles": [{"name": float("nan")}]}),
		],
		"rules": [
			*wizard._saving_problems({**empty, "item_sets": [{"name": "W"}, {"name": "w"}]}, empty),
			*wizard._saving_problems({**empty, "item_sets": [{"name": "w"}]}, saved),
		],
		"fallback": [
			*wizard._saving_problems({**empty, "default_fallback": {"method": "free", "amount": -1}}, empty),
			*wizard._saving_problems(
				{**empty, "zones": [{"name": "A", "fallback": {"method": "free", "amount": -1}}]}, empty
			),
		],
		None: [wizard.SHAPE_PROBLEM],
	}
	assert [len(listed) for listed in made.values()] == [4, 2, 2, 2, 1], (
		"each of the wizard's own sentences was made"
	)
	wizard = made
	sentences = [
		[step, sentence]
		for found in (engine, wizard)
		for step, listed in found.items()
		for sentence in listed
	]
	got = run_page_helpers(
		tmp_path, f"return {json.dumps(sentences)}.map(([, sentence]) => sp.problem_step(sentence));"
	)
	for (step, sentence), answer in zip(sentences, got, strict=True):
		assert answer == step, f"{sentence} -> {answer}, not {step}"


@needs_node
def test_a_refusal_is_read_a_sentence_a_line_as_plain_text(tmp_path):
	"""sp.refusal_lines reads the sentences of a refusal from Frappe's message path: one message, a list of them
	(apply_policy refuses with a list, escaped), sentences Frappe joined with <br> (a line each), what a code
	without a handler of its own hands over (the request), and nothing at all. Each goes through
	frappe.utils.html2text, here a stand-in that decodes (the browser script checks the real one)."""
	listed = json.dumps(
		{
			"message": [
				"Zone &quot;Gulf &amp; &lt;b&gt;Co&lt;/b&gt;&quot; has a problem.",
				"Rule 2 has another.",
			],
			"as_list": 1,
		}
	)
	single = json.dumps({"message": "Someone else is applying a change right now. Try again in a moment."})
	joined = json.dumps({"message": "One sentence.<br>Another one.<BR/>A &lt;b&gt;third&lt;/b&gt;."})
	got = run_page_helpers(
		tmp_path,
		f"""
		frappe.utils.html2text = (html) => html.replace(/<[^>]*>/g, "").replace(/&lt;/g, "<")
			.replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&amp;/g, "&");
		const messages = (...entries) => JSON.stringify(entries);
		return [
			sp.refusal_lines({{ _server_messages: messages({json.dumps(listed)}) }}),
			sp.refusal_lines({{ _server_messages: messages({json.dumps(single)}, "A <b>plain</b> sentence.") }}),
			sp.refusal_lines({{ getResponseHeader() {{}}, responseJSON: {{ _server_messages: messages({json.dumps(single)}) }} }}),
			sp.refusal_lines(undefined),
			sp.refusal_lines({{ _server_messages: "not a list" }}),
			sp.refusal_lines({{ _server_messages: messages({json.dumps(joined)}) }}),
		];
		""",
	)
	assert got == [
		['Zone "Gulf & <b>Co</b>" has a problem.', "Rule 2 has another."],
		["Someone else is applying a change right now. Try again in a moment.", "A plain sentence."],
		["Someone else is applying a change right now. Try again in a moment."],
		[],
		[],
		["One sentence.", "Another one.", "A <b>third</b>."],
	]


# ---------------------------------------------------------------- the Rates step and the Test tab
RATES_SAMPLE = Path(__file__).parent / "fixtures" / "shopify_shipping_config_sample.csv"
RATES_SAMPLE_CLOSED = Path(__file__).parent / "fixtures" / "shopify_countries_not_delivered_sample.csv"


def sample_card():
	"""The repository's trimmed sample of the Shopify export, read as import_rate_card reads it."""
	config = RATES_SAMPLE.read_bytes().decode("utf-8")
	closed = RATES_SAMPLE_CLOSED.read_bytes().decode("utf-8")
	return parse_shopify_export(config, closed)["card"]


def key_orders(value, path="card"):
	"""[(where, [keys in order])] of every dict in a value: what a JSON compare sees beyond the values."""
	if isinstance(value, dict):
		found = [(path, list(value))]
		for key, entry in value.items():
			found += key_orders(entry, f"{path}.{key}")
		return found
	if isinstance(value, list):
		return [order for index, entry in enumerate(value) for order in key_orders(entry, f"{path}[{index}]")]
	return []


@needs_node
def test_a_rate_card_the_wizard_takes_or_adds_to_has_its_keys_in_the_order_the_erp_keeps(tmp_path):
	"""dirty is a JSON compare, key order included, and the ERP keeps a rate card as JSON text with its keys sorted
	(shipping_policy_data._card_text), which is how wizard_state hands a saved card back. So the Rates step takes an
	imported card in that order (sp.card_as_kept), and the profile, zone and tier it adds have their keys in it too:
	the same card imported again, or an entry added and taken back, leaves the draft clean."""
	card = sample_card()
	got = run_page_helpers(
		tmp_path,
		f"""
		return {{
			card: sp.card_as_kept({json.dumps(card)}),
			profile: sp.new_card_profile("Pens"),
			zone: sp.new_card_zone("Gulf"),
			tier: sp.new_card_tier(0.01, 2, 40),
		}};
		""",
	)
	kept = policy_from_rows(rows_from_policy({"rate_card": card}, {})["policy"], [], [], {})["rate_card"]
	assert got["card"] == kept, "the same card"
	assert key_orders(got["card"]) == key_orders(kept), "with its keys in the order the ERP keeps them"
	assert key_orders(card) != key_orders(kept), "which is not the order the import gives"
	assert list(got["profile"]) == list(kept["profiles"][0]) == ["match", "name", "zones"]
	assert (
		list(got["zone"])
		== list(kept["profiles"][0]["zones"][0])
		== ["countries", "days", "method", "name", "tiers"]
	)
	assert list(got["tier"]) == list(kept["profiles"][0]["zones"][0]["tiers"][0]) == ["from", "price", "to"]
	# a new zone has one price at any weight, not typed yet: the card says so until it is typed
	assert got["zone"]["tiers"] == [{"from": None, "price": None, "to": None}]
	added = {**card, "profiles": [*card["profiles"], {**got["profile"], "zones": [got["zone"]]}]}
	assert any("has a price that has to be a number" in problem for problem in validate_card(added))


@needs_node
def test_the_rate_card_is_said_in_plain_words(tmp_path, wizard_module):
	"""The words of the card (wizard_rate_card.js): how much it holds, as card_summary counts it; a weight as the
	card's own sentences write it (lb_text); what a zone charges; the Review's lines of the card; whether anything
	uses the card, as shipping_policy.fallback_may_use_the_card answers for an enabled zone or the policy default;
	and a text's length as the server counts it."""
	card = sample_card()
	weights = [0.7, 2.0, 18.01, 1.984, 0.772, 4.1, 0.01, 10000.0]
	usa = card["profiles"][2]["zones"][0]
	flat = {"tiers": [{"from": None, "price": 35.0, "to": None}]}
	free = {"tiers": [{"from": None, "price": 0.0, "to": None}]}
	blank = {"tiers": [{"from": None, "price": None, "to": None}]}
	missing = {"tiers": [{"from": 0.01, "price": 40.0, "to": 2.0}, {"from": 2.01, "price": None, "to": 4.0}]}
	closed = {"name": "Not delivered", "enabled": True, "not_shipped": True, "countries": ["AF", "RU"]}
	policy = {"price_source": "rate_card", "rate_card": card, "zones": [closed]}
	names = {
		"AE": "United Arab Emirates",
		"US": "United States",
		"AF": "Afghanistan",
		"RU": "Russian Federation",
	}
	fallbacks = [
		{"default_fallback": {"method": "rate_card"}, "zones": []},
		{
			"default_fallback": {"method": "no_delivery"},
			"zones": [{"enabled": True, "fallback": {"method": "rate_card"}}],
		},
		{
			"default_fallback": {"method": "no_delivery"},
			"zones": [{"enabled": False, "fallback": {"method": "rate_card"}}],
		},
		{
			"default_fallback": {"method": "fixed"},
			"zones": [{"enabled": True, "fallback": {"method": "policy_default"}}],
		},
	]
	got = run_page_helpers(
		tmp_path,
		f"""
		const names = {json.dumps(names)};
		return {{
			counts: sp.card_counts({json.dumps(card)}),
			words: sp.counts_words(sp.card_counts({json.dumps(card)})),
			one: sp.counts_words({{ profiles: 1, zones: 1, tiers: 1, countries: 1 }}),
			pounds: {json.dumps(weights)}.map((weight) => sp.pounds(weight)),
			prices: [{json.dumps([usa, flat, free, blank, missing, {"tiers": []}])}].flat().map((zone) => sp.zone_price_words(zone)),
			summary: sp.card_summary({json.dumps(policy)}, (code) => names[code] || code),
			none: sp.card_summary({{ zones: [] }}),
			review: sp.policy_summary({json.dumps(policy)}, (code) => names[code] || code).card,
			carrier: sp.policy_summary({{ ...{json.dumps(policy)}, price_source: "carrier" }}).card,
			uses: {json.dumps(fallbacks)}.map((policy) => sp.uses_the_card(policy)),
			source: sp.uses_the_card({{ price_source: "rate_card", zones: [] }}),
			length: sp.text_length("a\\ud83d\\ude00b"),
		}};
		""",
	)
	assert got["counts"] == card_summary(card)
	assert got["words"] == "4 profiles, 10 zones, 20 weight tiers and 18 countries"
	assert got["one"] == "1 profile, 1 zone, 1 weight tier and 1 country"
	assert got["pounds"] == [lb_text(weight) for weight in weights]
	assert got["prices"] == [
		"5 weight tiers from 0.1 to 20 lb, $35.00 to $400.00",
		"$35.00 at any weight",
		"Free at any weight",
		"One price at any weight, not typed yet",
		"2 weight tiers from 0.01 to 4 lb, $40.00, a price not typed yet",
		"No prices yet",
	]
	summary = got["summary"]
	assert summary["intro"].startswith("The rate card, with weights in pounds and prices in US dollars.")
	assert [profile["name"] for profile in summary["profiles"]] == [
		profile["name"] for profile in card["profiles"]
	]
	assert summary["profiles"][0]["codes"] == "Product codes F-*-PT and F-CR-PC-*."
	assert summary["profiles"][2]["zones"][0] == (
		"USA: United States. 5 weight tiers from 0.1 to 20 lb, $35.00 to $400.00, International Express (4-7 days)."
	)
	assert (
		summary["profiles"][2]["zones"][3]
		== "Bahrain (Local): BH. Free at any weight, Free Shipping (1-2 days)."
	)
	# country names apart by semicolons: a name can hold a comma or an "and"
	assert summary["not_delivered"] == (
		"Delivery is not offered to Afghanistan; Russian Federation: their zone is not shipped to."
	)
	assert summary["profiles"][3]["codes"] == "Every other product.", "the profile with * takes what is left"
	assert got["none"]["none"] == "There is no rate card yet." and got["none"]["profiles"] == []
	assert got["review"] == summary, "the Review says the card with the same words, in rate-card mode only"
	assert got["carrier"] is None
	may_use = sys.modules["dsi_catalogue.shipping_policy"].fallback_may_use_the_card
	assert got["uses"] == [may_use(policy) for policy in fallbacks] == [True, True, False, False]
	assert got["source"] is True
	assert got["length"] == len("a\U0001f600b") == 3


@needs_node
def test_a_long_name_is_shown_cut_and_kept_whole(tmp_path):
	"""A name from a file can be of any length (validate_card refuses it, but the page shows it first): what the
	page shows is cut to 140 characters (sp.clip, the last an ellipsis), with the whole name as a title, and the
	card keeps it whole; what will change cuts each name it lists, and has them whole in the line's title. Country
	names are listed apart by semicolons (sp.country_list)."""
	card = sample_card()
	long_name = "Gulf " * 60  # 300 characters
	card["profiles"][0]["name"] = long_name
	card["profiles"][0]["zones"][0]["name"] = long_name
	got = run_page_helpers(
		tmp_path,
		f"""
		const card = {json.dumps(card)};
		const summary = sp.card_summary({{ price_source: "rate_card", rate_card: card, zones: [] }});
		const whole = card.profiles[0].name.trim();
		return {{
			clipped: sp.clip("x".repeat(200)),
			short: sp.clip("Gulf"),
			edge: [sp.clip("y".repeat(140)), sp.clip("y".repeat(141))],
			blank: [sp.clip(null), sp.clip(undefined)],
			profile: summary.profiles[0],
			zone: summary.profiles[0].zones[0],
			kept: card.profiles[0].name,
			changes: sp.change_lines({{ zones: {{ added: ["Gulf"] }}, rate_card: {{ added: [whole, "Refill Service"] }} }}),
			list: sp.country_list(["Korea, Democratic Peoples Republic of", "Heard Island and McDonald Islands", "Iran"]),
		}};
		""",
	)
	cut = long_name[:139] + "…"
	assert len(got["clipped"]) == 140 and got["clipped"].endswith("…") and got["clipped"][:139] == "x" * 139
	assert got["edge"] == ["y" * 140, "y" * 139 + "…"], "140 characters are shown whole, 141 are cut"
	assert (got["short"], got["blank"]) == ("Gulf", ["", ""])
	assert got["profile"]["name"] == cut and got["profile"]["title"] == long_name.strip()
	assert got["zone"].startswith(cut + ": ")
	assert got["kept"] == long_name, "the card keeps the whole name"
	assert got["changes"] == [
		{"label": "Zones added", "text": "Gulf"},
		{
			"label": "Rate card profiles added",
			"text": f"{cut} and Refill Service",
			"title": f"{long_name.strip()} and Refill Service",
		},
	]
	assert got["list"] == "Korea, Democratic Peoples Republic of; Heard Island and McDonald Islands; Iran"


@needs_node
def test_the_rates_step_lists_the_cards_own_problems_the_review_sends_it(tmp_path, wizard_module):
	"""The Rates step lists the sentences about the card itself (sp.is_card_problem), all of which the Review's
	"Fix it" sends to the Rates step (sp.problem_step); the price source's own sentences go to the Rates step too,
	but its switch says them, so they are not listed. One list of patterns says both (CARD_PROBLEMS)."""

	def problems(**policy):
		return validate_policy({"zones": [], "item_sets": [], "rules": [], **policy})

	card = {
		**TINY_CARD,
		"profiles": [
			{
				**TINY_CARD["profiles"][0],
				"zones": [{**TINY_CARD["profiles"][0]["zones"][0], "countries": ["ZZ"]}],
			}
		],
	}
	listed = [
		*problems(rate_card={"profiles": "x"}),
		*wizard_module._unknown_card_countries(card, {}),
		*wizard_module._unsavable_card({"profiles": [{"name": float("nan")}]}),
	]
	said_by_the_switch = [*problems(price_source="bogus"), *problems(price_source="rate_card")]
	elsewhere = [*problems(zones=[{"name": "", "enabled": True, "countries": ["AE"]}])]
	sentences = [*listed, *said_by_the_switch, *elsewhere]
	assert all((listed, said_by_the_switch, elsewhere)), "each kind of sentence was made"
	got = run_page_helpers(
		tmp_path,
		f"return {json.dumps(sentences)}.map((sentence) => [sp.is_card_problem(sentence), sp.problem_step(sentence)]);",
	)
	want = [
		*([True, "rates"] for _ in listed),
		*([False, "rates"] for _ in said_by_the_switch),
		*([False, "zones"] for _ in elsewhere),
	]
	assert got == want


@needs_node
def test_a_check_of_the_test_tab_says_its_status_in_words(tmp_path):
	"""sp.check_status (step_test.js) on the checks shipping_integration_check really makes: a check that passed, one
	that did not, a website that was not asked and one that limits the route (HTTP 429: neutral, not a failure)."""
	answer = {
		"message": {
			"policy_active": True,
			"price_source": "rate_card",
			"offered": True,
			"offers": [{"amount": 100.0}],
		}
	}
	checks = [
		shipping_integration_check.summarize_policy_response(200, 300, answer, 100.0),
		shipping_integration_check.summarize_carrier_response(ImportError("no dsi_shipping"), ms=5),
		shipping_integration_check.website_not_asked("The address is not usable."),
		shipping_integration_check.summarize_website_response(429, 120, {}, 100.0),
		shipping_integration_check.summarize_website_response(500, 120, {"error": "down"}, None),
		None,
	]
	got = run_page_helpers(tmp_path, f"return {json.dumps(checks)}.map((check) => sp.check_status(check));")
	assert [status["text"] for status in got] == [
		"Passed",
		"Did not pass",
		"Not asked",
		"Rate limited",
		"Did not pass",
		"Not run",
	]
	assert [status["kind"] for status in got] == [
		"passed",
		"failed",
		"not_asked",
		"limited",
		"failed",
		"missing",
	]


def test_the_rules_step_shows_the_asterisk_sentence_word_for_word():
	"""The plan's Global Constraints: the sentence is on the wizard's Rules step, verbatim."""
	assert f'"{ASTERISK}"' in (PAGE_DIR / "step_rules.js").read_text(encoding="utf-8")


# What the page says of the live effect, by the part that says it. The website prices from the ERP's answer only
# when the policy is on with the rate card as the price source; with the carrier it charges the carrier's own
# prices whatever the policy says (ruling P100, final review I1).
CARRIER_MODE_SENTENCES = {
	"step_rates.js": [
		"Customers pay the carrier's own price: for now the website applies the policy only with the rate card as "
		"the price source. The rules are kept, and Try an order shows what they would charge; there the rate card "
		"is used only where a fallback says Use the rate card.",
	],
	"step_review.js": [
		"The website will still charge the carrier's own prices: it applies the policy only with the rate card as "
		"the price source.",
		"Switched on: customers still pay the carrier's own prices",
		"Applied. The website still charges the carrier's own prices: it applies the policy only with the rate "
		"card as the price source.",
		"Applied and switched on. The website still charges the carrier's own prices: it applies the policy only "
		"with the rate card as the price source.",
		"The website still charges the carrier's own prices: it applies the policy only with the rate card as the "
		"price source.",
		"It is switched on, but the website still charges the carrier's own prices: it applies the policy only "
		"with the rate card as the price source.",
	],
	"wizard_tape.js": [
		"The policy is switched off, so today customers pay the carrier's price. This receipt shows what the "
		"policy would charge, but the website applies it only with the rate card as the price source.",
		# the tape knows only the draft, not what customers pay now, so its note says nothing is "still" so
		# (ruling P105)
		"With this draft the website charges the carrier's own prices: it applies the policy only with the rate "
		"card as the price source. This receipt shows what the policy would charge.",
	],
}
# What the Review step says when the change itself is what customers notice (ruling P105): from a saved policy on
# with the rate card to a draft on with the carrier, customers move to the carrier's own prices, so nothing is
# "still" so; and a policy switched off after one on with the carrier leaves the carrier's price as it was, so
# nothing comes back "again". "Still" and "again" stay where they are true (the pins above and below).
TRANSITION_SENTENCES = {
	"step_review.js": [
		"Customers will pay the carrier's own prices instead of the rate card: the website applies the policy "
		"only with the rate card as the price source.",
		"Switched on: customers will pay the carrier's own prices instead of the rate card",
		"Applied. Customers now pay the carrier's own prices instead of the rate card: the website applies the "
		"policy only with the rate card as the price source.",
		"Customers now pay the carrier's own prices instead of the rate card: the website applies the policy only "
		"with the rate card as the price source.",
		"The policy goes off: customers keep paying the carrier's price.",
		"Applied and switched off. Customers keep paying the carrier's price.",
		"It is switched off: customers keep paying the carrier's price.",
	],
}
# What the page says with the rate card as the price source, or of a policy that is off: as it was.
RATE_CARD_AND_OFF_SENTENCES = {
	# the rate card prices only while the policy is on (ruling P105)
	"step_rates.js": [
		"Customers pay from the rate card below once the policy is on; the carrier is not asked."
	],
	"step_review.js": [
		"Customers pay what this policy says as soon as it is applied.",
		"Customers pay the carrier's price again as soon as it is applied.",
		"The policy stays off: customers keep paying the carrier's price.",
		"Switched on: customers pay what this policy says",
		"Switched off: customers pay the carrier's price",
		"Applied. Customers see the new prices now.",
		"Applied and switched on. Customers see the new prices now.",
		"Applied and switched off. Customers pay the carrier's price again.",
		"Applied. The policy is off, so customers still pay the carrier's price.",
		"Customers see the new prices now.",
		"It is switched on: customers see the new prices now.",
		"It is switched off: customers pay the carrier's price again.",
		"The policy is off, so customers still pay the carrier's price.",
	],
	"wizard_tape.js": [
		"The policy is switched off, so today customers pay the carrier's price. This receipt shows what they "
		"would pay once you turn the policy on in the Review step.",
		"Customer would pay",
		"Customer pays",
	],
}


def test_with_the_carrier_as_the_price_source_the_page_never_says_the_website_applies_the_policy():
	"""Ruling P100: wherever the page speaks of what customers pay (the price source on the Rates step, the Active
	switch, the line beside Apply and what an Apply says on the Review step, the tape's receipt), with the carrier as
	the price source it says that the website still charges the carrier's own prices and applies the policy only with
	the rate card. Each sentence is read from the part that says it, word for word, as one string; the sentences
	for the rate card and for a policy that is off are still there as they were, and the one that said the carrier's
	price is shaped by the rules is gone."""
	for sentences in (CARRIER_MODE_SENTENCES, RATE_CARD_AND_OFF_SENTENCES):
		for part, said in sentences.items():
			source = (PAGE_DIR / part).read_text(encoding="utf-8")
			for sentence in said:
				assert f'"{sentence}"' in source, f"{part} says: {sentence}"
	served = served_page_script()
	assert "shaped by the rules" not in served
	assert "The rate card is used only where a fallback says Use the rate card." not in served


def test_a_switch_from_the_rate_card_to_the_carrier_is_said_as_a_change():
	"""Ruling P105 (the re-review's Minor and its "again" observation): the Review reads what customers pay now
	from the saved policy (the line beside Apply and the switch read store.original, an Apply's sentences read
	outcome.before). From the rate card, on, to the carrier, on, customers will pay, then now pay, the carrier's own
	prices instead of the rate card; "still" stays only where they already paid the carrier's own prices. Switched
	off, "again" stays only after a policy that was on with the rate card. The tape knows only the draft: its note
	says what the draft does. Each sentence is read from its part, word for word."""
	for part, said in TRANSITION_SENTENCES.items():
		source = (PAGE_DIR / part).read_text(encoding="utf-8")
		for sentence in said:
			assert f'"{sentence}"' in source, f"{part} says: {sentence}"
	review = (PAGE_DIR / "step_review.js").read_text(encoding="utf-8")
	assert 'return Boolean(policy?.active) && policy?.price_source === "rate_card";' in review
	assert review.count("pays_by_card(outcome.before)") == 2, "applied_sentence and late_sentence"
	assert review.count("pays_by_card(store.original)") == 3, "the line beside Apply (on, off) and the switch"
	tape = (PAGE_DIR / "wizard_tape.js").read_text(encoding="utf-8")
	assert "The website still charges" not in tape


# What the Review step says when an Apply's answer is lost and the saved policy still has the version the page
# sent (ruling P102, final review M6): the worker may still be waiting for the lock and write afterwards.
UNCONFIRMED = (
	"The server did not confirm the change, and it may still be saving. Reload the page to see the policy as it "
	"is; your draft stays in this browser until then."
)
# ... and when the saved policy cannot be read either (ruling P103): the request may have reached the server.
UNREACHABLE = (
	"Could not reach the server, so we cannot tell whether the change was saved. Reload the page to see the policy "
	"as it is; your draft stays in this browser until then."
)


def test_an_apply_whose_answer_is_lost_never_says_that_nothing_was_changed():
	"""Rulings P102 and P103: an Apply that hears no usable word reads the saved policy once. When its version is
	still the one the page sent, the write may yet come (the worker can wait 30 s for the lock); when the saved
	policy cannot be read either, the request may still have reached the server. Either way the step says the change
	is not confirmed and to reload, never that nothing was changed. No timer: wording only. The one claim left that
	the policy was not applied is the server's own refusal, which it rolled back."""
	review = (PAGE_DIR / "step_review.js").read_text(encoding="utf-8")
	assert f'"{UNCONFIRMED}"' in review
	assert 'found === "unchanged") return finish({ kind: "unconfirmed"' in review
	assert f'"{UNREACHABLE}"' in review
	assert 'return finish({ kind: "unreachable", draft: sent_key });' in review
	served = served_page_script()
	assert "did not accept the change" not in served
	assert "not_accepted" not in served
	assert "Nothing was changed" not in served
	assert served.count('__("The policy was not applied') == 2, (
		"only for a refusal the server sent with its words"
	)


def test_the_formula_names_the_rules_step_lists_are_the_servers():
	"""The line under a rule's formula names what a formula may use: a copy of shipping_formula's two lists."""
	from dsi_catalogue.shipping_formula import FUNCTIONS, VARIABLES

	words = (PAGE_DIR / "wizard_rule_words.js").read_text(encoding="utf-8")

	def listed(name):
		found = re.search(rf"^const {name} = (\[[^\]]*\]);$", words, re.MULTILINE)
		assert found, f"{name} is one line of wizard_rule_words.js"
		return tuple(json.loads(found.group(1)))

	assert listed("FORMULA_VARIABLES") == VARIABLES
	assert listed("FORMULA_FUNCTIONS") == FUNCTIONS


@needs_node
def test_el_builds_from_text_and_refuses_an_inline_event_handler(tmp_path):
	got = run_page_helpers(
		tmp_path,
		"""
		const out = {};
		for (const handler of ["alert(1)", 1, {}]) {
			try {
				sp.el("p", { onclick: handler });
				out[String(handler)] = "made";
			} catch (error) {
				out[String(handler)] = error.name;
			}
		}
		const made = sp.el("p", { onclick: () => 1, title: "<b>x</b>" }, "<i>y</i>", null, false, ["z"]);
		out.made = [typeof made.listeners.click, made.attributes.title, made.attributes.onclick, made.children];
		return out;
		""",
	)
	assert got == {
		"alert(1)": "TypeError",
		"1": "TypeError",
		"[object Object]": "TypeError",
		"made": ["function", "<b>x</b>", None, ["<i>y</i>", "z"]],
	}


def test_the_policy_form_opens_the_wizard_from_a_primary_button():
	form = FORM_SCRIPT.read_text(encoding="utf-8")
	assert '__("Open the wizard")' in form and 'frappe.set_route("shipping-policy-wizard")' in form
	assert '.addClass("btn-primary")' in form
	assert "frappe.utils.escape_html(hint)" in form, "the form's one line of HTML escapes its text"


def test_the_wizard_style_stays_on_the_page_and_has_three_accents():
	css = page_file(".css")
	selectors = css_selectors(css)
	assert len(selectors) > 50
	for selector in selectors:
		assert selector.startswith((".sp-wizard", '[data-theme="dark"] .sp-wizard')), selector
	for accent, light, dark in (
		("brass", "#9A7116", "#D8B259"),
		("sage", "#2B7A57", "#74C9A0"),
		("brick", "#A8473A", "#E5907F"),
	):
		assert f"--sp-{accent}: {light};" in css and f"--sp-{accent}: {dark};" in css
	assert "@media (prefers-reduced-motion: reduce)" in css
	# laid out by the room the wizard has (the desk's sidebar takes some of the window), written once: every
	# browser the desk supports has container queries
	assert "container: sp-wizard / inline-size;" in css and "@container sp-wizard (min-width: 860px)" in css
	assert css.count('"step tape"') == 1 and "@supports not (container-type" not in css
	for word in ("gradient", "text-transform", "monospace"):
		assert word not in css, word


def test_the_wizard_files_are_indented_with_tabs():
	for path in (PAGE_SCRIPT, *page_parts(), PAGE_DIR / "shipping_policy_wizard.css", FORM_SCRIPT):
		for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
			# a line inside a CSS comment block starts with " *"
			assert not line.startswith(" ") or line.startswith(" *"), (
				f"{path.name}:{number} starts with spaces"
			)


# ---------------------------------------------------------------- how the files reach erp2
PUSH = ROOT / "tools" / "erp2_push.sh"
FILE_LIST = ROOT / "tools" / "shipping_policy_files.txt"
FIXTURE_PATH = "tests/hermetic/fixtures/shipping_policy_example.json"
CONTAINER = "frappe_docker-backend-1"

# Children first (a table must exist before its parent points at it), then the doctypes that link to each
# other: the rule links to a zone and an item set, and the policy holds the rules.
RELOAD_ORDER = [
	"shipping_zone_country",
	"shipping_item_set_group",
	"shipping_item_set_item",
	"shipping_zone",
	"shipping_item_set",
	"shipping_policy_rule",
	"shipping_policy",
]

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="the push script is a bash script")


def listed_files():
	lines = [line.strip() for line in FILE_LIST.read_text(encoding="utf-8").splitlines()]
	return [line for line in lines if line and not line.startswith("#")]


def files_the_push_may_carry():
	"""Every file in the working tree that the push script's allow-list covers."""
	found = {FIXTURE_PATH}
	for pattern in (
		"dsi_catalogue/shipping_*",
		"dsi_catalogue/dsi_catalogue/doctype/shipping_*/*",
		"dsi_catalogue/dsi_catalogue/page/shipping_policy_wizard/*",
	):
		found |= {
			path.relative_to(ROOT).as_posix()
			for path in ROOT.glob(pattern)
			if path.is_file() and path.suffix != ".pyc"
		}
	return found


def test_the_push_list_names_every_file_the_push_may_carry():
	listed = listed_files()
	assert len(listed) == len(set(listed)), "a file is listed twice"
	expected = files_the_push_may_carry()
	assert not expected - set(listed), f"in the tree but not in the list: {sorted(expected - set(listed))}"
	assert not set(listed) - expected, f"in the list but not in the tree: {sorted(set(listed) - expected)}"


def test_the_push_list_carries_the_modules_the_native_tests_import_and_the_fixture():
	listed = set(listed_files())
	for module in (
		"shipping_formula",
		"shipping_rate_card",
		"shipping_engine",
		"shipping_policy_data",
		"shipping_integration_check",
		"shipping_policy",
	):
		assert f"dsi_catalogue/{module}.py" in listed
	assert FIXTURE_PATH in listed
	for name in DOCTYPES:
		folder = f"dsi_catalogue/dsi_catalogue/doctype/{folder_of(name)}"
		assert f"{folder}/{folder_of(name)}.json" in listed
		assert f"{folder}/{folder_of(name)}.py" in listed
	assert "dsi_catalogue/dsi_catalogue/doctype/shipping_policy/shipping_policy.js" in listed


def test_the_push_script_is_executable_and_never_deletes_or_migrates():
	assert os.access(PUSH, os.X_OK)
	script = PUSH.read_text(encoding="utf-8")
	assert script.startswith("#!/usr/bin/env bash\n")
	assert "set -euo pipefail" in script
	for forbidden in ("migrate", "--delete", "rm -", "rsync -a", "--force"):
		assert forbidden not in script, forbidden
	assert script.count("docker restart") == 1, "the one restart is of the backend container"


# What the stand-in rsync does after it has logged its call. The listed files come after "--" and the last
# argument is the destination. Like the real rsync, which sees what erp2 already has, it reports a path as new
# ("<f+++++++++ path", as --itemize-changes does) the first time it is given it, and never again: it keeps one
# marker file for each path it has "sent". Nothing else it is given (an option, the ssh command, the host) is
# ever reported as a file.
RSYNC_STAND_IN = """\
after=0
paths=()
for a in "$@"; do
	if [ "$after" = 1 ]; then paths+=("$a"); fi
	if [ "$a" = -- ]; then after=1; fi
done
unset 'paths[${#paths[@]}-1]'
for p in "${paths[@]}"; do
	if [ ! -e "@SENT@/$p" ]; then
		mkdir -p "$(dirname "@SENT@/$p")"
		: > "@SENT@/$p"
		echo "<f+++++++++ $p"
	fi
done
"""


class Push:
	"""A copy of the push script in a scratch repo, run with stand-ins for ssh, rsync and docker.

	The stand-ins record what they were called with and do nothing else, so a run cannot reach erp2. The
	rsync one also remembers which listed files it has been given (see RSYNC_STAND_IN), so that a second run
	finds them already there.
	"""

	def __init__(self, tmp_path, listed, make=None):
		self.root = tmp_path / "repo"
		self.script = self.root / "tools" / "erp2_push.sh"
		self.script.parent.mkdir(parents=True)
		shutil.copy(PUSH, self.script)
		self.script.chmod(0o755)
		(self.root / "tools" / "shipping_policy_files.txt").write_text(
			"\n".join(listed) + "\n", encoding="utf-8"
		)
		for relative in listed if make is None else make:
			target = self.root / relative
			target.parent.mkdir(parents=True, exist_ok=True)
			target.write_text("x", encoding="utf-8")
		self.log = tmp_path / "calls.log"
		self.sent = tmp_path / "sent"  # one marker file for each path the stand-in rsync has been given
		stubs = tmp_path / "bin"
		stubs.mkdir()
		for name in ("ssh", "rsync", "docker", "bench"):
			stub = stubs / name
			stub.write_text(
				"#!/usr/bin/env bash\n"
				f'echo "{name} $*" >> "{self.log}"\n'
				+ (RSYNC_STAND_IN.replace("@SENT@", str(self.sent)) if name == "rsync" else ""),
				encoding="utf-8",
			)
			stub.chmod(0o755)
		self.env = {**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}"}

	def run(self, *args):
		return subprocess.run(
			["bash", str(self.script), *args],
			capture_output=True,
			text=True,
			env=self.env,
			timeout=60,
			stdin=subprocess.DEVNULL,
		)

	def calls(self):
		return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []


@pytest.fixture
def real_list(tmp_path):
	return Push(tmp_path, listed_files())


@needs_bash
def test_a_dry_run_checks_the_list_prints_every_step_and_touches_nothing(real_list):
	result = real_list.run("--dry-run")
	assert result.returncode == 0, result.stderr
	assert real_list.calls() == [], "a dry run must not call ssh, rsync or docker at all"
	reloads = [line.split()[-1] for line in result.stdout.splitlines() if "reload-doc" in line]
	assert reloads == [*RELOAD_ORDER, "shipping_policy_wizard"], "the doctypes in order, then the wizard page"
	lines = result.stdout.splitlines()
	cleared = [number for number, line in enumerate(lines) if "clear-cache" in line]
	last_reload = max(number for number, line in enumerate(lines) if "reload-doc" in line)
	assert len(cleared) == 1 and cleared[0] > last_reload, "the cache is cleared once, after the reloads"
	assert "restart" not in result.stdout
	for relative in listed_files():
		assert relative in result.stdout


@needs_bash
def test_a_dry_run_with_restart_says_it_would_restart_only_the_backend(real_list):
	result = real_list.run("--dry-run", "--restart")
	assert result.returncode == 0, result.stderr
	restarts = [line for line in result.stdout.splitlines() if "restart" in line]
	assert len(restarts) == 1 and CONTAINER in restarts[0]
	assert real_list.calls() == []


@needs_bash
def test_a_real_run_copies_the_listed_files_reloads_in_order_and_clears_the_cache(real_list):
	result = real_list.run()
	assert result.returncode == 0, result.stderr
	calls = real_list.calls()
	copies = [call for call in calls if call.startswith("rsync ")]
	assert len(copies) == 1
	assert "-R" in copies[0].split() and "erp2-sandbox:dsi_catalogue_dev/" in copies[0]
	assert "--delete" not in copies[0]
	for relative in listed_files():
		assert relative in copies[0].split()
	remote = [call for call in calls if call.startswith("ssh ")]
	assert all("erp2-sandbox" in call and "docker exec" in call and CONTAINER in call for call in remote)
	assert len(remote) == 2, "one process reloads every doctype, one clears the cache, and nothing else"
	reload_call, cache_call = remote
	assert "/env/bin/python - erp.shaik.net dsi_catalogue doctype:" in reload_call
	asked = [word.rstrip("'") for word in reload_call.split() if word.startswith(("doctype:", "page:"))]
	assert asked == [*(f"doctype:{name}" for name in RELOAD_ORDER), "page:shipping_policy_wizard"]
	assert cache_call.rstrip("'").endswith("bench --site erp.shaik.net clear-cache")
	assert not any(call.startswith(("docker ", "bench ")) for call in calls)
	assert "restart" not in " ".join(calls)


@needs_bash
def test_restart_restarts_only_the_backend_container_and_comes_last(real_list):
	result = real_list.run("--restart")
	assert result.returncode == 0, result.stderr
	restarts = [call for call in real_list.calls() if "restart" in call]
	assert len(restarts) == 1
	assert restarts[0].endswith(f"docker restart {CONTAINER}")
	assert real_list.calls()[-1] == restarts[0]


@needs_bash
def test_running_it_again_copies_nothing_because_every_file_is_already_there(real_list):
	"""Idempotence, not just determinism: the stand-in rsync remembers what it has sent (as the real one sees
	what erp2 has), so the first run sends every listed file and the second sends none."""
	count = len(listed_files())
	first, second = real_list.run(), real_list.run()
	assert (first.returncode, second.returncode) == (0, 0), (first.stderr, second.stderr)
	assert f"{count} of {count} file(s) copied" in first.stdout
	assert first.stdout.count("  new      ") == count and "  updated  " not in first.stdout
	assert f"0 of {count} file(s) copied" in second.stdout
	assert "  new      " not in second.stdout and "  updated  " not in second.stdout
	# Both runs send the same commands: what is already current is skipped by rsync and by reload-doc.
	calls = real_list.calls()
	assert calls[: len(calls) // 2] == calls[len(calls) // 2 :]


@needs_bash
def test_a_changed_python_file_that_is_already_on_erp2_says_a_restart_is_needed(tmp_path):
	push = Push(tmp_path, ["dsi_catalogue/shipping_engine.py"])
	# rsync reports an existing file whose content changed as <fcst...... (a new file is <f+++++++++)
	rsync = tmp_path / "bin" / "rsync"
	rsync.write_text(
		f'#!/usr/bin/env bash\necho "rsync $*" >> "{push.log}"\necho "<fcst...... dsi_catalogue/shipping_engine.py"\n'
	)
	result = push.run()
	assert result.returncode == 0, result.stderr
	assert "NOTE" in result.stdout and "--restart" in result.stdout
	assert "restart" not in " ".join(push.calls()), "it asks; it does not restart on its own"
	assert "NOTE" not in push.run("--restart").stdout, "no reminder when it restarts anyway"


@needs_bash
def test_a_page_and_an_unknown_doctype_folder_are_reloaded_after_the_known_doctypes(tmp_path):
	listed = [
		"dsi_catalogue/dsi_catalogue/doctype/shipping_zone/shipping_zone.json",
		"dsi_catalogue/dsi_catalogue/doctype/shipping_policy/shipping_policy.json",
		"dsi_catalogue/dsi_catalogue/doctype/shipping_extra/shipping_extra.json",
		"dsi_catalogue/dsi_catalogue/page/shipping_policy_wizard/shipping_policy_wizard.json",
		"dsi_catalogue/dsi_catalogue/page/shipping_policy_wizard/shipping_policy_wizard.js",
	]
	push = Push(tmp_path, listed)
	result = push.run("--dry-run")
	assert result.returncode == 0, result.stderr
	reloads = [
		line.strip().split("reload-doc ")[-1] for line in result.stdout.splitlines() if "reload-doc" in line
	]
	assert reloads == [
		"dsi_catalogue doctype shipping_zone",
		"dsi_catalogue doctype shipping_policy",
		"dsi_catalogue doctype shipping_extra",
		"dsi_catalogue page shipping_policy_wizard",
	]


def reload_snippet():
	"""The Python the script sends into the container, taken out of the script."""
	script = PUSH.read_text(encoding="utf-8")
	start = script.index("<<'PY' || true\n") + len("<<'PY' || true\n")
	return script[start : script.index("\nPY\n", start) + 1]


class FakeFrappe:
	"""Records what the reload snippet asks of frappe."""

	def __init__(self, loaded=True):
		self.calls = []
		self.loaded = loaded
		self.local = type("Local", (), {"module_app": {"selling": "erpnext"}})()
		self.db = type("Db", (), {"commit": lambda _: self.calls.append("commit")})()

	def init(self, **kwargs):
		self.calls.append(("init", kwargs))

	def connect(self):
		self.calls.append("connect")

	def get_module_list(self, app):
		self.calls.append(("get_module_list", app))
		return ["Dsi Catalogue"]

	def scrub(self, text):
		return text.lower().replace(" ", "_")

	def reload_doc(self, *args):
		self.calls.append(("reload_doc", *args))
		return self.loaded

	def destroy(self):
		self.calls.append("destroy")


def run_snippet(monkeypatch, capsys, fake, *argv):
	import sys

	monkeypatch.setitem(sys.modules, "frappe", fake)
	monkeypatch.setattr(sys, "argv", ["-", *argv])
	exec(compile(reload_snippet(), "<reload>", "exec"), {"__name__": "__main__"})
	return capsys.readouterr().out


def test_the_reload_snippet_teaches_its_process_the_app_and_reloads_in_the_order_given(monkeypatch, capsys):
	fake = FakeFrappe()
	out = run_snippet(
		monkeypatch,
		capsys,
		fake,
		"erp.shaik.net",
		"dsi_catalogue",
		"doctype:shipping_zone_country",
		"doctype:shipping_zone",
		"page:shipping_policy_wizard",
	)
	assert fake.calls == [
		("init", {"site": "erp.shaik.net", "sites_path": "."}),
		"connect",
		("get_module_list", "dsi_catalogue"),
		("reload_doc", "dsi_catalogue", "doctype", "shipping_zone_country"),
		"commit",
		("reload_doc", "dsi_catalogue", "doctype", "shipping_zone"),
		"commit",
		("reload_doc", "dsi_catalogue", "page", "shipping_policy_wizard"),
		"commit",
		"destroy",
	]
	assert fake.local.module_app == {"selling": "erpnext", "dsi_catalogue": "dsi_catalogue"}
	assert "doctype shipping_zone_country: loaded" in out


def test_the_reload_snippet_says_when_a_doctype_was_already_current_and_always_lets_go_of_the_site(
	monkeypatch, capsys
):
	fake = FakeFrappe(loaded=False)
	out = run_snippet(monkeypatch, capsys, fake, "erp.shaik.net", "dsi_catalogue", "doctype:shipping_zone")
	assert "doctype shipping_zone: already current" in out
	failing = FakeFrappe()
	failing.reload_doc = lambda *args: (_ for _ in ()).throw(RuntimeError("bad json"))
	with pytest.raises(RuntimeError):
		run_snippet(monkeypatch, capsys, failing, "erp.shaik.net", "dsi_catalogue", "doctype:shipping_zone")
	assert failing.calls[-1] == "destroy"


REFUSED = [
	"dsi_catalogue/hooks.py",
	"dsi_catalogue/modules.txt",
	"dsi_catalogue/api.py",
	"dsi_catalogue/dsi_catalogue/workspace/website_content_review/website_content_review.json",
	"dsi_catalogue/dsi_catalogue/doctype/storefront_settings/storefront_settings.json",
	"dsi_catalogue/dsi_catalogue/doctype/shipping_zone/../storefront_settings/storefront_settings.json",
	"dsi_catalogue/shipping_x/../hooks.py",
	"dsi_catalogue/shipping_/../hooks.py",
	"dsi_catalogue/dsi_catalogue/doctype/shipping_zone/sub/dir/file.py",
	"dsi_catalogue/dsi_catalogue/page/other_page/other_page.json",
	"dsi_catalogue/dsi_catalogue/page/shipping_policy_wizard/sub/file.js",
	"tests/hermetic/fixtures/index_key_samples.json",
	"tests/hermetic/test_shipping_engine.py",
	"tools/erp2_push.sh",
	"/etc/passwd",
	"../outside/shipping_x.py",
	"./dsi_catalogue/shipping_engine.py",
	"dsi_catalogue//shipping_engine.py",
	"dsi_catalogue/shipping_engine.py;touch pwned",
	"dsi_catalogue/shipping_engine.py $(touch pwned)",
	"dsi_catalogue/shipping engine.py",
	"dsi_catalogue/Shipping_engine.py",
	# characters a shell, a glob or rsync would read as something other than a file name
	"dsi_catalogue/shipping_engine.py'",
	'dsi_catalogue/shipping_engine.py"',
	"dsi_catalogue/shipping_*.py",
	"dsi_catalogue/shipping_?ngine.py",
	"dsi_catalogue/shipping_[e]ngine.py",
	"dsi_catalogue/shipping_engine.py\\",
	"dsi_catalogue/shipping_engine.py/",
]


@needs_bash
@pytest.mark.parametrize("bad", REFUSED)
def test_a_path_outside_the_allow_list_is_refused_before_anything_is_copied_or_run(tmp_path, bad):
	push = Push(
		tmp_path, ["dsi_catalogue/shipping_engine.py", bad], make=["dsi_catalogue/shipping_engine.py"]
	)
	# the bad path is also made, when it can be, so that the allow-list is what refuses it
	target = push.root / bad
	if not bad.startswith(("/", ".")) and ".." not in bad and "$" not in bad and ";" not in bad:
		if not target.exists():
			target.parent.mkdir(parents=True, exist_ok=True)
			target.write_text("x", encoding="utf-8")
	for args in (("--dry-run",), ()):
		result = push.run(*args)
		assert result.returncode == 1, (args, result.stdout, result.stderr)
		assert "refused" in result.stderr and bad in result.stderr
		assert push.calls() == [], "nothing was sent"
		assert "reload-doc" not in result.stdout
	assert not (push.root / "pwned").exists()


@needs_bash
def test_a_listed_file_that_is_missing_or_not_a_plain_file_is_refused(tmp_path):
	listed = [
		"dsi_catalogue/shipping_engine.py",
		"dsi_catalogue/shipping_missing.py",
		"dsi_catalogue/shipping_link.py",
		"dsi_catalogue/dsi_catalogue/doctype/shipping_zone",
	]
	push = Push(tmp_path, listed, make=["dsi_catalogue/shipping_engine.py"])
	(push.root / "dsi_catalogue" / "shipping_link.py").symlink_to(
		push.root / "dsi_catalogue" / "shipping_engine.py"
	)
	(push.root / "dsi_catalogue" / "dsi_catalogue" / "doctype" / "shipping_zone").mkdir(parents=True)
	result = push.run("--dry-run")
	assert result.returncode == 1
	assert "refused" in result.stderr
	for name in ("shipping_missing.py", "shipping_link.py"):
		assert name in result.stderr
	assert push.calls() == []


@needs_bash
@pytest.mark.parametrize("leads_to", ["outside the repository", "elsewhere in the repository"])
def test_a_listed_file_reached_through_a_linked_folder_is_refused(tmp_path, leads_to):
	"""The path passes the allow-list and the file itself is no link, but a folder in front of it is: rsync would
	send the file from wherever that link leads, under the name of one inside the repository."""
	wanted = "dsi_catalogue/dsi_catalogue/doctype/shipping_zone/shipping_zone.py"
	push = Push(
		tmp_path, ["dsi_catalogue/shipping_engine.py", wanted], make=["dsi_catalogue/shipping_engine.py"]
	)
	elsewhere = tmp_path / "outside" if leads_to.startswith("outside") else push.root / "tools" / "elsewhere"
	(elsewhere / "doctype" / "shipping_zone").mkdir(parents=True)
	(elsewhere / "doctype" / "shipping_zone" / "shipping_zone.py").write_text("from the wrong place")
	(push.root / "dsi_catalogue" / "dsi_catalogue").symlink_to(elsewhere)
	for args in (("--dry-run",), ()):
		result = push.run(*args)
		assert result.returncode == 1, (args, result.stdout, result.stderr)
		assert "refused" in result.stderr and wanted in result.stderr and "link" in result.stderr
		assert "dsi_catalogue/shipping_engine.py" not in result.stderr, "the plain file is not blamed"
		assert push.calls() == [], "nothing was sent"
		assert "reload-doc" not in result.stdout


@needs_bash
def test_a_repository_that_is_opened_through_a_link_still_pushes(tmp_path):
	"""Each file's real path is compared with its path under the repository, so the repository's own path has to
	be the real one too: a checkout reached through a link must not make every file look as if it came from
	elsewhere."""
	push = Push(tmp_path, ["dsi_catalogue/shipping_engine.py"])
	through_a_link = tmp_path / "through_a_link"
	through_a_link.symlink_to(push.root)
	result = subprocess.run(
		["bash", str(through_a_link / "tools" / "erp2_push.sh"), "--dry-run"],
		capture_output=True,
		text=True,
		env=push.env,
		timeout=60,
		stdin=subprocess.DEVNULL,
	)
	assert result.returncode == 0, result.stderr
	assert "1 file(s) listed, all inside the allow-list" in result.stdout


@needs_bash
@pytest.mark.parametrize("args", [("--dry-run",), ()], ids=["a dry run", "a real run"])
def test_a_realpath_that_is_not_gnu_is_refused_up_front_in_plain_words(tmp_path, args):
	"""The link check compares each file with `realpath -e`, a GNU option (BSD and macOS refuse it). Without it
	every listed file would be reported as reached through a link, so the script says what is missing, before
	it looks at any file and before it sends anything."""
	push = Push(tmp_path, ["dsi_catalogue/shipping_engine.py"])
	realpath = tmp_path / "bin" / "realpath"
	realpath.write_text(
		"#!/usr/bin/env bash\necho \"realpath: invalid option -- 'e'\" >&2\nexit 1\n", encoding="utf-8"
	)
	realpath.chmod(0o755)
	result = push.run(*args)
	assert result.returncode == 1, (result.stdout, result.stderr)
	assert "erp2_push: GNU realpath is needed" in result.stderr
	assert "link" not in result.stderr and "refused" not in result.stderr, "not the sentence about each file"
	assert result.stdout == "" and push.calls() == [], "no file was looked at and nothing was sent"
	# asking for help needs no realpath, and a usage error is still a usage error
	assert push.run("--help").returncode == 0
	assert push.run("--everything").returncode == 2


@needs_bash
def test_comments_blank_lines_and_windows_line_endings_in_the_list_are_fine(tmp_path):
	push = Push(tmp_path, ["dsi_catalogue/shipping_engine.py"])
	(push.root / "tools" / "shipping_policy_files.txt").write_bytes(
		b"# a comment\r\n\r\n  dsi_catalogue/shipping_engine.py  \r\n"
	)
	result = push.run("--dry-run")
	assert result.returncode == 0, result.stderr
	assert "dsi_catalogue/shipping_engine.py" in result.stdout


@needs_bash
def test_an_unknown_option_is_a_usage_error(real_list):
	result = real_list.run("--everything")
	assert result.returncode == 2
	assert "Usage" in result.stderr
	assert real_list.calls() == []
