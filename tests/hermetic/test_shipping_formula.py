"""Pins the shipping-rule formula language: the documented examples, the
whitelist (everything else is refused in plain words), the size limits, and that
nothing a user types can reach eval/exec."""

import ast
import math
from pathlib import Path

import pytest

from dsi_catalogue import shipping_formula
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
		"(carrier_price > 5) + 1",
		"-(boxes > 0)",
		"max(boxes > 0, 1)",
		"(boxes > 0) < 2",
		"(boxes > 0) if carrier_price > 5 else 1",
	],
)
def test_a_yes_no_answer_is_never_used_as_a_number(source):
	compile_formula(source)
	with pytest.raises(FormulaError, match="yes/no"):
		evaluate(source, SAMPLE)


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
