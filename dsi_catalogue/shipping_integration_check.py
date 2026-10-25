"""Read-only integration diagnostics and bounded response/URL validation. No policy presets."""

import ipaddress
import json
import math
import re
import socket
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import urlsplit

from dsi_catalogue.shipping_engine import RATE_CARD_SERVICE_ID

CHECK_NAMES = ("erp_price_api", "carrier", "website")
HTTP_TIMEOUT_SECONDS = 15  # the longest a call may take, in all
MAX_BODY_BYTES = 64 * 1024  # the most of an answer that is read
DETAIL_LIMIT = 4096  # the most of an answer that is shown, as compact JSON
WEBSITE_RATE_LIMIT = 10  # requests a minute that the website's rates route allows

_PROBLEMS = ("timeout", "unreachable", "too_large", "unreadable")
_SHOWN = 5  # methods, rates or names listed in a sentence
_RATES_KEPT = 10  # rates kept in the carrier check's detail


# ---------------------------------------------------------------- small readers
def _number(value):
	"""A finite float from an int or a float, no bool, nothing beyond a trillion; otherwise None."""
	if isinstance(value, bool) or not isinstance(value, int | float):
		return None
	try:
		number = float(value)
	except OverflowError:
		return None
	return number if math.isfinite(number) and abs(number) <= 10**12 else None


def _whole(value) -> int:
	"""A time in milliseconds as a whole number of at least 0."""
	number = _number(value)
	return 0 if number is None else max(0, int(number))


def _text(value, limit) -> str:
	"""A short plain text: what is not text is empty, runs of white space and unprintable characters become
	one space, and a text over `limit` characters is cut and ends in three dots."""
	if not isinstance(value, str):
		return ""
	text = " ".join("".join(char if char.isprintable() else " " for char in value[: limit * 4]).split())
	return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


_CENT = Decimal("0.01")


def _cents(amount) -> Decimal:
	"""`amount` (a finite number) in cents, rounded half up from the decimal text of the number, as the engine
	rounds money: 2.675 is 2.68, not the 2.67 that Python's own formatting makes of its binary value."""
	return Decimal(str(amount)).quantize(_CENT, rounding=ROUND_HALF_UP)


def _money(amount, currency="USD") -> str:
	"""$100.00 for dollars, 4.20 BHD for anything else, in cents rounded half up (see `_cents`)."""
	cents = _cents(amount)
	if currency == "USD":
		return f"${cents:,.2f}"
	return f"{cents:,.2f} {currency}"


def _same_cents(one, other) -> bool:
	"""Are two amounts the same to the cent, rounded half up? Both missing is the same; only one missing is not."""
	if one is None or other is None:
		return one is None and other is None
	return _cents(one) == _cents(other)


def _dict(value) -> dict:
	return value if isinstance(value, dict) else {}


def _detail(check) -> dict:
	return _dict(_dict(check).get("detail"))


def _check(name, ok, status, ms, summary, detail) -> dict:
	return {
		"name": name,
		"ok": bool(ok),
		"status": status if type(status) is int else None,
		"ms": _whole(ms),
		"summary": summary,
		"detail": detail,
	}


# ---------------------------------------------------------------- what may be shown of a response
# A key is dropped when its name holds one of these (in any capitals): the word itself, or a longer name that has
# it, such as `oauth`, `x-api-key`, `accessToken`, `hmacSignature` or `PHPSESSID`.
_SECRET_WORDS = (
	"password",
	"passwd",
	"passphrase",
	"passcode",
	"secret",
	"token",
	"key",
	"auth",  # also authorization
	"cookie",
	"credential",
	"traceback",
	"session",
	"sessid",
	"signature",
	"private",
)
# Short words that are a piece of honest ones too (`sid` in `side`, `pass` in `passenger`): they drop a key only as
# a word of its own, where a name is split at anything that is not a letter or a digit and at capitals (`userSid`,
# `x_pass`, `db.pwd`).
_SECRET_SHORT_WORDS = ("sid", "pwd", "pass")
_NAME_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")
# `exc` is where Frappe puts a traceback in the answer of a site in developer mode.
_SECRET_NAMES = ("exc",)
_MAX_DEPTH = 8
# How much is kept of a long answer, from the most to the least: (entries of a list or a dict, characters of a
# text). The first that fits the limit is used.
_BUDGETS = ((50, 300), (20, 200), (10, 100), (5, 60), (3, 40), (1, 20))


