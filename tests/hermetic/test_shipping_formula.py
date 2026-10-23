"""Pins the shipping-rule formula language: the documented examples, the
whitelist (everything else is refused in plain words), the size limits, and that
nothing a user types can reach eval/exec."""

import ast
import itertools
import math
import random
import warnings
from pathlib import Path

import pytest

from dsi_catalogue import shipping_formula
from dsi_catalogue.shipping_engine import to_cents
from dsi_catalogue.shipping_formula import (
	FUNCTIONS,
	SAMPLE,
	VARIABLES,
	FormulaError,
	check_formula,
	compile_formula,
	evaluate,
)

EXAMPLES = [
	("carrier_price * 1.1", {}, 42.02),
	("max(carrier_price * 1.1, 45)", {}, 45),
	("carrier_price - 10 if order_value >= 500 else carrier_price", {}, 38.2),
	("carrier_price - 10 if order_value >= 500 else carrier_price", {"order_value": 600}, 28.2),
	("weight_kg * 6 + 8", {}, 22.4),
]

# Every one of these must be refused when the formula is saved.
UNSAFE_SOURCES = [
	"__import__('os').system('x')",
	"carrier_price.real",
	"carrier_price[0]",
	"'text'",
	"[1, 2]",
	"{1: 2}",
	"lambda: 1",
	"carrier_price ** 2",
	"unknown_name",
	"open('x')",
	"max(carrier_price, key=abs)",
	"(x for x in [1])",
	"carrier_price if",
	"",
	"   ",
	"a" * 301,
	"True",
	"None",
	"carrier_price; 1",
]

# More ways to write something the language does not have.
ALSO_REFUSED = [
	"1 if boxes in item_count else 0",
	"1 if boxes not in item_count else 0",
	"1 if boxes is item_count else 0",
	"1 if boxes is not item_count else 0",
	"~boxes",
	"boxes & 1",
	"boxes | 1",
	"boxes ^ 1",
	"boxes << 1",
	"boxes >> 1",
	"boxes @ 1",
	"1.5j + 1",
	"...",
	"b'1'",
	"f'{boxes}'",
	"(x := 1)",
	"await boxes",
	"max(*boxes)",
	"max(boxes, *item_count)",
	"max(**boxes)",
	"max",
	"ceil + 1",
	"carrier_price(1)",
	"max(1)(2)",
	"carrier_price " + chr(0xD7) + " 2",  # U+00D7, the multiplication sign, not an asterisk
	"carrier_price\x00",
	"1e999",
	"0x" + "f" * 298,
]

# Numbers no float can hold: an infinity, an integer that overflows a float, and a product that does.
TOO_LARGE = ["1e999", "0x" + "f" * 298, "carrier_price * 1e308 * 10"]


def _short(source):
	return repr(source) if len(source) < 40 else f"{len(source)}-characters"


def _refusal(source):
	"""The message a user would see when `source` is refused at save time."""
	with pytest.raises(FormulaError) as caught:
		check_formula(source)
	return str(caught.value)


def test_the_interface_constants():
	assert issubclass(FormulaError, ValueError)
	assert VARIABLES == ("carrier_price", "order_value", "weight_kg", "volume_l", "boxes", "item_count")
	assert FUNCTIONS == ("min", "max", "round", "ceil", "floor", "abs")
	assert SAMPLE == {
		"carrier_price": 38.2,
		"order_value": 310.0,
		"weight_kg": 2.4,
		"volume_l": 6.0,
		"boxes": 1,
		"item_count": 2,
	}


def test_the_four_documented_examples_evaluate():
	for source, changes, expected in EXAMPLES:
		assert evaluate(source, {**SAMPLE, **changes}) == pytest.approx(expected), source
		# ...and saving them is accepted: the checked tree comes back, the sample check is silent
		assert isinstance(compile_formula(source), ast.Expression)
		assert check_formula(source) is None


