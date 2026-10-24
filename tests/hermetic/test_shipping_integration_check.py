"""Pins the pure helpers behind the wizard's Test tab: the sample order, the guard on the website's address,
the summaries of the three checks and the one-sentence verdict.

The Frappe layer (`shipping_policy_wizard.test_integration`) does the asking: an HTTP call to the ERP's own
price API, an in-process call to the carrier, an HTTP call to the website. It hands what came back to these
functions, which say it in plain words. Nothing here needs Frappe, a network or a clock. The guard that decides
whether the website's address may be called at all is tested against hostile addresses, with a stand-in for
the resolver that looks host names up.
"""

import ast
import inspect
import ipaddress
import json
import socket
import unicodedata
from pathlib import Path

import pytest

from dsi_catalogue import shipping_integration_check as integration
from dsi_catalogue.shipping_integration_check import (
	CHECK_NAMES,
	check_website_url,
	pick_sample_order,
	rate_card_amount,
	sanitize_detail,
	summarize_carrier_response,
	summarize_policy_response,
	summarize_website_response,
	unexpected_failure,
	verdict_for,
	website_not_asked,
)

MODULE = Path(integration.__file__)
CHECK_KEYS = {"name", "ok", "status", "ms", "summary", "detail"}
PUBLIC = "93.184.216.34"
SECRET = "hunter2-SECRET"


# ---------------------------------------------------------------- stand-ins and sample answers
def resolving(*addresses):
	"""A stand-in for socket.getaddrinfo that answers every name with these addresses and notes what it was asked."""
	asked = []

	def resolver(host, port, *rest, **more):
		asked.append((host, port))
		answers = []
		for address in addresses:
			if ":" in address:
				answers.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, port, 0, 0)))
			else:
				answers.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port)))
		return answers

	resolver.asked = asked
	return resolver


def never_asked():
	"""A stand-in resolver that must not be asked: it notes each call, and the test checks there was none. (It
	answers with nothing rather than raising, because the guard would take a raised error for a failed lookup.)"""

	def resolver(host, port, *rest, **more):
		resolver.calls.append(host)
		return []

	resolver.calls = []
	return resolver


def row(name, code, key, published=1):
	return {"name": name, "item_code": code, "index_key": key, "published": published}


# Website Items as the Frappe layer reads them, in no useful order.
WEB_ITEMS = [
	row("WEB-ITM-0030", "10000097", "{P-DQ-BP-RR}"),
	row("WEB-ITM-0010", "10005021", "{F-CR-OSA-M-PT}"),
	row("WEB-ITM-0012", "10005030", "{F-CR-OSA-M-100-RF}"),
	row("WEB-ITM-0011", "10005020", "{F-CR-OSA-M-40}"),
	row("WEB-ITM-0020", "10005040", "{F-CR-OSA-W-40}"),
	row("WEB-ITM-0031", "10000098", "{P-DQ-BP-RS}"),
	row("WEB-ITM-0001", "10009999", "{F-CR-AK-UP-40}", published=0),
	row("WEB-ITM-0002", "10009998", "{P-ZZ-ZZ}", published=0),
]

OFFER = {
	"service_id": "rate-card",
	"name": "International Express (4-7 days)",
	"amount": 100.0,
	"source": "rate_card",
	"reason": None,
	"estimated_days": 7,
}


def policy_body(**answer):
	"""What the ERP's price_delivery answers over HTTP: the answer under "message"."""
	message = {
		"policy_active": True,
		"price_source": "rate_card",
		"zone": None,
		"offered": True,
		"reason": None,
		"message": "",
		"offers": [OFFER],
	}
	message.update(answer)
	return {"message": message}


def site_method(method_id="rate-card", name="International Express", amount=100):
	return {
		"id": method_id,
		"provider": "",
		"providerCode": "",
		"serviceName": name,
		"scope": "International",
		"estimatedDays": 7,
		"shippingAmount": amount,
		"currency": "USD",
		"codSupported": False,
		"collectionDate": None,
		"deliveryDate": None,
	}


def site_body(*methods, plan="rate-card"):
	"""What the website's shipping rates route answers."""
	return {
		"success": True,
		"package": {"total_units": 2, "total_boxes": 0, "total_weight_kg": 0, "boxes": []},
		"packingPlan": plan,
		"methods": list(methods),
		"defaultMethodId": methods[0]["id"] if methods else None,
	}


CARRIER_REPLY = {
	"success": True,
	"rates": [
		{
			"provider": "Aramex BH",
			"service_name": "Aramex BH GCC PPX",
			"service_id": "Aramex BH GCC PPX",
			"scope": "GCC",
			"estimated_days": 3,
			"cost": 38.2,
			"currency": "USD",
			"cod_supported": False,
		}
	],
}

NO_RATES_SENTENCE = (
	"The carrier returned no rates (the carrier account may be blocked or may not serve this country). "
	"See the Error Log entries titled get_shipping_rates."
)


def erp_check(**answer):
	"""The first check, as the ERP answers it, with the policy giving the same price."""
	body = policy_body(**answer)
	offers = body["message"]["offers"]
	return summarize_policy_response(200, 180, body, offers[0]["amount"] if offers else None)


def carrier_check(reply=CARRIER_REPLY):
	return summarize_carrier_response(reply)


def website_check(body=None, status=200, erp_amount=100.0, **more):
	return summarize_website_response(
		status, 640, site_body(site_method()) if body is None else body, erp_amount, **more
	)


def assert_check(check, name):
	"""The shape every check has, whatever happened."""
	assert set(check) == CHECK_KEYS
	assert check["name"] == name
	assert type(check["ok"]) is bool
	assert check["status"] is None or (type(check["status"]) is int)
	assert type(check["ms"]) is int and check["ms"] >= 0
	assert isinstance(check["summary"], str) and check["summary"].endswith(".")
	assert isinstance(check["detail"], dict)
	json.dumps(check, allow_nan=False)  # plain data the page can be sent


# ---------------------------------------------------------------- the module
def test_the_module_is_pure_python_with_no_frappe_and_no_http_library():
	tree = ast.parse(MODULE.read_text(encoding="utf-8"))
	imported = set()
	for node in ast.walk(tree):
		if isinstance(node, ast.Import):
			imported |= {alias.name.split(".")[0] for alias in node.names}
		elif isinstance(node, ast.ImportFrom):
			imported.add((node.module or "").split(".")[0])
	assert not imported & {"frappe", "requests", "urllib3", "http", "dsi_core"}, imported
	assert imported <= {
		"decimal",
		"dsi_catalogue",
		"fnmatch",
		"ipaddress",
		"json",
		"math",
		"re",
		"socket",
		"urllib",
	}, imported


def test_the_three_checks_have_fixed_names_in_a_fixed_order():
	assert CHECK_NAMES == ("erp_price_api", "carrier", "website")


# ---------------------------------------------------------------- the sample order
def test_the_sample_order_is_the_first_full_size_fragrance_and_the_first_watch_by_name():
	order = pick_sample_order(WEB_ITEMS)
	assert order == {
		"country_code": "US",
		"country": "United States",
		"city": "New York",
		"items": [{"item_code": "10005020", "qty": 1}, {"item_code": "10000097", "qty": 1}],
		"note": "",
	}


def test_the_sample_order_does_not_depend_on_the_order_the_rows_come_in():
	assert pick_sample_order(list(reversed(WEB_ITEMS))) == pick_sample_order(WEB_ITEMS)
	original = json.dumps(WEB_ITEMS)
	pick_sample_order(WEB_ITEMS)
	assert json.dumps(WEB_ITEMS) == original, "the rows are not changed"


@pytest.mark.parametrize(
	("key", "is_full_size_fragrance"),
	[
		("{F-CR-OSA-M-40}", True),
		("F-CR-OSA-M-40", True),
		("{f-cr-osa-m-40}", True),
		(" {F-CR-OSA-M-40} ", True),
		("{{F-CR-XYZ-IGS}}", True),
		("{F-CR-OSA-RF-40-W}", True),  # RF in the middle of a key is not a refill
		("{F-CR-OSA-M-PT}", False),  # petite
		("{F-CR-OSA-M-100-RF}", False),  # refill
		("{F-CR-PC-PTT-01C}", False),  # petite collection
		("{S-AB-CD}", False),
		("{FX-1}", False),
		("", False),
		(None, False),
		(5, False),
	],
)
def test_which_index_keys_count_as_a_full_size_fragrance(key, is_full_size_fragrance):
	rows = [row("A", "1", key), row("B", "2", "{P-DQ-BP-RR}")]
	order = pick_sample_order(rows)
	assert (order["note"] == "") is is_full_size_fragrance, order
	assert order["items"][0]["item_code"] == ("1" if is_full_size_fragrance else "2")


