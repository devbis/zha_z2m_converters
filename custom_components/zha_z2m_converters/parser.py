"""Static parser for the safe, declarative subset of converter definitions."""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

from .lexer import Token, tokenize
from .mapping import CONVERTER_MAP, LUMI_SINGLE_OPERATION_MODE_BASIC_MODELS
from .model import (
    Binding,
    ConfigureAction,
    CustomClusterSpec,
    DeviceDefinition,
    Diagnostic,
    EndpointCluster,
    Expose,
    Expression,
    ParseResult,
)
from .source import load_sources


class UnsupportedSyntax(Exception):
    pass


@dataclass
class _ObjectParser:
    tokens: list[Token]
    index: int = 0
    constants: dict[str, Any] | None = None

    def current(self) -> Token:
        if self.index >= len(self.tokens):
            return Token("eof", "", 0, 0, 0)
        return self.tokens[self.index]

    def take(self, value: str | None = None) -> Token:
        token = self.current()
        if value is not None and token.value != value:
            raise UnsupportedSyntax(f"expected {value!r}, got {token.value!r}")
        self.index += 1
        return token

    def parse_value(self) -> Any:
        """Parse a value and discard the TypeScript-only ``as const`` assertion."""
        value = self._parse_value()
        if self.current().value == "as":
            if self.index + 1 >= len(self.tokens) or self.tokens[self.index + 1].value != "const":
                raise UnsupportedSyntax("only 'as const' assertions are supported")
            self.take("as")
            self.take("const")
        return value

    def _parse_value(self) -> Any:
        token = self.current()
        if self.looks_like_predicate():
            return self.parse_predicate()
        if token.value == "{":
            return self.parse_object()
        if token.value == "[":
            self.take("[")
            values = []
            while self.current().value != "]":
                if self.current().value == "." and self.index + 2 < len(self.tokens):
                    spread_start = self.index
                    if all(self.tokens[self.index + offset].value == "." for offset in range(3)):
                        self.index += 3
                        try:
                            values.append({"__spread__": self.parse_value()})
                        except UnsupportedSyntax:
                            values.append({"__unsupported__": "array-spread"})
                            self.skip_to_object_boundary(spread_start, boundaries=(",", "]"))
                        if self.current().value == ",":
                            self.take(",")
                        elif self.current().value != "]":
                            raise UnsupportedSyntax("expected comma after array spread")
                        continue
                value_start = self.index
                try:
                    values.append(self.parse_value())
                except UnsupportedSyntax:
                    values.append({"__unsupported__": "array-item"})
                    self.skip_to_array_boundary(value_start)
                if self.current().value == ",":
                    self.take(",")
                elif self.current().value != "]":
                    raise UnsupportedSyntax("expected comma in array")
            self.take("]")
            return values
        if token.kind == "string":
            self.take()
            return _decode_string(token.value)
        if token.kind == "number":
            self.take()
            value = token.value.replace("_", "")
            try:
                return int(value, 0) if not any(c in value for c in ".eE") else float(value)
            except ValueError as exc:
                raise UnsupportedSyntax(f"invalid number {value}") from exc
        if token.value in ("true", "false", "null", "undefined"):
            self.take()
            return {"true": True, "false": False, "null": None, "undefined": None}[token.value]
        if token.value in ("-", "+"):
            sign = self.take().value
            value = self.parse_value()
            if not isinstance(value, (int, float)):
                raise UnsupportedSyntax("unary sign requires a number")
            return -value if sign == "-" else value
        if token.kind == "identifier":
            name = self.take().value
            parts = [name]
            while self.current().value in (".", "?."):
                self.take()
                parts.append(self.take().value)
            if self.current().value == "<" and self.looks_like_generic_call():
                self.skip_balanced("<", ">")
            if self.current().value == "(":
                value: Any = {"__call__": ".".join(parts), "args": self.parse_call_args()}
                methods = []
                while self.current().value in (".", "?."):
                    self.take()
                    method = self.take().value
                    if self.current().value != "(":
                        raise UnsupportedSyntax("property access after call is not static")
                    methods.append({"name": method, "args": self.parse_call_args()})
                return {"__fluent__": value, "methods": methods} if methods else value
            if self.current().value == "[":
                self.take("[")
                index = self.parse_value()
                self.take("]")
                if not isinstance(index, (str, int)):
                    raise UnsupportedSyntax("indexed access must use a static key")
                return {"__indexed__": ".".join(parts), "index": index}
            if self.constants and len(parts) == 1 and name in self.constants:
                return self.constants[name]
            return {"__identifier__": ".".join(parts)}
        raise UnsupportedSyntax(f"unsupported value {token.value!r}")

    def looks_like_predicate(self) -> bool:
        """Recognize a one-argument arrow predicate without evaluating it."""
        if self.current().kind == "identifier" and self.index + 1 < len(self.tokens):
            return self.tokens[self.index + 1].value == "=>"
        if self.current().value != "(":
            return False
        depth = 0
        index = self.index
        while index < len(self.tokens):
            value = self.tokens[index].value
            if value == "(":
                depth += 1
            elif value == ")":
                depth -= 1
                if depth == 0:
                    return index + 1 < len(self.tokens) and self.tokens[index + 1].value == "=>"
            index += 1
        return False

    def parse_predicate(self) -> dict[str, Any]:
        """Parse the small declarative predicate subset used by modernExtend."""
        if self.current().value == "(":
            self.take("(")
            parameter = self.take()
            if parameter.kind != "identifier" or self.current().value != ")":
                raise UnsupportedSyntax("predicate must have one parameter")
            self.take(")")
        else:
            parameter = self.take()
            if parameter.kind != "identifier":
                raise UnsupportedSyntax("predicate parameter must be an identifier")
        self.take("=>")

        if (
            self.current().kind == "identifier"
            and self.current().value == parameter.value
            and self.index + 1 < len(self.tokens)
            and self.tokens[self.index + 1].value in {",", "}"}
        ):
            self.take()
            return {"__identity__": parameter.value}

        if self.current().value == "!":
            self.take("!")
            values = self.parse_value()
            if self.current().value != ".":
                raise UnsupportedSyntax("predicate requires a static includes call")
            self.take(".")
            if self.take().value != "includes" or self.current().value != "(":
                raise UnsupportedSyntax("predicate requires a static includes call")
            self.take("(")
            argument = self.take()
            if argument.kind != "identifier" or argument.value != parameter.value:
                raise UnsupportedSyntax("predicate includes argument must match its parameter")
            self.take(")")
            if not isinstance(values, list) or not all(isinstance(item, (str, int, float, bool)) for item in values):
                raise UnsupportedSyntax("predicate includes list must be static")
            return {"__predicate__": {"op": "not_in", "values": values}}

        argument = self.take()
        if argument.kind != "identifier" or argument.value != parameter.value:
            raise UnsupportedSyntax("predicate comparison must use its parameter")
        operator = self.take().value
        if operator not in {"==", "===", "!=", "!=="}:
            raise UnsupportedSyntax("unsupported predicate operator")
        expected = self.parse_value()
        if not isinstance(expected, (str, int, float, bool)):
            raise UnsupportedSyntax("predicate comparison value must be static")
        return {
            "__predicate__": {
                "op": "equals" if operator in {"==", "==="} else "not_equals",
                "value": expected,
            }
        }

    def parse_call_args(self) -> list[Any]:
        self.take("(")
        args = []
        while self.current().value != ")":
            args.append(self.parse_value())
            if self.current().value == ",":
                self.take(",")
            elif self.current().value != ")":
                raise UnsupportedSyntax("expected comma in call")
        self.take(")")
        return args

    def parse_object(self) -> dict[str, Any]:
        self.take("{")
        result: dict[str, Any] = {}
        while self.current().value not in {"}", ""}:
            if self.current().value == ".":
                value_start = self.index
                self.skip_to_object_boundary(value_start)
                result["<spread>"] = {"__unsupported__": "<spread>"}
                if self.current().value == ",":
                    self.take(",")
                continue
            key = self.take()
            if key.kind not in ("identifier", "string", "number"):
                raise UnsupportedSyntax("object key must be static")
            key_value = _decode_string(key.value) if key.kind == "string" else key.value
            self.take(":")
            value_start = self.index
            try:
                if key_value == "configure":
                    result[str(key_value)] = self.parse_configure()
                elif key_value == "endpoint" and self.looks_like_predicate():
                    result[str(key_value)] = self.parse_static_endpoint()
                else:
                    result[str(key_value)] = self.parse_value()
                if self.current().value not in (",", "}"):
                    raise UnsupportedSyntax("unsupported expression after property value")
            except UnsupportedSyntax:
                # A definition may contain executable fields such as configure.
                # Preserve the surrounding static object and mark only that field
                # as unsupported instead of rejecting the whole device.
                result[str(key_value)] = {"__unsupported__": str(key_value)}
                self.skip_to_object_boundary(value_start)
            if self.current().value == ",":
                self.take(",")
            elif self.current().value != "}":
                raise UnsupportedSyntax("expected comma in object")
        self.take("}")
        return result

    def parse_static_endpoint(self) -> dict[str, Any]:
        """Parse an endpoint callback whose result is a static object."""
        if self.current().value == "(":
            self.skip_balanced("(", ")")
        elif self.current().kind == "identifier":
            self.take()
        else:
            raise UnsupportedSyntax("endpoint callback must have static parameters")
        self.take("=>")
        wrapped = False
        if self.current().value == "(":
            self.take("(")
            wrapped = True
        if wrapped:
            value = self.parse_object()
            self.take(")")
        elif self.current().value == "{":
            self.take("{")
            if self.current().value != "return":
                raise UnsupportedSyntax("endpoint callback must return a static object")
            self.take("return")
            value = self.parse_object()
            if self.current().value == ";":
                self.take(";")
            self.take("}")
        else:
            value = self.parse_object()
        if not isinstance(value, dict) or not value or not all(
            isinstance(key, str) and isinstance(endpoint, int) and not isinstance(endpoint, bool)
            for key, endpoint in value.items()
        ):
            raise UnsupportedSyntax("endpoint map must contain only integer endpoint ids")
        return {"__endpoint_map__": value}

    def parse_configure(self) -> Any:
        """Parse a callback shell while retaining only its static call expressions."""
        if self.current().value == "async":
            self.take()
        if self.current().value != "(":
            return self.parse_value()
        self.skip_balanced("(", ")")
        self.take("=>")
        self.take("{")
        statements: list[Any] = []
        locals_: dict[str, Any] = {}
        unsupported = False
        if self.constants is None:
            self.constants = {}
        while self.current().value != "}":
            if self.current().value == ";":
                self.take()
                continue
            if self.current().value == "try":
                statement_start = self.index
                try:
                    try_statements, try_unsupported = self.parse_static_configure_try()
                    statements.extend(try_statements)
                    unsupported = unsupported or try_unsupported
                except UnsupportedSyntax:
                    unsupported = True
                    self.skip_to_object_boundary(statement_start, boundaries=(";", "}"))
                continue
            if self.current().value == "for":
                loop_start = self.index
                try:
                    loop_statements, loop_unsupported = self.parse_static_configure_loop()
                    statements.extend(loop_statements)
                    unsupported = unsupported or loop_unsupported
                except UnsupportedSyntax:
                    unsupported = True
                    self.skip_to_object_boundary(loop_start, boundaries=(";", "}"))
                continue
            statement_start = self.index
            try:
                if self.current().value in {"const", "let", "var"}:
                    self.take()
                    name = self.take()
                    if name.kind != "identifier":
                        raise UnsupportedSyntax("configure local name must be an identifier")
                    while self.current().value not in {"=", ";", "}"}:
                        self.take()
                    self.take("=")
                    local_value = self.parse_value()
                    locals_[name.value] = local_value
                    self.constants[name.value] = local_value
                else:
                    if self.current().value == "await":
                        self.take()
                    statements.append(self.parse_value())
                if self.current().value == ";":
                    self.take()
                elif self.current().value != "}":
                    raise UnsupportedSyntax("configure statement must end with a semicolon")
            except UnsupportedSyntax:
                unsupported = True
                self.skip_to_object_boundary(statement_start, boundaries=(";", "}"))
                if self.current().value == ";":
                    self.take()
        if self.current().value != "}":
            raise UnsupportedSyntax("unclosed configure callback")
        self.take("}")
        value: dict[str, Any] = {"__configure__": statements, "__locals__": locals_}
        if unsupported:
            value["__unsupported__"] = "configure"
        return value

    def parse_static_configure_loop(self) -> tuple[list[Any], bool]:
        """Expand a loop over a statically known list without evaluating JavaScript."""
        self.take("for")
        self.take("(")
        declaration = self.take()
        if declaration.value not in {"const", "let", "var"}:
            raise UnsupportedSyntax("configure loop requires a variable declaration")
        variable = self.take()
        if variable.kind != "identifier" or self.take().value != "of":
            raise UnsupportedSyntax("configure loop requires a static of expression")
        values = self.parse_value()
        self.take(")")
        self.take("{")

        body_start = self.index
        depth = 1
        while self.current().kind != "eof" and depth:
            token = self.take()
            if token.value == "{":
                depth += 1
            elif token.value == "}":
                depth -= 1
        if depth:
            raise UnsupportedSyntax("unclosed configure loop")
        body_end = self.index - 1
        if not isinstance(values, list) or not values:
            raise UnsupportedSyntax("configure loop iterable must be a non-empty static list")

        statements: list[Any] = []
        unsupported = False
        body = self.tokens[body_start:body_end]
        for item in values:
            nested = _ObjectParser(body, constants={**(self.constants or {}), variable.value: item})
            parsed = nested.parse_configure_statements()
            statements.extend(parsed[0])
            unsupported = unsupported or parsed[1]
        return statements, unsupported

    def parse_configure_statements(self) -> tuple[list[Any], bool]:
        """Parse configure statements from a token slice until end-of-input."""
        statements: list[Any] = []
        locals_: dict[str, Any] = {}
        unsupported = False
        if self.constants is None:
            self.constants = {}
        while self.current().kind != "eof":
            if self.current().value == ";":
                self.take()
                continue
            if self.current().value == "try":
                statement_start = self.index
                try:
                    try_statements, try_unsupported = self.parse_static_configure_try()
                    statements.extend(try_statements)
                    unsupported = unsupported or try_unsupported
                except UnsupportedSyntax:
                    unsupported = True
                    self.skip_to_object_boundary(statement_start, boundaries=(";",))
                continue
            if self.current().value == "for":
                loop_start = self.index
                try:
                    loop_statements, loop_unsupported = self.parse_static_configure_loop()
                    statements.extend(loop_statements)
                    unsupported = unsupported or loop_unsupported
                except UnsupportedSyntax:
                    unsupported = True
                    self.skip_to_object_boundary(loop_start, boundaries=(";",))
                continue
            statement_start = self.index
            try:
                if self.current().value in {"const", "let", "var"}:
                    self.take()
                    name = self.take()
                    if name.kind != "identifier":
                        raise UnsupportedSyntax("configure local name must be an identifier")
                    while self.current().value not in {"=", ";"}:
                        self.take()
                    self.take("=")
                    local_value = self.parse_value()
                    locals_[name.value] = local_value
                    self.constants[name.value] = local_value
                else:
                    if self.current().value == "await":
                        self.take()
                    statements.append(self.parse_value())
                if self.current().value == ";":
                    self.take()
                elif self.current().kind != "eof":
                    raise UnsupportedSyntax("configure statement must end with a semicolon")
            except UnsupportedSyntax:
                unsupported = True
                self.skip_to_object_boundary(statement_start, boundaries=(";",))
                if self.current().value == ";":
                    self.take()
        return statements, unsupported

    def parse_static_configure_try(self) -> tuple[list[Any], bool]:
        """Extract static configure calls from a try block without executing it."""
        self.take("try")
        body = self.take_block_tokens()
        statements, unsupported = _ObjectParser(
            body,
            constants={**(self.constants or {})},
        ).parse_configure_statements()

        saw_handler = False
        if self.current().value == "catch":
            saw_handler = True
            self.take("catch")
            if self.current().value == "(":
                self.skip_balanced("(", ")")
            catch_body = self.take_block_tokens()
            unsupported = unsupported or bool(catch_body)
        if self.current().value == "finally":
            saw_handler = True
            self.take("finally")
            finally_body = self.take_block_tokens()
            unsupported = unsupported or bool(finally_body)
        if not saw_handler:
            raise UnsupportedSyntax("configure try requires catch or finally")
        return statements, unsupported

    def take_block_tokens(self) -> list[Token]:
        """Consume a brace-delimited block and return its inner tokens."""
        self.take("{")
        start = self.index
        depth = 1
        while self.current().kind != "eof" and depth:
            token = self.take()
            if token.value == "{":
                depth += 1
            elif token.value == "}":
                depth -= 1
        if depth:
            raise UnsupportedSyntax("unclosed configure block")
        return self.tokens[start : self.index - 1]

    def looks_like_generic_call(self) -> bool:
        """Distinguish TypeScript generic calls from comparison operators."""
        depth = 0
        index = self.index
        while index < len(self.tokens):
            value = self.tokens[index].value
            if value == "<":
                depth += 1
            elif value == ">":
                depth -= 1
                if depth == 0:
                    next_value = self.tokens[index + 1].value if index + 1 < len(self.tokens) else ""
                    return next_value in {"(", ".", "?."}
            elif depth and value in {";", ")", "]", "}"}:
                return False
            index += 1
        return False

    def skip_to_object_boundary(self, start: int, boundaries: tuple[str, ...] = (",", "}")) -> None:
        """Skip one unsupported property value without crossing its object."""
        stack: list[str] = []
        pairs = {
            ")": "(",
            "]": "[",
            "}": "{",
        }
        for token in self.tokens[start : self.index]:
            if token.value in ("(", "[", "{"):
                stack.append(token.value)
            elif token.value in pairs and stack and stack[-1] == pairs[token.value]:
                stack.pop()
        while self.current().kind != "eof":
            value = self.current().value
            if not stack and value in boundaries:
                return
            if value in ("(", "[", "{"):
                stack.append(value)
            elif value in pairs:
                if stack and stack[-1] == pairs[value]:
                    stack.pop()
                elif value in boundaries:
                    return
            self.take()

    def skip_to_array_boundary(self, start: int) -> None:
        """Skip one unsupported array item without crossing its enclosing array."""
        stack: list[str] = []
        pairs = {
            ")": "(",
            "]": "[",
            "}": "{",
        }
        index = start
        while index < len(self.tokens):
            value = self.tokens[index].value
            if value in ("(", "[", "{"):
                stack.append(value)
            elif value in pairs:
                if stack and stack[-1] == pairs[value]:
                    stack.pop()
                elif not stack and value == "]":
                    self.index = index
                    return
            elif not stack and value == ",":
                self.index = index
                return
            index += 1
        self.index = len(self.tokens)

    def skip_balanced(self, opening: str, closing: str) -> None:
        self.take(opening)
        depth = 1
        while depth and self.current().kind != "eof":
            token = self.take()
            if token.value == opening:
                depth += 1
            elif token.value == closing:
                depth -= 1
        if depth:
            raise UnsupportedSyntax("unclosed expression")