def test_functions_and_boolean_logic():
	assert evaluate("min(ceil(carrier_price), floor(order_value / 10))", SAMPLE) == 31
	assert evaluate("round(carrier_price, 1)", SAMPLE) == 38.2
	assert evaluate("1 if carrier_price > 10 and not order_value < 5 else 0", SAMPLE) == 1
	assert evaluate("boxes * 5 + item_count", SAMPLE) == 7


@pytest.mark.parametrize(
	("source", "expected"),
	[
		("7 % 4", 3),
		("-7 % 4", 1),
		("7.5 // 2", 3),
		("-7 // 2", -4),
		("10 / 4", 2.5),
		("-carrier_price", -38.2),
		("+boxes", 1),
		("--boxes", 1),
		("(1 + 2) * 3 - 4 / 2", 7),
		("max(1, 5, 3)", 5),
		("min(4, 2, 8)", 2),
		("abs(-carrier_price)", 38.2),
		("floor(-1.5)", -2),
		("ceil(-1.5)", -1),
		("round(carrier_price)", 38),
		("round(1234, -2)", 1200),
		("round(38.26, boxes)", 38.3),
		# halves go away from zero, from the number as it is written: 2.5 is 3, 2.675 is 2.68
		("round(0.5)", 1),
		("round(1.5)", 2),
		("round(2.5)", 3),
		("round(3.5)", 4),
		("round(-0.5)", -1),
		("round(-2.5)", -3),
		("round(1.005, 2)", 1.01),
		("round(2.675, 2)", 2.68),
		("round(0.285, 2)", 0.29),
		("round(1.45, 1)", 1.5),
		("round(-2.675, 2)", -2.68),
		("round(1225, -1)", 1230),
		("round(-1225, -1)", -1230),
		# a number of places beyond what a float can hold changes nothing, or leaves nothing
		("round(1.5, 400)", 1.5),
		("round(1.5, -400)", 0),
		("round(1.5, 1000000000000000000000000000000)", 1.5),
		("round(1.5, -1000000000000000000000000000000)", 0),
	],
)
def test_arithmetic_and_function_results(source, expected):
	assert evaluate(source, SAMPLE) == pytest.approx(expected)


def test_results_are_floats_and_never_negative_zero():
	for source in (
		"boxes * 5 + item_count",
		"ceil(carrier_price)",
		"floor(order_value)",
		"round(carrier_price)",
		"abs(boxes)",
		"-boxes",
	):
		assert type(evaluate(source, SAMPLE)) is float, source
	# max(-0.0, 0) is -0.0 in Python, which would print as "-0.00" once clamped
	for source in ("0 * -1", "round(-0.4, 0)"):
		assert math.copysign(1.0, evaluate(source, SAMPLE)) == 1.0, source


def test_round_to_two_places_is_the_cents_rounding_of_the_engine():
	"""Money is rounded half up from the number as it is written, in a formula and in the engine alike (every
	value here from 0.001 to 3 in thousandths, so every third value ends in a half cent)."""
	for thousandths in range(1, 3001):
		amount = thousandths / 1000
		assert evaluate(f"round({amount!r}, 2)", SAMPLE) == to_cents(amount), amount
	for amount in (1.005, 2.675, 10.005, 107.615, 0.125, 1000000.005, 123456.785, 4.35, 8.345):
		assert evaluate(f"round({amount!r}, 2)", SAMPLE) == to_cents(amount), amount


ROUNDED_VALUES = [
	0.0,
	-0.0,
	0.5,
	-1.5,
	2.675,
	123456.789,
	1e22,
	1e-7,
	5e-324,
	-5e-324,
	1e300,
	1.7976931348623157e308,
	-1.7976931348623157e308,
]
ROUNDED_PLACES = [
	-1e30,
	-1000,
	-401,
	-400,
	-309,
	-308,
	-2,
	-1,
	0,
	1,
	2,
	16,
	323,
	324,
	325,
	400,
	401,
	1000,
	1e30,
]