@pytest.mark.parametrize(
	("key", "is_watch"),
	[
		("{P-DQ-BP-RR}", True),
		("P-DQ-BP-RR", True),
		("{p-x}", True),
		("{S-P-X}", False),
		("{PX-1}", False),
		(None, False),
	],
)
def test_which_index_keys_count_as_a_watch(key, is_watch):
	rows = [row("A", "1", "{F-CR-OSA-M-40}"), row("B", "2", key)]
	order = pick_sample_order(rows)
	assert (order["note"] == "") is is_watch, order


def test_unpublished_items_are_not_preferred():
	order = pick_sample_order([row("A", "1", "{F-CR-OSA-M-40}", published=0), *WEB_ITEMS[:5]])
	assert [item["item_code"] for item in order["items"]] == ["10005020", "10000097"]
	assert order["note"] == ""


def test_other_items_stand_in_when_there_is_no_published_fragrance_or_no_watch_and_the_note_says_so():
	no_watch = [r for r in WEB_ITEMS if not r["index_key"].startswith("{P-")]
	order = pick_sample_order(no_watch)
	assert order["items"][0] == {"item_code": "10005020", "qty": 1}, "the fragrance is still the first"
	assert len(order["items"]) == 2
	assert order["items"][1]["item_code"] != "10005020"
	assert "watch" in order["note"] and order["note"].endswith(".")
	no_fragrance = [r for r in WEB_ITEMS if not r["index_key"].startswith("{F-CR-OSA-M-40")]
	order = pick_sample_order([r for r in no_fragrance if r["index_key"] != "{F-CR-OSA-W-40}"])
	assert "fragrance" in order["note"] and order["note"].endswith(".")
	assert order["items"][0] == {"item_code": "10000097", "qty": 1}, (
		"the watch comes first when there is no fragrance"
	)
	assert len(order["items"]) == 2


def test_with_no_published_items_any_two_website_items_are_used_and_the_note_says_so():
	unpublished = [dict(r, published=0) for r in WEB_ITEMS]
	order = pick_sample_order(unpublished)
	assert [item["item_code"] for item in order["items"]] == ["10009999", "10009998"], "the first two by name"
	assert order["note"] and order["note"].endswith(".")
	assert (order["country_code"], order["city"]) == ("US", "New York")


def test_a_site_with_no_website_items_gets_a_sample_order_with_no_items_and_a_note():
	for rows in ([], None, "nope", [None, 5, "x", {}]):
		order = pick_sample_order(rows)
		assert order["items"] == [] and order["note"].endswith("."), rows
		assert order["country_code"] == "US"


def test_one_item_is_not_picked_twice_and_odd_rows_are_skipped():
	rows = [
		row("A", "10005020", "{F-CR-OSA-M-40}"),
		row("B", "10005020", "{P-DQ-BP-RR}"),  # the same item code under another Website Item
		{"name": "C", "item_code": "", "index_key": "{P-X}", "published": 1},
		{"name": "D", "item_code": None, "index_key": "{P-X}", "published": 1},
		{"name": "E", "index_key": "{P-X}", "published": 1},
		None,
		7,
	]
	order = pick_sample_order(rows)
	codes = [item["item_code"] for item in order["items"]]
	assert codes == ["10005020"] and len(set(codes)) == len(codes)


# ---------------------------------------------------------------- the guard on the website's address
GOOD_ADDRESSES = [
	"https://dsi-web-n.netlify.app",
	"https://dsi-web-n.netlify.app/",
	"https://designershaik.com",
	"  https://designershaik.com/ \n",
	"HTTPS://Designershaik.COM/",
	"https://designershaik.com:443/",
	"https://designershaik.com:80",
	"https://xn--bcher-kva.example.com/",
]


@pytest.mark.parametrize("address", GOOD_ADDRESSES)
def test_a_public_https_address_is_allowed(address):
	resolver = resolving(PUBLIC)
	assert check_website_url(address, resolver=resolver) == (True, "")
	assert len(resolver.asked) == 1, "the name is looked up once"
	host, port = resolver.asked[0]
	assert host == host.lower() and not host.endswith(".")
	assert port in (443, 80)


def test_the_resolver_is_asked_with_the_port_of_the_address():
	for address, port in (("https://shop.example.com/", 443), ("https://shop.example.com:80/", 80)):
		resolver = resolving(PUBLIC)
		assert check_website_url(address, resolver=resolver) == (True, "")
		assert resolver.asked == [("shop.example.com", port)]


HOSTILE = {
	"the cloud metadata service": "http://169.254.169.254/",
	"the metadata service over https": "https://169.254.169.254/latest/meta-data/",
	"a user name and password": "https://user:pw@host.example.com/",
	"an empty user name": "https://@example.com/",
	"a password and nothing else": "https://:pw@example.com/",
	"a file": "file:///etc/passwd",
	"ftp": "ftp://example.com/",
	"javascript": "javascript:alert(1)",
	"no scheme": "example.com",
	"no scheme, a port": "example.com:443",
	"a scheme-relative address": "//example.com/",
	"IPv6 loopback": "https://[::1]/",
	"IPv4 mapped loopback": "https://[::ffff:127.0.0.1]/",
	"IPv6 unique local": "https://[fd00::1]/",
	"a private IPv4 address": "https://10.0.0.5/",
	"another private IPv4 address": "https://192.168.1.10/",
	"IPv4 loopback": "https://127.0.0.1/",
	"IPv4 loopback over http": "http://127.0.0.1/",
	"link-local": "https://169.254.1.1/",
	"this host": "https://0.0.0.0/",
	"a public IP number": f"https://{PUBLIC}/",
	"a public IPv6 number": "https://[2606:4700:4700::1111]/",
	"loopback written short": "https://127.1/",
	"loopback written as one number": "https://2130706433/",
	"loopback written in hex": "https://0x7f000001/",
	"loopback written in octal": "https://0177.0.0.1/",
	"a port": "https://example.com:8443/",
	"the ssh port": "https://example.com:22/",
	"a port that is no number": "https://example.com:abc/",
	"a port that is too big": "https://example.com:99999/",
	"plain http": "http://example.com/",
	"a path": "https://example.com/shop",
	"a query": "https://example.com/?a=1",
	"a query with no path": "https://example.com?a=1",
	"an empty query": "https://example.com/?",
	"a fragment": "https://example.com/#x",
	"an empty string": "",
	"only spaces": "   ",
	"nothing": None,
	"a number": 5,
	"a list": ["https://example.com"],
	"a very long address": "https://" + "a" * 300 + ".com/",
	"a space inside": "https://exa mple.com/",
	"a tab inside": "https://exa\tmple.com/",
	"a line break inside": "https://example.com/\nGET /x",
	"a NUL": "https://example.com\x00.evil.com/",
	"a backslash": "https://example.com\\@evil.com/",
	"a host with no dot": "https://intranet/",
	"an underscore in the host": "https://my_site.example.com/",
	"an accented host": "https://bücher.de/",
	"a host with a double dot": "https://example..com/",
	"a host that starts with a hyphen": "https://-example.com/",
	"a numeric last label": "https://example.123/",
	"no host": "https:///",
	"no host, a port": "https://:443/",
	"an unclosed bracket": "https://[::1/",
	"localhost on another port": "https://localhost:8443/",
	"localhost with a dot": "https://localhost./",
	"a user on localhost": "https://x@localhost/",
}


@pytest.mark.parametrize("address", list(HOSTILE.values()), ids=list(HOSTILE))
def test_a_hostile_or_unusable_address_is_refused_before_anything_is_looked_up(address):
	resolver = never_asked()
	ok, reason = check_website_url(address, resolver=resolver)
	assert resolver.calls == [], "refused without a lookup"
	assert ok is False
	assert isinstance(reason, str) and reason.endswith(".")
	assert "pw" not in reason.split() and "evil" not in reason, "the address is not repeated in the reason"
	if isinstance(address, str) and len(address) > 12:
		assert address.strip() not in reason


REASONS = [
	("", "No website address is set in Storefront Settings."),
	("https://" + "a" * 300 + ".com/", "The website address is too long."),
	("https://exa mple.com/", "The website address contains spaces or characters that are not allowed."),
	(
		"https://example.com\\evil.com/",
		"The website address contains spaces or characters that are not allowed.",
	),
	("ftp://example.com/", "The website address has to start with https://."),
	("http://example.com/", "The website address has to start with https://."),
	("https://user:pw@example.com/", "The website address must not contain a user name or a password."),
	("https:///", "The website address has no host name."),
	("https://example.com:8443/", "The website address uses a port other than 80 or 443."),
	(
		"https://example.com/shop",
		"The website address has to be the website's address alone, with no path, query or fragment.",
	),
	(f"https://{PUBLIC}/", "The website address has to use a host name, not an IP number."),
	("https://10.0.0.5/", "The website address points at a private or local network address."),
	("https://intranet/", "The website address is not a valid host name."),
]