def _decode_string(value: str) -> str:
    if value.startswith("`"):
        if "${" in value:
            raise UnsupportedSyntax("template interpolation is not static")
        return value[1:-1]
    try:
        return ast.literal_eval(value)
    except (SyntaxError, ValueError) as exc:
        raise UnsupportedSyntax(f"invalid string {value!r}") from exc


def _validate_with_tree_sitter(text: str) -> bool:
    """Validate syntax when optional tree-sitter dependencies are installed."""
    try:
        from tree_sitter import Language, Parser  # type: ignore
        from tree_sitter_typescript import language_typescript  # type: ignore
    except ImportError:
        return False
    language = language_typescript()
    try:
        language = Language(language)
    except TypeError:
        pass
    parser = Parser(language)
    tree = parser.parse(text.encode())
    return not tree.root_node.has_error


def _find_assignments(
    tokens: list[Token],
    names: set[str],
    constants: dict[str, Any] | None = None,
) -> list[tuple[Token, Any]]:
    found: list[tuple[Token, Any]] = []
    for index, token in enumerate(tokens):
        if token.kind != "identifier" or token.value not in names:
            continue
        equals = index + 1
        # TypeScript declarations commonly contain a type annotation between
        # the variable name and the assignment operator.
        while equals < len(tokens) and tokens[equals].value not in ("=", ";", "{") and equals - index < 40:
            equals += 1
        if equals >= len(tokens) or tokens[equals].value != "=":
            continue
        start = equals + 1
        if tokens[start].value not in ("{", "["):
            continue
        end = _matching_index(tokens, start)
        if end is None:
            continue
        parser = _ObjectParser(tokens[start : end + 1], constants={**(constants or {})})
        try:
            found.append((token, parser.parse_value()))
        except UnsupportedSyntax:
            found.append((token, None))
    return found


def _contains_dynamic_value(value: Any) -> bool:
    """Return whether a parsed literal contains an executable parser node."""
    if isinstance(value, list):
        return any(_contains_dynamic_value(item) for item in value)
    if isinstance(value, dict):
        if any(key.startswith("__") for key in value):
            return True
        return any(_contains_dynamic_value(item) for item in value.values())
    return False


def _find_static_constants(tokens: list[Token]) -> dict[str, Any]:
    """Collect literal variable declarations without evaluating expressions."""
    constants: dict[str, Any] = {}
    for index, token in enumerate(tokens):
        if token.value not in {"const", "let", "var"}:
            continue
        name_index = index + 1
        while name_index < len(tokens) and tokens[name_index].value not in {"=", ";"}:
            name_index += 1
        if name_index >= len(tokens) or tokens[name_index].value != "=" or name_index == index + 1:
            continue
        name = tokens[index + 1]
        if name.kind != "identifier":
            continue
        start = name_index + 1
        end = start
        depth = 0
        while end < len(tokens):
            value = tokens[end].value
            if value in {"{", "[", "("}:
                depth += 1
            elif value in {"}", "]", ")"}:
                depth -= 1
            if depth == 0 and value == ";":
                break
            end += 1
        if start >= end:
            continue
        parser = _ObjectParser(tokens[start:end], constants={**constants})
        try:
            value = parser.parse_value()
        except UnsupportedSyntax:
            continue
        if parser.current().kind != "eof" or _contains_dynamic_value(value):
            continue
        constants[name.value] = value
    return constants


def _matching_index(tokens: list[Token], start: int) -> int | None:
    opening = tokens[start].value
    closing = "}" if opening == "{" else "]"
    depth = 0
    for index in range(start, len(tokens)):
        if tokens[index].value == opening:
            depth += 1
        elif tokens[index].value == closing:
            depth -= 1
            if depth == 0:
                return index
    return None


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _identifier(value: Any) -> str | None:
    if isinstance(value, dict) and set(value) == {"__identifier__"}:
        return str(value["__identifier__"])
    return _string(value)


def _call_name(value: Any) -> str | None:
    if isinstance(value, dict) and "__call__" in value:
        return str(value["__call__"])
    return _identifier(value)


def _expose(value: Any) -> Expose | None:
    if isinstance(value, dict) and "__fluent__" in value:
        expose = _expose(value["__fluent__"])
        if expose is None:
            return None
        supported_methods = {
            "withUnit",
            "withDescription",
            "withValueMin",
            "withValueMax",
            "withValueStep",
            "withEndpoint",
            "withCategory",
            "withAccess",
            "setAccess",
            "withProperty",
            "withLabel",
            "withBrightness",
            "withColorTemp",
            "withColor",
            "withState",
            "withFeature",
            "withFeatures",
            "withSetpoint",
            "withLocalTemperature",
            "withSystemMode",
            "withRunningState",
        }
        for method in value.get("methods", []):
            name = method.get("name")
            args = method.get("args", [])
            if name not in supported_methods:
                return None
            first = args[0] if args else None
            if name == "withUnit":
                expose = replace(expose, unit=_static_text(first))
            elif name == "withDescription":
                expose = replace(expose, description=_static_text(first))
            elif name == "withValueMin" and isinstance(first, (int, float)):
                expose = replace(expose, value_min=first)
            elif name == "withValueMax" and isinstance(first, (int, float)):
                expose = replace(expose, value_max=first)
            elif name == "withValueStep" and isinstance(first, (int, float)):
                expose = replace(expose, value_step=first)
            elif name == "withEndpoint":
                expose = replace(expose, endpoint=first if isinstance(first, (str, int)) else None)
            elif name == "withCategory":
                expose = replace(expose, category=_static_text(first))
            elif name in {"withAccess", "setAccess"}:
                access = _static_text(first)
                if access:
                    expose = replace(expose, access=tuple(access.lower().split("_")))
            elif name == "withProperty":
                expose = replace(expose, property=_static_text(first))
        return expose
    if isinstance(value, dict) and "__call__" in value:
        call = str(value["__call__"]).rsplit(".", 1)[-1]
        args = value.get("args", [])
        if not isinstance(args, list):
            args = []
        name = next((item for item in args if isinstance(item, str)), call)
        aliases = {
            "temperature": "temperature",
            "humidity": "humidity",
            "pressure": "pressure",
            "illuminance": "illuminance",
            "occupancy": "occupancy",
            "contact": "contact",
            "co2": "co2",
            "pm25": "pm25",
            "battery": "battery",
            "voltage": "voltage",
            "current": "current",
            "power": "power",
            "energy": "energy",
            "switch": "switch",
            "light": "light",
            "cover": "cover",
            "lock": "lock",
            "numeric": "numeric",
            "number": "numeric",
            "binary": "binary",
            "enum": "enum",
            "text": "text",
            "button": "button",
            "action": "button",
            "climate": "climate",
            "fan": "fan",
            "gas": "binary",
            "smoke": "binary",
            "water_leak": "binary",
            "carbon_monoxide": "binary",
            "tamper": "binary",
            "battery_low": "binary",
            "child_lock": "binary",
            "power_apparent": "numeric",
            "power_factor": "numeric",
            "power_reactive": "numeric",
            "device_temperature": "numeric",
            "battery_voltage": "numeric",
            "soil_moisture": "numeric",
            "co2": "numeric",
            "produced_energy": "numeric",
        }
        expose_type = aliases.get(call)
        if expose_type:
            return Expose(type=expose_type, name=name, property=name)
        return None
    if not isinstance(value, dict):
        return None
    kind = _string(value.get("type")) or _string(value.get("name"))
    name = _string(value.get("name")) or _string(value.get("property"))
    if not kind or not name:
        return None
    access = value.get("access", [])
    if isinstance(access, str):
        access = [access]
    if not isinstance(access, list):
        access = []
    vals = value.get("values", [])
    if not isinstance(vals, list):
        vals = []
    return Expose(
        type=kind,
        name=name,
        property=_string(value.get("property")) or name,
        access=tuple(str(item) for item in access if isinstance(item, (str, int))),
        endpoint=value.get("endpoint") if isinstance(value.get("endpoint"), (str, int)) else None,
        unit=_string(value.get("unit")),
        device_class=_string(value.get("deviceClass")) or _string(value.get("device_class")),
        state_class=_string(value.get("stateClass")) or _string(value.get("state_class")),
        value_min=value.get("valueMin") if isinstance(value.get("valueMin"), (int, float)) else None,
        value_max=value.get("valueMax") if isinstance(value.get("valueMax"), (int, float)) else None,
        value_step=value.get("valueStep") if isinstance(value.get("valueStep"), (int, float)) else None,
        values=tuple(vals),
        description=_string(value.get("description")),
        category=_string(value.get("category")),
    )


_MODERN_EXTEND_SENSOR_MACROS: dict[str, tuple[str, str, str, str | None, int | float | None]] = {
    "temperature": ("temperature", "msTemperatureMeasurement", "measuredValue", "°C", 100),
    "humidity": ("humidity", "msRelativeHumidity", "measuredValue", "%", 100),
    "pressure": ("pressure", "msPressureMeasurement", "measuredValue", "kPa", 10),
    "illuminance": ("illuminance", "msIlluminanceMeasurement", "measuredValue", "lx", None),
    "flow": ("flow", "msFlowMeasurement", "measuredValue", "m³/h", 10),
    "soilMoisture": ("soil_moisture", "msSoilMoisture", "measuredValue", "%", 100),
    "windSpeed": ("wind_speed", "msWindSpeed", "measuredValue", "m/s", 100),
    "co2": ("co2", "msCO2", "measuredValue", "ppm", None),
    "pm25": ("pm25", "pm25Measurement", "measuredValue", "µg/m³", None),
}
_SUPPORTED_METADATA_MACROS = {
    "identify",
    "deviceEndpoints",
    "forcePowerSource",
    "forceDeviceType",
    "linkQuality",
    "quirkCheckinInterval",
    "reconfigureReportingsOnDeviceAnnounce",
    "skipDefaultResponse",
    "bindCluster",
    "lumiZigbeeOTA",
}
_STATIC_CLUSTER_IDS = {
    "genBasic": 0x0000,
    "genPowerCfg": 0x0001,
    "genDeviceTempCfg": 0x0002,
    "genIdentify": 0x0003,
    "genGroups": 0x0004,
    "genScenes": 0x0005,
    "genOnOff": 0x0006,
    "genLevelCtrl": 0x0008,
    "genBinaryInput": 0x001F,
    "genAnalogInput": 0x000C,
    "lightingColorCtrl": 0x0300,
    "closuresDoorLock": 0x0101,
    "closuresWindowCovering": 0x0102,
    "hvacThermostat": 0x0201,
    "hvacFanCtrl": 0x0202,
    "hvacUserInterfaceCfg": 0x0204,
    "msTemperatureMeasurement": 0x0402,
    "msPressureMeasurement": 0x0403,
    "msRelativeHumidity": 0x0405,
    "msOccupancySensing": 0x0406,
    "msCO2": 0x040D,
    "pm25Measurement": 0x042A,
    "ssIasZone": 0x0500,
    "seMetering": 0x0702,
    "haElectricalMeasurement": 0x0B04,
}
_STATIC_ZCL_TYPES = {
    "BOOL": "Bool",
    "BOOLEAN": "Bool",
    "BITMAP8": "bitmap8",
    "BITMAP16": "bitmap16",
    "ENUM8": "enum8",
    "ENUM16": "enum16",
    "UINT8": "uint8_t",
    "UINT16": "uint16_t",
    "UINT32": "uint32_t",
    "UINT48": "uint48_t",
    "UINT64": "uint64_t",
    "SINGLE": "Single",
    "FLOAT32": "Single",
    "INT8": "int8s",
    "INT16": "int16s",
    "INT32": "int32s",
    "INT64": "int64s",
    "CHAR_STR": "CharacterString",
    "LONG_CHAR_STR": "LongCharacterString",
    "OCTET_STR": "LVBytes",
    "OCTET_STRING": "LVBytes",
    "BUFFER": "LVBytes",
}


def _static_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and set(value) == {"__identifier__"}:
        return str(value["__identifier__"]).rsplit(".", 1)[-1]
    return None


def _static_value(value: Any) -> str | int | float | None:
    if isinstance(value, (str, int, float)):
        return value
    if isinstance(value, dict) and set(value) == {"__identifier__"}:
        return str(value["__identifier__"]).rsplit(".", 1)[-1]
    if isinstance(value, dict) and isinstance(value.get("ID"), (int, str)):
        return value["ID"]
    if isinstance(value, dict) and isinstance(value.get("ID"), dict):
        return _static_text(value["ID"])
    return None