def test_round_answers_for_any_finite_number_and_any_whole_number_of_places():
	"""However far the places go, round() answers at once with a finite number: the number itself when it has
	no more decimals than asked, nothing when the places are beyond the biggest float, and "too large" only
	when rounding up carries past the biggest float."""
	for value in ROUNDED_VALUES:
		for places in ROUNDED_PLACES:
			source = f"round({value!r}, {float(places)!r})"
			try:
				result = evaluate(source, SAMPLE)
			except FormulaError as error:
				assert "too large" in str(error), (source, str(error))
				continue
			assert type(result) is float and math.isfinite(result), (source, result)
			assert result != 0 or math.copysign(1.0, result) == 1.0, (source, "never -0.0")
			if places >= 325:  # a float never has so many decimals
				assert result == value + 0.0, (source, result)
			if places <= -309:  # the biggest float is under half of 1e309
				assert result == 0.0, (source, result)


@pytest.mark.parametrize(
	("condition", "holds"),
	[
		("boxes < 2", True),
		("boxes < 1", False),
		("boxes <= 1", True),
		("boxes <= 0", False),
		("boxes > 0", True),
		("boxes > 1", False),
		("boxes >= 1", True),
		("boxes >= 2", False),
		("boxes == 1", True),
		("boxes == 2", False),
		("boxes != 2", True),
		("boxes != 1", False),
		("0 < boxes < 2", True),
		("0 < boxes < 1", False),
		("1 < boxes < 2", False),
	],
)
def test_comparisons_choose_between_two_numbers(condition, holds):
	assert evaluate(f"10 if {condition} else 20", SAMPLE) == (10 if holds else 20)


def test_conditions_only_run_the_branch_they_pick():
	empty = {**SAMPLE, "boxes": 0}
	assert evaluate("carrier_price / boxes if boxes else 7", empty) == 7
	assert evaluate("1 if boxes > 0 and carrier_price / boxes > 5 else 2", empty) == 2
	assert evaluate("1 if boxes == 0 or carrier_price / boxes > 5 else 2", empty) == 1


@pytest.mark.parametrize("source", UNSAFE_SOURCES, ids=_short)
def test_every_unsafe_or_unsupported_source_is_refused(source):
	with pytest.raises(FormulaError):
		check_formula(source)


@pytest.mark.parametrize("source", ALSO_REFUSED, ids=_short)
def test_other_unsupported_sources_are_refused(source):
	with pytest.raises(FormulaError):
		check_formula(source)


def test_a_formula_that_is_not_a_number_is_refused_when_evaluated():
	# valid to write (it can sit inside `a if condition else b`), but it is not a price
	assert isinstance(compile_formula("carrier_price > 5"), ast.Expression)
	with pytest.raises(FormulaError):
		evaluate("carrier_price > 5", SAMPLE)


@pytest.mark.parametrize(
	"source",
	[
		"carrier_price > 5",
		"not carrier_price",
		"1 < boxes < 3",
		"carrier_price > 5 and boxes",
		"boxes or 0",
		"(boxes > 0) if carrier_price > 5 else 1",
	],
)
def test_a_formula_as_a_whole_that_gives_a_yes_no_answer_is_refused_when_evaluated(source):
	"""A yes/no answer is fine to write (it can sit inside `a if condition else b`), but the formula as a whole
	has to give a number, which only `evaluate` can tell for the order it is given."""
	compile_formula(source)
	with pytest.raises(FormulaError, match="yes/no"):
		evaluate(source, SAMPLE)