def _is_secret(key) -> bool:
	text = str(key)
	name = text.casefold()
	if name in _SECRET_NAMES or any(word in name for word in _SECRET_WORDS):
		return True
	return any(word.casefold() in _SECRET_SHORT_WORDS for word in _NAME_WORDS.findall(text))


def _clean(value, depth, entries, characters):
	if isinstance(value, dict):
		if depth >= _MAX_DEPTH:
			return "..."
		kept = {}
		for key, item in value.items():
			if len(kept) >= entries:
				break
			if not _is_secret(key):
				kept[str(key)[:100]] = _clean(item, depth + 1, entries, characters)
		return kept
	if isinstance(value, list | tuple):
		if depth >= _MAX_DEPTH:
			return "..."
		return [_clean(item, depth + 1, entries, characters) for item in list(value)[:entries]]
	if isinstance(value, bool) or value is None:
		return value
	if isinstance(value, int):
		return value if abs(value) < 10**15 else "(a very large number)"
	if isinstance(value, float):
		return value if math.isfinite(value) else None
	if isinstance(value, str):
		return value if len(value) <= characters else value[: characters - 3] + "..."
	try:
		text = str(value)
	except Exception:
		return "?"
	return text if len(text) <= characters else text[: characters - 3] + "..."


def _size(value) -> int:
	return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def sanitize_detail(obj, limit=DETAIL_LIMIT) -> dict:
	"""`obj` (the parsed JSON of an answer) as a dict that is safe to show: plain JSON data, never more than
	`limit` bytes as compact JSON, with every key that looks like a secret dropped at any depth.

	A key is dropped when its name holds `password`, `passwd`, `passphrase`, `passcode`, `secret`, `token`,
	`key`, `auth`, `cookie`, `credential`, `traceback`, `session`, `sessid`, `signature` or `private` in any
	capitals, when `sid`, `pwd` or `pass` is a word of it (split at anything that is not a letter or a digit,
	and at capitals: `userSid`, `x_pass`), or when it is `exc` (a traceback, in the answer of a Frappe site in
	developer mode). The keys of the answers read here (`offers`, `service_id`, `methods`, `packingPlan`,
	`exc_type` and the rest) all stay. What is not a dict is shown under "value". A long answer keeps
	its start: lists and dicts are cut to their first entries and long texts to their first characters, in
	steps, until it fits; the verdict reads the first method or offer of an answer, which survives. Anything
	nested more than eight levels deep, a number that is not finite and an object that is not JSON become "...",
	None or text. The input is not changed, and this never raises.
	"""
	if limit < 50:
		return {}
	for entries, characters in _BUDGETS:
		clean = _clean(obj, 0, entries, characters)
		if not isinstance(clean, dict):
			clean = {"value": clean}
		if _size(clean) <= limit:
			return clean
	text = json.dumps(clean, separators=(",", ":"), ensure_ascii=False)
	return {"truncated": True, "text": text[: limit - 60]}


# ---------------------------------------------------------------- the guard on the website's address
_R_NONE = "No website address is set in Storefront Settings."
_R_LONG = "The website address is too long."
_R_CHARS = "The website address contains spaces or characters that are not allowed."
_R_SCHEME = "The website address has to start with https://."
_R_USERINFO = "The website address must not contain a user name or a password."
_R_NO_HOST = "The website address has no host name."
_R_PORT = "The website address uses a port other than 80 or 443."
_R_PATH = "The website address has to be the website's address alone, with no path, query or fragment."
_R_IP_LITERAL = "The website address has to use a host name, not an IP number."
_R_PRIVATE = "The website address points at a private or local network address."
_R_HOST = "The website address is not a valid host name."
_R_LOOKUP = "The website address could not be looked up."
_R_RESOLVES = "The website address resolves to a private or local network address."