def _static_cluster_id(value: Any) -> int | None:
    """Resolve a literal cluster ID or a standard ZCL cluster ID reference."""
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 0xFFFF:
        return value
    identifier = _identifier(value)
    if identifier:
        parts = identifier.split(".")
        if len(parts) >= 2 and parts[-1] == "ID":
            return _STATIC_CLUSTER_IDS.get(parts[-2])
    return None


def _static_zcl_type(value: Any) -> str | None:
    """Resolve the subset of zigpy data types representable without JS."""
    identifier = _identifier(value)
    if identifier is None:
        return None
    return _STATIC_ZCL_TYPES.get(identifier.rsplit(".", 1)[-1].upper())


def _static_manufacturer_code(value: Any) -> int | None:
    """Resolve literal manufacturer codes used by custom cluster definitions."""
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 0xFFFF:
        return value
    identifier = _identifier(value)
    if identifier and identifier.rsplit(".", 1)[-1] == "LUMI_UNITED_TECHOLOGY_LTD_SHENZHEN":
        return 0x115F
    if identifier and identifier.rsplit(".", 1)[-1] == "DEVELCO":
        return 0x1015
    return None


def _custom_cluster_spec(call: Any) -> CustomClusterSpec | None:
    """Parse a literal deviceAddCustomCluster schema into the IR."""
    call_name = _call_name(call) if isinstance(call, dict) else None
    if call_name is None or call_name.rsplit(".", 1)[-1] != "deviceAddCustomCluster":
        return None
    args = call.get("args")
    if not isinstance(args, list) or len(args) != 2:
        return None
    name = _static_text(args[0])
    definition = args[1]
    cluster_id = _static_cluster_id(definition.get("ID") if isinstance(definition, dict) else None)
    if not name or cluster_id is None or not isinstance(definition, dict):
        return None
    raw_manufacturer_code = definition.get("manufacturerCode")
    manufacturer_code = _static_manufacturer_code(raw_manufacturer_code) if raw_manufacturer_code is not None else None
    if raw_manufacturer_code is not None and manufacturer_code is None:
        return None
    raw_attributes = definition.get("attributes", {})
    raw_commands = definition.get("commands", {})
    if not isinstance(raw_attributes, dict) or not isinstance(raw_commands, dict):
        return None
    attributes: list[dict[str, Any]] = []
    for key, raw_attribute in raw_attributes.items():
        if not isinstance(key, str) or not isinstance(raw_attribute, dict):
            return None
        attribute_name = _static_text(raw_attribute.get("name")) or key
        attribute_id = _static_value(raw_attribute.get("ID"))
        data_type = _static_zcl_type(raw_attribute.get("type"))
        if not attribute_name or not isinstance(attribute_id, int) or isinstance(attribute_id, bool) or data_type is None:
            return None
        raw_attribute_manufacturer_code = raw_attribute.get("manufacturerCode")
        attribute_manufacturer_code = (
            _static_manufacturer_code(raw_attribute_manufacturer_code)
            if raw_attribute_manufacturer_code is not None
            else None
        )
        if raw_attribute_manufacturer_code is not None and attribute_manufacturer_code is None:
            return None
        attribute = {"name": attribute_name, "id": attribute_id, "type": data_type, "write": raw_attribute.get("write") is True}
        if raw_attribute_manufacturer_code is not None:
            attribute["manufacturer_code"] = attribute_manufacturer_code
        attributes.append(attribute)
    commands: list[dict[str, Any]] = []
    for key, raw_command in raw_commands.items():
        if not isinstance(key, str) or not isinstance(raw_command, dict):
            return None
        command_name = _static_text(raw_command.get("name")) or key
        command_id = _static_value(raw_command.get("ID"))
        parameters = raw_command.get("parameters", [])
        if not command_name or not isinstance(command_id, int) or isinstance(command_id, bool) or not isinstance(parameters, list):
            return None
        parsed_parameters: list[dict[str, str]] = []
        for parameter in parameters:
            if not isinstance(parameter, dict):
                return None
            parameter_name = _static_text(parameter.get("name"))
            data_type = _static_zcl_type(parameter.get("type"))
            if not parameter_name or data_type is None:
                return None
            parsed_parameters.append({"name": parameter_name, "type": data_type})
        command = {"name": command_name, "id": command_id, "parameters": parsed_parameters}
        raw_command_manufacturer_code = raw_command.get("manufacturerCode")
        command_manufacturer_code = (
            _static_manufacturer_code(raw_command_manufacturer_code)
            if raw_command_manufacturer_code is not None
            else None
        )
        if raw_command_manufacturer_code is not None and command_manufacturer_code is None:
            return None
        if raw_command_manufacturer_code is not None:
            command["manufacturer_code"] = command_manufacturer_code
        commands.append(command)
    return CustomClusterSpec(name, cluster_id, manufacturer_code, tuple(attributes), tuple(commands))


def _lumi_cluster_spec() -> CustomClusterSpec:
    """Return the fixed schema used by lumi.modernExtend.addManuSpecificLumiCluster."""
    return CustomClusterSpec(
        "manuSpecificLumi",
        0xFCC0,
        0x115F,
        (
            {"name": "mode", "id": 0x0009, "type": "uint8_t", "write": True},
            {"name": "powerOutageCount", "id": 0x0005, "type": "uint32_t", "write": False},
            {"name": "energy", "id": 0x0095, "type": "uint32_t", "write": False},
            {"name": "voltage", "id": 0x0096, "type": "uint32_t", "write": False},
            {"name": "current", "id": 0x0097, "type": "uint32_t", "write": False},
            {"name": "power", "id": 0x0098, "type": "uint32_t", "write": False},
            {"name": "switchMode", "id": 0x0004, "type": "uint16_t", "write": True},
            {"name": "illuminance", "id": 0x0112, "type": "uint32_t", "write": True},
            {"name": "displayUnit", "id": 0x0114, "type": "uint8_t", "write": True},
            {"name": "movement", "id": 0x0118, "type": "uint8_t", "write": False},
            {"name": "airQuality", "id": 0x0129, "type": "uint8_t", "write": True},
            {"name": "flipIndicatorLight", "id": 0x00F0, "type": "uint8_t", "write": True},
            {"name": "operationMode", "id": 0x0200, "type": "uint8_t", "write": True},
            {"name": "powerOutageMemory", "id": 0x0201, "type": "Bool", "write": True},
            {"name": "ledDisabledNight", "id": 0x0203, "type": "Bool", "write": True},
            {"name": "autoOff", "id": 0x0202, "type": "Bool", "write": True},
            {"name": "detectionInterval", "id": 0x0102, "type": "uint8_t", "write": True},
            {"name": "motionSensitivity", "id": 0x010C, "type": "uint8_t", "write": True},
            {"name": "clickMode", "id": 0x0125, "type": "uint8_t", "write": True},
            {"name": "clickModeAlt", "id": 0x0286, "type": "uint8_t", "write": True},
            {"name": "selftest", "id": 0x0127, "type": "Bool", "write": True},
            {"name": "overloadProtection", "id": 0x020B, "type": "Single", "write": True},
            {"name": "powerOutageMode", "id": 0x0517, "type": "uint8_t", "write": True},
            {"name": "dimmingRangeMin", "id": 0x0515, "type": "uint8_t", "write": True},
            {"name": "dimmingRangeMax", "id": 0x0516, "type": "uint8_t", "write": True},
            {"name": "curtainReverse", "id": 0x0400, "type": "Bool", "write": True},
            {"name": "curtainHandOpen", "id": 0x0401, "type": "Bool", "write": True},
            {"name": "curtainCalibrated", "id": 0x0402, "type": "Bool", "write": True},
        ),
    )


def _lumi_basic_operation_mode_cluster_spec() -> CustomClusterSpec:
    """Return the manufacturer-specific attributes used by single-gang Lumi switches."""
    return CustomClusterSpec(
        "genBasic",
        0x0000,
        attributes=(
            {"name": "lumiOperationModeLeft", "id": 0xFF22, "type": "uint8_t", "write": True, "manufacturer_code": 0x115F},
            {"name": "lumiOperationModeRight", "id": 0xFF23, "type": "uint8_t", "write": True, "manufacturer_code": 0x115F},
        ),
    )


def _ikea_unknown_cluster_spec() -> CustomClusterSpec:
    """Return IKEA's empty manufacturer-specific cluster schema."""
    return CustomClusterSpec("manuSpecificIkeaUnknown", 0xFC7C, 0x117C)


def _develco_gen_basic_cluster_spec() -> CustomClusterSpec:
    """Return Develco's manufacturer-specific Basic cluster schema."""
    manufacturer_code = 0x1015
    return CustomClusterSpec(
        "genBasic",
        0x0000,
        attributes=(
            {"name": "develcoPrimarySwVersion", "id": 0x8000, "type": "LVBytes", "write": True, "manufacturer_code": manufacturer_code},
            {"name": "develcoPrimaryHwVersion", "id": 0x8020, "type": "LVBytes", "write": True, "manufacturer_code": manufacturer_code},
            {"name": "develcoLedControl", "id": 0x8100, "type": "bitmap8", "write": True, "manufacturer_code": manufacturer_code},
            {"name": "develcoTxPower", "id": 0x8101, "type": "enum8", "write": True, "manufacturer_code": manufacturer_code},
        ),
    )


def _develco_ias_zone_cluster_spec() -> CustomClusterSpec:
    """Return Develco's manufacturer-specific IAS Zone schema."""
    return CustomClusterSpec(
        "ssIasZone",
        0x0500,
        attributes=(
            {"name": "develcoZoneStatusInterval", "id": 0x8000, "type": "uint16_t", "write": True, "manufacturer_code": 0x1015},
            {"name": "develcoAlarmOffDelay", "id": 0x8001, "type": "uint16_t", "write": True, "manufacturer_code": 0x1015},
        ),
    )


def _develco_air_quality_cluster_spec() -> CustomClusterSpec:
    """Return Develco's manufacturer-specific air-quality schema."""
    return CustomClusterSpec(
        "manuSpecificDevelcoAirQuality",
        0xFC03,
        0x1015,
        (
            {"name": "measuredValue", "id": 0x0000, "type": "uint16_t", "write": True},
            {"name": "minMeasuredValue", "id": 0x0001, "type": "uint16_t", "write": True},
            {"name": "maxMeasuredValue", "id": 0x0002, "type": "uint16_t", "write": True},
            {"name": "resolution", "id": 0x0003, "type": "uint16_t", "write": True},
        ),
    )


def _develco_se_metering_cluster_spec() -> CustomClusterSpec:
    """Return Develco's manufacturer-specific metering schema."""
    return CustomClusterSpec(
        "seMetering",
        0x0702,
        attributes=(
            {"name": "develcoPulseConfiguration", "id": 0x0300, "type": "uint16_t", "write": True, "manufacturer_code": 0x1015},
            {"name": "develcoCurrentSummation", "id": 0x0301, "type": "uint48_t", "write": True, "manufacturer_code": 0x1015},
            {"name": "develcoInterfaceMode", "id": 0x0302, "type": "enum16", "write": True, "manufacturer_code": 0x1015},
        ),
    )


def _endpoint_map(value: Any) -> dict[str, int]:
    """Return a static endpoint name-to-id map, if one is available."""
    if not isinstance(value, dict) or set(value) != {"__endpoint_map__"}:
        return {}
    endpoints = value["__endpoint_map__"]
    if not isinstance(endpoints, dict):
        return {}
    return {
        str(name): endpoint
        for name, endpoint in endpoints.items()
        if isinstance(name, str) and isinstance(endpoint, int) and not isinstance(endpoint, bool)
    }


def _endpoint_map_for_extend(call: Any) -> dict[str, int]:
    """Extract a static endpoint map from a deviceEndpoints extend."""
    call_name = _call_name(call)
    if not call_name or call_name.rsplit(".", 1)[-1] != "deviceEndpoints":
        return {}
    args = _call_args(call)
    endpoints = args.get("endpoints")
    if not isinstance(endpoints, dict):
        return {}
    return {
        str(name): endpoint
        for name, endpoint in endpoints.items()
        if isinstance(name, str) and isinstance(endpoint, int) and not isinstance(endpoint, bool)
    }


def _resolve_endpoint(value: str | int | None, endpoints: dict[str, int]) -> str | int | None:
    if isinstance(value, str):
        return endpoints.get(value, value)
    return value


