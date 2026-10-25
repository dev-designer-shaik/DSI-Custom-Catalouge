"""public_summary names the countries of the enabled zones.

A zone record has no `everywhere_else` flag in the one-page model; the lookup of
country names still read one and raised KeyError on every call, so the public
shipping summary failed. shipping_policy.py imports half of Frappe, so the one
function is lifted out of the source and run against a stub.
"""

import ast
import types
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "dsi_catalogue" / "shipping_policy.py"


def _country_names():
	tree = ast.parse(SOURCE.read_text())
	node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_country_names")
	frappe = types.SimpleNamespace(get_all=lambda doctype, **kw: [{"name": "Bahrain", "code": "bh"}])
	namespace = {"frappe": frappe}
	exec(compile(ast.Module([node], []), str(SOURCE), "exec"), namespace)
	return namespace["_country_names"]


def test_country_names_for_enabled_zones_without_an_everywhere_else_flag():
	policy = {
		"zones": [
			{"name": "GCC", "enabled": 1, "countries": ["BH"]},
			{"name": "Off", "enabled": 0, "countries": ["KW"]},
		]
	}
	assert _country_names()(policy) == {"BH": "Bahrain"}


def test_no_enabled_zone_means_no_lookup():
	assert _country_names()({"zones": [{"name": "Off", "enabled": 0, "countries": ["KW"]}]}) == {}