_URL_LIMIT = 255
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_TOP_LEVEL = re.compile(r"[a-z]{2,63}|xn--[a-z0-9-]{1,59}")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _is_public(address) -> bool:
	"""Can this address be reached over the internet? Not a private, loopback, link-local, multicast, reserved or
	unspecified one, and an IPv6 address that carries an IPv4 one (mapped, 6to4, NAT64) is judged by that one."""
	if address.version == 6:
		for carried in (address.ipv4_mapped, address.sixtofour):
			if carried is not None and not _is_public(carried):
				return False
		if address in _NAT64 and not _is_public(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)):
			return False
	return bool(
		address.is_global
		and not (
			address.is_private
			or address.is_loopback
			or address.is_link_local
			or address.is_multicast
			or address.is_reserved
			or address.is_unspecified
			or getattr(address, "is_site_local", False)  # IPv6 only: fec0::/10, deprecated but still in use
		)
	)


def _is_host_name(name) -> bool:
	"""A name with dots that a public DNS could hold: letters, digits and hyphens in each label, and a top-level
	label of letters (or an xn-- one), so that 127.1, 2130706433 and 0x7f000001 are no names."""
	labels = name.split(".")
	return (
		len(name) <= 253
		and len(labels) >= 2
		and all(_LABEL.fullmatch(label) for label in labels)
		and _TOP_LEVEL.fullmatch(labels[-1]) is not None
	)


def check_website_url(url, resolver=socket.getaddrinfo) -> tuple[bool, str]:
	"""May the server call this address? Returns (True, "") or (False, a plain sentence saying what is wrong,
	which never repeats the address).

	The address has to be the website's own and public: https (http only for the name `localhost`), a host
	name and no IP number, no user name or password, port 80 or 443 or none, no path, query or fragment, at
	most 255 characters and no spaces or control characters. The name is then looked up with `resolver` (the
	standard `socket.getaddrinfo` unless a test gives another), and every address it resolves to has to be
	public: a name that points at a private, loopback or link-local address is refused, whatever its spelling.
	`localhost` as that literal name is allowed, without a lookup, for a site on the same machine.

	It does not stop a name that changes its answer between this check and the call (DNS rebinding).
	"""
	if not isinstance(url, str) or not url.strip():
		return False, _R_NONE
	text = url.strip()
	if len(text) > _URL_LIMIT:
		return False, _R_LONG
	if not text.isascii() or any(ord(char) < 33 or ord(char) == 127 or char == "\\" for char in text):
		return False, _R_CHARS
	try:
		parts = urlsplit(text)
	except ValueError:
		return False, _R_HOST
	scheme = parts.scheme.lower()
	if scheme not in ("http", "https"):
		return False, _R_SCHEME
	if "@" in parts.netloc:
		return False, _R_USERINFO
	try:
		port = parts.port
	except ValueError:
		return False, _R_PORT
	host = (parts.hostname or "").lower()
	if not host:
		return False, _R_NO_HOST
	if port not in (None, 80, 443):
		return False, _R_PORT
	if parts.path not in ("", "/") or parts.query or parts.fragment or "?" in text or "#" in text:
		return False, _R_PATH
	if host == "localhost":
		return True, ""
	try:
		literal = ipaddress.ip_address(host)
	except ValueError:
		literal = None
	if literal is not None:
		return False, _R_IP_LITERAL if _is_public(literal) else _R_PRIVATE
	if scheme != "https":
		return False, _R_SCHEME
	name = host[:-1] if host.endswith(".") else host
	if not _is_host_name(name):
		return False, _R_HOST
	try:
		answers = resolver(name, port or 443)
		addresses = [ipaddress.ip_address(str(answer[4][0]).split("%")[0]) for answer in answers]
	except Exception:  # no answer, or one that is not a list of addresses
		return False, _R_LOOKUP
	if not addresses:
		return False, _R_LOOKUP
	if not all(_is_public(address) for address in addresses):
		return False, _R_RESOLVES
	return True, ""