def _call_args(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    args = value.get("args", [])
    if args and isinstance(args[0], dict) and "__call__" not in args[0] and "__identifier__" not in args[0]:
        return args[0]
    return {}


def _is_predicate(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {"__predicate__"} and isinstance(value["__predicate__"], dict)


def predicate_matches(value: Any, manufacturer_name: str | None) -> bool:
    """Evaluate only the parser's data-only predicate representation."""
    if manufacturer_name is None or not _is_predicate(value):
        return False
    predicate = value["__predicate__"]
    if predicate.get("op") == "equals":
        return manufacturer_name == predicate.get("value")
    if predicate.get("op") == "not_equals":
        return manufacturer_name != predicate.get("value")
    if predicate.get("op") == "not_in":
        values = predicate.get("values")
        return isinstance(values, list) and manufacturer_name not in values
    return False


def _tuya_dp_type(value: Any) -> str | None:
    type_name = _static_text(value)
    if type_name in {"raw", "bool", "number", "string", "enum", "bitmap"}:
        return type_name
    return None


def _tuya_dp_lookup(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not value:
        return None
    result: dict[str, Any] = {}
    for exposed, raw in value.items():
        if not isinstance(exposed, str) or not isinstance(raw, (str, int, float, bool)):
            return None
        result[str(raw)] = exposed
    return result


def _tuya_dp_expose(kind: str, args: dict[str, Any]) -> tuple[Expose | None, str | None]:
    custom = _expose(args.get("expose")) if "expose" in args else None
    name = _static_text(args.get("name"))
    if custom is not None:
        return custom, custom.name
    if not name:
        return None, None
    read_only = args.get("readOnly") is True
    access = ("state",) if read_only else ("state", "set")
    description = _static_text(args.get("description"))
    endpoint = _static_value(args.get("endpoint"))
    if kind == "dpEnumLookup":
        lookup = args.get("lookup")
        values = tuple(str(item) for item in lookup if isinstance(item, str)) if isinstance(lookup, dict) else ()
        return Expose("enum", name, name, access, endpoint=endpoint, values=values, description=description), name
    if kind == "dpBinary":
        return Expose("binary", name, name, access, endpoint=endpoint, description=description), name
    return (
        Expose(
            "numeric",
            name,
            name,
            access,
            endpoint=endpoint,
            unit=_static_text(args.get("unit")),
            value_min=args.get("valueMin") if isinstance(args.get("valueMin"), (int, float)) else None,
            value_max=args.get("valueMax") if isinstance(args.get("valueMax"), (int, float)) else None,
            value_step=args.get("valueStep") if isinstance(args.get("valueStep"), (int, float)) else None,
            description=description,
        ),
        name,
    )


def _tuya_dp_extend(kind: str, args: dict[str, Any]) -> tuple[list[Expose], list[Binding], str, bool]:
    dp = args.get("dp")
    type_name = _tuya_dp_type(args.get("type"))
    expose, name = _tuya_dp_expose(kind, args)
    unsupported = False
    if not isinstance(dp, int) or isinstance(dp, bool) or not 0 <= dp <= 0xFF or expose is None or name is None:
        unsupported = True
    if type_name is None:
        unsupported = True
    if args.get("skip") not in (None, False):
        unsupported = True
    expression: Expression | None = None
    if kind == "dpEnumLookup":
        lookup = _tuya_dp_lookup(args.get("lookup"))
        if lookup is None:
            unsupported = True
        else:
            expression = Expression("lookup", (lookup,))
    elif kind == "dpBinary":
        value_on = args.get("valueOn")
        value_off = args.get("valueOff")
        if not isinstance(value_on, list) or len(value_on) != 2 or not isinstance(value_off, list) or len(value_off) != 2:
            unsupported = True
        elif not isinstance(value_on[0], (str, int, float, bool)) or not isinstance(value_off[0], (str, int, float, bool)):
            unsupported = True
        else:
            expression = Expression("lookup", ({str(value_on[1]): value_on[0], str(value_off[1]): value_off[0]},))
    else:
        scale = args.get("scale")
        if isinstance(scale, (int, float)) and not isinstance(scale, bool) and scale != 0:
            expression = Expression("divide", (scale,))
        elif (
            isinstance(scale, list)
            and len(scale) == 4
            and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in scale)
            and scale[0] != scale[1]
            and scale[2] != scale[3]
        ):
            expression = Expression("map_range", tuple(scale))
        elif scale is not None and not isinstance(scale, (int, float)):
            unsupported = True
        elif isinstance(scale, list):
            unsupported = True
    if type_name is None or not isinstance(dp, int) or expose is None or name is None:
        return ([expose] if expose is not None else []), [], kind, False
    converter = f"tuya_dp.{name}"
    binding_kwargs = {
        "converter": converter,
        "cluster": "manuSpecificTuya",
        "attribute": "dpValues",
        "dp": dp,
        "data_type": type_name,
        "endpoint": expose.endpoint,
        "expression": expression,
    }
    bindings = [Binding(direction="report", **binding_kwargs)]
    if "set" in expose.access:
        bindings.append(Binding(direction="command", **binding_kwargs))
    return [expose], bindings, kind, not unsupported


def _fingerprints(value: Any) -> list[dict[str, str]]:
    """Expand static fingerprint helpers without importing converter code."""
    result: list[dict[str, str]] = []
    values = value if isinstance(value, list) else [{"__spread__": value}]
    for item in values:
        call = item.get("__spread__") if isinstance(item, dict) else None
        if isinstance(call, dict) and call.get("__call__") == "tuya.fingerprint":
            args = call.get("args", [])
            if len(args) == 2 and isinstance(args[0], str) and isinstance(args[1], list):
                for manufacturer in args[1]:
                    if isinstance(manufacturer, str):
                        result.append({"modelID": args[0], "manufacturerName": manufacturer})
            continue
        if isinstance(item, dict) and item.get("__call__") == "tuya.fingerprint":
            args = item.get("args", [])
            if len(args) == 2 and isinstance(args[0], str) and isinstance(args[1], list):
                for manufacturer in args[1]:
                    if isinstance(manufacturer, str):
                        result.append({"modelID": args[0], "manufacturerName": manufacturer})
            continue
        if not isinstance(item, dict):
            continue
        model_id = _static_text(item.get("modelID"))
        manufacturer = _static_text(item.get("manufacturerName"))
        if model_id and manufacturer:
            result.append({"modelID": model_id, "manufacturerName": manufacturer})
    return result


def _white_label_fingerprints(value: Any, zigbee_models: list[str]) -> list[dict[str, str]]:
    """Extract exact manufacturer fingerprints from static Tuya white labels."""
    if not isinstance(value, list):
        return []
    result: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict) or item.get("__call__") != "tuya.whitelabel":
            continue
        args = item.get("args", [])
        if len(args) < 4 or not isinstance(args[3], list):
            continue
        for manufacturer in args[3]:
            if not isinstance(manufacturer, str):
                continue
            for model_id in zigbee_models:
                result.append({"modelID": model_id, "manufacturerName": manufacturer})
    return result


def _modern_extend(call: Any) -> tuple[list[Expose], list[Binding], str | None, bool]:
    """Expand a safe modernExtend call structurally, never by calling JS."""
    if not isinstance(call, dict) or "__call__" not in call:
        return [], [], None, False
    name = str(call["__call__"]).rsplit(".", 1)[-1]
    args = _call_args(call)
    alias_name = name
    alias_unsupported: set[str] = set()
    if name in {"ledvanceLight", "tuyaLight"}:
        name = "light"
        if call["__call__"].endswith("ledvanceLight") and args.get("color") is True:
            args = {**args, "color": {"modes": ["xy", "hs"]}}
    elif name == "sengledLight":
        alias_unsupported = set(args) - {"colorTemp", "color"}
        if args.get("effect") is True or args.get("powerOnBehavior") is True:
            alias_unsupported.update({key for key in ("effect", "powerOnBehavior") if args.get(key) is True})
        name = "light"
        # Sengled's wrapper disables features that the base light macro would
        # otherwise add by default. The static light entities are equivalent.
        args = {"effect": False, "powerOnBehavior": False, **args}
    elif name == "ikeaLight":
        alias_unsupported = set(args) - {"colorTemp", "color", "ota"}
        for option in (
            "effect",
            "powerOnBehavior",
            "turnsOffAtBrightness1",
            "configureReporting",
            "levelConfig",
            "levelReportingConfig",
            "moveToLevelWithOnOffDisable",
        ):
            if args.get(option) not in (None, False):
                alias_unsupported.add(option)
        if isinstance(args.get("endpointNames"), list) and args["endpointNames"]:
            alias_unsupported.add("endpointNames")
        if isinstance(args.get("colorTemp"), dict) and args["colorTemp"].get("viaColor") is True:
            alias_unsupported.add("colorTemp.viaColor")
        name = "light"
        # ikeaLight(true) means IKEA's standard 250..454 mired range.
        if args.get("colorTemp") is True:
            args = {**args, "colorTemp": {"range": [250, 454]}}
    elif name == "gledoptoLight":
        alias_unsupported = set(args) - {"colorTemp", "color"}
        for option in (
            "powerOnBehavior",
            "turnsOffAtBrightness1",
            "configureReporting",
            "levelConfig",
            "levelReportingConfig",
            "moveToLevelWithOnOffDisable",
        ):
            if args.get(option) not in (None, False):
                alias_unsupported.add(option)
        if isinstance(args.get("endpointNames"), list) and args["endpointNames"]:
            alias_unsupported.add("endpointNames")
        name = "light"
    elif name == "ledvanceOnOff":
        name = "onOff"
    if name in _SUPPORTED_METADATA_MACROS:
        return [], [], name, True
    if name == "deviceAddCustomCluster":
        return [], [], name, _custom_cluster_spec(call) is not None
    if name == "addManuSpecificLumiCluster":
        return [], [], name, True
    if name == "addCustomClusterManuSpecificIkeaUnknown":
        return [], [], name, True
    if name == "addCustomClusterManuSpecificDevelcoGenBasic":
        return [], [], name, True
    if name in {
        "addCustomClusterManuSpecificDevelcoIasZone",
        "addCustomClusterManuSpecificDevelcoAirQuality",
        "addCustomDevelcoSeMeteringCluster",
    }:
        return [], [], name, True
    if name == "readGenBasicPrimaryVersions":
        return [], [], name, True
    if name == "addTuyaCommonPrivateCluster":
        # The cluster schema is fixed in lib/tuya.ts. Keep this macro separate
        # from generic custom-cluster parsing until a declarative schema path
        # is available for arbitrary vendor clusters.
        return [], [], name, True
    if name == "lumiOnOff":
        return _lumi_on_off_extend(args)
    lumi_simple = _lumi_simple_extend(name, args)
    if lumi_simple is not None:
        exposes, bindings = lumi_simple
        return exposes, bindings, name, True
    if name == "tuyaBase":
        unsupported = set(args) - {"dp", "queryOnConfigure", "bindBasicOnConfigure"}
        for option in ("queryOnConfigure", "bindBasicOnConfigure"):
            if option in args and args[option] not in (None, True, False):
                unsupported.add(option)
        if args.get("dp") is True:
            return [], [Binding("tuya_datapoints", "manuSpecificTuya", "dpValues", direction="event")], name, not unsupported
        return [], [], name, not unsupported
    if name in {"dpEnumLookup", "dpBinary", "dpNumeric"}:
        return _tuya_dp_extend(name, args)
    if name == "dpOnOff":
        exposes, bindings, macro, supported = _tuya_dp_extend(
            "dpBinary",
            {"name": "state", "type": {"__identifier__": "tuya.dataTypes.bool"}, "valueOn": ["ON", True], "valueOff": ["OFF", False], **args},
        )
        if exposes:
            exposes[0] = replace(exposes[0], type="switch")
        return exposes, bindings, macro, supported
    if name in {
        "dpTemperature",
        "dpHumidity",
        "dpBattery",
        "dpBatteryState",
        "dpTemperatureUnit",
        "dpContact",
        "dpAction",
        "dpIlluminance",
        "dpGas",
        "dpPowerOnBehavior",
    }:
        wrapper = name
        defaults: dict[str, Any]
        kind: str
        expose_type: str | None = None
        if wrapper == "dpTemperature":
            defaults, kind, expose_type = {"name": "temperature", "type": {"__identifier__": "tuya.dataTypes.number"}, "readOnly": True, "scale": 10, "unit": "°C"}, "dpNumeric", "temperature"
        elif wrapper == "dpHumidity":
            defaults, kind, expose_type = {"name": "humidity", "type": {"__identifier__": "tuya.dataTypes.number"}, "readOnly": True, "unit": "%"}, "dpNumeric", "humidity"
        elif wrapper == "dpBattery":
            defaults, kind, expose_type = {"name": "battery", "type": {"__identifier__": "tuya.dataTypes.number"}, "readOnly": True, "unit": "%"}, "dpNumeric", "battery"
        elif wrapper == "dpBatteryState":
            defaults, kind, expose_type = {"name": "battery_state", "type": {"__identifier__": "tuya.dataTypes.number"}, "readOnly": True, "lookup": {"low": 0, "medium": 1, "high": 2}}, "dpEnumLookup", "enum"
        elif wrapper == "dpTemperatureUnit":
            defaults, kind, expose_type = {"name": "temperature_unit", "type": {"__identifier__": "tuya.dataTypes.enum"}, "readOnly": True, "lookup": {"celsius": 0, "fahrenheit": 1}}, "dpEnumLookup", "enum"
        elif wrapper == "dpContact":
            invert = args.get("invert") is True
            defaults, kind, expose_type = {
                "name": "contact",
                "type": {"__identifier__": "tuya.dataTypes.bool"},
                "readOnly": True,
                "valueOn": [True, True if invert else False],
                "valueOff": [False, False if invert else True],
            }, "dpBinary", "contact"
        elif wrapper == "dpAction":
            defaults, kind, expose_type = {"name": "action", "type": {"__identifier__": "tuya.dataTypes.number"}, "readOnly": True}, "dpEnumLookup", "button"
        elif wrapper == "dpIlluminance":
            defaults, kind, expose_type = {"name": "illuminance", "type": {"__identifier__": "tuya.dataTypes.number"}, "readOnly": True}, "dpNumeric", "illuminance"
        elif wrapper == "dpGas":
            invert = args.get("invert") is True
            defaults, kind, expose_type = {
                "name": "gas",
                "type": {"__identifier__": "tuya.dataTypes.enum"},
                "readOnly": True,
                "valueOn": [True, 1 if not invert else 0],
                "valueOff": [False, 0 if not invert else 1],
            }, "dpBinary", "binary"
        else:
            defaults, kind, expose_type = {
                "name": "power_on_behavior",
                "type": {"__identifier__": "tuya.dataTypes.enum"},
                "lookup": {"off": 0, "on": 1, "previous": 2},
            }, "dpEnumLookup", "enum"
        exposes, bindings, _, supported = _tuya_dp_extend(kind, {**defaults, **args})
        if exposes and expose_type:
            exposes[0] = replace(exposes[0], type=expose_type)
        return exposes, bindings, wrapper, supported
    if name in _MODERN_EXTEND_SENSOR_MACROS:
        expose_name, cluster, attribute, unit, scale = _MODERN_EXTEND_SENSOR_MACROS[name]
        expose = Expose("numeric", expose_name, expose_name, ("state",), unit=unit)
        if _is_identity_expression(args.get("scale")):
            return [expose], [Binding(name, cluster, attribute, direction="report")], name, True
        expression = Expression("divide", (scale,)) if scale else None
        return [expose], [Binding(name, cluster, attribute, direction="report", expression=expression)], name, True
    if name == "onOff":
        expose = Expose("switch", "state", "state", ("state", "set"))
        return [expose], [Binding("on_off", "genOnOff", "onOff", direction="report")], name, True
    if name == "tuyaOnOff":
        expose = Expose("switch", "state", "state", ("state", "set"))
        supported_options = {
            "endpoints",
            "switchType",
            "switchTypeCurtain",
            "onOffCountdown",
            "powerOutageMemory",
            "powerOnBehavior2",
            "powerOnBehavior3",
            "electricalMeasurements",
            "electricalMeasurementsFzConverter",
            "indicatorMode",
            "indicatorModeNoneRelayPos",
            "childLock",
            "switchTypeButton",
            "switchMode",
            "inchingSwitch",
            "backlightModeOffNormalInverted",
            "backlightModeLowMediumHigh",
            "backlightModeOffOn",
        }
        unsupported_options = set(args) - supported_options
        endpoint_names = args.get("endpoints")
        if endpoint_names is not None and (
            not isinstance(endpoint_names, list)
            or not endpoint_names
            or not all(isinstance(endpoint, str) for endpoint in endpoint_names)
        ):
            unsupported_options.add("endpoints")
            endpoint_names = None
        if endpoint_names:
            exposes = [replace(expose, endpoint=endpoint) for endpoint in endpoint_names]
            bindings = [
                Binding("on_off", "genOnOff", "onOff", direction="report", endpoint=endpoint)
                for endpoint in endpoint_names
            ]
        else:
            exposes = [expose]
            bindings = [Binding("on_off", "genOnOff", "onOff", direction="report")]
        if "onOffCountdown" in args and args["onOffCountdown"] is True:
            countdown_expose = Expose(
                "numeric",
                "countdown",
                "countdown",
                ("state", "set"),
                unit="s",
                value_min=0,
                value_max=43200,
                value_step=1,
            )
            countdown_endpoints = endpoint_names or [None]
            for endpoint in countdown_endpoints:
                exposes.append(replace(countdown_expose, endpoint=endpoint))
                bindings.extend(
                    [
                        Binding("on_off_countdown", "genOnOff", "onTime", direction="report", endpoint=endpoint),
                        Binding("on_off_countdown", "genOnOff", command="state", direction="command", endpoint=endpoint),
                        Binding(
                            "on_off_countdown",
                            "genOnOff",
                            command="onWithTimedOff",
                            direction="command",
                            endpoint=endpoint,
                        ),
                    ]
                )
        elif "onOffCountdown" in args and not _is_predicate(args["onOffCountdown"]):
            unsupported_options.add("onOffCountdown")
        if args.get("switchType") is True:
            exposes.append(
                Expose(
                    "enum",
                    "switch_type",
                    "switch_type",
                    ("state", "set"),
                    values=("toggle", "state", "momentary"),
                    category="config",
                )
            )
            switch_type_expression = Expression("lookup", ({"0": "toggle", "1": "state", "2": "momentary"},))
            bindings.extend(
                [
                    Binding("switch_type", "manuSpecificTuya3", "switchType", direction="report", expression=switch_type_expression),
                    Binding("switch_type", "manuSpecificTuya3", "switchType", direction="command", expression=switch_type_expression),
                ]
            )
        if args.get("switchTypeCurtain") is True:
            exposes.append(
                Expose(
                    "enum",
                    "switch_type_curtain",
                    "switch_type_curtain",
                    ("state", "set"),
                    values=("flip-switch", "sync-switch", "button-switch", "button2-switch"),
                    category="config",
                )
            )
            switch_type_curtain_expression = Expression(
                "lookup",
                ({"0": "flip-switch", "1": "sync-switch", "2": "button-switch", "3": "button2-switch"},),
            )
            bindings.extend(
                [
                    Binding(
                        "switch_type_curtain",
                        "manuSpecificTuya3",
                        "switchType",
                        direction="report",
                        expression=switch_type_curtain_expression,
                    ),
                    Binding(
                        "switch_type_curtain",
                        "manuSpecificTuya3",
                        "switchType",
                        direction="command",
                        expression=switch_type_curtain_expression,
                    ),
                ]
            )
        # tuyaOnOff uses one of the two static Tuya power-on attributes unless
        # a manufacturer-dependent or otherwise unsupported variant is used.
        if args.get("powerOnBehavior2") is True:
            power_on_expose = Expose(
                "enum",
                "power_on_behavior",
                "power_on_behavior",
                ("state", "set"),
                values=("off", "on", "previous"),
                category="config",
            )
            power_on_behavior_expression = Expression("lookup", ({"0": "off", "1": "on", "2": "previous"},))
            power_endpoints = endpoint_names or [None]
            for endpoint in power_endpoints:
                exposes.append(replace(power_on_expose, endpoint=endpoint))
                bindings.extend(
                    [
                        Binding(
                            "power_on_behavior",
                            "manuSpecificTuya3",
                            "powerOnBehavior",
                            direction="report",
                            endpoint=endpoint,
                            expression=power_on_behavior_expression,
                        ),
                        Binding(
                            "power_on_behavior",
                            "manuSpecificTuya3",
                            "powerOnBehavior",
                            direction="command",
                            endpoint=endpoint,
                            expression=power_on_behavior_expression,
                        ),
                    ]
                )
        elif args.get("powerOutageMemory") is True:
            exposes.append(
                Expose(
                    "enum",
                    "power_outage_memory",
                    "power_outage_memory",
                    ("state", "set"),
                    values=("off", "on", "restore"),
                    category="config",
                )
            )
            power_outage_expression = Expression("lookup", ({"0": "off", "1": "on", "2": "restore"},))
            bindings.extend(
                [
                    Binding("power_outage_memory", "genOnOff", "moesStartUpOnOff", direction="report", expression=power_outage_expression),
                    Binding("power_outage_memory", "genOnOff", "moesStartUpOnOff", direction="command", expression=power_outage_expression),
                ]
            )
        elif (
            not _is_predicate(args.get("powerOutageMemory"))
            and not _is_predicate(args.get("powerOnBehavior2"))
            and ("powerOnBehavior3" not in args or args.get("powerOnBehavior3") is False)
        ):
            exposes.append(
                Expose(
                    "enum",
                    "power_on_behavior",
                    "power_on_behavior",
                    ("state", "set"),
                    values=("off", "on", "previous"),
                    category="config",
                )
            )
            power_on_behavior_expression = Expression("lookup", ({"0": "off", "1": "on", "2": "previous"},))
            bindings.extend(
                [
                    Binding("power_on_behavior", "genOnOff", "moesStartUpOnOff", direction="report", expression=power_on_behavior_expression),
                    Binding("power_on_behavior", "genOnOff", "moesStartUpOnOff", direction="command", expression=power_on_behavior_expression),
                ]
            )
        elif args.get("powerOnBehavior3") is True:
            power_on_behavior_expression = Expression("lookup", ({"0": "off", "1": "on", "2": "previous"},))
            power_endpoints = endpoint_names or [None]
            for endpoint in power_endpoints:
                exposes.append(
                    Expose(
                        "enum",
                        "power_on_behavior",
                        "power_on_behavior",
                        ("state", "set"),
                        endpoint=endpoint,
                        values=("off", "on", "previous"),
                        category="config",
                    )
                )
                bindings.extend(
                    [
                        Binding(
                            "power_on_behavior_3",
                            "manuSpecificTuya",
                            "powerOnBehavior3",
                            direction="report",
                            endpoint=endpoint,
                            expression=power_on_behavior_expression,
                        ),
                        Binding(
                            "power_on_behavior_3",
                            "manuSpecificTuya",
                            "powerOnBehavior3",
                            direction="command",
                            endpoint=endpoint,
                            expression=power_on_behavior_expression,
                        ),
                    ]
                )
        if args.get("electricalMeasurements") is True:
            exposes.extend(
                [
                    Expose("numeric", "power", "power", ("state",), unit="W"),
                    Expose("numeric", "current", "current", ("state",), unit="A"),
                    Expose("numeric", "voltage", "voltage", ("state",), unit="V"),
                    Expose("numeric", "energy", "energy", ("state",), unit="kWh"),
                ]
            )
            bindings.extend(
                [
                    Binding(name, "haElectricalMeasurement", "activePower", direction="report"),
                    Binding(name, "haElectricalMeasurement", "rmsCurrent", direction="report"),
                    Binding(name, "haElectricalMeasurement", "rmsVoltage", direction="report"),
                    Binding(name, "seMetering", "currentSummDelivered", direction="report"),
                ]
            )
        if args.get("indicatorMode") is True:
            exposes.append(
                Expose(
                    "enum",
                    "indicator_mode",
                    "indicator_mode",
                    ("state", "set"),
                    values=("off", "off/on", "on/off", "on"),
                    category="config",
                )
            )
            indicator_expression = Expression("lookup", ({"0": "off", "1": "off/on", "2": "on/off", "3": "on"},))
            bindings.extend(
                [
                    Binding("indicator_mode", "genOnOff", "tuyaBacklightMode", direction="report", expression=indicator_expression),
                    Binding("indicator_mode", "genOnOff", "tuyaBacklightMode", direction="command", expression=indicator_expression),
                ]
            )
        if args.get("indicatorModeNoneRelayPos") is True:
            exposes.append(
                Expose(
                    "enum",
                    "indicator_mode",
                    "indicator_mode",
                    ("state", "set"),
                    values=("none", "relay", "pos"),
                    category="config",
                )
            )
            indicator_none_relay_pos_expression = Expression("lookup", ({"0": "none", "1": "relay", "2": "pos"},))
            bindings.extend(
                [
                    Binding(
                        "indicator_mode_none_relay_pos",
                        "genOnOff",
                        "tuyaBacklightMode",
                        direction="report",
                        expression=indicator_none_relay_pos_expression,
                    ),
                    Binding(
                        "indicator_mode_none_relay_pos",
                        "genOnOff",
                        "tuyaBacklightMode",
                        direction="command",
                        expression=indicator_none_relay_pos_expression,
                    ),
                ]
            )
        if args.get("childLock") is True:
            exposes.append(Expose("binary", "child_lock", "child_lock", ("state", "set"), category="config"))
            child_lock_expression = Expression("lookup", ({"True": "LOCK", "False": "UNLOCK", "1": "LOCK", "0": "UNLOCK"},))
            bindings.extend(
                [
                    Binding("child_lock", "genOnOff", "childLock", direction="report", expression=child_lock_expression),
                    Binding("child_lock", "genOnOff", "childLock", direction="command", expression=child_lock_expression),
                ]
            )
        if args.get("backlightModeOffNormalInverted") is True:
            exposes.append(
                Expose(
                    "enum",
                    "backlight_mode",
                    "backlight_mode",
                    ("state", "set"),
                    values=("off", "normal", "inverted"),
                    category="config",
                )
            )
            backlight_expression = Expression("lookup", ({"0": "off", "1": "normal", "2": "inverted"},))
            bindings.extend(
                [
                    Binding("backlight_mode", "genOnOff", "tuyaBacklightMode", direction="report", expression=backlight_expression),
                    Binding("backlight_mode", "genOnOff", "tuyaBacklightMode", direction="command", expression=backlight_expression),
                ]
            )
        if args.get("backlightModeLowMediumHigh") is True:
            exposes.append(
                Expose(
                    "enum",
                    "backlight_mode",
                    "backlight_mode",
                    ("state", "set"),
                    values=("low", "medium", "high"),
                    category="config",
                )
            )
            backlight_expression = Expression("lookup", ({"0": "low", "1": "medium", "2": "high"},))
            bindings.extend(
                [
                    Binding("backlight_mode", "genOnOff", "tuyaBacklightMode", direction="report", expression=backlight_expression),
                    Binding("backlight_mode", "genOnOff", "tuyaBacklightMode", direction="command", expression=backlight_expression),
                ]
            )
        if args.get("backlightModeOffOn") is True:
            exposes.append(Expose("binary", "backlight_mode", "backlight_mode", ("state", "set"), category="config"))
            backlight_expression = Expression("lookup", ({"0": False, "1": True},))
            bindings.extend(
                [
                    Binding("backlight_mode_off_on", "genOnOff", "tuyaBacklightSwitch", direction="report", expression=backlight_expression),
                    Binding("backlight_mode_off_on", "genOnOff", "tuyaBacklightSwitch", direction="command", expression=backlight_expression),
                ]
            )
        if args.get("switchTypeButton") is True:
            exposes.append(
                Expose(
                    "enum",
                    "switch_type_button",
                    "switch_type_button",
                    ("state", "set"),
                    values=("release", "press"),
                    category="config",
                )
            )
            switch_button_expression = Expression("lookup", ({"0": "release", "1": "press"},))
            bindings.extend(
                [
                    Binding("switch_type_button", "manuSpecificTuya3", "switchType", direction="report", expression=switch_button_expression),
                    Binding("switch_type_button", "manuSpecificTuya3", "switchType", direction="command", expression=switch_button_expression),
                ]
            )
        if args.get("switchMode") is True:
            switch_mode_expression = Expression("lookup", ({"0": "switch", "1": "scene"},))
            switch_mode_endpoints = endpoint_names or [None]
            for endpoint in switch_mode_endpoints:
                exposes.append(
                    Expose(
                        "enum",
                        "switch_mode",
                        "switch_mode",
                        ("state", "set"),
                        endpoint=endpoint,
                        values=("switch", "scene"),
                        category="config",
                    )
                )
                bindings.extend(
                    [
                        Binding(
                            "switch_mode",
                            "manuSpecificTuya3",
                            "switchMode",
                            direction="report",
                            endpoint=endpoint,
                            expression=switch_mode_expression,
                        ),
                        Binding(
                            "switch_mode",
                            "manuSpecificTuya3",
                            "switchMode",
                            direction="command",
                            endpoint=endpoint,
                            expression=switch_mode_expression,
                        ),
                ]
            )
        if args.get("inchingSwitch") is True:
            quantity = len(endpoint_names) if endpoint_names else 1
            for endpoint_number in range(1, quantity + 1):
                exposes.extend(
                    [
                        Expose(
                            "binary",
                            f"inching_control_{endpoint_number}",
                            f"inching_control_{endpoint_number}",
                            ("state", "set"),
                            endpoint=1,
                            category="config",
                        ),
                        Expose(
                            "numeric",
                            f"inching_time_{endpoint_number}",
                            f"inching_time_{endpoint_number}",
                            ("state", "set"),
                            endpoint=1,
                            unit="s",
                            value_min=1,
                            value_max=65535,
                            value_step=1,
                            category="config",
                        ),
                    ]
                )
                bindings.extend(
                    [
                        Binding(
                            f"inching_control_{endpoint_number}",
                            "manuSpecificTuya4",
                            f"inching_control_{endpoint_number}",
                            endpoint=1,
                            direction="report",
                        ),
                        Binding(
                            f"inching_control_{endpoint_number}",
                            "manuSpecificTuya4",
                            f"inching_control_{endpoint_number}",
                            endpoint=1,
                            direction="command",
                        ),
                        Binding(
                            f"inching_time_{endpoint_number}",
                            "manuSpecificTuya4",
                            f"inching_time_{endpoint_number}",
                            endpoint=1,
                            direction="report",
                        ),
                        Binding(
                            f"inching_time_{endpoint_number}",
                            "manuSpecificTuya4",
                            f"inching_time_{endpoint_number}",
                            endpoint=1,
                            direction="command",
                        ),
                    ]
                )
        for option in ("switchType", "switchTypeCurtain", "onOffCountdown", "switchMode", "inchingSwitch", "indicatorModeNoneRelayPos"):
            if option in args and args[option] is not True and not _is_predicate(args[option]):
                if args[option] is not False:
                    unsupported_options.add(option)
        for option in (
            "powerOutageMemory",
            "powerOnBehavior2",
            "powerOnBehavior3",
            "electricalMeasurements",
            "indicatorMode",
            "childLock",
            "switchTypeButton",
            "switchTypeCurtain",
            "switchMode",
            "inchingSwitch",
            "indicatorModeNoneRelayPos",
            "backlightModeOffNormalInverted",
            "backlightModeLowMediumHigh",
            "backlightModeOffOn",
        ):
            if option in args and args[option] is not True and args[option] is not False and not _is_predicate(args[option]):
                unsupported_options.add(option)
        if "electricalMeasurementsFzConverter" in args:
            converter = _call_name(args["electricalMeasurementsFzConverter"])
            if converter != "tuya.fz.TS011F_electrical_measurement":
                unsupported_options.add("electricalMeasurementsFzConverter")
        return exposes, bindings, name, not unsupported_options
    if name == "battery":
        exposes = []
        if args.get("percentage", True) is not False:
            exposes.append(Expose("numeric", "battery", "battery", ("state",), unit="%", category="diagnostic"))
        if args.get("voltage", False) is True:
            exposes.append(Expose("numeric", "voltage", "voltage", ("state",), unit="mV", category="diagnostic"))
        if args.get("lowStatus", False) is True:
            exposes.append(Expose("binary", "battery_low", "battery_low", ("state",), category="diagnostic"))
        bindings = [Binding(name, "genPowerCfg", "batteryPercentageRemaining", direction="report", expression=Expression("divide", (2,)))]
        if args.get("voltage", False) is True:
            bindings.append(Binding(name, "genPowerCfg", "batteryVoltage", direction="report"))
        return exposes, bindings, name, True
    if name == "light":
        exposes = [Expose("light", "light", "state", ("state", "set"))]
        bindings = [
            Binding(name, "genOnOff", "onOff", direction="report"),
            Binding(name, "genLevelCtrl", "currentLevel", direction="report"),
        ]
        if args.get("colorTemp"):
            value_min = value_max = None
            color_temp = args["colorTemp"]
            if isinstance(color_temp, dict):
                color_temp_range = color_temp.get("range")
                if (
                    isinstance(color_temp_range, list)
                    and len(color_temp_range) == 2
                    and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in color_temp_range)
                ):
                    value_min, value_max = color_temp_range
            exposes.append(
                Expose(
                    "numeric",
                    "color_temperature",
                    "color_temperature",
                    ("state", "set"),
                    unit="mired",
                    value_min=value_min,
                    value_max=value_max,
                )
            )
            bindings.append(Binding(name, "lightingColorCtrl", "colorTemperature", direction="report"))
        if args.get("color"):
            exposes.append(Expose("numeric", "color", "color", ("state", "set")))
            bindings.append(Binding(name, "lightingColorCtrl", None, direction="report"))
        return exposes, bindings, alias_name if alias_name != name else name, not alias_unsupported
    if name in {"numeric", "binary", "text", "enumLookup", "actionEnumLookup"}:
        expose_type = {"numeric": "numeric", "binary": "binary", "text": "text", "enumLookup": "enum", "actionEnumLookup": "enum"}[name]
        expose_name = _static_text(args.get("name")) or _static_text(args.get("property"))
        cluster = _static_value(args.get("cluster"))
        attribute = _static_value(args.get("attribute"))
        if not expose_name or not cluster or not attribute:
            return [], [], name, False
        access = _static_text(args.get("access")) or "ALL"
        unit = _static_text(args.get("unit"))
        lookup = args.get("lookup")
        values = tuple(str(item) for item in lookup if isinstance(item, (str, int))) if isinstance(lookup, dict) else ()
        expose = Expose(
            expose_type,
            expose_name,
            expose_name,
            tuple(access.lower().split("_")),
            endpoint=_static_text(args.get("endpointName")) or _static_text(args.get("endpoint")),
            unit=unit,
            value_min=args.get("valueMin") if isinstance(args.get("valueMin"), (int, float)) else None,
            value_max=args.get("valueMax") if isinstance(args.get("valueMax"), (int, float)) else None,
            value_step=args.get("valueStep") if isinstance(args.get("valueStep"), (int, float)) else None,
            values=values,
            description=_static_text(args.get("description")),
            category=_static_text(args.get("entityCategory")),
        )
        scale = args.get("scale")
        expression = Expression("divide", (scale,)) if isinstance(scale, (int, float)) and scale != 0 else None
        return [expose], [Binding(name, cluster, attribute, direction="report", expression=expression)], name, True
    if name == "occupancy":
        return [Expose("occupancy", "occupancy", "occupancy", ("state",))], [Binding(name, "msOccupancySensing", "occupancy", direction="report")], name, True
    if name == "deviceTemperature":
        return [Expose("numeric", "device_temperature", "device_temperature", ("state",), unit="°C", category="diagnostic")], [
            Binding(name, "genDeviceTempCfg", "currentTemperature", direction="report")
        ], name, True
    if name in {"electricityMeter", "gasMeter"}:
        if name == "electricityMeter":
            fields = [
                ("power", "electricalMeasurement", "activePower", "W"),
                ("voltage", "electricalMeasurement", "rmsVoltage", "V"),
                ("current", "electricalMeasurement", "rmsCurrent", "A"),
                ("energy", "metering", "currentSummDelivered", "kWh"),
            ]
        else:
            fields = [("volume_flow_rate", "metering", "instantaneousDemand", "m³/h"), ("gas", "metering", "currentSummDelivered", "m³")]
        exposes = []
        bindings = []
        for field_name, cluster, attribute, unit in fields:
            if args.get(field_name, True) is not False:
                exposes.append(Expose("numeric", field_name, field_name, ("state",), unit=unit))
                bindings.append(Binding(name, cluster, attribute, direction="report"))
        return exposes, bindings, name, True
    if name == "windowCovering":
        controls = args.get("controls", [])
        controls = [item for item in controls if isinstance(item, str)] if isinstance(controls, list) else []
        if not controls:
            return [], [], name, False
        exposes = [Expose("cover", "cover", "cover", ("state", "set"))]
        bindings = [Binding(name, "closuresWindowCovering", "currentPositionLiftPercentage", direction="report")]
        if "tilt" in controls:
            bindings.append(Binding(name, "closuresWindowCovering", "currentPositionTiltPercentage", direction="report"))
        return exposes, bindings, name, True
    if name == "lock":
        if "pinCodeCount" not in args:
            return [], [], name, False
        return [Expose("lock", "lock", "lock_state", ("state", "set"))], [Binding(name, "closuresDoorLock", "lockState", direction="report")], name, True
    if name == "fan":
        return [Expose("fan", "fan", "state", ("state", "set"))], [Binding(name, "hvacFanCtrl", "fanMode", direction="report")], name, True
    if name == "thermostat":
        return [Expose("climate", "climate", "local_temperature", ("state", "set"))], [Binding(name, "hvacThermostat", "localTemp", direction="report")], name, True
    if name == "iasZoneAlarm":
        zone_type = _static_text(args.get("zoneType"))
        expose_type = "contact" if zone_type == "contact" else "occupancy" if zone_type in {"occupancy", "motion"} else "binary"
        property_name = "contact" if expose_type == "contact" else "occupancy" if expose_type == "occupancy" else "alarm"
        return [Expose(expose_type, property_name, property_name, ("state",))], [Binding(name, "ssIasZone", "zoneStatus", direction="report")], name, True
    if name in {"commandsOnOff", "commandsLevelCtrl", "commandsColorCtrl"}:
        defaults = {
            "commandsOnOff": ("genOnOff", ("on", "off", "toggle")),
            "commandsLevelCtrl": ("genLevelCtrl", ("brightness_move_to_level", "brightness_move_up", "brightness_move_down", "brightness_stop")),
            "commandsColorCtrl": ("lightingColorCtrl", ("color_temperature_move_stop", "color_temperature_move_up", "color_temperature_move_down")),
        }
        cluster, default_commands = defaults[name]
        commands = args.get("commands", default_commands)
        if not isinstance(commands, (list, tuple)) or not all(isinstance(item, str) for item in commands):
            return [], [], name, False
        exposes = [Expose("enum", "action", "action", ("state",), values=tuple(commands), category="diagnostic")]
        bindings = [Binding(name, cluster, command=command, direction="event") for command in commands]
        return exposes, bindings, name, True
    return [], [], name, False