@pytest.mark.parametrize(("address", "reason"), REASONS)
def test_each_refusal_says_what_is_wrong_in_its_own_plain_words(address, reason):
	assert check_website_url(address, resolver=never_asked()) == (False, reason)


def test_a_lookup_that_fails_or_finds_a_private_address_says_so():
	def gone(host, port, *rest, **more):
		raise socket.gaierror(-2, "Name or service not known")

	assert check_website_url("https://shop.example.com/", resolver=gone) == (
		False,
		"The website address could not be looked up.",
	)
	assert check_website_url("https://shop.example.com/", resolver=resolving("10.1.2.3")) == (
		False,
		"The website address resolves to a private or local network address.",
	)


@pytest.mark.parametrize(
	"address",
	[
		"127.0.0.1",
		"192.168.1.5",
		"169.254.169.254",
		"10.0.0.1",
		"172.16.0.9",
		"100.64.0.1",
		"0.0.0.0",
		"224.0.0.1",
	],
)
def test_a_host_name_that_resolves_to_a_private_or_local_ipv4_address_is_refused(address):
	assert check_website_url("https://shop.example.com/", resolver=resolving(address))[0] is False


@pytest.mark.parametrize(
	"address",
	[
		"::1",
		"fe80::1",
		"fc00::1",
		"fd12:3456::1",
		"::",
		"::ffff:127.0.0.1",
		"::ffff:7f00:1",
		"::ffff:192.168.0.1",
		"2002:7f00:1::",  # 6to4 for 127.0.0.1
		"64:ff9b::7f00:1",  # NAT64 for 127.0.0.1
		"ff02::1",
		"fec0::1",  # site-local: deprecated, and still used inside some old networks
	],
)
def test_a_host_name_that_resolves_to_a_private_or_local_ipv6_address_is_refused(address):
	ok, reason = check_website_url("https://shop.example.com/", resolver=resolving(address))
	assert ok is False and reason.endswith(".")


ADDRESS_FLAGS = (
	"is_private",
	"is_loopback",
	"is_link_local",
	"is_multicast",
	"is_reserved",
	"is_unspecified",
	"is_site_local",
)


@pytest.mark.parametrize("flag", ADDRESS_FLAGS)
def test_each_kind_of_address_that_is_not_public_is_refused_by_its_own_check(flag):
	# The standard library already calls most of these ranges private or not global, and decides some of them
	# differently from one Python to the next: each check has to stand on its own.
	flags = {name: property(lambda self: False) for name in ADDRESS_FLAGS}
	flags.update({"is_global": property(lambda self: True), flag: property(lambda self: True)})
	address = type("Odd", (ipaddress.IPv4Address,), flags)("93.184.216.34")
	assert integration._is_public(address) is False
	plain = {name: property(lambda self: False) for name in ADDRESS_FLAGS}
	plain["is_global"] = property(lambda self: True)
	assert integration._is_public(type("Plain", (ipaddress.IPv4Address,), plain)("93.184.216.34")) is True


@pytest.mark.parametrize(
	"text",
	[
		"::ffff:127.0.0.1",
		"::ffff:10.0.0.1",
		"2002:7f00:1::",
		"2002:a00:1::",
		"64:ff9b::7f00:1",
		"64:ff9b::a00:1",
	],
)
def test_an_ipv6_address_that_carries_a_private_ipv4_address_is_never_public(text):
	# Mapped, 6to4 and NAT64 addresses are judged by the IPv4 address they carry, whatever the address says of itself.
	flags = {name: property(lambda self: False) for name in ADDRESS_FLAGS}
	flags["is_global"] = property(lambda self: True)
	assert integration._is_public(type("Odd", (ipaddress.IPv6Address,), flags)(text)) is False
	assert (
		integration._is_public(type("Odd", (ipaddress.IPv6Address,), flags)("2606:4700:4700::1111")) is True
	)
	assert integration._is_public(type("Odd", (ipaddress.IPv6Address,), flags)("64:ff9b::808:808")) is True


def test_one_private_answer_among_public_ones_refuses_the_name():
	assert check_website_url("https://shop.example.com/", resolver=resolving(PUBLIC, "127.0.0.1"))[0] is False
	assert check_website_url("https://shop.example.com/", resolver=resolving("127.0.0.1", PUBLIC))[0] is False
	assert check_website_url("https://shop.example.com/", resolver=resolving(PUBLIC, "2606:4700:4700::1111"))[
		0
	]


def test_a_name_that_cannot_be_looked_up_is_refused():
	def gaierror(host, port, *rest, **more):
		raise socket.gaierror(-2, "Name or service not known")

	def broken(host, port, *rest, **more):
		raise RuntimeError("resolver down")

	def nothing(host, port, *rest, **more):
		return []

	def nonsense(host, port, *rest, **more):
		return [(2, 1, 6, "", ("not an address", port))]

	def short(host, port, *rest, **more):
		return [(2, 1, 6)]

	for resolver in (gaierror, broken, nothing, nonsense, short):
		ok, reason = check_website_url("https://shop.example.com/", resolver=resolver)
		assert ok is False and reason.endswith("."), resolver.__name__


def test_a_name_that_only_looks_like_localhost_is_looked_up_and_refused():
	assert check_website_url("https://foo.localhost/", resolver=resolving("127.0.0.1"))[0] is False
	assert check_website_url("https://localhost.example.com/", resolver=resolving("127.0.0.1"))[0] is False


@pytest.mark.parametrize(
	"address", ["https://localhost/", "http://localhost/", "HTTP://LOCALHOST:80/", "https://localhost:443"]
)
def test_localhost_is_allowed_only_as_that_literal_name_and_is_not_looked_up(address):
	resolver = never_asked()
	assert check_website_url(address, resolver=resolver) == (True, "")
	assert resolver.calls == [], "localhost is not looked up"


def test_the_default_resolver_is_the_standard_one_and_what_it_really_answers_is_read():
	default = inspect.signature(check_website_url).parameters["resolver"].default
	assert default is socket.getaddrinfo
	asked = []

	def really_at_localhost(host, port, *rest, **more):
		# A real lookup through the default resolver, of the one name every machine knows without a network (it
		# is in /etc/hosts). What it hands back, a list of tuples with the address at [4][0], is what the guard
		# reads; the name of the address under test is only noted.
		asked.append((host, port))
		return default("localhost", port)

	assert check_website_url("https://shop.example.com/", resolver=really_at_localhost) == (
		False,
		"The website address resolves to a private or local network address.",
	)
	assert asked == [("shop.example.com", 443)]


# ---------------------------------------------------------------- what may be shown of a response
def test_sanitize_drops_secrets_at_any_depth_whatever_the_case():
	body = {
		"password": SECRET,
		"Secret": SECRET,
		"api_key": SECRET,
		"x-api-key": SECRET,
		"accessToken": SECRET,
		"Authorization": f"Bearer {SECRET}",
		"Set-Cookie": SECRET,
		"credentials": {"user": SECRET},
		"ok": "kept",
		"nested": {"deep": {"refresh_token": SECRET, "fine": 1, "list": [{"PASSWD": SECRET, "x": 2}, 3]}},
	}
	clean = sanitize_detail(body)
	assert SECRET not in json.dumps(clean)
	assert clean == {"ok": "kept", "nested": {"deep": {"fine": 1, "list": [{"x": 2}, 3]}}}
	assert "password" in body, "the input is not changed"


@pytest.mark.parametrize(
	"key",
	[
		"auth",
		"Auth",
		"x-auth",
		"oauth",
		"authToken",
		"pwd",
		"userPwd",
		"user_pwd",
		"PWD",
		"passwd",
		"pass",
		"Pass",
		"x_pass",
		"db.pass",
		"pass-word",
		"passphrase",
		"passcode",
		"sid",
		"SID",
		"userSid",
		"user_sid",
		"connect.sid",
		"session",
		"sessionId",
		"mySession",
		"PHPSESSID",
		"sessid",
		"signature",
		"hmacSignature",
		"private",
		"isPrivate",
		"cookie",
		"Set-Cookie",
	],
)
def test_sanitize_drops_the_names_that_nearly_name_a_secret(key):
	body = {
		key: SECRET,
		"kept": 1,
		"nested": {key: [{key: SECRET}], "fine": 2},
		"list": [{key: SECRET, "ok": 3}],
	}
	clean = sanitize_detail(body)
	assert clean == {"kept": 1, "nested": {"fine": 2}, "list": [{"ok": 3}]}, key
	assert SECRET not in json.dumps(clean)