# A yes/no answer used where a number is needed: an operand of + - * / % //, of unary + or -, of a comparison, or
# an argument of a function. The branches the SAMPLE order does not take are checked just the same.
YES_NO_AS_A_NUMBER = [
	"(carrier_price > 5) + 1",
	"-(boxes > 0)",
	"+(boxes > 0)",
	"max(boxes > 0, 1)",
	"(boxes > 0) < 2",
	"1 < (boxes > 0)",
	"1 if (boxes > 0) == (carrier_price > 5) else 2",
	"1 + (not boxes)",
	"2 * (boxes and item_count)",
	"(boxes or 0) / 2",
	"round(carrier_price > 5)",
	"round(carrier_price, boxes > 0)",
	"abs(not boxes)",
	"ceil(boxes > 0) + 1",
	"(weight_kg > 5) * 10 if order_value > 1000 else carrier_price",
	"-(boxes > 0) if boxes > 5 else 1",
	"max(boxes > 0, 1) if boxes > 5 else 2",
	"1 if boxes < 5 else (boxes > 0) + 1",
	"1 if boxes < 5 else 2 * (boxes or item_count)",
	"1 if boxes > 5 else (1 if boxes > 3 else (boxes > 0) * 2)",
	# an a-if-condition-else-b whose branches differ is a number on one of them only, so it is no number either
	"-(1 if boxes > 5 else boxes > 0)",
	"1 + (boxes if carrier_price > 5 else boxes > 0)",
	"max(1, (boxes > 0) if carrier_price > 5 else 2)",
	"(1 if carrier_price > 5 else boxes > 0) < 2",
]


@pytest.mark.parametrize("source", YES_NO_AS_A_NUMBER, ids=_short)
def test_a_yes_no_answer_used_as_a_number_is_refused_when_the_formula_is_saved(source):
	"""Even on a branch the SAMPLE order does not take, where `evaluate` would never notice it."""
	for refuse in (compile_formula, check_formula, lambda text: evaluate(text, SAMPLE)):
		with pytest.raises(FormulaError, match="yes/no"):
			refuse(source)


# A yes/no answer where a number is not needed: the whole formula (`evaluate` refuses that), the condition of an
# a-if-condition-else-b, an operand of and, or and not, and either branch of an a-if-condition-else-b.
YES_NO_NOT_AS_A_NUMBER = [
	"1 if boxes > 0 else 2",
	"1 if not boxes else 2",
	"1 if boxes and carrier_price else 2",
	"1 if (boxes > 0) and (carrier_price > 5) else 2",
	"1 if not (boxes > 0 or carrier_price < 1) else 2",
	"1 if boxes > 0 or not boxes else 2",
	"1 if (1 if boxes else boxes > 0) else 2",  # an either as a condition
	"1 if (1 if boxes else boxes > 0) and boxes else 2",  # an either as an operand of `and`
	"1 if not (1 if boxes else boxes > 0) else 2",  # an either as an operand of `not`
	"(boxes > 0) if carrier_price > 5 else 1",
	"1 if carrier_price > 5 else (boxes > 0)",
	"(boxes > 0) if carrier_price > 5 else (not boxes)",
]


@pytest.mark.parametrize("source", YES_NO_NOT_AS_A_NUMBER, ids=_short)
def test_a_yes_no_answer_that_is_not_used_as_a_number_is_accepted(source):
	assert isinstance(compile_formula(source), ast.Expression)


def _random_formula(rng, depth):
	"""A random well-formed formula in which numbers and yes/no answers are mixed freely."""
	if depth == 0 or rng.random() < 0.12:
		return rng.choice(["boxes", "carrier_price", "item_count", "2", "0", "1.5"])

	def part():
		return _random_formula(rng, depth - 1)

	shapes = [
		lambda: f"({part()} {rng.choice('+-*')} {part()})",
		lambda: f"(-{part()})",
		lambda: f"({part()} {rng.choice(['<', '>=', '=='])} {part()})",
		lambda: f"({part()} {rng.choice(['and', 'or'])} {part()})",
		lambda: f"(not {part()})",
		lambda: f"({part()} if {part()} else {part()})",
		lambda: f"({part()} if {part()} else {part()})",
		lambda: f"{rng.choice(['max', 'min'])}({part()}, {part()})",
		lambda: f"abs({part()})",
	]
	return rng.choice(shapes)()


