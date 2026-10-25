import copy
import time

import pytest

from dsi_catalogue.shipping_engine import pattern_matches, resolve, tier_price, validate_policy


def policy():
	return {
		"active": True,
		"fallback": {"method": "no_delivery", "amount": 0},
		"zones": [
			{"name": "USA", "enabled": True, "countries": ["US"]},
			{"name": "BH", "enabled": True, "countries": ["BH"]},
		],
		"item_sets": [
			{"name": "Petite", "patterns": ["F-*-PT"], "items": [], "item_groups": []},
			{"name": "Perfume", "patterns": ["F-*"], "items": [], "item_groups": []},
		],
		"rules": [row("Petite", "USA", 3), row("Perfume", "USA", 8), row("", "", 2)],
	}


def row(group, zone, amount):
	return {
		"item_set": group or None,
		"zone": zone,
		"method": "table",
		"services": [],
		"price_table": [{"up_to_lb": None, "price": amount}],
		"days": 4,
		"label": "Delivery",
	}


def item(code="F-X-PT", qty=1, weight=0.35):
	return {"item_code": code, "index_key": code, "groups": [], "qty": qty, "weight_kg": weight}


def order(*items, country="US", directory=None):
	return {
		"country_code": country,
		"items": list(items) or [item()],
		"directory": directory,
		"order_value": 100,
	}


def service(name="A", **kw):
	return {
		"name": name,
		"label": name,
		"provider": "Provider " + name,
		"carrier_code": "CODE-" + name,
		"scope": "International",
		"estimated_days": 3,
		"enabled": True,
		"countries": ["US"],
		"max_weight_kg": 0,
		**kw,
	}


def quoted(o, quotes=None, boxes=None):
	return {
		**o,
		"shipment_quotes": {
			"0": {
				"boxes": boxes or [{"weight_kg": 1, "volume_l": 2}],
				"quotes": quotes
				if quotes is not None
				else [
					{"service_id": "A", "amount": 7, "boxes_quoted": 1},
					{"service_id": "B", "amount": 11, "boxes_quoted": 1},
				],
			}
		},
	}


def test_groups_claim_products_before_destination():
	p = policy()
	p["rules"][0]["zone"] = "BH"
	assert resolve(p, order())["reason"] == "not_covered"
	p["fallback"] = {"method": "fixed", "amount": 0}
	assert resolve(p, order())["reason"] == "not_covered"
	p["rules"].pop(0)
	assert resolve(p, order())["reason"] == "not_covered"


def test_mixed_cart_adds_each_group_once():
	r = resolve(policy(), order(item(), item("F-X"), item("P-X")))
	assert r["offers"][0]["amount"] == 13
	assert len(r["offers"][0]["trace"]) == 3


def test_named_empty_set_never_becomes_catchall():
	p = policy()
	p["item_sets"][0]["patterns"] = []
	assert resolve(p, order())["offers"][0]["amount"] == 8


def test_not_delivered_always_wins():
	p = policy()
	p["zones"].append(
		{
			"name": "Blocked",
			"countries": ["US"],
			"not_shipped": True,
			"enabled": True,
			"not_offered_message": "No delivery here",
		}
	)
	p["fallback"] = {"method": "fixed", "amount": 0}
	r = resolve(p, order())
	assert r["reason"] == "not_shipped"
	assert r["message"] == "No delivery here"


@pytest.mark.parametrize("weight,expected", [(0, 1), (0.1, 1), (1, 1), (1.001, 2), (2, 2), (2.001, None)])
def test_table_boundaries(weight, expected):
	assert tier_price([{"up_to_lb": 1, "price": 1}, {"up_to_lb": 2, "price": 2}], weight) == expected


def test_unbounded_tier_and_free_are_explicit():
	assert tier_price([{"up_to_lb": None, "price": 0}], 1e8) == 0
	p = policy()
	p["rules"][0]["price_table"] = [{"up_to_lb": 0.1, "price": 3}]
	p["fallback"] = {"method": "fixed", "amount": 0}
	assert resolve(p, order())["reason"] == "too_heavy"


def test_missing_weight_reported():
	r = resolve(policy(), order(item(weight=0)))
	assert r["offers"][0]["trace"][0]["warnings"]
	assert r["offers"][0]["trace"][0]["weight_lb"] == 0.772


