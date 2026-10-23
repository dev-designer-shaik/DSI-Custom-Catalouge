"""A small, safe formula language for shipping rules.

An ERP user writes a price such as ``max(carrier_price * 1.1, 45)`` and this
module checks and calculates it. It is pure Python with no Frappe import, so it
is tested standalone.

The text is parsed into a syntax tree, only a whitelist of node types is let
through, and the checked tree is calculated by the small interpreter below.
Nothing a user types is ever handed to Python to run, so a formula can do
arithmetic and nothing else.

A formula can contain:
- numbers (plain int or float literals; no text, True/False or None)
- the names in VARIABLES
- the functions in FUNCTIONS, with positional arguments only. round() rounds a half away from zero, from the
  number as it is written (round(2.5) is 3, round(-2.5) is -3, round(2.675, 2) is 2.68), as money is rounded
  to cents; Python's own round() gives 2, -2 and 2.67
- + - * / % // and brackets (there is no power operator)
- comparisons < <= > >= == != (they can be chained)
- and, or, not
- ``a if condition else b``

A comparison, and, or and not give a yes/no answer, never a number. They choose
between two numbers inside ``a if condition else b``, but a formula as a whole
must give a number. Using a yes/no answer as a number (adding it, comparing it,
giving it to a function) is refused when the formula is saved, on every branch of
an ``a if condition else b``, not only on the one a sample order takes.

Every failure is a FormulaError whose message is a plain sentence for an ERP
user. Nothing else escapes `compile_formula`, `evaluate` or `check_formula`.
"""

import ast
import io
import itertools
import math
import operator
import sys
import tokenize
from decimal import ROUND_HALF_UP, Context, Decimal

VARIABLES = ("carrier_price", "order_value", "weight_kg", "volume_l", "boxes", "item_count")

# The values a formula is run against when it is saved (see `check_formula`).
SAMPLE: dict[str, float] = {
	"carrier_price": 38.2,
	"order_value": 310.0,
	"weight_kg": 2.4,
	"volume_l": 6.0,
	"boxes": 1,
	"item_count": 2,
}

MAX_LENGTH = 300  # characters, not counting whitespace around the formula
MAX_NODES = 80  # expression nodes: every number, name, call and operation counts as one


class FormulaError(ValueError):
	"""A formula that cannot be used. The message is plain language for an ERP user."""


# A float has at most 309 digits before the point, and `_round` goes to 400 places, so this leaves room for
# the biggest float rounded to the most places. Decimal's own 28 digits would make `quantize` raise for
# round(1e30, 2). (The engine keeps a context of its own for cents: it imports this module, so the two
# cannot share one without a cycle.)
_ROOM = Context(prec=800)
# A float has at most 324 decimals, so any number of places past that changes nothing, and the biggest float
# is under half of 1e309, so any number of places below -309 gives 0. Clamping the places to this range keeps
# a huge number of places (round(x, 1e30)) from costing anything.
_MOST_PLACES = 400


def _round(value, places=None):
	"""`value` rounded to `places` decimals (to a whole number when none are given; negative places round to
	tens, hundreds...), with halves going away from zero, as the engine rounds money to cents.

	The number is read as it is written, not as the binary number nearest to it, so round(2.675, 2) is 2.68
	and round(2.5) is 3, where Python's own round() gives 2.67 and 2 (it reads the binary value, and a tie goes
	to the even digit). A number that already has no more decimals than asked comes back as it is.
	"""
	if places is None:
		places = 0.0
	elif not places.is_integer():
		raise FormulaError("The second value of round() must be a whole number.")
	places = int(max(-_MOST_PLACES, min(_MOST_PLACES, places)))
	quantum = Decimal((0, (1,), -places))  # 1E-2 for 2 places, 1E+1 for -1
	return float(Decimal(repr(value)).quantize(quantum, rounding=ROUND_HALF_UP, context=_ROOM))


# name: (what it calls, fewest values, most values, how an error words the count)
_FUNCTIONS = {
	"min": (min, 2, math.inf, "at least 2 values"),
	"max": (max, 2, math.inf, "at least 2 values"),
	"round": (_round, 1, 2, "1 or 2 values"),
	"ceil": (math.ceil, 1, 1, "1 value"),
	"floor": (math.floor, 1, 1, "1 value"),
	"abs": (abs, 1, 1, "1 value"),
}
FUNCTIONS = tuple(_FUNCTIONS)

_BINARY = {
	ast.Add: operator.add,
	ast.Sub: operator.sub,
	ast.Mult: operator.mul,
	ast.Div: operator.truediv,
	ast.Mod: operator.mod,
	ast.FloorDiv: operator.floordiv,
}
_UNARY = (ast.UAdd, ast.USub, ast.Not)
_COMPARE = {
	ast.Lt: operator.lt,
	ast.LtE: operator.le,
	ast.Gt: operator.gt,
	ast.GtE: operator.ge,
	ast.Eq: operator.eq,
	ast.NotEq: operator.ne,
}

