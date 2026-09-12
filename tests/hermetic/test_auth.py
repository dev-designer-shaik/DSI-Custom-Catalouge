"""Pins the token gate: constant-time, fail-closed on empty either side."""

import pytest
from dsi_catalogue.auth import token_matches


class TestTokenMatches:
	@pytest.mark.parametrize(
		"supplied,expected,expected_result",
		[
			("right-token", "right-token", True),
			("wrong-token", "right-token", False),
			("right-token", "", False),
			("", "right-token", False),
			("", "", False),
			(None, "right-token", False),
		],
	)
	def test_matrix(self, supplied, expected, expected_result):
		assert token_matches(supplied, expected) is expected_result
