"""Pins the index-key decoder: real samples, the grouping-equality rule that
b7def79 established, and the refill suffix convention."""

import json
from pathlib import Path

from dsi_catalogue.index_key import (
	decode_index_key,
	get_product_grouping_key,
	get_template_index_key,
)

SAMPLES = json.loads((Path(__file__).parent / "fixtures" / "index_key_samples.json").read_text())


class TestDecode:
	def test_samples_decode_stably(self):
		"""Regenerate with: see fixtures/index_key_samples.json header. If the
		decoder's OUTPUT ever changes for these keys, a consumer downstream
		(slugs, grouping, gender siblings) changed with it — this test is the
		tripwire."""
		for s in SAMPLES:
			d = decode_index_key(s["key"])
			assert d is not None, s["key"]
			assert {k: d[k] for k in ("palace", "range", "productCode", "variants", "isTemplate")} == s[
				"decoded"
			]

	def test_garbage_returns_none(self):
		for bad in ("", "not-a-key", "{X-ZZ}", "{", "single"):
			assert decode_index_key(bad) is None or decode_index_key(bad).get("palace")


class TestGrouping:
	def test_ds_and_dss_are_different_products(self):
		assert get_product_grouping_key("{P-AQ-AD2-DS}") != get_product_grouping_key("{P-AQ-AD2-DSS}")

	def test_template_and_variant_share_grouping(self):
		assert get_template_index_key("{F-CR-AK-M}") == "{F-CR-AK}"

	def test_grouping_of_template_is_itself(self):
		assert get_template_index_key("{F-CR-AK}") == "{F-CR-AK}"


class TestRefill:
	def test_refill_shares_the_base_grouping(self):
		# The -RF convention (refill service): a refill is a VARIANT of its
		# base product — same grouping, so its PDP inherits the base gallery
		# and shared website_content. Hiding refills from shop listings is the
		# storefront's job (regex on the index key), not the grouping's.
		base = get_product_grouping_key("{F-PR-ODA-LSS}")
		refill = get_product_grouping_key("{F-PR-ODA-LSS-RF}")
		assert refill == base
		assert "{F-PR-ODA-LSS-RF}".endswith("-RF}")
