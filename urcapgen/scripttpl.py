"""The URScript template language — the reference implementation.

A node's ``script`` in ``urcap.toml`` is rendered at runtime by the URCap itself: by
``ScriptTemplate.java`` on PolyScope 5 and by ``urcapgen-runtime.js`` on PolyScope X. This
module is the specification both are tested against (``urcapgen parity``): the same template
and the same values must give the same lines on all three.

The language is a strict subset of Mustache, with no HTML escaping:

``{{key}}``
    The field's value as a URScript literal (:func:`literal`).
``{{#key}}…{{/key}}``
    The block when the field is truthy: a bool that is true, a number that is not 0, a
    string or choice that is not empty. A pose is always truthy.
``{{^key}}…{{/key}}``
    The block when the field is falsy.
``{{#key=value}}…{{/key}}`` / ``{{^key=value}}…{{/key}}``
    The block when the field's raw value (:func:`raw`) is / is not ``value``.
``{{! comment }}``
    Nothing.

A line holding only a section tag or a comment (and whitespace) disappears entirely, so
sections can sit on their own lines without leaving blank ones.

Values are ``(type, value)`` pairs; the types are the field types of the spec:
``int``, ``float``, ``bool``, ``string``, ``choice``, ``pose``.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal

# A line that is only a section/comment tag: the tag stays, the line's whitespace and its
# newline go. Kept byte-identical in ScriptTemplate.java and urcapgen-runtime.js.
STANDALONE = re.compile(r"(?m)^[ \t]*(\{\{[#^/!][^}]*\}\})[ \t]*\r?\n")
TAG = re.compile(r"\{\{([#^/!]?)\s*([^}]*?)\s*\}\}")
NAME = re.compile(r"[a-z][a-z0-9_]*")


class TemplateError(ValueError):
    pass


def number(x: float) -> str:
    """A float as URScript sees it: six decimals at most, half away from zero, trailing zeros
    dropped, always with a decimal point (``1.0``, ``0.25``, ``-0.000001``)."""
    d = Decimal(x).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0")
        if s.endswith("."):
            s += "0"
    else:
        s += ".0"
    if s in ("-0.0",):
        s = "0.0"
    return s


def literal(kind: str, value) -> str:
    """The URScript literal for a value of a field type."""
    if kind == "int":
        return str(int(value))
    if kind == "float":
        return number(float(value))
    if kind == "bool":
        return "True" if value else "False"
    if kind == "string":
        s = str(value)
        if '"' in s or "\n" in s or "\r" in s:
            raise TemplateError("a string value cannot hold a double quote or a line break")
        return f'"{s}"'
    if kind == "choice":
        return str(value)
    if kind == "pose":
        vals = list(value)
        if len(vals) != 6:
            raise TemplateError("a pose has six numbers")
        return "p[" + ", ".join(number(float(v)) for v in vals) + "]"
    raise TemplateError(f"unknown field type {kind!r}")


def raw(kind: str, value) -> str:
    """What ``{{#key=value}}`` compares against: the bare value."""
    if kind == "bool":
        return "true" if value else "false"
    if kind == "string" or kind == "choice":
        return str(value)
    return literal(kind, value)


def truthy(kind: str, value) -> bool:
    if kind == "bool":
        return bool(value)
    if kind in ("int", "float"):
        return float(value) != 0.0
    if kind in ("string", "choice"):
        return str(value) != ""
    return True


def _parse(template: str):
    """Template text → a tree of ``("text", s)``, ``("var", name)``,
    ``("section", inverted, name, eq or None, children)``."""
    text = STANDALONE.sub(r"\1", template)
    root: list = []
    stack: list[tuple[str, list]] = [("", root)]
    pos = 0
    for m in TAG.finditer(text):
        if m.start() > pos:
            stack[-1][1].append(("text", text[pos : m.start()]))
        pos = m.end()
        sigil, body = m.group(1), m.group(2)
        if sigil == "!":
            continue
        if sigil in ("#", "^"):
            name, _, eq = body.partition("=")
            name = name.strip()
            if not NAME.fullmatch(name):
                raise TemplateError(f"bad section name {name!r}")
            children: list = []
            stack[-1][1].append(("section", sigil == "^", name, eq.strip() if _ else None, children))
            stack.append((name, children))
            continue
        if sigil == "/":
            name = body.partition("=")[0].strip()
            if len(stack) == 1 or stack[-1][0] != name:
                raise TemplateError(f"{{{{/{name}}}}} closes nothing open")
            stack.pop()
            continue
        if not NAME.fullmatch(body):
            raise TemplateError(f"bad variable {{{{{body}}}}}")
        stack[-1][1].append(("var", body))
    if len(stack) != 1:
        raise TemplateError(f"{{{{#{stack[-1][0]}}}}} is never closed")
    if pos < len(text):
        root.append(("text", text[pos:]))
    return root


def names(template: str) -> set[str]:
    """Every field name a template uses (for the generator's static check)."""
    out: set[str] = set()

    def walk(nodes):
        for n in nodes:
            if n[0] == "var":
                out.add(n[1])
            elif n[0] == "section":
                out.add(n[2])
                walk(n[4])

    walk(_parse(template))
    return out


def render(template: str, values: dict[str, tuple[str, object]], display: bool = False) -> str:
    """Render ``template`` with ``values`` = ``{key: (type, value)}``. ``display``: for a node's
    label, not URScript — strings without their quotes."""

    def get(name):
        if name not in values:
            raise TemplateError(f"no field {name!r}")
        return values[name]

    def walk(nodes, out: list[str]):
        for n in nodes:
            if n[0] == "text":
                out.append(n[1])
            elif n[0] == "var":
                kind, value = get(n[1])
                out.append(str(value) if display and kind == "string" else literal(kind, value))
            else:
                _, inverted, name, eq, children = n
                kind, value = get(name)
                hit = truthy(kind, value) if eq is None else raw(kind, value) == eq
                if hit != inverted:
                    walk(children, out)

    out: list[str] = []
    walk(_parse(template), out)
    return "".join(out)


def lines(script: str) -> list[str]:
    """A rendered script as the robot runs it: lines without their indentation, empty lines
    dropped (PolyScope X's ScriptBuilder drops them; indentation is cosmetic in URScript).
    The parity tests compare these."""
    return [ln.strip() for ln in script.splitlines() if ln.strip()]