def _lumi_simple_extend(name: str, args: dict[str, Any]) -> tuple[list[Expose], list[Binding]] | None:
    """Expand fixed Lumi manufacturer attributes without evaluating JavaScript."""
    definitions: dict[str, tuple[str, int, str, dict[str, Any]]] = {
        "lumiButtonLock": (
            "button_lock",
            0x0200,
            "binary",
            {"ON": 0, "OFF": 1},
        ),
        "lumiPowerOutageMemory": (
            "power_outage_memory",
            0x0201,
            "binary",
            {True: 1, False: 0},
        ),
        "lumiLedDisabledNight": (
            "led_disabled_night",
            0x0203,
            "binary",
            {True: 1, False: 0},
        ),
        "lumiFlipIndicatorLight": (
            "flip_indicator_light",
            0x00F0,
            "binary",
            {"ON": 1, "OFF": 0},
        ),
        "lumiChildLock": (
            "child_lock",
            0x0285,
            "binary",
            {"LOCK": 1, "UNLOCK": 0},
        ),
        "lumiLockRelay": (
            "lock_relay",
            0x0285,
            "binary",
            {True: 1, False: 0},
        ),
        "lumiOperationMode": (
            "operation_mode",
            0x0200,
            "enum",
            {"decoupled": 0, "control_relay": 1},
        ),
        "lumiSwitchType": (
            "switch_type",
            0x000A,
            "enum",
            {"toggle": 1, "momentary": 2, "none": 3},
        ),
        "lumiClickMode": (
            "click_mode",
            0x0125,
            "enum",
            {"fast": 1, "multi": 2},
        ),
        "lumiSwitchMode": (
            "mode_switch",
            0x0004,
            "enum",
            {"quick_mode": 1, "anti_flicker_mode": 4},
        ),
        "lumiLedIndicator": (
            "led_indicator",
            0x0203,
            "binary",
            {"ON": 1, "OFF": 0},
        ),
        "lumiPowerOnBehavior": (
            "power_on_behavior",
            0x0517,
            "enum",
            {"on": 0, "previous": 1, "off": 2, "inverted": 3},
        ),
    }
    definition = definitions.get(name)
    numeric_definitions: dict[str, tuple[str, str, int, str, float | None, float | None, float | None, float | None]] = {
        "lumiDimmingRangeMin": ("dimming_range_minimum", "manuSpecificLumi", 0x0515, "%", 1, 99, 1, None),
        "lumiDimmingRangeMax": ("dimming_range_maximum", "manuSpecificLumi", 0x0516, "%", 2, 100, 1, None),
        "lumiOffOnDuration": ("off_on_duration", "genLevelCtrl", 0x0012, "s", 0, 10, 0.5, 10),
        "lumiOnOffDuration": ("on_off_duration", "genLevelCtrl", 0x0013, "s", 0, 10, 0.5, 10),
    }
    numeric_definition = numeric_definitions.get(name)
    if numeric_definition is not None:
        if any(key not in {"access", "attribute", "description", "entityCategory", "unit", "valueMax", "valueMin", "valueStep"} for key in args):
            return None
        expose_name, cluster, default_attribute, default_unit, default_min, default_max, default_step, scale = numeric_definition
        attribute = _static_value(args.get("attribute")) if "attribute" in args else default_attribute
        if not isinstance(attribute, (int, str)):
            return None
        access_name = _static_text(args.get("access")) or "ALL"
        access = ("state", "set") if access_name == "ALL" else tuple(access_name.lower().split("_"))
        expression = Expression("divide", (scale,)) if scale else None
        return [
            Expose(
                "numeric",
                expose_name,
                expose_name,
                access,
                unit=_static_text(args.get("unit")) or default_unit,
                value_min=args.get("valueMin", default_min),
                value_max=args.get("valueMax", default_max),
                value_step=args.get("valueStep", default_step),
                description=_static_text(args.get("description")),
                category=_static_text(args.get("entityCategory")) or "config",
            )
        ], [
            Binding(name, cluster, attribute, direction="report", expression=expression),
            Binding(name, cluster, attribute, direction="command", expression=expression),
        ]
    if definition is not None or name == "lumiOverloadProtection":
        allowed_args = {"access", "attribute", "description", "endpointName", "entityCategory", "lookup", "valueMax", "valueMin", "valueStep"}
        if any(key not in allowed_args for key in args):
            return None
    if definition is not None:
        expose_name, default_attribute, expose_type, default_lookup = definition
        attribute = _static_value(args.get("attribute")) if "attribute" in args else default_attribute
        lookup = args.get("lookup", default_lookup)
        if not isinstance(attribute, int) or not isinstance(lookup, dict):
            return None
        if not all(isinstance(key, (str, int, bool)) and isinstance(value, (str, int, float, bool)) for key, value in lookup.items()):
            return None
        expression = Expression("lookup", ({str(value): key for key, value in lookup.items()},))
        values = () if expose_type == "binary" else tuple(str(key) for key in lookup)
        access_name = _static_text(args.get("access")) or "ALL"
        access = ("state", "set") if access_name == "ALL" else tuple(access_name.lower().split("_"))
        category = _static_text(args.get("entityCategory")) or "config"
        expose = Expose(
            expose_type,
            expose_name,
            expose_name,
            access,
            values=values,
            endpoint=_static_text(args.get("endpointName")),
            description=_static_text(args.get("description")),
            category=category,
        )
        return [expose], [
            Binding(name, "manuSpecificLumi", attribute, direction="report", expression=expression),
            Binding(name, "manuSpecificLumi", attribute, direction="command", expression=expression),
        ]
    if name == "lumiOverloadProtection":
        attribute = _static_value(args.get("attribute")) if "attribute" in args else 0x020B
        if not isinstance(attribute, int):
            return None
        access_name = _static_text(args.get("access")) or "ALL"
        access = ("state", "set") if access_name == "ALL" else tuple(access_name.lower().split("_"))
        return [
            Expose(
                "numeric",
                "overload_protection",
                "overload_protection",
                access,
                unit="W",
                value_min=100,
                value_max=args.get("valueMax", 3840),
                value_step=args.get("valueStep") if isinstance(args.get("valueStep"), (int, float)) else None,
                endpoint=_static_text(args.get("endpointName")),
                description=_static_text(args.get("description")),
                category=_static_text(args.get("entityCategory")) or "config",
            )
        ], [
            Binding(name, "manuSpecificLumi", attribute, direction="report"),
            Binding(name, "manuSpecificLumi", attribute, direction="command"),
        ]
    if name == "lumiPower":
        if set(args) - {"access", "description", "entityCategory", "unit"}:
            return None
        access_name = _static_text(args.get("access")) or "STATE"
        access = ("state", "set") if access_name == "ALL" else tuple(access_name.lower().split("_"))
        return [
            Expose(
                "numeric",
                "power",
                "power",
                access,
                unit=_static_text(args.get("unit")) or "W",
                description=_static_text(args.get("description")),
                category=_static_text(args.get("entityCategory")) or "diagnostic",
            )
        ], [Binding(name, "genAnalogInput", "presentValue", direction="report")]
    if name == "lumiElectricityMeter":
        if any(key not in {"energy", "voltage", "current"} for key in args) or any(
            not isinstance(value, bool) for value in args.values()
        ):
            return None
        fields = {
            "energy": ("energy", 0x0095, "kWh", None),
            "voltage": ("voltage", 0x0096, "V", Expression("divide", (10,))),
            "current": ("current", 0x0097, "A", Expression("divide", (1000,))),
        }
        exposes: list[Expose] = []
        bindings: list[Binding] = []
        for option, (expose_name, attribute, unit, expression) in fields.items():
            if args.get(option, True) is False:
                continue
            exposes.append(Expose("numeric", expose_name, expose_name, ("state",), unit=unit))
            bindings.append(Binding(f"{name}_{option}", "manuSpecificLumi", attribute, direction="report", expression=expression))
        return exposes, bindings
    return None