# ---------------------------------------------------------------- a call that gave no answer
def _http(status) -> str:
	return f" HTTP {status}" if type(status) is int else ""


def _no_answer(name, who, status, ms, problem, what) -> dict:
	"""The check of a call that gave no answer worth reading. `who` is the ERP or the website, `what` what an
	answer would have held."""
	problem = problem if problem in _PROBLEMS else "unreadable"
	if problem == "timeout":
		text = f"The {who} did not answer within {HTTP_TIMEOUT_SECONDS} seconds."
	elif problem == "unreachable":
		text = f"The {who} could not be reached from the server."
	elif problem == "too_large":
		text = (
			f"The {who} answered{_http(status)} with more than {MAX_BODY_BYTES // 1024} KB, "
			f"which is far more than {what}, so it was not read."
		)
	else:
		text = f"The {who} answered{_http(status)}, but not with JSON."
	return _check(name, False, status, ms, text, {"problem": problem})


def website_not_asked(reason) -> dict:
	"""The website check when the website could not be asked: `reason` is the sentence `check_website_url`
	gave."""
	reason = _text(reason, 200)
	if reason and not reason.endswith("."):
		reason += "."
	return _check(
		"website", False, None, 0, f"The website was not asked. {reason}".rstrip(), {"problem": "not_asked"}
	)


def unexpected_failure(name, ms) -> dict:
	"""A check that stopped on an error nobody expected. The error itself is for the Error Log, not for here."""
	summary = "This check stopped because of an unexpected error, which was written to the Error Log."
	return _check(_text(name, 40), False, None, ms, summary, {"problem": "error"})


# ---------------------------------------------------------------- the first check: the ERP's price API
def _answer(body):
	"""The delivery answer inside the body of the ERP's reply ({"message": {...}}), or None if it is not one."""
	message = _dict(body).get("message")
	if (
		isinstance(message, dict)
		and isinstance(message.get("offers"), list)
		and isinstance(message.get("offered"), bool)
	):
		return message
	return None


def _first_amount(answer):
	offers = [offer for offer in answer["offers"] if isinstance(offer, dict)]
	return _number(offers[0].get("amount")) if offers else None


def _words(reason) -> str:
	return _text(reason, 40).replace("_", " ")


def _offers_phrase(answer) -> str:
	if answer.get("needs_quotes"):
		return "waiting for provider quotes"
	offers = [offer for offer in answer["offers"] if isinstance(offer, dict)]
	if not offers:
		reason = _words(answer.get("reason"))
		return f"no delivery offered ({reason})" if reason and not answer["offered"] else "no offers"
	amount = _first_amount(answer)
	price = "no price" if amount is None else _money(amount)
	return f"1 offer, {price}" if len(offers) == 1 else f"{len(offers)} offers, the first {price}"


