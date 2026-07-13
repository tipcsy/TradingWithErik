"""
Deklaratív paraméter-kényszerek BIZTONSÁGOS kiértékelése.

A kényszerek a stratégia optimizer-configjában élnek (strategy/config/<name>.json
→ optimizer.constraints), kifejezés-STRINGEKként, pl.:
    "wpr_m15_buy_extreme < wpr_m15_buy_trigger"
    "wpr_m15_buy_trigger < wpr_m15_sell_extreme"

Így a paraméter-tér érvényessége EGY helyen (a configban) van definiálva:
  • az optimizer ezzel SZŰR (és a range-ek `gt`/`lt` metaadatából dinamikus
    tartományt is szab, hogy érvénytelen kombó elő se álljon);
  • a stratégia `constraints_ok`-ja UGYANEZT a listát értékeli;
  • belőle egyszerű betöltési figyelmeztetés is építhető.

BIZTONSÁG: NEM `eval` — szűk `ast`-fehérlista: összehasonlítások (`<`, `<=`, `>`,
`>=`, `==`, `!=`, láncolva is: `a < b < c`), `and`/`or`, paraméternevek és
szám-literálok (előjeles). Bármi más → hiba. Így egy configba írt kifejezés nem
tud mellékhatást okozni.
"""

from __future__ import annotations

import ast
import logging

log = logging.getLogger(__name__)

_ALLOWED_CMP = (ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq)
_PARSE_CACHE: dict[str, ast.AST] = {}
_WARNED: set[str] = set()


def _compile(expr: str) -> ast.AST:
    tree = _PARSE_CACHE.get(expr)
    if tree is None:
        tree = ast.parse(expr, mode="eval")
        _PARSE_CACHE[expr] = tree
    return tree


def _cmp(op, a, b) -> bool:
    if isinstance(op, ast.Lt):   return a < b
    if isinstance(op, ast.LtE):  return a <= b
    if isinstance(op, ast.Gt):   return a > b
    if isinstance(op, ast.GtE):  return a >= b
    if isinstance(op, ast.Eq):   return a == b
    if isinstance(op, ast.NotEq): return a != b
    raise ValueError(f"nem támogatott összehasonlítás: {type(op).__name__}")


def _eval(node, params: dict):
    if isinstance(node, ast.Expression):
        return _eval(node.body, params)
    if isinstance(node, ast.BoolOp):
        vals = [_eval(v, params) for v in node.values]
        if isinstance(node.op, ast.And):
            return all(vals)
        if isinstance(node.op, ast.Or):
            return any(vals)
        raise ValueError("nem támogatott bool-művelet")
    if isinstance(node, ast.Compare):
        left = _eval(node.left, params)
        for op, right_node in zip(node.ops, node.comparators):
            right = _eval(right_node, params)
            if not isinstance(op, _ALLOWED_CMP):
                raise ValueError(f"nem támogatott összehasonlítás: {type(op).__name__}")
            if not _cmp(op, left, right):
                return False
            left = right                      # láncolt: a < b < c
        return True
    if isinstance(node, ast.Name):
        if node.id not in params:
            raise KeyError(node.id)
        return params[node.id]
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_eval(node.operand, params)
    raise ValueError(f"nem támogatott kifejezés-elem: {type(node).__name__}")


def check(params: dict, constraints) -> bool:
    """Minden kényszer teljesül-e a `params`-ra? Üres/None → True.

    Egy nem kiértékelhető (elgépelt/ismeretlen nevű) kifejezést KIHAGY (és EGYSZER
    figyelmeztet a logba) — egy config-elírás ne állítsa meg az egész
    optimalizálást; a `validate()` a hibás kifejezéseket külön feltárja."""
    for expr in (constraints or []):
        try:
            if not _eval(_compile(expr), params):
                return False
        except Exception as e:
            if expr not in _WARNED:
                _WARNED.add(expr)
                log.warning("Kihagyott (hibás) paraméter-kényszer %r: %s", expr, e)
    return True


def violations(params: dict, constraints) -> list[str]:
    """A megsértett (vagy nem kiértékelhető) kényszer-kifejezések listája."""
    out = []
    for expr in (constraints or []):
        try:
            if not _eval(_compile(expr), params):
                out.append(expr)
        except Exception:
            out.append(expr)
    return out


def validate(constraints, known_keys) -> list[tuple[str, str]]:
    """Indításkori ellenőrzés: minden kifejezés parse-olható-e ÉS csak ISMERT
    paraméternevekre hivatkozik-e. Visszaad: [(kifejezés, hiba-ok), …] a
    problémásakról (üres = minden rendben). A hívó ezt logolhatja."""
    problems = []
    known = set(known_keys)
    for expr in (constraints or []):
        try:
            tree = _compile(expr)
        except SyntaxError as e:
            problems.append((expr, f"szintaktikai hiba: {e}"))
            continue
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        unknown = names - known
        if unknown:
            problems.append((expr, f"ismeretlen paraméter(ek): {', '.join(sorted(unknown))}"))
    return problems
