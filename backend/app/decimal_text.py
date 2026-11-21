"""Exact finite decimal strings, without Decimal context or float conversion."""
import re


def canonical_decimal(value):
    if not isinstance(value, str) or not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", value):
        raise ValueError("單價須為十進位文字，不可使用指數、逗號或非有限數字")
    negative = value.startswith("-")
    whole, dot, fraction = value.lstrip("-").partition(".")
    whole = whole.lstrip("0") or "0"
    fraction = fraction.rstrip("0")
    result = whole + ("." + fraction if fraction else "")
    return ("-" if negative and result != "0" else "") + result


def exact_product(left, right):
    from decimal import Decimal, Inexact, Rounded, localcontext, MAX_EMAX, MIN_EMIN
    left, right = canonical_decimal(left), canonical_decimal(right)
    with localcontext() as context:
        context.prec = len(left) + len(right) + 2
        context.Emax, context.Emin = MAX_EMAX, MIN_EMIN
        context.traps[Inexact] = context.traps[Rounded] = True
        return canonical_decimal(format(Decimal(left) * Decimal(right), "f"))


def exact_sum(values):
    from decimal import Decimal, Inexact, Rounded, localcontext, MAX_EMAX, MIN_EMIN
    values = [canonical_decimal(value) for value in values]
    if not values:
        return "0"
    integer_digits = max(len(value.lstrip("-").partition(".")[0]) for value in values)
    scale = max(len(value.partition(".")[2]) for value in values)
    with localcontext() as context:
        context.prec = integer_digits + scale + len(str(len(values))) + 2
        context.Emax, context.Emin = MAX_EMAX, MIN_EMIN
        context.traps[Inexact] = context.traps[Rounded] = True
        result = sum((Decimal(value) for value in values), Decimal(0))
        return canonical_decimal(format(result, "f"))


def exact_difference(left, right):
    right = canonical_decimal(right)
    opposite = right[1:] if right.startswith("-") else ("-"+right if right != "0" else "0")
    return exact_sum([left, opposite])
