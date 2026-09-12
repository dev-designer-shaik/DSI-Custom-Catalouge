"""Constant-time token comparison, extracted so it can be pinned hermetically.

Empty expected or empty supplied NEVER matches — the old form fell through
to an `expected and supplied == expected` shape where a missing configured
token plus an empty header could alias to True on some codepaths.
"""

import hmac


def token_matches(supplied: str, expected: str | None) -> bool:
	if not expected:
		return False
	supplied = supplied or ""
	return hmac.compare_digest(supplied.encode(), str(expected).encode())