_TOO_BIG = "The formula is too long or complex. Shorten it, or split the work across several rules."
_TOO_LARGE = "The formula involves a number that is too large to use."
_YES_NO = (
	"A yes/no answer (from a comparison, 'and', 'or' or 'not') can't be used as a number. "
	"Use it inside 'a if condition else b' to choose between two numbers."
)
_NOT_ALLOWED = (
	"That part of the formula is not allowed. A formula is made of numbers, "
	f"the names {', '.join(VARIABLES)}, the functions {', '.join(FUNCTIONS)}, "
	"+ - * / % //, comparisons (< <= > >= == !=), and, or, not, and 'a if condition else b'."
)


# ---------------------------------------------------------------- checking
def _check(node):
	"""Raise FormulaError unless `node` is built only from allowed parts."""
	kind = type(node)
	if kind is ast.Constant:
		_check_literal(node.value)
	elif kind is ast.Name:
		_check_name(node.id)
	elif kind is ast.BinOp and type(node.op) in _BINARY:
		_check_each(node.left, node.right)
	elif kind is ast.UnaryOp and type(node.op) in _UNARY:
		_check_each(node.operand)
	elif kind is ast.BoolOp:
		_check_each(*node.values)
	elif kind is ast.Compare and all(type(op) in _COMPARE for op in node.ops):
		_check_each(node.left, *node.comparators)
	elif kind is ast.IfExp:
		_check_each(node.test, node.body, node.orelse)
	elif kind is ast.Call:
		_check_call(node)
	else:
		raise FormulaError(_NOT_ALLOWED)


def _check_each(*nodes):
	for node in nodes:
		_check(node)


def _check_literal(value):
	if type(value) not in (int, float):  # bool is a type of its own, so True and False are refused too
		raise FormulaError("Only numbers can be used in a formula, not text, True/False or None.")
	# Compared with the largest float rather than converted: float() and math.isfinite() both
	# raise for an integer too big for a float, and 0x followed by 298 f's is one of those.
	if abs(value) > sys.float_info.max:
		raise FormulaError(_TOO_LARGE)


def _check_name(name):
	if name in VARIABLES:
		return
	if name in _FUNCTIONS:
		raise FormulaError(f"Functions need brackets: write {name}(...), not {name} on its own.")
	raise FormulaError(f"Unknown name '{name}'. You can use: {', '.join(VARIABLES)}.")


def _check_call(node):
	name = node.func.id if isinstance(node.func, ast.Name) else None
	if name is None:
		raise FormulaError(f"Only these functions can be called: {', '.join(FUNCTIONS)}.")
	if name not in _FUNCTIONS:
		raise FormulaError(f"Unknown function '{name}'. You can use: {', '.join(FUNCTIONS)}.")
	if node.keywords:
		raise FormulaError("Functions take plain values in order, not name=value arguments.")
	_check_each(*node.args)
	_, fewest, most, wording = _FUNCTIONS[name]
	if not fewest <= len(node.args) <= most:
		raise FormulaError(f"The function {name}() needs {wording}.")


# What a part of a formula gives: a number, a yes/no answer (a comparison, and, or, not), or either (an
# `a if condition else b` whose branches differ, so it is a number on one path and a yes/no on the other).
_NUMBER, _ANSWER, _EITHER = "number", "yes/no", "either"


def _kind(node):
	"""What a checked node gives. Raises FormulaError where a yes/no answer (or an either) is used as a number:
	the rule `_eval_number` applies when a formula is calculated, applied here to every branch, including the
	ones the order being calculated never takes. `not`, `and`, `or` and a condition take either kind."""
	kind = type(node)
	if kind is ast.BinOp:
		_need_numbers(node.left, node.right)
	elif kind is ast.UnaryOp and isinstance(node.op, ast.Not):
		_kind(node.operand)
		return _ANSWER
	elif kind is ast.UnaryOp:
		_need_numbers(node.operand)
	elif kind is ast.BoolOp:
		for operand in node.values:
			_kind(operand)
		return _ANSWER
	elif kind is ast.Compare:
		_need_numbers(node.left, *node.comparators)
		return _ANSWER
	elif kind is ast.IfExp:
		_kind(node.test)
		body, orelse = _kind(node.body), _kind(node.orelse)
		return body if body == orelse else _EITHER
	elif kind is ast.Call:
		_need_numbers(*node.args)
	return _NUMBER  # a number, a name, or the result of an operation or a function


def _need_numbers(*nodes):
	"""Raise FormulaError if any of `nodes` can give a yes/no answer. All of them are typed first, so a
	misuse deeper inside one is reported rather than this one."""
	kinds = [_kind(node) for node in nodes]
	if any(kind != _NUMBER for kind in kinds):
		raise FormulaError(_YES_NO)


