"""The assistant's shipping tools take a plain scenario and answer in plain words."""

import json

import pytest

from dsi_catalogue.assistant_tools import simulation as sim


def test_a_scenario_becomes_the_order_the_page_prices():
	order = json.loads(sim.order_argument({"country_code": "us", "items": [{"item_code": "A", "qty": 2}], "city": " Austin "}))
	assert order == {"country_code": "US", "items": [{"item_code": "A", "qty": 2}], "city": "Austin", "postcode": ""}


def test_items_may_arrive_as_json_text_and_quantity_defaults_to_one():
	order = json.loads(sim.order_argument({"country_code": "BH", "items": '[{"item_code": "A"}]'}))
	assert order["items"] == [{"item_code": "A", "qty": 1}]


@pytest.mark.parametrize("bad", [
	{"country_code": "USA", "items": [{"item_code": "A"}]},
	{"country_code": "", "items": [{"item_code": "A"}]},
	{"country_code": "BH", "items": []},
	{"country_code": "BH", "items": "not json"},
	{"country_code": "BH", "items": [{"item_code": "A"}] * 51},
])
def test_a_bad_scenario_is_refused_with_a_reason(bad):
	with pytest.raises(ValueError):
		sim.order_argument(bad)


def test_the_saved_policy_is_used_unless_the_caller_gives_one():
	saved = {"zones": [], "item_sets": [], "rules": []}
	assert sim.draft_argument({}, saved) is saved
	assert sim.draft_argument({"policy": json.dumps({"zones": [1]})}, saved) == {"zones": [1]}
	with pytest.raises(ValueError):
		sim.draft_argument({"policy": "[1]"}, saved)


def test_the_summary_names_the_cheapest_offer():
	out = sim.summarize({"offered": True, "offers": [
		{"name": "Express", "provider": "Aramex", "amount": 40, "estimated_days": 2, "source": "carrier"},
		{"name": "Standard", "provider": "Aramex", "amount": 25, "estimated_days": 5, "source": "table"},
	]})
	assert out["offered"] and "cheapest Standard at USD 25" in out["summary"]
	assert [o["price_usd"] for o in out["offers"]] == [40, 25]


def test_when_nothing_is_offered_the_summary_says_why():
	out = sim.summarize({"offered": False, "reason": "not_covered", "message": "We do not deliver there."})
	assert not out["offered"] and "We do not deliver there." in out["summary"] and out["offers"] == []