def _lumi_on_off_extend(args: dict[str, Any]) -> tuple[list[Expose], list[Binding], str, bool]:
    """Expand the static subset of lumiOnOff used by current definitions."""
    supported_options = {
        "deviceTemperature",
        "endpointNames",
        "lockRelay",
        "operationMode",
        "powerOutageCount",
        "powerOutageMemory",
    }
    if set(args) - supported_options:
        return [], [], "lumiOnOff", False
    endpoint_names = args.get("endpointNames")
    if endpoint_names is not None and (
        not isinstance(endpoint_names, list)
        or not endpoint_names
        or not all(isinstance(item, str) for item in endpoint_names)
    ):
        return [], [], "lumiOnOff", False
    endpoints = endpoint_names or [None]
    exposes = [Expose("switch", "state", "state", ("state", "set"), endpoint=endpoint) for endpoint in endpoints]
    bindings = [Binding("on_off", "genOnOff", "onOff", direction="report", endpoint=endpoint) for endpoint in endpoints]

    if args.get("deviceTemperature", True) is True:
        exposes.append(Expose("numeric", "device_temperature", "device_temperature", ("state",), unit="°C", category="diagnostic"))
        bindings.append(Binding("device_temperature", "genDeviceTempCfg", "currentTemperature", direction="report"))
    if args.get("powerOutageCount", True) is True:
        exposes.append(Expose("numeric", "power_outage_count", "power_outage_count", ("state",), category="diagnostic"))
        bindings.append(
            Binding(
                "lumi_power_outage_count",
                "manuSpecificLumi",
                5,
                direction="report",
                expression=Expression("subtract", (1,)),
            )
        )
    if args.get("powerOutageMemory") == "binary":
        generated_exposes, generated_bindings = _lumi_simple_extend("lumiPowerOutageMemory", {}) or ([], [])
        exposes.extend(generated_exposes)
        bindings.extend(generated_bindings)
    elif args.get("powerOutageMemory") == "enum":
        generated_exposes, generated_bindings = _lumi_simple_extend("lumiPowerOnBehavior", {}) or ([], [])
        exposes.extend(generated_exposes)
        bindings.extend(generated_bindings)
    elif "powerOutageMemory" in args and args["powerOutageMemory"] is not False:
        return [], [], "lumiOnOff", False
    if args.get("operationMode") is True:
        operation_endpoints = endpoint_names or [None]
        for endpoint in operation_endpoints:
            generated_exposes, generated_bindings = _lumi_simple_extend(
                "lumiOperationMode",
                {"endpointName": endpoint} if endpoint is not None else {},
            ) or ([], [])
            exposes.extend(generated_exposes)
            bindings.extend(generated_bindings)
    if args.get("lockRelay") is True:
        lock_endpoints = endpoint_names or [None]
        for endpoint in lock_endpoints:
            generated_exposes, generated_bindings = _lumi_simple_extend(
                "lumiLockRelay",
                {"endpointName": endpoint} if endpoint is not None else {},
            ) or ([], [])
            exposes.extend(generated_exposes)
            bindings.extend(generated_bindings)
    return exposes, bindings, "lumiOnOff", True


def _endpoint_clusters_for_extend(call: Any) -> list[EndpointCluster]:
    """Extract endpoint cluster changes from safe modernExtend metadata."""
    call_name = _call_name(call)
    if call_name and call_name.rsplit(".", 1)[-1] == "lumiZigbeeOTA":
        # Lumi devices may omit the OTA client cluster from endpoint 1 even
        # though their firmware supports standard Zigbee OTA updates.
        return [EndpointCluster(1, "genOta", "output")]
    return []


