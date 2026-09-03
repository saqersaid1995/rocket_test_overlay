from __future__ import annotations

import ast
import operator

# =====================================================================
# Dynamic channel system, Phase 5 -- virtual (derived) channels.
# =====================================================================
#
# A virtual channel's value is a formula over other channels' current
# values (e.g. "motor.thrust / motor.chamber_pressure"). Formulas are
# never passed to eval() or exec(). They're parsed as a Python
# expression AST and walked by hand against a strict allow-list:
# arithmetic operators and numeric literals only. Anything else --
# function calls, attribute access, subscripting, comprehensions,
# imports, string ops -- is rejected before it ever executes.
# =====================================================================

_ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_ALLOWED_UNARYOPS = {
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


class FormulaError(ValueError):
    pass


def evaluate_formula(formula: str, channel_values: dict) -> float:
    """Evaluate `formula`, an arithmetic expression referencing channel ids
    (e.g. "motor.thrust / motor.chamber_pressure"), against a dict of
    {channel_id: numeric_value}. Raises FormulaError on anything unsafe,
    unknown, or non-numeric -- never partially evaluates.
    """
    if not formula or not formula.strip():
        raise FormulaError("formula is empty")

    # Channel ids contain dots, which Python's grammar treats as attribute
    # access -- substitute each referenced id with a safe bare identifier
    # before parsing, longest ids first so no id is a substring of another.
    working = formula
    substitutions: dict[str, float] = {}
    for index, channel_id in enumerate(sorted(channel_values, key=len, reverse=True)):
        if channel_id in working:
            placeholder = f"__ch{index}__"
            working = working.replace(channel_id, placeholder)
            value = channel_values[channel_id]
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise FormulaError(f"referenced channel '{channel_id}' has no numeric value")
            substitutions[placeholder] = float(value)

    try:
        tree = ast.parse(working, mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"invalid formula syntax: {exc}") from exc

    def _eval(node):
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise FormulaError("only numeric literals are allowed")
            return float(node.value)
        if isinstance(node, ast.BinOp):
            op = _ALLOWED_BINOPS.get(type(node.op))
            if op is None:
                raise FormulaError(f"operator {type(node.op).__name__} is not allowed")
            return op(_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp):
            op = _ALLOWED_UNARYOPS.get(type(node.op))
            if op is None:
                raise FormulaError(f"unary operator {type(node.op).__name__} is not allowed")
            return op(_eval(node.operand))
        if isinstance(node, ast.Name):
            if node.id not in substitutions:
                raise FormulaError(f"unknown channel reference in formula: {node.id}")
            return substitutions[node.id]
        raise FormulaError(f"expression type {type(node).__name__} is not allowed in formulas")

    try:
        result = _eval(tree)
    except ZeroDivisionError as exc:
        raise FormulaError("division by zero") from exc
    if not isinstance(result, (int, float)):
        raise FormulaError("formula did not evaluate to a number")
    return float(result)