@pytest.mark.parametrize(
	"key",
	[
		# every key of the price API's answer and of the website's
		"message",
		"policy_active",
		"price_source",
		"zone",
		"offered",
		"reason",
		"offers",
		"service_id",
		"name",
		"amount",
		"source",
		"estimated_days",
		"success",
		"package",
		"packingPlan",
		"methods",
		"defaultMethodId",
		"id",
		"provider",
		"providerCode",
		"serviceName",
		"scope",
		"estimatedDays",
		"shippingAmount",
		"currency",
		"codSupported",
		"collectionDate",
		"deliveryDate",
		"total_units",
		"total_boxes",
		"total_weight_kg",
		"dimensions",
		"boxes",
		"box_code",
		"error",
		"exception",
		"exc_type",
		"_server_messages",
		# short words that are only a piece of an honest word
		"side",
		"sidebar",
		"residual",
		"decide",
		"passenger",
		"bypass",
		"compass",
		"passage",
		"pwdx",
		"serviceId",
		"ServiceID",
		"outside_id",
	],
)
def test_sanitize_keeps_the_names_that_only_look_like_a_secret(key):
	assert sanitize_detail({key: 1, "nested": {key: [{key: 2}]}}) == {key: 1, "nested": {key: [{key: 2}]}}


def test_sanitize_drops_the_tracebacks_a_failing_frappe_site_sends_in_developer_mode():
	body = {
		"exc_type": "ValidationError",
		"exception": "frappe.exceptions.ValidationError",
		"exc": '["Traceback (most recent call last):\\n  File \\"/home/frappe/apps/x.py\\", line 1"]',
		"traceback": "Traceback (most recent call last)",
		"Traceback": "again, in other capitals",
		"_server_messages": '["a message"]',
	}
	assert sanitize_detail(body) == {
		"exc_type": "ValidationError",
		"exception": "frappe.exceptions.ValidationError",
		"_server_messages": '["a message"]',
	}


def test_sanitize_wraps_what_is_not_a_dict_and_keeps_plain_values():
	assert sanitize_detail([1, "a", None, True, 2.5]) == {"value": [1, "a", None, True, 2.5]}
	assert sanitize_detail("text") == {"value": "text"}
	assert sanitize_detail(None) == {"value": None}
	assert sanitize_detail({}) == {}
	assert sanitize_detail({"a": {"b": [1, {"c": None}]}}) == {"a": {"b": [1, {"c": None}]}}


def test_sanitize_makes_plain_json_out_of_anything():
	class Odd:
		def __str__(self):
			return "an odd thing"

	class Worse:
		def __str__(self):
			raise RuntimeError("no")

	clean = sanitize_detail(
		{
			"nan": float("nan"),
			"inf": float("-inf"),
			"odd": Odd(),
			"worse": Worse(),
			"bytes": b"abc",
			"set": {1},
			"tuple": (1, 2),
			3: "an int key",
			"huge": 10**40,
		}
	)
	text = json.dumps(clean, allow_nan=False)
	assert clean["nan"] is None and clean["inf"] is None
	assert clean["odd"] == "an odd thing"
	assert isinstance(clean["worse"], str)
	assert clean["tuple"] == [1, 2]
	assert clean["3"] == "an int key"
	assert json.loads(text) == clean