def _bindings(values: Any, direction: str) -> list[Binding]:
    if not isinstance(values, list):
        return []
    result = []
    for item in values:
        converter = _identifier(item)
        if converter:
            result.append(Binding(converter=converter, direction=direction))
    return result


def _configure_endpoint(value: Any, locals_: dict[str, Any]) -> str | int | None:
    if isinstance(value, dict) and set(value) == {"__identifier__"}:
        local = locals_.get(str(value["__identifier__"]))
        if local is not None:
            return _configure_endpoint(local, locals_)
    if isinstance(value, dict) and value.get("__call__") == "device.getEndpoint":
        args = value.get("args", [])
        return _static_value(args[0]) if args and isinstance(_static_value(args[0]), (str, int)) else None
    if isinstance(value, dict) and value.get("__indexed__") in {"device.endpoints", "device.endpoint"}:
        index = value.get("index")
        if isinstance(index, int):
            return f"__endpoint_index__:{index}"
    identifier = _identifier(value)
    if identifier:
        match = re.fullmatch(r"(?:endpoint|ep)(\d+)", identifier)
        if match:
            return int(match.group(1))
    return None


def _resolve_config_value(value: Any, locals_: dict[str, Any]) -> Any:
    """Resolve static configure locals without evaluating expressions."""
    if isinstance(value, dict) and set(value) == {"__identifier__"}:
        name = str(value["__identifier__"])
        if name in locals_:
            return _resolve_config_value(locals_[name], locals_)
        parts = name.split(".")
        if parts[0] in locals_:
            resolved: Any = locals_[parts[0]]
            for part in parts[1:]:
                if not isinstance(resolved, dict) or part not in resolved:
                    return value
                resolved = resolved[part]
            return _resolve_config_value(resolved, locals_)
        return value
    if isinstance(value, list):
        return [_resolve_config_value(item, locals_) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_config_value(item, locals_) for key, item in value.items()}
    return value


def _is_coordinator_endpoint(value: Any) -> bool:
    return _identifier(value) in {"coordinatorEndpoint", "coordinator"}


def _static_manufacturer_option(value: Any, locals_: dict[str, Any]) -> tuple[bool, int | None]:
    """Resolve the manufacturer option accepted by read/reporting calls."""
    if value is None:
        return True, None
    resolved = _resolve_config_value(value, locals_)
    if not isinstance(resolved, dict):
        return False, None
    raw_code = resolved.get("manufacturerCode")
    if raw_code is None:
        return True, None
    code = _static_manufacturer_code(raw_code)
    return code is not None, code


def _configure_reporting_actions(
    endpoint: str | int,
    cluster: Any,
    payload: Any,
    manufacturer_code: int | None = None,
) -> list[ConfigureAction] | None:
    if not isinstance(cluster, (str, int)) or not isinstance(payload, list) or not payload:
        return None
    actions: list[ConfigureAction] = []
    for item in payload:
        if not isinstance(item, dict):
            return None
        attribute = _static_value(item.get("attribute"))
        minimum = _static_value(item.get("minimumReportInterval"))
        maximum = _static_value(item.get("maximumReportInterval"))
        change = _static_value(item.get("reportableChange"))
        if not isinstance(attribute, (str, int)):
            return None
        if not isinstance(minimum, (int, float)) or not isinstance(maximum, (int, float)):
            return None
        if change is not None and not isinstance(change, (int, float)):
            return None
        actions.append(
            ConfigureAction(
                "configure_reporting",
                endpoint,
                cluster,
                attributes=(attribute,),
                minimum_interval=minimum,
                maximum_interval=maximum,
                reportable_change=change,
                target="device",
                manufacturer_code=manufacturer_code,
            )
        )
    return actions


def _configure_command_action(
    endpoint: str | int,
    args: list[Any],
    locals_: dict[str, Any],
) -> ConfigureAction | None:
    if len(args) not in {2, 3}:
        return None
    cluster = _static_value(args[0])
    command = _static_value(args[1])
    payload = _resolve_config_value(args[2], locals_) if len(args) == 3 else {}
    if not isinstance(cluster, (str, int)) or not isinstance(command, (str, int)) or not isinstance(payload, dict):
        return None
    if any(_static_value(value) is None and value is not None for value in payload.values()):
        return None
    static_payload = {str(key): _static_value(value) for key, value in payload.items()}
    return ConfigureAction("command", endpoint, cluster, command=command, payload=static_payload, target="device")


def _configure_write_action(
    endpoint: str | int,
    args: list[Any],
    locals_: dict[str, Any],
) -> ConfigureAction | None:
    """Convert a literal endpoint.write call into a declarative action."""
    if len(args) not in {2, 3}:
        return None
    cluster = _static_value(args[0])
    raw_payload = _resolve_config_value(args[1], locals_)
    options_valid, manufacturer_code = _static_manufacturer_option(
        args[2] if len(args) == 3 else None,
        locals_,
    )
    if not isinstance(cluster, (str, int)) or not isinstance(raw_payload, dict) or not options_valid:
        return None

    payload: dict[str, Any] = {}
    for attribute, raw_value in raw_payload.items():
        if not isinstance(attribute, (str, int)):
            return None
        if isinstance(raw_value, dict) and "value" in raw_value:
            value = _static_value(raw_value["value"])
            if value is None and raw_value["value"] is not None:
                return None
        else:
            value = _static_value(raw_value)
            if value is None and raw_value is not None:
                return None
        payload[attribute] = value
    return ConfigureAction(
        "write",
        endpoint,
        cluster,
        payload=payload,
        target="device",
        manufacturer_code=manufacturer_code,
    )


_REPORTING_HELPERS: dict[str, tuple[str, str, int | float, int | float, int | float | None, bool]] = {
    "onOff": ("genOnOff", "onOff", 0, 3600, 0, False),
    "onTime": ("genOnOff", "onTime", 0, 3600, 40, False),
    "batteryPercentageRemaining": ("genPowerCfg", "batteryPercentageRemaining", 3600, 65000, 0, True),
    "batteryVoltage": ("genPowerCfg", "batteryVoltage", 3600, 65000, 0, True),
    "batteryAlarmState": ("genPowerCfg", "batteryAlarmState", 3600, 65000, 0, True),
    "brightness": ("genLevelCtrl", "currentLevel", 1, 3600, 1, False),
    "colorTemperature": ("lightingColorCtrl", "colorTemperature", 0, 3600, 1, False),
    "currentPositionLiftPercentage": ("closuresWindowCovering", "currentPositionLiftPercentage", 1, 3600, 1, False),
    "currentPositionTiltPercentage": ("closuresWindowCovering", "currentPositionTiltPercentage", 1, 3600, 1, False),
    "occupancy": ("msOccupancySensing", "occupancy", 0, 3600, 0, False),
    "temperature": ("msTemperatureMeasurement", "measuredValue", 10, 3600, 100, False),
    "humidity": ("msRelativeHumidity", "measuredValue", 10, 3600, 100, False),
    "pressure": ("msPressureMeasurement", "measuredValue", 10, 3600, 5, False),
    "pressureExtended": ("msPressureMeasurement", "scaledValue", 10, 3600, 5, False),
    "illuminance": ("msIlluminanceMeasurement", "measuredValue", 10, 3600, 5, False),
    "co2": ("msCO2", "measuredValue", 10, 3600, 1, False),
    "deviceTemperature": ("genDeviceTempCfg", "currentTemperature", 300, 3600, 1, False),
    "soil_moisture": ("msSoilMoisture", "measuredValue", 10, 3600, 100, False),
    "instantaneousDemand": ("seMetering", "instantaneousDemand", 5, 3600, 1, False),
    "currentSummDelivered": ("seMetering", "currentSummDelivered", 5, 3600, 257, False),
    "currentSummReceived": ("seMetering", "currentSummReceived", 5, 3600, 257, False),
    "doorState": ("closuresDoorLock", "doorState", 0, 3600, 0, False),
    "thermostatSystemMode": ("hvacThermostat", "systemMode", 10, 3600, None, False),
    "thermostatTemperature": ("hvacThermostat", "localTemp", 0, 3600, 10, False),
    "thermostatKeypadLockMode": ("hvacUserInterfaceCfg", "keypadLockout", 10, 3600, None, False),
    "thermostatTemperatureCalibration": ("hvacThermostat", "localTemperatureCalibration", 0, 3600, 0, False),
    "thermostatOccupiedHeatingSetpoint": ("hvacThermostat", "occupiedHeatingSetpoint", 0, 3600, 10, False),
    "thermostatUnoccupiedHeatingSetpoint": ("hvacThermostat", "unoccupiedHeatingSetpoint", 0, 3600, 10, False),
    "thermostatOccupiedCoolingSetpoint": ("hvacThermostat", "occupiedCoolingSetpoint", 0, 3600, 10, False),
    "thermostatUnoccupiedCoolingSetpoint": ("hvacThermostat", "unoccupiedCoolingSetpoint", 0, 3600, 10, False),
    "thermostatPIHeatingDemand": ("hvacThermostat", "pIHeatingDemand", 0, 3600, 10, False),
    "thermostatPICoolingDemand": ("hvacThermostat", "pICoolingDemand", 0, 3600, 10, False),
    "thermostatRunningState": ("hvacThermostat", "runningState", 0, 3600, 0, False),
    "thermostatRunningMode": ("hvacThermostat", "runningMode", 10, 3600, None, False),
    "thermostatOccupancy": ("hvacThermostat", "occupancy", 0, 3600, 0, False),
    "thermostatSetpointChangeSource": ("hvacThermostat", "setpointChangeSource", 10, 3600, None, False),
    "thermostatTemperatureSetpointHold": ("hvacThermostat", "tempSetpointHold", 0, 3600, 0, False),
    "thermostatTemperatureSetpointHoldDuration": ("hvacThermostat", "tempSetpointHoldDuration", 0, 3600, 10, False),
    "thermostatAcLouverPosition": ("hvacThermostat", "acLouverPosition", 0, 3600, None, False),
    "lockState": ("closuresDoorLock", "lockState", 0, 3600, 0, False),
    "activePower": ("haElectricalMeasurement", "activePower", 5, 3600, 1, False),
    "reactivePower": ("haElectricalMeasurement", "reactivePower", 5, 3600, 1, False),
    "apparentPower": ("haElectricalMeasurement", "apparentPower", 5, 3600, 1, False),
    "rmsCurrent": ("haElectricalMeasurement", "rmsCurrent", 5, 3600, 1, False),
    "rmsVoltage": ("haElectricalMeasurement", "rmsVoltage", 5, 3600, 1, False),
    "powerFactor": ("haElectricalMeasurement", "powerFactor", 0, 65000, 1, False),
    "fanMode": ("hvacFanCtrl", "fanMode", 0, 3600, 0, False),
    "acFrequency": ("haElectricalMeasurement", "acFrequency", 5, 300, 10, False),
    "presentValue": ("genBinaryInput", "presentValue", 10, 60, 1, False),
}


def _reporting_helper_actions(call: str | None, args: list[Any], locals_: dict[str, Any]) -> list[ConfigureAction] | None:
    if not call or not call.startswith("reporting."):
        return None
    helper = call.rsplit(".", 1)[-1]
    definition = _REPORTING_HELPERS.get(helper)
    if definition is None or len(args) not in {1, 2}:
        return None
    endpoint = _configure_endpoint(args[0], locals_)
    if endpoint is None:
        return None
    cluster, attribute, minimum, maximum, change, reads_after = definition
    if len(args) == 2:
        overrides = args[1]
        if not isinstance(overrides, dict):
            return None
        minimum = _static_value(overrides.get("min")) if "min" in overrides else minimum
        maximum = _static_value(overrides.get("max")) if "max" in overrides else maximum
        change = _static_value(overrides.get("change")) if "change" in overrides else change
    if not isinstance(minimum, (int, float)) or not isinstance(maximum, (int, float)):
        return None
    if change is not None and not isinstance(change, (int, float)):
        return None
    actions = [
        ConfigureAction(
            "configure_reporting",
            endpoint,
            cluster,
            attributes=(attribute,),
            minimum_interval=minimum,
            maximum_interval=maximum,
            reportable_change=change,
            target="device",
        )
    ]
    if reads_after:
        actions.append(ConfigureAction("read", endpoint, cluster, attributes=(attribute,), target="device"))
    return actions