def test_a_formula_saved_as_a_number_never_meets_a_yes_no_answer_as_a_number():
	"""The save check against the run-time rule, on random formulas: when `(formula) * 1` is accepted (so the
	formula gives a number on every branch), no values of the names make `evaluate` raise the yes/no error."""
	rng = random.Random(20261023)
	grid = itertools.product((0, 1, 5), (0.0, 3.0, 40.0), (0, 2))
	values = [{**SAMPLE, "boxes": b, "carrier_price": c, "item_count": i} for b, c, i in grid]
	accepted = 0
	for _ in range(1500):
		source = _random_formula(rng, rng.randint(1, 5))
		try:
			compile_formula(f"({source}) * 1")
		except FormulaError:
			continue  # a yes/no used as a number somewhere, or too big a formula
		accepted += 1
		for given in values:
			assert isinstance(evaluate(source, given), float), source  # raises the yes/no error if it is hit
	assert accepted > 100, "the fuzz must reach formulas that are accepted"


def test_node_limit():
	too_many_characters = "carrier_price + " * 100 + "1"
	with pytest.raises(FormulaError, match="too long or complex"):
		compile_formula(too_many_characters)
	# 201 characters, so it is the cap on nodes that refuses this one, not the length limit
	too_many_nodes = "1+" * 100 + "1"
	assert len(too_many_nodes) <= 300
	with pytest.raises(FormulaError, match="too long or complex"):
		compile_formula(too_many_nodes)


def test_the_limits_are_exact():
	# 300 characters are fine, 301 are not
	at_the_limit = "1" + " " * 297 + "+1"
	assert len(at_the_limit) == 300
	assert evaluate(at_the_limit, SAMPLE) == 2
	with pytest.raises(FormulaError, match="too long or complex"):
		compile_formula("1" + " " * 298 + "+1")
	# 80 expression nodes are fine, 81 are not (a negated 1, then 39 more additions of 1)
	assert evaluate("-1" + "+1" * 39, SAMPLE) == 38
	with pytest.raises(FormulaError, match="too long or complex"):
		compile_formula("1" + "+1" * 40)


@pytest.mark.parametrize("source", ["-" * 299 + "1", "1" + "+(1" * 70 + ")" * 70], ids=_short)
def test_deeply_nested_formulas_are_refused_cleanly(source):
	with pytest.raises(FormulaError, match="too long or complex"):
		compile_formula(source)


def test_surrounding_whitespace_is_ignored():
	# a text box adds a newline, and Python itself refuses a formula that starts with a space
	assert evaluate("\n  carrier_price * 2  \n", SAMPLE) == pytest.approx(76.4)


def test_runtime_problems_are_formula_errors():
	with pytest.raises(FormulaError, match="divide"):
		evaluate("carrier_price / 0", SAMPLE)
	with pytest.raises(FormulaError):
		evaluate("carrier_price * 1e308 * 10", SAMPLE)
	with pytest.raises(FormulaError):
		evaluate("carrier_price + weight_kg", {"carrier_price": 38.2})


@pytest.mark.parametrize("source", ["carrier_price / 0", "carrier_price % 0", "carrier_price // 0"])
def test_dividing_by_zero_is_named_for_every_kind_of_division(source):
	with pytest.raises(FormulaError, match="divide"):
		evaluate(source, SAMPLE)


@pytest.mark.parametrize("source", TOO_LARGE, ids=_short)
def test_numbers_too_large_to_use_are_refused(source):
	with pytest.raises(FormulaError, match="too large"):
		evaluate(source, SAMPLE)


def test_missing_or_unusable_values_name_the_variable():
	# only the names the formula uses need a value
	assert evaluate("boxes + 1", {"boxes": 1}) == 2
	with pytest.raises(FormulaError, match="weight_kg"):
		evaluate("carrier_price + weight_kg", {"carrier_price": 38.2})
	for unusable in (True, "2.4", None, float("nan"), float("inf")):
		with pytest.raises(FormulaError, match="weight_kg"):
			evaluate("weight_kg", {**SAMPLE, "weight_kg": unusable})