def test_sanitize_cuts_the_result_to_the_limit_and_keeps_the_start_of_it():
	body = {"success": True, "methods": [{"id": f"m{n}", "amount": n, "name": "x" * 400} for n in range(500)]}
	for limit in (4096, 1000, 300):
		clean = sanitize_detail(body, limit)
		size = len(json.dumps(clean, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
		assert size <= limit, (limit, size)
		assert clean.get("success") is True or clean.get("truncated") is True
	clean = sanitize_detail(body)
	assert clean["success"] is True
	assert clean["methods"][0]["id"] == "m0", "the first entries survive, which the verdict reads"


def test_sanitize_default_limit_is_four_kilobytes():
	assert integration.DETAIL_LIMIT == 4096
	big = {"text": "é" * 100_000, "list": list(range(100_000))}
	clean = sanitize_detail(big)
	assert len(json.dumps(clean, ensure_ascii=False).encode("utf-8")) <= 4096


def test_sanitize_survives_deep_and_looping_structures():
	deep = leaf = {}
	for _ in range(5000):
		leaf["next"] = {}
		leaf = leaf["next"]
	clean = sanitize_detail(deep)
	json.dumps(clean, allow_nan=False)
	loop = []
	loop.append(loop)
	clean = sanitize_detail({"loop": loop, "after": 1})
	json.dumps(clean, allow_nan=False)
	assert clean["after"] == 1
	nested = []
	for _ in range(5000):
		nested = [nested]
	json.dumps(sanitize_detail({"x": nested}), allow_nan=False)


def test_sanitize_falls_back_to_a_cut_text_when_nothing_else_fits():
	key = "k" * 100
	awkward = {key: {key: {key: 1}}}
	clean = sanitize_detail(awkward, 120)
	assert clean.get("truncated") is True and isinstance(clean["text"], str)
	assert len(json.dumps(clean, separators=(",", ":"))) <= 120
	assert sanitize_detail(awkward, 1000) == {key: {key: {key: 1}}}, "it fits, so it is not cut"


def test_sanitize_a_tiny_limit_gives_an_empty_detail_not_an_error():
	assert sanitize_detail({"a": "b" * 1000}, 10) == {}


# ---------------------------------------------------------------- the first check: the ERP's price API
def test_the_erp_check_says_what_the_example_says_when_the_policy_prices_the_order():
	check = summarize_policy_response(200, 180, policy_body(), 100.0)
	assert_check(check, "erp_price_api")
	assert check["ok"] is True and check["status"] == 200 and check["ms"] == 180
	assert (
		check["summary"] == "The ERP answered in 180 ms: policy ON, price source rate card, 1 offer, $100.00."
	)
	assert check["detail"]["message"]["offers"][0]["amount"] == 100.0
	assert check["detail"]["message"]["price_source"] == "rate_card"


def test_the_erp_check_compares_with_the_policys_own_price_to_the_cent():
	for own in (100.0, 100, 100.004, 99.996):
		assert summarize_policy_response(200, 1, policy_body(), own)["ok"] is True, own
	for own in (90.0, 100.01, 99.98, 0.0):
		check = summarize_policy_response(200, 1, policy_body(), own)
		assert check["ok"] is False, own
		assert "The public endpoint and the policy disagree." in check["summary"]
		assert "$100.00" in check["summary"]


def test_the_erp_check_names_both_amounts_when_they_disagree():
	check = summarize_policy_response(200, 50, policy_body(), 90.0)
	assert_check(check, "erp_price_api")
	assert "$90.00" in check["summary"] and "$100.00" in check["summary"]
	one_side_only = summarize_policy_response(
		200, 50, policy_body(offers=[], offered=False, reason="not_covered"), 90.0
	)
	assert one_side_only["ok"] is False and "$90.00" in one_side_only["summary"]
	the_other = summarize_policy_response(200, 50, policy_body(), None)
	assert the_other["ok"] is False and "The public endpoint and the policy disagree." in the_other["summary"]


def test_the_erp_check_describes_a_refusal_an_off_policy_and_several_offers():
	refused = summarize_policy_response(
		200,
		12,
		policy_body(
			offered=False,
			reason="not_covered",
			message="No delivery option applies to this order.",
			offers=[],
		),
		None,
	)
	assert refused["ok"] is True, "the endpoint works and agrees with the policy"
	assert refused["summary"] == (
		"The ERP answered in 12 ms: policy ON, price source rate card, no delivery offered (not covered)."
	)
	too_heavy = summarize_policy_response(
		200, 12, policy_body(offered=False, reason="too_heavy", offers=[]), None
	)
	assert "no delivery offered (too heavy)" in too_heavy["summary"]
	off = summarize_policy_response(
		200, 9, policy_body(policy_active=False, price_source="carrier", offered=False, offers=[]), None
	)
	assert off["ok"] is True
	assert off["summary"] == "The ERP answered in 9 ms: policy OFF, no offers."
	carrier = summarize_policy_response(
		200, 30, policy_body(price_source="carrier", reason="fallback_offer"), 100.0
	)
	assert "policy ON, price source carrier" in carrier["summary"]
	two = [{**OFFER, "amount": 38.2}, {**OFFER, "amount": 44.0, "service_id": "b"}]
	several = summarize_policy_response(200, 30, policy_body(price_source="carrier", offers=two), 38.2)
	assert several["summary"].endswith("2 offers, the first $38.20.")


def test_the_erp_check_reports_a_status_that_is_not_200():
	for status, body in (
		(403, {"exc_type": "PermissionError", "exception": "frappe.exceptions.PermissionError"}),
		(417, {"exc_type": "ValidationError"}),
		(500, None),
		(502, "<html>Bad Gateway</html>"),
		(404, {}),
		(301, None),
	):
		check = summarize_policy_response(status, 20, body, 100.0)
		assert_check(check, "erp_price_api")
		assert check["ok"] is False and check["status"] == status
		assert f"HTTP {status}" in check["summary"]
	assert (
		"PermissionError"
		in summarize_policy_response(403, 20, {"exc_type": "PermissionError"}, 1.0)["summary"]
	)


def test_the_erp_check_reports_an_answer_it_cannot_read_without_guessing():
	for body in (
		None,
		[],
		"text",
		7,
		{},
		{"message": "text"},
		{"message": {}},
		{"message": {"offers": "none", "offered": True}},
		{"message": {"offers": [], "offered": "yes"}},
		{"message": []},
	):
		check = summarize_policy_response(200, 20, body, 100.0)
		assert_check(check, "erp_price_api")
		assert check["ok"] is False, body
		assert "not with a delivery price" in check["summary"], body


def test_the_erp_check_reports_a_call_that_got_no_answer():
	expected = {
		"timeout": "The ERP did not answer within 15 seconds.",
		"unreachable": "The ERP could not be reached from the server.",
		"too_large": "more than 64 KB",
		"unreadable": "but not with JSON",
	}
	for problem, words in expected.items():
		status = None if problem in ("timeout", "unreachable") else 200
		check = summarize_policy_response(status, 15000, None, None, problem=problem)
		assert_check(check, "erp_price_api")
		assert check["ok"] is False and check["status"] == status
		assert words in check["summary"], problem
		assert check["detail"] == {"problem": problem}
	assert_check(summarize_policy_response(None, 5, None, None, problem="something new"), "erp_price_api")
	assert_check(summarize_policy_response("200", 5, policy_body(), 100.0), "erp_price_api")
	assert summarize_policy_response(None, 5, policy_body(), 100.0)["ok"] is False


def test_a_status_that_is_not_a_whole_number_is_no_status():
	for status in (True, False, 200.0, "200", [200], {}):
		for check in (
			summarize_policy_response(status, 5, policy_body(), 100.0),
			summarize_website_response(status, 5, site_body(site_method()), 100.0),
		):
			assert check["status"] is None and check["ok"] is False, status


def test_the_erp_checks_ms_is_a_whole_number_of_at_least_zero():
	for ms in (180, 180.9, -4, None, "slow", float("nan"), True):
		check = summarize_policy_response(200, ms, policy_body(), 100.0)
		assert type(check["ms"]) is int and check["ms"] >= 0, ms


def test_the_erp_checks_detail_is_the_answer_cut_and_cleaned():
	body = policy_body()
	body["message"]["offers"][0]["api_key"] = SECRET
	body["authorization"] = SECRET
	check = summarize_policy_response(200, 5, body, 100.0)
	assert SECRET not in json.dumps(check)
	assert check["detail"]["message"]["offers"][0]["amount"] == 100.0


# ---------------------------------------------------------------- the rate-card amount a later check compares with
def test_the_rate_card_amount_is_there_only_when_the_policy_is_on_with_the_rate_card_and_offers_delivery():
	assert rate_card_amount(erp_check()) == 100.0
	assert rate_card_amount(erp_check(price_source="carrier")) is None
	assert rate_card_amount(erp_check(policy_active=False)) is None
	assert rate_card_amount(erp_check(offered=False, reason="too_heavy", offers=[])) is None
	assert rate_card_amount(summarize_policy_response(200, 1, policy_body(), 90.0)) is None, (
		"the check failed"
	)
	assert rate_card_amount(summarize_policy_response(None, 1, None, None, problem="timeout")) is None
	for junk in (
		None,
		{},
		[],
		"x",
		{"detail": 5},
		{"ok": True, "detail": {"message": 3}},
		{"ok": True, "detail": {"message": {"offers": [5]}}},
	):
		assert rate_card_amount(junk) is None


# ---------------------------------------------------------------- the second check: the carrier
def test_the_carrier_check_lists_the_rates_it_returned():
	check = summarize_carrier_response(CARRIER_REPLY)
	assert_check(check, "carrier")
	assert check["ok"] is True and check["status"] is None
	assert check["summary"] == "The carrier returned 1 rate: Aramex BH GCC PPX $38.20."
	assert check["detail"] == {
		"count": 1,
		"rates": [
			{
				"service_id": "Aramex BH GCC PPX",
				"name": "Aramex BH GCC PPX",
				"amount": 38.2,
				"currency": "USD",
			}
		],
	}


@pytest.mark.parametrize(
	("amount", "dollars"),
	[(3.675, "$3.68"), (2.675, "$2.68"), (1.005, "$1.01"), (0.125, "$0.13"), (38.2, "$38.20")],
)
def test_amounts_are_shown_in_cents_rounded_half_up_like_the_engine(amount, dollars):
	# Python's own formatting rounds the binary value (2.675 is really 2.67499...), which is not what the engine
	# does with money: it rounds the decimal text half up, so these have to agree with it.
	rate = {"service_id": "a", "service_name": "A", "cost": amount, "currency": "USD"}
	assert summarize_carrier_response({"success": True, "rates": [rate]})["summary"] == (
		f"The carrier returned 1 rate: A {dollars}."
	)
	other = summarize_carrier_response({"success": True, "rates": [{**rate, "currency": "BHD"}]})
	assert other["summary"] == f"The carrier returned 1 rate: A {dollars[1:]} BHD."
	site = summarize_website_response(200, 5, site_body(site_method(amount=amount)), None)
	assert f"International Express {dollars}." in site["summary"]
	policy = summarize_policy_response(200, 5, policy_body(offers=[{**OFFER, "amount": amount}]), amount)
	assert policy["summary"].endswith(f"1 offer, {dollars}.")


@pytest.mark.parametrize(
	("one", "other", "same"),
	[
		(2.675, 2.68, True),
		(1.005, 1.01, True),
		(0.125, 0.13, True),
		(3.675, 3.68, True),
		(1.005, 1.0, False),
		(1.004, 1.0, True),
		(100, 100.004, True),
		(99.996, 100, True),
		(100.01, 100, False),
		(99.98, 100, False),
	],
)
def test_two_amounts_are_the_same_when_they_are_the_same_cents_rounded_half_up(one, other, same):
	policy = summarize_policy_response(200, 5, policy_body(offers=[{**OFFER, "amount": one}]), other)
	assert policy["ok"] is same, (one, other)
	site = summarize_website_response(200, 5, site_body(site_method(amount=one)), other)
	assert site["ok"] is same, (one, other)


def test_the_carrier_check_with_several_rates_names_the_first_few():
	rates = [
		{"service_id": f"s{n}", "service_name": f"Service {n}", "cost": 4.2 * (n + 1), "currency": "USD"}
		for n in range(7)
	]
	check = summarize_carrier_response({"success": True, "rates": rates}, ms=420)
	assert check["ok"] is True and check["ms"] == 420
	assert check["summary"].startswith("The carrier returned 7 rates: Service 0 $4.20, Service 1 $8.40")
	assert check["summary"].endswith("and 2 more.")
	assert check["detail"]["count"] == 7 and len(check["detail"]["rates"]) <= 7
	other = summarize_carrier_response(
		{"success": True, "rates": [{"service_id": "a", "service_name": "A", "cost": 4.2, "currency": "BHD"}]}
	)
	assert other["summary"] == "The carrier returned 1 rate: A 4.20 BHD."


def test_zero_rates_are_reported_in_the_words_of_the_decision_and_nothing_more():
	check = summarize_carrier_response({"success": True, "rates": []}, ms=300)
	assert_check(check, "carrier")
	assert check["ok"] is False and check["ms"] == 300
	assert check["summary"] == NO_RATES_SENTENCE
	assert check["detail"] == {"count": 0, "rates": []}


def test_a_carrier_answer_that_is_not_a_list_of_rates_is_a_failure_in_plain_words():
	for reply in (
		None,
		"nope",
		5,
		[],
		{},
		{"success": False, "rates": []},
		{"success": True},
		{"rates": "x"},
		{"success": True, "rates": {"a": 1}},
	):
		check = summarize_carrier_response(reply)
		assert_check(check, "carrier")
		assert check["ok"] is False, reply
		assert "did not answer with a list of rates" in check["summary"], reply


def test_odd_rates_in_a_carrier_answer_are_skipped_not_fatal():
	reply = {
		"success": True,
		"rates": [
			None,
			5,
			"x",
			{},
			{"service_id": "a"},
			{"cost": "cheap"},
			{"service_id": "ok", "cost": 3, "currency": 4},
		],
	}
	check = summarize_carrier_response(reply)
	assert_check(check, "carrier")
	assert check["ok"] is True, "one rate could be read"
	assert "$3.00" in check["summary"]
	assert (
		summarize_carrier_response({"success": True, "rates": [None, 5, {}]})["summary"] == NO_RATES_SENTENCE
	)


def test_a_missing_shipping_module_is_said_in_one_plain_sentence():
	check = summarize_carrier_response(ImportError("No module named 'dsi_shipping'"))
	assert_check(check, "carrier")
	assert check["ok"] is False
	assert check["summary"] == "The shipping module is not installed on this site."


def test_a_blocked_account_is_named_but_the_carriers_own_words_never_are():
	for text in (
		"Account is blocked",
		"ACCOUNT BLOCKED by Aramex",
		"SOAP fault: the account is blocked, user=admin pw=hunter2",
	):
		check = summarize_carrier_response(RuntimeError(text))
		assert_check(check, "carrier")
		assert check["ok"] is False
		assert check["summary"] == "The carrier says its account is blocked."
		assert "hunter2" not in json.dumps(check) and "SOAP" not in json.dumps(check)


def test_any_other_carrier_error_is_reported_without_its_text():
	for error in (RuntimeError(f"HTTP 500 {SECRET}"), ValueError(SECRET), KeyError(SECRET), Exception()):
		check = summarize_carrier_response(error)
		assert_check(check, "carrier")
		assert check["ok"] is False
		assert SECRET not in json.dumps(check)
		assert check["summary"] == "The carrier could not be asked: the call stopped with an error."


def test_the_carrier_checks_detail_never_carries_more_than_the_fields_it_names():
	reply = {
		"success": True,
		"rates": [
			{
				"service_id": "a",
				"service_name": "A",
				"cost": 1.0,
				"currency": "USD",
				"api_key": SECRET,
				"provider": "Aramex",
				"account": {"token": SECRET},
			}
		],
		"debug": SECRET,
	}
	check = summarize_carrier_response(reply)
	assert SECRET not in json.dumps(check)
	assert set(check["detail"]["rates"][0]) == {"service_id", "name", "amount", "currency"}


# ---------------------------------------------------------------- the third check: the website
def test_the_website_check_lists_the_methods_and_says_the_first_amount_matches():
	check = summarize_website_response(200, 640, site_body(site_method()), 100.0)
	assert_check(check, "website")
	assert check["ok"] is True and check["status"] == 200 and check["ms"] == 640
	assert check["summary"] == (
		"The website answered in 640 ms with 1 delivery method: International Express $100.00. "
		"Its first amount matches the ERP's $100.00."
	)
	assert check["detail"]["methods"][0]["id"] == "rate-card"
	assert check["detail"]["packingPlan"] == "rate-card"


def test_the_website_check_fails_when_its_first_amount_is_not_the_erps():
	check = summarize_website_response(200, 640, site_body(site_method(amount=90)), 100.0)
	assert check["ok"] is False
	assert check["summary"].endswith("Its first amount, $90.00, is not the ERP's $100.00.")


def test_the_website_check_does_not_compare_when_the_erp_gave_no_amount():
	methods = [site_method("Aramex BH GCC PPX", "Aramex BH GCC PPX", 38.2), site_method("b", "Other", 44)]
	check = summarize_website_response(200, 640, site_body(*methods, plan="single"), None)
	assert_check(check, "website")
	assert check["ok"] is True
	assert check["summary"] == (
		"The website answered in 640 ms with 2 delivery methods: Aramex BH GCC PPX $38.20, Other $44.00."
	)
	assert "ERP" not in check["summary"]


def test_the_website_check_names_the_first_few_methods_only():
	methods = [site_method(f"m{n}", f"Method {n}", n + 1) for n in range(8)]
	check = summarize_website_response(200, 640, site_body(*methods), None)
	assert "8 delivery methods" in check["summary"] and check["summary"].count("$") == 5
	assert check["summary"].endswith("and 3 more.")


def test_a_method_with_no_name_is_shown_by_its_id_as_short_plain_text():
	# The id of a method with no serviceName is the label, so it is cleaned like every other text of an answer:
	# cut short, no control or bidi characters. (Its raw value is only ever compared, never shown.)
	hostile = "<img src=x onerror=alert(1)>" + "\u202e" + "\x00" + "X" * 5000
	method = {key: value for key, value in site_method(hostile, amount=100).items() if key != "serviceName"}
	check = summarize_website_response(200, 640, site_body(method), 100.0)
	assert len(check["summary"]) < 400, len(check["summary"])
	assert not [char for char in check["summary"] if unicodedata.category(char) in ("Cc", "Cf")]
	assert check["summary"].startswith(
		"The website answered in 640 ms with 1 delivery method: <img src=x onerror"
	)
	assert "..." in check["summary"]
	# An id with nothing printable in it has no label at all.
	method["id"] = "\u202e\u200b \x00"
	check = summarize_website_response(200, 640, site_body(method), None)
	assert check["summary"] == (
		"The website answered in 640 ms with 1 delivery method: a method with no name $100.00."
	)
	# The raw id still decides whether the website used the rate card.
	rate_card = {key: value for key, value in site_method("rate-card").items() if key != "serviceName"}
	check = summarize_website_response(200, 640, site_body(rate_card), 100.0)
	assert check["ok"] is True
	assert check["summary"].startswith(
		"The website answered in 640 ms with 1 delivery method: rate-card $100.00."
	)


def test_the_website_check_with_no_methods_fails_and_says_so():
	for body in (site_body(), {"success": True, "methods": []}, {"success": True}, {"methods": "x"}):
		check = summarize_website_response(200, 640, body, 100.0)
		assert_check(check, "website")
		assert check["ok"] is False, body
		assert "no delivery methods" in check["summary"]


def test_a_429_is_reported_as_the_sites_rate_limit_and_not_as_a_failure_of_the_integration():
	check = summarize_website_response(429, 120, {"error": "Too many requests"}, 100.0)
	assert_check(check, "website")
	assert check["ok"] is False and check["status"] == 429
	assert "HTTP 429" in check["summary"]
	assert "10 requests a minute" in check["summary"]
	assert "not a failure of the integration" in check["summary"]
	assert summarize_website_response(429, 120, None, None)["status"] == 429


def test_a_redirect_is_reported_and_never_followed():
	for status in (301, 302, 307, 308):
		check = summarize_website_response(status, 50, None, 100.0)
		assert check["ok"] is False and check["status"] == status
		assert f"HTTP {status}" in check["summary"] and "redirect" in check["summary"]
		assert "not followed" in check["summary"]


def test_a_refusal_from_the_website_gives_its_sentence_cut_short():
	check = summarize_website_response(400, 90, {"success": False, "error": "Cart is empty"}, 100.0)
	assert check["ok"] is False and check["status"] == 400
	assert check["summary"] == "The website answered HTTP 400: Cart is empty."
	check = summarize_website_response(500, 90, {"success": False, "error": "x" * 1000 + "."}, None)
	assert len(check["summary"]) < 300
	assert (
		summarize_website_response(500, 90, {"error": 5}, None)["summary"] == "The website answered HTTP 500."
	)
	assert summarize_website_response(502, 90, "<html>", None)["summary"] == "The website answered HTTP 502."
	assert summarize_website_response(400, 90, {"error": "Could not resolve country."}, None)[
		"summary"
	].endswith("Could not resolve country.")


def test_a_website_that_gave_no_answer_is_reported_by_why():
	expected = {
		"timeout": "The website did not answer within 15 seconds.",
		"unreachable": "The website could not be reached from the server.",
		"too_large": "more than 64 KB",
		"unreadable": "but not with JSON",
	}
	for problem, words in expected.items():
		status = None if problem in ("timeout", "unreachable") else 200
		check = summarize_website_response(status, 15000, None, 100.0, problem=problem)
		assert_check(check, "website")
		assert check["ok"] is False and check["status"] == status
		assert words in check["summary"], problem
		assert check["detail"] == {"problem": problem}


def test_a_website_that_was_not_asked_says_why_and_never_repeats_the_address():
	check = website_not_asked("The website address has to start with https://.")
	assert_check(check, "website")
	assert check["ok"] is False and check["status"] is None
	assert check["summary"] == "The website was not asked. The website address has to start with https://."
	assert check["detail"] == {"problem": "not_asked"}


def test_a_reason_without_a_full_stop_gets_one_and_a_long_one_is_cut():
	assert website_not_asked("The address is not usable")["summary"] == (
		"The website was not asked. The address is not usable."
	)
	long = website_not_asked("x" * 1000)["summary"]
	assert len(long) < 260 and long.endswith("..."), "cut short, and still a sentence that ends"
	assert website_not_asked(None)["summary"] == "The website was not asked."
	assert website_not_asked(5)["summary"] == "The website was not asked."


def test_numbers_that_cannot_be_read_are_left_out_not_fatal():
	reply = {
		"success": True,
		"rates": [{"service_id": "a", "service_name": "A", "cost": 10**400, "currency": "USD"}],
	}
	assert summarize_carrier_response(reply)["summary"] == NO_RATES_SENTENCE, "a price too big to be one"
	body = site_body(site_method(amount=10**400))
	check = summarize_website_response(200, 5, body, None)
	assert check["ok"] is False and "no delivery methods" in check["summary"]
	check = summarize_policy_response(200, 5, policy_body(offers=[{**OFFER, "amount": 10**400}]), None)
	assert check["ok"] is True, "no price on either side"
	assert "no price" in check["summary"]


def test_an_error_that_cannot_even_be_turned_into_text_is_still_reported():
	class Silent(Exception):
		def __str__(self):
			raise RuntimeError("no")

	check = summarize_carrier_response(Silent())
	assert check["ok"] is False
	assert check["summary"] == "The carrier could not be asked: the call stopped with an error."


def test_an_unexpected_failure_is_reported_without_saying_what_it_was():
	check = unexpected_failure("carrier", 87)
	assert_check(check, "carrier")
	assert check["ok"] is False and check["ms"] == 87
	assert (
		check["summary"]
		== "This check stopped because of an unexpected error, which was written to the Error Log."
	)
	assert check["detail"] == {"problem": "error"}
	assert_check(unexpected_failure("website", None), "website")


def test_the_website_checks_detail_is_the_answer_cut_and_cleaned():
	body = site_body(site_method())
	body["methods"][0]["token"] = SECRET
	body["debug"] = {"Authorization": SECRET}
	body["package"]["boxes"] = [{"n": n, "note": "x" * 300} for n in range(200)]
	check = summarize_website_response(200, 640, body, 100.0)
	assert SECRET not in json.dumps(check)
	assert len(json.dumps(check["detail"], separators=(",", ":"))) <= 4096
	assert check["detail"]["methods"][0]["id"] == "rate-card"
	assert check["detail"]["packingPlan"] == "rate-card"


# ---------------------------------------------------------------- the verdict
AGREE = (
	"The ERP price API and the website agree: customers are charged $100.00 from the rate card, "
	"and the carrier is not asked."
)
STILL_THE_CARRIER = (
	"The website still asks the carrier: it returned the carrier's methods, not the rate card's."
)
NO_RATES_ON_THE_CARRIER = (
	"The carrier returns no rates, so while the price source is Carrier customers see no delivery method."
)
# Ruling P100 (final review I1): the website prices from the ERP's answer only when the policy is on with the rate
# card as the price source; with the carrier it charges the carrier's own prices, whatever the policy says.
CARRIER_PRICES = (
	"The price source is the carrier, so customers pay the carrier's own prices: for now the website applies the "
	"policy only with the rate card as the price source."
)


def test_the_verdict_when_the_erp_and_the_website_agree():
	assert verdict_for([erp_check(), carrier_check(), website_check()]) == AGREE
	# The carrier does not matter to a rate-card policy: it may be blocked.
	blocked = summarize_carrier_response({"success": True, "rates": []})
	assert verdict_for([erp_check(), blocked, website_check()]) == AGREE


def test_the_verdict_when_the_website_still_asks_the_carrier():
	carrier_methods = site_body(site_method("Aramex BH GCC PPX", "Aramex BH GCC PPX", 38.2), plan="single")
	site = summarize_website_response(200, 640, carrier_methods, 100.0)
	assert site["ok"] is False, "its first amount is not the ERP's"
	assert verdict_for([erp_check(), carrier_check(), site]) == STILL_THE_CARRIER
	# Even for an order the rate card would not deliver.
	refused = erp_check(offered=False, reason="not_covered", offers=[])
	site = summarize_website_response(200, 640, carrier_methods, None)
	assert verdict_for([refused, carrier_check(), site]) == STILL_THE_CARRIER


CARRIER_PLAN = "volume_desc|min_delta_chargeable|lowest_chargeable"
STILL_THE_CARRIER_AND_NOTHING = (
	"The website still asks the carrier, and the carrier gave it no delivery method, so customers see none."
)


def test_the_verdict_when_the_website_still_asks_the_carrier_and_the_carrier_gives_it_nothing():
	# What the deployed website answers while the Aramex account is blocked: HTTP 200, no methods, and the
	# packing plan of the carrier path (the rate card's path says "rate-card").
	body = {**site_body(plan=CARRIER_PLAN), "package": {"total_units": 2, "total_boxes": 2, "boxes": []}}
	site = summarize_website_response(200, 8503, body, 100.0)
	assert site["ok"] is False and "no delivery methods" in site["summary"]
	assert verdict_for([erp_check(), carrier_check(), site]) == STILL_THE_CARRIER_AND_NOTHING
	# Whatever the rate card would offer: the website is not using it.
	refused = erp_check(offered=False, reason="not_covered", offers=[])
	assert verdict_for([refused, carrier_check(), summarize_website_response(200, 5, body, None)]) == (
		STILL_THE_CARRIER_AND_NOTHING
	)


@pytest.mark.parametrize(
	("status", "body"),
	[
		(200, site_body(plan="rate-card")),  # the rate card's own path, and it found nothing to offer
		(200, site_body(plan="")),
		(200, site_body(plan=None)),
		(200, site_body(plan=5)),
		(200, {"success": True, "methods": []}),  # no plan at all
		(400, {"success": False, "error": "Cart is empty", "packingPlan": CARRIER_PLAN}),
		(500, {"packingPlan": CARRIER_PLAN, "methods": []}),
	],
)
def test_an_empty_answer_is_not_the_carriers_unless_it_says_so_and_was_a_success(status, body):
	site = summarize_website_response(status, 5, body, 100.0)
	verdict = verdict_for([erp_check(), carrier_check(), site])
	assert verdict != STILL_THE_CARRIER_AND_NOTHING
	assert verdict.startswith(
		"The ERP price API charges $100.00 from the rate card, but the website did not give"
	)


def test_the_verdict_when_the_website_charges_the_rate_card_but_another_amount():
	site = summarize_website_response(200, 640, site_body(site_method(amount=90)), 100.0)
	assert verdict_for([erp_check(), carrier_check(), site]) == (
		"The website charges $90.00 but the ERP price API charges $100.00 from the rate card: they disagree."
	)


def test_the_verdict_when_the_website_is_limiting_requests():
	site = summarize_website_response(429, 120, None, 100.0)
	assert verdict_for([erp_check(), carrier_check(), site]) == (
		"The ERP price API charges $100.00 from the rate card, but the website limits this check to "
		"10 requests a minute, so run it again in a minute."
	)


def test_the_verdict_when_the_website_gave_no_usable_answer():
	for site in (
		summarize_website_response(None, 15000, None, 100.0, problem="timeout"),
		summarize_website_response(None, 5, None, 100.0, problem="unreachable"),
		summarize_website_response(200, 5, None, 100.0, problem="unreadable"),
		summarize_website_response(200, 5, None, 100.0, problem="too_large"),
		summarize_website_response(500, 5, {"error": "Failed to fetch shipping rates"}, 100.0),
		summarize_website_response(302, 5, None, 100.0),
		unexpected_failure("website", 5),
		summarize_website_response(200, 5, {"success": True, "methods": []}, 100.0),
	):
		assert verdict_for([erp_check(), carrier_check(), site]) == (
			"The ERP price API charges $100.00 from the rate card, but the website did not give a usable "
			"answer (see the third check)."
		), site["summary"]


def test_the_verdict_when_the_website_was_not_asked():
	site = website_not_asked("No website address is set in Storefront Settings.")
	assert verdict_for([erp_check(), carrier_check(), site]) == (
		"The ERP price API charges $100.00 from the rate card; the website was not asked because its "
		"address is not usable (see the third check)."
	)


def test_the_verdict_when_the_rate_card_offers_no_delivery_for_the_sample_order():
	refused = erp_check(offered=False, reason="too_heavy", offers=[])
	site = summarize_website_response(
		400, 90, {"success": False, "error": "This order is heavier than our delivery rates cover."}, None
	)
	assert verdict_for([refused, carrier_check(), site]) == (
		"The rate card offers no delivery for the sample order (too heavy), so customers see no delivery method for it."
	)
	not_covered = erp_check(offered=False, reason="not_covered", offers=[])
	assert "(not covered)" in verdict_for([not_covered, carrier_check(), site])
	no_reason = erp_check(offered=False, reason=None, offers=[])
	assert verdict_for([no_reason, carrier_check(), site]).startswith(
		"The rate card offers no delivery for the sample order, so"
	)


def test_the_verdict_when_the_erp_price_api_is_the_problem():
	unusable = "The ERP price API did not give a usable answer, so the website cannot get delivery prices from the policy (see the first check)."
	for erp in (
		summarize_policy_response(None, 15000, None, None, problem="timeout"),
		summarize_policy_response(500, 5, None, None),
		summarize_policy_response(200, 5, {"message": "x"}, None),
		unexpected_failure("erp_price_api", 5),
	):
		assert verdict_for([erp, carrier_check(), website_check()]) == unusable, erp["summary"]
	disagree = summarize_policy_response(200, 5, policy_body(), 90.0)
	assert verdict_for([disagree, carrier_check(), website_check()]) == (
		"The public ERP endpoint and the policy disagree about the price of the sample order, so fix that "
		"before trusting the website (see the first check)."
	)


def test_the_verdict_when_the_policy_is_off():
	off = erp_check(policy_active=False, price_source="carrier", offered=False, offers=[], reason=None)
	assert verdict_for([off, carrier_check(), website_check(None, erp_amount=None)]) == (
		"The shipping policy is switched off, so customers pay the carrier's own prices."
	)
	none = summarize_carrier_response({"success": True, "rates": []})
	assert verdict_for([off, none, website_check(None, erp_amount=None)]) == (
		"The shipping policy is switched off and the carrier returns no rates, so customers see no delivery method."
	)


def test_the_verdict_when_the_price_source_is_the_carrier():
	carrier_mode = erp_check(price_source="carrier", reason="fallback_offer")
	verdict = verdict_for([carrier_mode, carrier_check(), website_check(None, erp_amount=None)])
	assert verdict == CARRIER_PRICES
	assert "rules" not in verdict, "it never says customers pay through the policy's rules"
	none = summarize_carrier_response({"success": True, "rates": []})
	assert verdict_for([carrier_mode, none, website_check(None, erp_amount=None)]) == NO_RATES_ON_THE_CARRIER
	blocked = summarize_carrier_response(RuntimeError("Account is blocked"))
	assert (
		verdict_for([carrier_mode, blocked, website_check(None, erp_amount=None)]) == NO_RATES_ON_THE_CARRIER
	)


def website_troubles():
	"""Website checks that did not work out, and none of them a 429: nothing came back, nonsense came back, an
	error status, a redirect, no method at all, and a check that stopped on an error."""
	return {
		"no answer": summarize_website_response(None, 15000, None, None, problem="timeout"),
		"unreachable": summarize_website_response(None, 5, None, None, problem="unreachable"),
		"unreadable": summarize_website_response(200, 5, None, None, problem="unreadable"),
		"too large": summarize_website_response(200, 5, None, None, problem="too_large"),
		"an error status": summarize_website_response(
			500, 5, {"error": "Failed to fetch shipping rates"}, None
		),
		"a refusal": summarize_website_response(400, 5, {"success": False, "error": "Cart is empty"}, None),
		"a redirect": summarize_website_response(302, 5, None, None),
		"no methods": summarize_website_response(200, 5, {"success": True, "methods": []}, None),
		"an unexpected failure": unexpected_failure("website", 5),
	}


def carrier_mode_checks():
	"""The ERP's check for a policy that is off, and for one that is on with the carrier as its price source."""
	off = erp_check(policy_active=False, price_source="carrier", offered=False, offers=[], reason=None)
	carrier_source = erp_check(price_source="carrier", reason="fallback_offer")
	return {
		"off": (off, "The shipping policy is switched off, so customers pay the carrier's own prices."),
		"carrier": (carrier_source, CARRIER_PRICES),
	}


@pytest.mark.parametrize("mode", ["off", "carrier"])
def test_the_verdict_names_a_website_that_did_not_work_out_when_the_carrier_prices_the_order(mode):
	erp, said = carrier_mode_checks()[mode]
	for what, site in website_troubles().items():
		assert verdict_for([erp, carrier_check(), site]) == (
			f"{said[:-1]}, but the website did not give a usable answer (see the third check)."
		), what
	not_asked = website_not_asked("The website address has to start with https://.")
	assert verdict_for([erp, carrier_check(), not_asked]) == (
		f"{said[:-1]}; the website was not asked because its address is not usable (see the third check)."
	)


@pytest.mark.parametrize("mode", ["off", "carrier"])
def test_the_verdict_for_a_carrier_priced_order_is_what_it_was_when_the_website_is_no_trouble(mode):
	erp, said = carrier_mode_checks()[mode]
	assert verdict_for([erp, carrier_check(), website_check(None, erp_amount=None)]) == said
	# A 429 is the site limiting how often the check may be made: it says nothing about the website.
	limited = summarize_website_response(429, 120, {"error": "Too many requests"}, None)
	assert verdict_for([erp, carrier_check(), limited]) == said
	# Without rates from the carrier it is the carrier that is the trouble, whatever the website did.
	none = summarize_carrier_response({"success": True, "rates": []})
	expected = (
		"The shipping policy is switched off and the carrier returns no rates, so customers see no delivery "
		"method."
		if mode == "off"
		else NO_RATES_ON_THE_CARRIER
	)
	for site in (*website_troubles().values(), limited, website_check(None, erp_amount=None)):
		assert verdict_for([erp, none, site]) == expected


def test_the_verdict_does_not_depend_on_the_order_of_the_checks_and_survives_missing_ones():
	checks = [erp_check(), carrier_check(), website_check()]
	assert verdict_for(list(reversed(checks))) == AGREE
	for partial in ([], checks[:2], checks[1:], [checks[0]]):
		verdict = verdict_for(partial)
		assert isinstance(verdict, str) and verdict.endswith("."), partial
		assert "did not all run" in verdict


def test_the_verdict_never_raises_and_is_always_one_plain_sentence_for_hostile_checks():
	junk = [
		None,
		5,
		"x",
		{},
		{"name": "website"},
		{"name": "website", "ok": True, "detail": 7},
		{
			"name": "erp_price_api",
			"ok": True,
			"status": 200,
			"detail": {"message": {"offers": [{"amount": "x"}]}},
		},
		{"name": "website", "ok": True, "status": 200, "detail": {"methods": [5, None, {"id": 3}]}},
		{"name": "carrier", "ok": "yes", "detail": None},
	]
	for one in junk:
		for checks in (
			[one],
			[one, one, one],
			[erp_check(), one, one],
			[erp_check(), carrier_check(), one],
			[one, carrier_check(), website_check()],
		):
			verdict = verdict_for(checks)
			assert isinstance(verdict, str) and verdict.endswith(".")
	assert isinstance(verdict_for(None), str)
	assert isinstance(verdict_for("abc"), str)
	assert isinstance(verdict_for({"erp_price_api": 1}), str)


# ---------------------------------------------------------------- hygiene over everything
def test_no_secret_in_any_input_reaches_any_output():
	hostile_policy = policy_body()
	hostile_policy["message"]["offers"][0].update(api_key=SECRET, name=f"x {SECRET[:3]}")
	hostile_policy["Authorization"] = f"Bearer {SECRET}"
	hostile_site = site_body(site_method())
	hostile_site["cookie"] = SECRET
	hostile_site["methods"][0]["secret"] = SECRET
	outputs = [
		summarize_policy_response(200, 1, hostile_policy, 100.0),
		summarize_policy_response(500, 1, {"exc_type": "Error", "password": SECRET}, 1.0),
		summarize_carrier_response(
			{"success": True, "rates": [{"service_id": "a", "cost": 1, "token": SECRET}]}
		),
		summarize_carrier_response(RuntimeError(SECRET)),
		summarize_carrier_response(ImportError(SECRET)),
		summarize_website_response(200, 1, hostile_site, 100.0),
		summarize_website_response(400, 1, {"error": "bad", "token": SECRET}, None),
		website_not_asked("The website address has to start with https://."),
		unexpected_failure("carrier", 1),
	]
	for check in outputs:
		assert SECRET not in json.dumps(check)
	checks = [outputs[0], outputs[2], outputs[5]]
	assert SECRET not in verdict_for(checks)


def test_every_summary_and_verdict_is_plain_text_without_markup_or_a_traceback():
	checks = [
		erp_check(),
		erp_check(offered=False, offers=[], reason="not_covered"),
		carrier_check(),
		summarize_carrier_response({"success": True, "rates": []}),
		summarize_carrier_response(RuntimeError("boom")),
		website_check(),
		summarize_website_response(429, 1, None, None),
		summarize_website_response(None, 1, None, None, problem="timeout"),
		website_not_asked("The website address is too long."),
	]
	for check in checks:
		for word in ("Traceback", "<", ">", "{", "}", "None", "nan"):
			assert word not in check["summary"], (word, check["summary"])
	verdict = verdict_for([erp_check(), carrier_check(), website_check()])
	for word in ("Traceback", "<", ">", "{", "}", "None"):
		assert word not in verdict