def _configure_actions(
    value: Any,
    constants: dict[str, Any] | None = None,
) -> tuple[list[ConfigureAction], bool]:
    """Extract a small whitelist of bind and read operations from a callback."""
    if value is None or value == []:
        return [], False
    if not isinstance(value, dict) or "__configure__" not in value:
        return [], True
    local_values = value.get("__locals__", {})
    if not isinstance(local_values, dict):
        local_values = {}
    locals_ = {**(constants or {}), **local_values}
    actions: list[ConfigureAction] = []
    unsupported = "__unsupported__" in value
    for statement in value.get("__configure__", []):
        if not isinstance(statement, dict):
            unsupported = True
            continue
        if "__fluent__" in statement:
            base = statement["__fluent__"]
            endpoint = _configure_endpoint(base, locals_)
            for method in statement.get("methods", []):
                if method.get("name") not in {"bind", "read", "write", "command"} or endpoint is None:
                    unsupported = True
                    continue
                args = method.get("args", [])
                if method.get("name") == "command":
                    command_action = _configure_command_action(endpoint, args, locals_)
                    if command_action is not None:
                        actions.append(command_action)
                    else:
                        unsupported = True
                    continue
                if method.get("name") == "write":
                    write_action = _configure_write_action(endpoint, args, locals_)
                    if write_action is not None:
                        actions.append(write_action)
                    else:
                        unsupported = True
                    continue
                if method.get("name") == "bind":
                    if len(args) != 2:
                        unsupported = True
                        continue
                    if _is_coordinator_endpoint(args[0]):
                        cluster = _static_value(args[1])
                    elif _is_coordinator_endpoint(args[1]):
                        cluster = _static_value(args[0])
                    else:
                        cluster = None
                    if isinstance(cluster, (str, int)):
                        actions.append(ConfigureAction("bind", endpoint, cluster))
                    else:
                        unsupported = True
                    continue
                if len(args) in {2, 3} and isinstance(_resolve_config_value(args[1], locals_), list):
                    cluster = _static_value(args[0])
                    attributes_value = _resolve_config_value(args[1], locals_)
                    attributes = tuple(_static_value(item) for item in attributes_value)
                    options_valid, manufacturer_code = _static_manufacturer_option(
                        args[2] if len(args) == 3 else None,
                        locals_,
                    )
                    if (
                        isinstance(cluster, (str, int))
                        and all(isinstance(item, (str, int)) for item in attributes)
                        and options_valid
                    ):
                        actions.append(
                            ConfigureAction(
                                "read",
                                endpoint,
                                cluster,
                                attributes=attributes,
                                target="device",
                                manufacturer_code=manufacturer_code,
                            )
                        )
                    else:
                        unsupported = True
                else:
                    unsupported = True
            continue
        call = _call_name(statement)
        args = statement.get("args", [])
        if call in {"tuya.configureQuery", "tuya.configureBindBasic"}:
            if len(args) == 2 and _identifier(args[0]) == "device" and _is_coordinator_endpoint(args[1]):
                if call.endswith("configureQuery"):
                    actions.append(
                        ConfigureAction(
                            "command",
                            1,
                            "manuSpecificTuya",
                            command="dataQuery",
                            payload={},
                            target="device",
                        )
                    )
                else:
                    actions.append(ConfigureAction("bind", 1, "genBasic"))
            else:
                unsupported = True
            continue
        if call == "tuya.configureMagicPacket":
            if len(args) == 2 and _identifier(args[0]) == "device" and _is_coordinator_endpoint(args[1]):
                actions.append(
                    ConfigureAction(
                        "read",
                        0,
                        "genBasic",
                        attributes=("manufacturerName", "zclVersion", "appVersion", "modelId", "powerSource", 0xFFFE),
                        target="device",
                    )
                )
            else:
                unsupported = True
            continue
        if call and not call.startswith("reporting.") and call.rsplit(".", 1)[-1] in {"bind", "read", "write", "command", "configureReporting"}:
            method_name = call.rsplit(".", 1)[-1]
            receiver = {"__identifier__": call.rsplit(".", 1)[0]}
            endpoint = _configure_endpoint(receiver, locals_)
            if endpoint is None:
                unsupported = True
                continue
            if method_name == "bind" and len(args) == 2:
                if _is_coordinator_endpoint(args[0]):
                    cluster = _static_value(args[1])
                elif _is_coordinator_endpoint(args[1]):
                    cluster = _static_value(args[0])
                else:
                    cluster = None
                if isinstance(cluster, (str, int)):
                    actions.append(ConfigureAction("bind", endpoint, cluster))
                else:
                    unsupported = True
                continue
            if method_name == "read" and len(args) in {2, 3} and isinstance(_resolve_config_value(args[1], locals_), list):
                cluster = _static_value(args[0])
                attributes_value = _resolve_config_value(args[1], locals_)
                attributes = tuple(_static_value(item) for item in attributes_value)
                options_valid, manufacturer_code = _static_manufacturer_option(
                    args[2] if len(args) == 3 else None,
                    locals_,
                )
                if (
                    isinstance(cluster, (str, int))
                    and all(isinstance(item, (str, int)) for item in attributes)
                    and options_valid
                ):
                    actions.append(
                        ConfigureAction(
                            "read",
                            endpoint,
                            cluster,
                            attributes=attributes,
                            target="device",
                            manufacturer_code=manufacturer_code,
                        )
                    )
                else:
                    unsupported = True
                continue
            if method_name == "configureReporting" and len(args) in {2, 3}:
                options_valid, manufacturer_code = _static_manufacturer_option(
                    args[2] if len(args) == 3 else None,
                    locals_,
                )
                reporting_actions = _configure_reporting_actions(
                    endpoint,
                    _static_value(args[0]),
                    _resolve_config_value(args[1], locals_),
                    manufacturer_code if options_valid else None,
                )
                if reporting_actions is not None and options_valid:
                    actions.extend(reporting_actions)
                else:
                    unsupported = True
                continue
            if method_name == "write":
                write_action = _configure_write_action(endpoint, args, locals_)
                if write_action is not None:
                    actions.append(write_action)
                else:
                    unsupported = True
                continue
            if method_name == "command":
                command_action = _configure_command_action(endpoint, args, locals_)
                if command_action is not None:
                    actions.append(command_action)
                else:
                    unsupported = True
                continue
            unsupported = True
            continue
        if call == "reporting.readMeteringMultiplierDivisor":
            endpoint = _configure_endpoint(args[0], locals_) if len(args) == 1 else None
            if endpoint is None:
                unsupported = True
            else:
                actions.append(ConfigureAction("read", endpoint, "seMetering", attributes=("multiplier", "divisor"), target="device"))
            continue
        if call == "reporting.readEletricalMeasurementMultiplierDivisors":
            endpoint = _configure_endpoint(args[0], locals_) if args and len(args) <= 2 else None
            read_frequency = args[1] is True if len(args) == 2 else False
            if endpoint is None or (len(args) == 2 and not isinstance(args[1], bool)):
                unsupported = True
            else:
                attributes = (
                    "acVoltageMultiplier",
                    "acVoltageDivisor",
                    "acCurrentMultiplier",
                    "acCurrentDivisor",
                    "acPowerMultiplier",
                    "acPowerDivisor",
                )
                if read_frequency:
                    attributes += ("acFrequencyDivisor", "acFrequencyMultiplier")
                actions.append(ConfigureAction("read", endpoint, "haElectricalMeasurement", attributes=attributes, target="device"))
            continue
        helper_actions = _reporting_helper_actions(call, args, locals_)
        if helper_actions is not None:
            actions.extend(helper_actions)
            continue
        if call == "reporting.bind":
            clusters = _resolve_config_value(args[2], locals_) if len(args) == 3 else None
            endpoint = _configure_endpoint(args[0], locals_) if len(args) >= 1 else None
            if endpoint is None or len(args) != 3 or not _is_coordinator_endpoint(args[1]) or not isinstance(clusters, list):
                unsupported = True
                continue
            static_clusters = [_static_value(item) for item in clusters]
            if not static_clusters or not all(isinstance(item, (str, int)) for item in static_clusters):
                unsupported = True
                continue
            actions.extend(ConfigureAction("bind", endpoint, cluster) for cluster in static_clusters)
            continue
        unsupported = True
    return actions, unsupported


def _device(
    raw: dict[str, Any],
    token: Token,
    filename: str,
    diagnostics: list[Diagnostic],
    constants: dict[str, Any] | None = None,
) -> DeviceDefinition | None:
    model = _string(raw.get("model"))
    vendor = _string(raw.get("vendor")) or _string(raw.get("manufacturer"))
    raw_models = raw.get("zigbeeModel")
    zigbee_models = []
    if isinstance(raw_models, list):
        zigbee_models = [item for item in raw_models if isinstance(item, str)]
    elif isinstance(raw_models, str):
        zigbee_models = [raw_models]
    if not model and zigbee_models:
        model = zigbee_models[0]
    if not model and not zigbee_models:
        diagnostics.append(Diagnostic("error", "missing-model", "definition has no static model/zigbeeModel", filename, token.line, token.column))
        return None
    raw_exposes = raw.get("exposes", [])
    fingerprints = _fingerprints(raw.get("fingerprint"))
    fingerprints.extend(_white_label_fingerprints(raw.get("whiteLabel"), zigbee_models))
    fingerprints = list({(item["modelID"], item["manufacturerName"]): item for item in fingerprints}.values())
    exposes = [_expose(item) for item in raw_exposes] if isinstance(raw_exposes, list) else []
    exposes = [item for item in exposes if item is not None]
    from_zigbee = _bindings(raw.get("fromZigbee"), "report")
    to_zigbee = _bindings(raw.get("toZigbee"), "command")
    configure_actions, configure_unsupported = _configure_actions(raw.get("configure"), constants)
    endpoint_clusters: list[EndpointCluster] = []
    custom_clusters: list[str] = []
    custom_cluster_specs: list[CustomClusterSpec] = []
    extends: list[str] = []
    unsupported_macros: list[str] = []
    conditional_extends: list[dict[str, Any]] = []
    dynamic_extend = False
    endpoint_map = _endpoint_map(raw.get("endpoint"))
    unsupported_fields = [
        str(key)
        for key, value in raw.items()
        if (isinstance(value, dict) and "__unsupported__" in value)
        or (key == "configure" and configure_unsupported)
    ]
    extend_values = raw.get("extend", []) if isinstance(raw.get("extend"), list) else []
    for item in extend_values:
        endpoint_map.update(_endpoint_map_for_extend(item))
    for item in extend_values:
        macro_name = _call_name(item)
        if macro_name:
            extends.append(macro_name)
        generated_exposes, generated_from, name, supported = _modern_extend(item)
        endpoint_clusters.extend(_endpoint_clusters_for_extend(item))
        if name == "tuyaBase":
            args = _call_args(item)
            if args.get("queryOnConfigure") is True:
                configure_actions.append(
                    ConfigureAction(
                        "command",
                        1,
                        "manuSpecificTuya",
                        command="dataQuery",
                        payload={},
                        target="device",
                    )
                )
            if args.get("bindBasicOnConfigure") is True:
                configure_actions.append(ConfigureAction("bind", 1, "genBasic"))
        if name == "addTuyaCommonPrivateCluster":
            custom_clusters.append("manuSpecificTuya4")
        if name == "addManuSpecificLumiCluster":
            custom_cluster_specs.append(_lumi_cluster_spec())
        if name == "addCustomClusterManuSpecificIkeaUnknown":
            custom_cluster_specs.append(_ikea_unknown_cluster_spec())
        if name == "addCustomClusterManuSpecificDevelcoGenBasic":
            custom_cluster_specs.append(_develco_gen_basic_cluster_spec())
        if name == "addCustomClusterManuSpecificDevelcoIasZone":
            custom_cluster_specs.append(_develco_ias_zone_cluster_spec())
        if name == "addCustomClusterManuSpecificDevelcoAirQuality":
            custom_cluster_specs.append(_develco_air_quality_cluster_spec())
        if name == "addCustomDevelcoSeMeteringCluster":
            custom_cluster_specs.append(_develco_se_metering_cluster_spec())
        if name == "readGenBasicPrimaryVersions":
            configure_actions.append(
                ConfigureAction(
                    "read",
                    1,
                    "genBasic",
                    attributes=("develcoPrimarySwVersion", "develcoPrimaryHwVersion"),
                    target="device",
                )
            )
        custom_cluster_spec = _custom_cluster_spec(item)
        if custom_cluster_spec is not None:
            custom_cluster_specs.append(custom_cluster_spec)
        if name == "tuyaOnOff":
            args = _call_args(item)
            endpoint_names = args.get("endpoints")
            if isinstance(endpoint_names, list) and endpoint_names and any(
                not isinstance(endpoint, str) or endpoint not in endpoint_map for endpoint in endpoint_names
            ):
                supported = False
            for option, value in args.items():
                if _is_predicate(value):
                    conditional_extends.append(
                        {
                            "call": item,
                            "option": option,
                            "predicate": value,
                            "endpoint_map": dict(endpoint_map),
                        }
                    )
        if name and not supported:
            unsupported_macros.append(name)
        if name == "electricityMeter":
            args = _call_args(item)
            dynamic_extend = dynamic_extend or any(
                key.startswith(("fz", "tz")) and value is not None for key, value in args.items()
            )
        exposes.extend(generated_exposes)
        from_zigbee.extend(generated_from)
    if endpoint_map:
        exposes = [replace(expose, endpoint=_resolve_endpoint(expose.endpoint, endpoint_map)) for expose in exposes]
        from_zigbee = [replace(binding, endpoint=_resolve_endpoint(binding.endpoint, endpoint_map)) for binding in from_zigbee]
    converter_names = {
        binding.converter.rsplit(".", 1)[-1]
        for binding in [*from_zigbee, *to_zigbee]
    }
    if model in LUMI_SINGLE_OPERATION_MODE_BASIC_MODELS and converter_names & {
        "lumi_operation_mode_basic",
        "lumi_switch_operation_mode_basic",
    }:
        custom_cluster_specs.append(_lumi_basic_operation_mode_cluster_spec())
    if dynamic_extend:
        unsupported_macros.append("dynamic-expression")
    partial = bool(unsupported_macros or unsupported_fields)
    for key in ("fromZigbee", "toZigbee", "exposes"):
        value = raw.get(key)
        if isinstance(value, list) and any(isinstance(item, dict) and _is_dynamic_value(item, key) for item in value):
            partial = True
    if partial and not unsupported_macros:
        unsupported_macros.append("dynamic-expression")
    if partial:
        diagnostics.append(
            Diagnostic(
                "warning",
                "partial-definition",
                "dynamic converter or extend expressions were skipped; static device data was retained",
                filename,
                token.line,
                token.column,
                path=model,
            )
        )
    return DeviceDefinition(
        manufacturer=vendor,
        model=model,
        zigbee_models=zigbee_models or [model],
        fingerprints=fingerprints,
        description=_string(raw.get("description")),
        exposes=exposes,
        from_zigbee=from_zigbee,
        to_zigbee=to_zigbee,
        extends=extends,
        configure_actions=configure_actions,
        endpoint_clusters=endpoint_clusters,
        custom_clusters=custom_clusters,
        custom_cluster_specs=custom_cluster_specs,
        conditional_extends=conditional_extends,
        unsupported_macros=unsupported_macros,
        unsupported_fields=unsupported_fields,
        source=filename,
        source_line=token.line,
        partial=partial,
    )


def _is_dynamic_value(value: dict[str, Any], key: str) -> bool:
    if "__identifier__" in value:
        if key in {"fromZigbee", "toZigbee"}:
            converter = str(value["__identifier__"]).rsplit(".", 1)[-1]
            return converter not in CONVERTER_MAP
        return True
    if "__unsupported__" in value:
        return True
    if "__call__" in value:
        if key == "exposes":
            return _expose(value) is None
        return True
    if "__fluent__" in value:
        return key == "exposes" and _expose(value) is None
    return False


def _is_identity_expression(value: Any) -> bool:
    """Return whether a parsed arrow expression returns its only parameter."""
    return isinstance(value, dict) and set(value) == {"__identity__"}


def _record_rejection(result: ParseResult, diagnostic: Diagnostic) -> None:
    """Record a rejected definition in both diagnostics and the summary list."""
    result.diagnostics.append(diagnostic)
    result.rejected_details.append(diagnostic)
    result.rejected_definitions += 1


def parse_source(text: str, filename: str = "<memory>") -> ParseResult:
    """Parse definitions without importing or executing the source module."""
    result = ParseResult(syntax_validated=_validate_with_tree_sitter(text))
    tokens = tokenize(text)
    constants = _find_static_constants(tokens)
    assignments = _find_assignments(tokens, {"definitions", "definition"}, constants)
    if not assignments:
        result.diagnostics.append(Diagnostic("warning", "no-definitions", "no static definitions assignment found", filename))
        return result
    for token, value in assignments:
        values = value if isinstance(value, list) else [value]
        if value is None:
            _record_rejection(
                result,
                Diagnostic(
                    "warning",
                    "unsupported-definition",
                    "definition contains unsupported dynamic syntax",
                    filename,
                    token.line,
                    token.column,
                    path="definitions",
                ),
            )
            continue
        for raw in values:
            if not isinstance(raw, dict):
                result.diagnostics.append(Diagnostic("warning", "unsupported-definition", "definition is not a static object", filename, token.line, token.column))
                continue
            if set(raw) in ({"__unsupported__"}, {"__spread__"}):
                # A spread entry from an aggregate index is not a device object.
                if raw.get("__unsupported__") == "array-item":
                    _record_rejection(
                        result,
                        Diagnostic(
                            "warning",
                            "unsupported-definition",
                            "definition contains unsupported dynamic syntax",
                            filename,
                            token.line,
                            token.column,
                            path="definitions",
                        ),
                    )
                continue
            extend = raw.get("extend")
            if isinstance(extend, list) and any(
                item == {"__unsupported__": "array-item"} for item in extend
            ):
                _record_rejection(
                    result,
                    Diagnostic(
                        "warning",
                        "unsupported-definition",
                        "definition contains unsupported dynamic syntax",
                        filename,
                        token.line,
                        token.column,
                        path=_string(raw.get("model")),
                    ),
                )
                continue
            device = _device(raw, token, filename, result.diagnostics, constants)
            if device:
                result.devices.append(device)
    return result


def parse_path(path: str | Path) -> ParseResult:
    """Parse every TypeScript device source in a file or converter snapshot."""
    return parse_paths([path])


def parse_paths(paths: Iterable[str | Path]) -> ParseResult:
    """Parse and combine definitions from multiple converter source paths."""
    combined = ParseResult()
    for path in paths:
        for source in load_sources(path):
            result = parse_source(source.text, source.filename)
            combined.source_files += 1
            combined.devices.extend(result.devices)
            combined.diagnostics.extend(result.diagnostics)
            combined.rejected_details.extend(result.rejected_details)
            combined.syntax_validated = combined.syntax_validated or result.syntax_validated
            combined.rejected_definitions += result.rejected_definitions
    return combined