@pytest.mark.parametrize("source", [None, 42, 4.5, b"carrier_price", ["carrier_price"]])
def test_a_formula_must_be_text(source):
	with pytest.raises(FormulaError, match="must be written as text"):
		compile_formula(source)
	with pytest.raises(FormulaError, match="must be written as text"):
		evaluate(source, SAMPLE)


def test_unknown_name_message_lists_what_is_allowed():
	assert _refusal("cost") == (
		"Unknown name 'cost'. You can use: carrier_price, order_value, weight_kg, volume_l, boxes, item_count."
	)


def test_the_other_ways_to_misuse_a_name_are_named_too():
	unknown_function = _refusal("open('x')")
	assert "Unknown function 'open'" in unknown_function
	assert "You can use: min, max, round, ceil, floor, abs." in unknown_function
	assert "Unknown function 'carrier_price'" in _refusal("carrier_price(1)")
	assert "write max(...)" in _refusal("max + 1")
	assert "name=value" in _refusal("max(carrier_price, key=abs)")


@pytest.mark.parametrize(
	("source", "fragment"),
	[
		("max(carrier_price)", "max() needs at least 2 values"),
		("min(carrier_price)", "min() needs at least 2 values"),
		("min()", "min() needs at least 2 values"),
		("round()", "round() needs 1 or 2 values"),
		("round(1, 2, 3)", "round() needs 1 or 2 values"),
		("ceil(1, 2)", "ceil() needs 1 value"),
		("floor()", "floor() needs 1 value"),
		("abs(1, 2)", "abs() needs 1 value"),
	],
)
def test_function_argument_counts_are_checked(source, fragment):
	assert fragment in _refusal(source)


@pytest.mark.parametrize(
	"source",
	[
		"max(carrier_price) if boxes > 5 else 1",
		"1 if boxes < 5 else open('x')",
		"1 if boxes < 5 else missing_name",
		"1 if boxes < 5 else carrier_price ** 2",
		"1 if boxes < 5 else boxes in item_count",
	],
)
def test_a_bad_part_is_refused_even_where_the_sample_order_never_goes(source):
	with pytest.raises(FormulaError):
		compile_formula(source)


def test_round_needs_a_whole_number_of_places():
	with pytest.raises(FormulaError, match="whole number"):
		evaluate("round(carrier_price, 1.5)", SAMPLE)


# A number written straight against the word after it. Python reads the first four today but warns (and may
# refuse them in a later version), so whether a formula was accepted would depend on the version and on the
# warning settings. The others are a syntax error already.
NUMBER_THEN_WORD = [
	"1if boxes else 2",
	"1or 2",
	"2and boxes",
	"0x1for 2",
	"1_000if boxes else 2",
	"1jif boxes else 2",
	"0b1if boxes else 2",
	"1e5if boxes else 2",
	"carrier_price+1if boxes else 2",
	"(1if boxes else 2)",
	"boxes if 1else 2",
	"1.5e3x",
]


@pytest.mark.parametrize("source", NUMBER_THEN_WORD, ids=_short)
def test_a_number_written_straight_against_a_word_is_refused_with_or_without_warnings(source):
	"""The same sentence however warnings are set, and the parser is never asked to warn about it."""
	with warnings.catch_warnings(record=True) as caught:
		warnings.simplefilter("always")
		normal = _refusal(source)
	assert caught == [], [str(warning.message) for warning in caught]
	with warnings.catch_warnings():
		warnings.simplefilter("error")  # as under python -W error
		assert _refusal(source) == normal
	assert normal.startswith("Put a space between a number and the word after it. Write '")
	assert normal.endswith("."), normal


