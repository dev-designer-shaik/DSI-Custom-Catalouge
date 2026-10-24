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

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from dsi_catalogue.shipping_engine import LABELS, resolve, validate_policy
from dsi_catalogue.shipping_policy_data import policy_from_rows, rows_from_policy
from dsi_catalogue.shipping_rate_card import validate_card

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


def test_policy_defaults_ship_the_policy_off():
	fields = fields_of(POLICY)
	assert fields["active"]["fieldtype"] == "Check"
	assert fields["active"]["default"] == "0"
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
		"Carrier: customers pay the carrier's price with the rules below. "
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
	assert "@media (prefers-reduced-motion: reduce)" in css and "@media (max-width: 899.98px)" in css
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