def summarize_policy_response(status, ms, body, in_process_amount, *, problem=None) -> dict:
	"""The first check, from the ERP's reply to a POST of `price_delivery`.

	`status` is the HTTP status (None if there was none), `ms` the time the call took, `body` the reply parsed
	as JSON (None if it was not JSON) and `in_process_amount` the first offer's amount that the policy itself
	gives for the same order in this process (None for no offer). `problem` says why there is no reply to read
	("timeout", "unreachable", "too_large", "unreadable"). The check is ok when the ERP answered with HTTP 200
	and a delivery answer whose first offer costs what the policy says, to the cent.
	"""
	name = "erp_price_api"
	if problem is not None or type(status) is not int:
		return _no_answer(name, "ERP", status, ms, problem or "unreachable", "a delivery price")
	if status != 200:
		kind = _text(_dict(body).get("exc_type"), 60)
		named = f" ({kind})" if kind else ""
		summary = f"The ERP answered HTTP {status}{named} instead of a price."
		return _check(
			name, False, status, ms, summary, sanitize_detail(body) if isinstance(body, dict) else {}
		)
	answer = _answer(body)
	detail = sanitize_detail(body) if isinstance(body, dict) else {}
	if answer is None:
		return _check(
			name,
			False,
			status,
			ms,
			"The ERP answered, but not with a delivery price the website can read.",
			detail,
		)
	policy = "policy ON" if answer.get("policy_active") else "policy OFF"
	own = _number(in_process_amount)
	same = _same_cents(_first_amount(answer), own)
	summary = f"The ERP answered in {_whole(ms)} ms: {policy}, {_offers_phrase(answer)}."
	if not same:
		own_text = "no offer" if own is None else _money(own)
		summary += f" The public endpoint and the policy disagree. Here the policy itself gives {own_text}."
	return _check(name, same, status, ms, summary, detail)


def policy_amount(check):
	"""The final price of a successful active policy response, if available."""
	if _dict(check).get("ok") is not True:
		return None
	answer = _answer(_detail(check))
	if answer is None or answer.get("policy_active") is not True:
		return None
	return _first_amount(answer) if answer["offered"] else None


# ---------------------------------------------------------------- the second check: the carrier
def _carrier_error(error) -> tuple[str, str]:
	"""(sentence, problem code) for an error from the shipping module. Its own words are never passed on: they
	can hold what the provider's server sent back, credentials included."""
	if isinstance(error, ImportError):
		return "The shipping module is not installed on this site.", "not_installed"
	try:
		blocked = "blocked" in str(error).casefold()
	except Exception:
		blocked = False
	if blocked:
		return "The carrier says its account is blocked.", "blocked"
	return "The carrier could not be asked: the call stopped with an error.", "error"


def _rate(entry):
	"""One rate of the shipping module's reply as {"service_id", "name", "amount", "currency"}, or None."""
	entry = _dict(entry)
	amount = _number(entry.get("cost"))
	service_id = _text(entry.get("service_id"), 140)
	name = _text(entry.get("service_name"), 140) or service_id
	if amount is None or not name:
		return None
	currency = entry.get("currency")
	if not (isinstance(currency, str) and re.fullmatch(r"[A-Za-z]{3}", currency)):
		currency = "USD"
	return {"service_id": service_id or name, "name": name, "amount": amount, "currency": currency.upper()}


def summarize_carrier_response(rates_or_error, *, ms=0) -> dict:
	"""The second check, from what `dsi_shipping.api.get_shipping_rates` returned ({"success", "rates": [...]})
	or the exception it raised. It is ok when the carrier returned at least one rate.

	The shipping module swallows the errors of its carriers and returns no rates, so no rates is the usual
	sign of a carrier that refuses (the account is blocked, say) or does not serve the country; the Error Log
	holds the reason, which is not repeated here.
	"""
	name = "carrier"
	if isinstance(rates_or_error, BaseException):
		sentence, problem = _carrier_error(rates_or_error)
		return _check(name, False, None, ms, sentence, {"problem": problem})
	reply = _dict(rates_or_error)
	rates = reply.get("rates") if reply.get("success") is True else None
	if not isinstance(rates, list):
		return _check(name, False, None, ms, "The carrier did not answer with a list of rates.", {})
	listed = [rate for rate in map(_rate, rates) if rate is not None]
	if not listed:
		summary = (
			"The carrier returned no rates (the carrier account may be blocked or may not serve this country). "
			"See the Error Log entries titled get_shipping_rates."
		)
		return _check(name, False, None, ms, summary, {"count": 0, "rates": []})
	shown = ", ".join(
		f"{rate['name']} {_money(rate['amount'], rate['currency'])}" for rate in listed[:_SHOWN]
	)
	more = f" and {len(listed) - _SHOWN} more" if len(listed) > _SHOWN else ""
	plural = "" if len(listed) == 1 else "s"
	summary = f"The carrier returned {len(listed)} rate{plural}: {shown}{more}."
	detail = sanitize_detail({"count": len(listed), "rates": listed[:_RATES_KEPT]})
	return _check(name, True, None, ms, summary, detail)