def test_the_sentence_for_a_number_against_a_word_shows_what_to_write():
	assert _refusal("1if boxes else 2") == (
		"Put a space between a number and the word after it. Write '1 if', not '1if'."
	)
	assert _refusal("carrier_price + 0x1for 2") == (
		"Put a space between a number and the word after it. Write '0x1f or', not '0x1for'."
	)


@pytest.mark.parametrize(
	"source",
	[
		"1e5",
		"1 if boxes else 2",
		"(1)if boxes else 2",
		"boxes*2",
		"2*boxes",
		"1.5e3 * boxes",
		"1_000 + boxes",
		"weight_kg",
	],
	ids=_short,
)
def test_a_number_that_is_not_against_a_word_is_still_accepted_without_a_warning(source):
	with warnings.catch_warnings():
		warnings.simplefilter("error")
		assert isinstance(compile_formula(source), ast.Expression)


def test_a_formula_the_scan_cannot_read_gets_the_parsers_message():
	"""Whatever the tokenizer refuses falls through to the usual message about where it is wrong."""
	for source in (
		"max(carrier_price",
		"carrier_price $",
		"carrier_price \\ 1",
		"'unclosed",
		"carrier_price\x00",
	):
		message = _refusal(source)
		assert not message.startswith("Put a space"), (source, message)


def test_a_syntax_error_says_what_and_where():
	message = _refusal("max(carrier_price")
	assert "not written correctly" in message
	assert "near character" in message
	assert "never closed" in message


def test_every_refusal_is_a_plain_sentence():
	problems = ["carrier_price / 0", "carrier_price * 1e308 * 10", "carrier_price > 5", "round(1, 1.5)"]
	for source in [*UNSAFE_SOURCES, *ALSO_REFUSED, *problems]:
		message = _refusal(source)
		assert message[0].isupper() and message.endswith("."), (source, message)
		for jargon in ("Traceback", "Error", "NoneType", "object", "AST", "ast."):
			assert jargon not in message, (source, message)


@pytest.mark.parametrize("error", [SyntaxError, ValueError, RecursionError, MemoryError])
def test_parse_failures_never_escape_as_another_exception(monkeypatch, error):
	def explode(*args, **kwargs):
		raise error("boom")

	with monkeypatch.context() as patch:
		patch.setattr(shipping_formula.ast, "parse", explode)
		with pytest.raises(FormulaError):
			compile_formula("carrier_price")


@pytest.mark.parametrize(
	"error", [TypeError, ValueError, OverflowError, ZeroDivisionError, RecursionError, MemoryError]
)
def test_calculation_failures_never_escape_as_another_exception(monkeypatch, error):
	def explode(*args, **kwargs):
		raise error("boom")

	monkeypatch.setattr(shipping_formula, "_eval", explode)
	with pytest.raises(FormulaError) as caught:
		evaluate("carrier_price", SAMPLE)
	assert "boom" not in str(caught.value)


def test_a_value_too_big_for_a_float_is_a_formula_error():
	with pytest.raises(FormulaError):
		evaluate("boxes", {**SAMPLE, "boxes": 10**400})
	with pytest.raises(FormulaError):
		evaluate("carrier_price", None)


def test_the_interpreter_refuses_a_tree_the_checker_never_passed():
	unchecked = ast.parse("carrier_price.real", mode="eval").body
	with pytest.raises(FormulaError):
		shipping_formula._eval(unchecked, SAMPLE)


def test_module_never_calls_eval_exec_or_compile():
	# Reads the module's own source. The mode="eval" argument of ast.parse is only a string, not a call.
	forbidden = {"eval", "exec", "compile", "__import__"}
	source = Path(shipping_formula.__file__).read_text(encoding="utf-8")
	found = []
	for node in ast.walk(ast.parse(source)):
		if isinstance(node, ast.Name) and node.id in forbidden:
			found.append(node.id)
		elif isinstance(node, ast.Attribute) and node.attr in forbidden:
			found.append(f".{node.attr}")
	assert found == []