def test_all_provider_options_and_metadata_survive():
	p = policy()
	p["rules"][0]["method"] = "carrier"
	o = order(directory=[service(), service("B")])
	assert resolve(p, o)["needs_quotes"]
	r = resolve(p, quoted(o))
	assert [x["amount"] for x in r["offers"]] == [7, 11]
	assert [x["carrier_code"] for x in r["offers"]] == ["CODE-A", "CODE-B"]
	p["rules"][0]["services"] = ["B"]
	assert [x["service_id"] for x in resolve(p, quoted(o))["offers"]] == ["B"]


def test_formula_prices_every_provider_offer():
	p = policy()
	p["rules"][0].update(method="formula", formula="carrier_price * 1.2 + 2")
	r = resolve(p, quoted(order(directory=[service(), service("B")])))
	assert [o["amount"] for o in r["offers"]] == [10.4, 15.2]
	assert len(r["offers"]) == 2
	assert all(o["source"] == "formula" for o in r["offers"])


@pytest.mark.parametrize(
	"q",
	[
		[],
		[{"service_id": "A", "amount": float("nan"), "boxes_quoted": 1}],
		[{"service_id": "A", "amount": 7, "boxes_quoted": 0}],
		[{"service_id": "unknown", "amount": 7, "boxes_quoted": 1}],
	],
)
def test_failure_never_becomes_free(q):
	p = policy()
	p["rules"][0]["method"] = "carrier"
	o = quoted(order(directory=[service()]), q)
	assert resolve(p, o)["reason"] == "not_offered"
	p["fallback"] = {"method": "fixed", "amount": 9}
	assert resolve(p, o)["offers"][0]["amount"] == 9


def test_fallback_only_prices_failed_shipment():
	p = policy()
	p["rules"][0]["method"] = "carrier"
	p["fallback"] = {"method": "fixed", "amount": 9}
	o = quoted(order(item(), item("P-X"), directory=[service()]), [])
	assert resolve(p, o)["offers"][0]["amount"] == 11


@pytest.mark.parametrize(
	"s",
	[service(enabled=False), service(countries=[]), service(countries=["BH"]), service(max_weight_kg=0.5)],
)
def test_carrier_unserved_has_no_fallback(s):
	p = policy()
	p["rules"][0]["method"] = "carrier"
	p["fallback"] = {"method": "fixed", "amount": 0}
	assert resolve(p, quoted(order(directory=[s])))["reason"] == "not_served"


def test_partial_cartons_drop_service():
	p = policy()
	p["rules"][0]["method"] = "carrier"
	o = quoted(order(directory=[service()]), boxes=[{"weight_kg": 1, "volume_l": 1}] * 2)
	assert resolve(p, o)["reason"] == "not_offered"


def test_manual_price_for_multiple_services():
	p = policy()
	p["rules"][0]["services"] = ["A", "B"]
	r = resolve(p, order(directory=[service(), service("B")]))
	assert len(r["offers"]) == 2 and all(x["amount"] == 3 for x in r["offers"])


@pytest.mark.parametrize("code,expected", [("{f-x-pt}", True), ("F-X-RF", False), ("F--PT", True)])
def test_patterns(code, expected):
	assert pattern_matches("F-*-PT", code) == expected


def test_pattern_classes_and_bounds():
	assert pattern_matches("X-?[AB]", "X-1A")
	assert not pattern_matches("*" * 13, "abc")
	start = time.monotonic()
	assert not pattern_matches("*a" * 12 + "b", "a" * 10000)
	assert time.monotonic() - start < 1


@pytest.mark.parametrize("junk", [None, [], "bad", 0, {}, {"active": True}, float("nan")])
def test_junk_never_raises(junk):
	assert isinstance(resolve(junk, junk), dict)
	assert validate_policy(junk)


def test_validation_and_overlapping_zones():
	p = policy()
	assert not validate_policy(p)
	p["rules"].append(row("Petite", "USA", 1))
	assert validate_policy(p)
	p = policy()
	p["rules"][0]["price_table"] = [{"up_to_lb": None, "price": 1}, {"up_to_lb": 2, "price": 2}]
	assert validate_policy(p)
	p = policy()
	p["item_sets"].append({"name": "pétite", "items": []})
	assert validate_policy(p)
	p = policy()
	p["rules"][0].update(method="formula", formula='__import__("os")')
	assert validate_policy(p)


def test_off_returns_inactive():
	p = policy()
	p["active"] = False
	assert resolve(p, order())["policy_active"] is False