# ---------------------------------------------------------------- the third check: the website
def _methods(body) -> list[tuple[str, str, float]]:
	"""(id, label, amount) of the delivery methods in the website's reply. The id is as the website wrote it,
	for comparing (is it the rate card's?); the label is what a sentence shows: the method's name, else its id,
	cleaned and cut like every text of an answer, else "a method with no name"."""
	listed = _dict(body).get("methods")
	methods = []
	for entry in listed if isinstance(listed, list) else []:
		entry = _dict(entry)
		method_id, amount = entry.get("id"), _number(entry.get("shippingAmount"))
		if isinstance(method_id, str) and method_id and amount is not None:
			label = _text(entry.get("serviceName"), 140) or _text(method_id, 140) or "a method with no name"
			methods.append((method_id, label, amount))
	return methods


def _refusal(body) -> str:
	"""The website's own sentence for a refusal, cut short, or ""."""
	return _text(_dict(body).get("error"), 160)


def summarize_website_response(status, ms, body, erp_amount, *, problem=None) -> dict:
	"""The third check, from the reply of the website's shipping rates route.

	`erp_amount` is the amount the ERP's price API offers for the same cart when the policy is on with the rate
	card (see `rate_card_amount`), or None when there is nothing to compare with. The check is ok when the
	website answered HTTP 200 with at least one delivery method and, if there is an amount to compare with,
	its first method costs that. A 429 is the website limiting how often this route may be called (10 requests
	a minute): it is said as that, and is not a failure of the integration, but nothing was checked either.
	"""
	name = "website"
	if problem is not None or type(status) is not int:
		return _no_answer(name, "website", status, ms, problem or "unreachable", "a list of delivery methods")
	detail = sanitize_detail(body) if isinstance(body, dict) else {}
	if status == 429:
		summary = (
			f"The website answered HTTP 429: it allows this route {WEBSITE_RATE_LIMIT} requests a minute and is "
			"limiting this one. That is not a failure of the integration. Run the check again in a minute."
		)
		return _check(name, False, status, ms, summary, detail)
	if 300 <= status < 400:
		summary = (
			f"The website answered HTTP {status}, a redirect. Redirects are not followed, so the address in "
			"Storefront Settings may be out of date (an old domain, or http where https is needed)."
		)
		return _check(name, False, status, ms, summary, detail)
	if status != 200:
		sentence = _refusal(body)
		tail = f": {sentence}{'' if sentence.endswith('.') else '.'}" if sentence else "."
		return _check(name, False, status, ms, f"The website answered HTTP {status}{tail}", detail)
	methods = _methods(body)
	if not methods:
		summary = f"The website answered in {_whole(ms)} ms but returned no delivery methods."
		return _check(name, False, status, ms, summary, detail)
	shown = ", ".join(f"{label} {_money(amount)}" for _, label, amount in methods[:_SHOWN])
	more = f" and {len(methods) - _SHOWN} more" if len(methods) > _SHOWN else ""
	plural = "" if len(methods) == 1 else "s"
	summary = (
		f"The website answered in {_whole(ms)} ms with {len(methods)} delivery method{plural}: {shown}{more}."
	)
	ok, first = True, methods[0][2]
	expected = _number(erp_amount)
	if expected is not None:
		if _same_cents(first, expected):
			summary += f" Its first amount matches the ERP's {_money(expected)}."
		else:
			ok = False
			summary += f" Its first amount, {_money(first)}, is not the ERP's {_money(expected)}."
	return _check(name, ok, status, ms, summary, detail)