# ---------------------------------------------------------------- evaluating
def _eval(node, values):
	"""Calculate a checked tree. A yes/no answer is a bool; every number is a finite float."""
	kind = type(node)
	if kind is ast.Constant:
		return float(node.value)
	if kind is ast.Name:
		return _variable(node.id, values)
	if kind is ast.BinOp:
		calculate = _BINARY[type(node.op)]
		return _finite(calculate(_eval_number(node.left, values), _eval_number(node.right, values)))
	if kind is ast.UnaryOp:
		if isinstance(node.op, ast.Not):
			return not _eval(node.operand, values)
		operand = _eval_number(node.operand, values)
		return -operand if isinstance(node.op, ast.USub) else operand
	if kind is ast.BoolOp:
		# Like Python, `or` stops at its first true operand and `and` at its first false one, so
		# `boxes > 0 and carrier_price / boxes > 5` is safe when boxes is 0. The answer is yes/no.
		stops_on = isinstance(node.op, ast.Or)
		for operand in node.values:
			if bool(_eval(operand, values)) is stops_on:
				return stops_on
		return not stops_on
	if kind is ast.Compare:
		left = _eval_number(node.left, values)
		for op, comparator in zip(node.ops, node.comparators, strict=True):
			right = _eval_number(comparator, values)
			if not _COMPARE[type(op)](left, right):
				return False
			left = right
		return True
	if kind is ast.IfExp:
		return _eval(node.body if _eval(node.test, values) else node.orelse, values)
	if kind is ast.Call:
		function = _FUNCTIONS[node.func.id][0]
		return _finite(float(function(*[_eval_number(arg, values) for arg in node.args])))
	raise FormulaError(_NOT_ALLOWED)  # not reachable for a tree that `_check` passed


def _eval_number(node, values):
	"""Calculate `node`, which has to give a number rather than a yes/no answer."""
	value = _eval(node, values)
	if isinstance(value, bool):
		raise FormulaError(_YES_NO)
	return value


def _finite(value):
	# A float that overflows becomes inf without an error; stop it before it spreads.
	if not math.isfinite(value):
		raise FormulaError(_TOO_LARGE)
	return value


def _variable(name, values):
	if name not in values:
		raise FormulaError(f"No value was given for '{name}'.")
	value = values[name]
	if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
		raise FormulaError(f"The value given for '{name}' is not a usable number.")
	return float(value)


def _number_against_a_word(text):
	"""(number, word) of the first number written straight against the word after it, as in `1if boxes else 2`,
	else None. Python reads that, but warns and may refuse it in a later version, so whether a formula was
	accepted would depend on the version and on the warning settings. Text the tokenizer cannot read is left
	to the parser, which says what is wrong with it."""
	try:
		tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
	except (tokenize.TokenError, SyntaxError, ValueError):  # a null byte is a ValueError before Python 3.12
		return None
	for number, word in itertools.pairwise(tokens):
		if number.type == tokenize.NUMBER and word.type == tokenize.NAME and number.end == word.start:
			return number.string, word.string
	return None


# ---------------------------------------------------------------- public API
def compile_formula(source: str) -> ast.Expression:
	"""Parse a formula and check it against the whitelist. Returns the checked tree."""
	if not isinstance(source, str):
		raise FormulaError("The formula must be written as text.")
	text = source.strip()  # Python refuses a leading space, and a text box adds a newline
	if not text:
		raise FormulaError("The formula is empty. Write a formula such as 'carrier_price * 1.1'.")
	if len(text) > MAX_LENGTH:
		raise FormulaError(_TOO_BIG)
	glued = _number_against_a_word(text)
	if glued is not None:
		number, word = glued
		raise FormulaError(
			f"Put a space between a number and the word after it. Write '{number} {word}', not '{number}{word}'."
		)
	try:
		tree = ast.parse(text, mode="eval")
	except SyntaxError as err:
		where = f" near character {err.offset}" if err.offset else ""
		raise FormulaError(f"The formula is not written correctly{where}: {err.msg}.") from None
	except ValueError:  # Python before 3.12 refuses a null byte this way
		raise FormulaError("The formula contains a character that cannot be used.") from None
	except (RecursionError, MemoryError):
		raise FormulaError(_TOO_BIG) from None
	# Counted before the tree is walked, so the walks below never go deeper than MAX_NODES.
	if sum(isinstance(node, ast.expr) for node in ast.walk(tree)) > MAX_NODES:
		raise FormulaError(_TOO_BIG)
	_check(tree.body)
	_kind(tree.body)  # the formula as a whole may still be a yes/no: `evaluate` refuses that for an order
	return tree


def evaluate(source: str, variables: dict[str, float]) -> float:
	"""Calculate a formula for the given values. Gives a finite float or raises FormulaError."""
	tree = compile_formula(source)
	try:
		value = _eval(tree.body, variables)
	except FormulaError:  # first: FormulaError is a ValueError, so the clause below would eat it
		raise
	except ZeroDivisionError:
		raise FormulaError("The formula tries to divide by zero.") from None
	except OverflowError:
		raise FormulaError(_TOO_LARGE) from None
	except (RecursionError, MemoryError):
		raise FormulaError(_TOO_BIG) from None
	except (TypeError, ValueError) as err:  # not expected once the tree is checked
		raise FormulaError("The formula could not be calculated.") from err
	if isinstance(value, bool):
		raise FormulaError(_YES_NO)
	return value + 0.0  # turns -0.0 into 0.0, which would otherwise print as "-0.00"


def check_formula(source: str) -> None:
	"""Refuse a formula that cannot be used, or that gives no number for the SAMPLE order."""
	evaluate(source, SAMPLE)
