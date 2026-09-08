"""Static parser for the safe, declarative subset of converter definitions."""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .lexer import Token, tokenize
from .mapping import CONVERTER_MAP
from .model import Binding, ConfigureAction, DeviceDefinition, Diagnostic, Expose, Expression, ParseResult
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
        token = self.current()
        if token.value == "{":
            return self.parse_object()
        if token.value == "[":
            self.take("[")
            values = []
            while self.current().value != "]":
                value_start = self.index
                try:
                    values.append(self.parse_value())
                except UnsupportedSyntax:
                    values.append({"__unsupported__": "array-item"})
                    self.skip_to_object_boundary(value_start, boundaries=(",", "]"))
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
            if self.constants and "." not in parts and name in self.constants:
                return self.constants[name]
            return {"__identifier__": ".".join(parts)}
        raise UnsupportedSyntax(f"unsupported value {token.value!r}")

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
                result[str(key_value)] = self.parse_configure() if key_value == "configure" else self.parse_value()
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
        while self.current().value != "}":
            if self.current().value == ";":
                self.take()
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
                    locals_[name.value] = self.parse_value()
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


def _find_assignments(tokens: list[Token], names: set[str]) -> list[tuple[Token, Any]]:
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
        parser = _ObjectParser(tokens[start : end + 1])
        try:
            found.append((token, parser.parse_value()))
        except UnsupportedSyntax:
            found.append((token, None))
    return found


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


def _call_args(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    args = value.get("args", [])
    if args and isinstance(args[0], dict) and "__call__" not in args[0] and "__identifier__" not in args[0]:
        return args[0]
    return {}


def _modern_extend(call: Any) -> tuple[list[Expose], list[Binding], str | None, bool]:
    """Expand a safe modernExtend call structurally, never by calling JS."""
    if not isinstance(call, dict) or "__call__" not in call:
        return [], [], None, False
    name = str(call["__call__"]).rsplit(".", 1)[-1]
    args = _call_args(call)
    if name in {"ledvanceLight", "tuyaLight"}:
        name = "light"
        if call["__call__"].endswith("ledvanceLight") and args.get("color") is True:
            args = {**args, "color": {"modes": ["xy", "hs"]}}
    elif name == "ledvanceOnOff":
        name = "onOff"
    if name in _SUPPORTED_METADATA_MACROS:
        return [], [], name, True
    if name in _MODERN_EXTEND_SENSOR_MACROS:
        expose_name, cluster, attribute, unit, scale = _MODERN_EXTEND_SENSOR_MACROS[name]
        expose = Expose("numeric", expose_name, expose_name, ("state",), unit=unit)
        expression = Expression("divide", (scale,)) if scale else None
        return [expose], [Binding(name, cluster, attribute, direction="report", expression=expression)], name, True
    if name == "onOff":
        expose = Expose("switch", "state", "state", ("state", "set"))
        return [expose], [Binding(name, "genOnOff", "onOff", direction="report")], name, True
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
            exposes.append(Expose("numeric", "color_temperature", "color_temperature", ("state", "set"), unit="mired"))
            bindings.append(Binding(name, "lightingColorCtrl", "colorTemperature", direction="report"))
        if args.get("color"):
            exposes.append(Expose("numeric", "color", "color", ("state", "set")))
            bindings.append(Binding(name, "lightingColorCtrl", None, direction="report"))
        return exposes, bindings, name, True
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
    return None


def _is_coordinator_endpoint(value: Any) -> bool:
    return _identifier(value) in {"coordinatorEndpoint", "coordinator"}


def _configure_reporting_actions(
    endpoint: str | int,
    cluster: Any,
    payload: Any,
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
            )
        )
    return actions


_REPORTING_HELPERS: dict[str, tuple[str, str, int | float, int | float, int | float | None, bool]] = {
    "onOff": ("genOnOff", "onOff", 0, 3600, 0, False),
    "batteryPercentageRemaining": ("genPowerCfg", "batteryPercentageRemaining", 3600, 65000, 0, True),
    "batteryVoltage": ("genPowerCfg", "batteryVoltage", 3600, 65000, 0, True),
    "batteryAlarmState": ("genPowerCfg", "batteryAlarmState", 3600, 65000, 0, True),
    "brightness": ("genLevelCtrl", "currentLevel", 1, 3600, 1, False),
    "colorTemperature": ("lightingColorCtrl", "colorTemperature", 0, 3600, 1, False),
    "occupancy": ("msOccupancySensing", "occupancy", 0, 3600, 0, False),
    "temperature": ("msTemperatureMeasurement", "measuredValue", 10, 3600, 100, False),
    "humidity": ("msRelativeHumidity", "measuredValue", 10, 3600, 100, False),
    "pressure": ("msPressureMeasurement", "measuredValue", 10, 3600, 5, False),
    "illuminance": ("msIlluminanceMeasurement", "measuredValue", 10, 3600, 5, False),
    "instantaneousDemand": ("seMetering", "instantaneousDemand", 5, 3600, 1, False),
    "currentSummDelivered": ("seMetering", "currentSummDelivered", 5, 3600, 257, False),
    "currentSummReceived": ("seMetering", "currentSummReceived", 5, 3600, 257, False),
    "thermostatTemperature": ("hvacThermostat", "localTemp", 0, 3600, 10, False),
    "thermostatOccupiedHeatingSetpoint": ("hvacThermostat", "occupiedHeatingSetpoint", 0, 3600, 10, False),
    "thermostatUnoccupiedHeatingSetpoint": ("hvacThermostat", "unoccupiedHeatingSetpoint", 0, 3600, 10, False),
    "thermostatRunningState": ("hvacThermostat", "runningState", 0, 3600, 0, False),
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
        )
    ]
    if reads_after:
        actions.append(ConfigureAction("read", endpoint, cluster, attributes=(attribute,)))
    return actions


def _configure_actions(value: Any) -> tuple[list[ConfigureAction], bool]:
    """Extract a small whitelist of bind and read operations from a callback."""
    if value is None or value == []:
        return [], False
    if not isinstance(value, dict) or "__configure__" not in value:
        return [], True
    locals_ = value.get("__locals__", {})
    if not isinstance(locals_, dict):
        locals_ = {}
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
                if method.get("name") not in {"bind", "read"} or endpoint is None:
                    unsupported = True
                    continue
                args = method.get("args", [])
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
                if len(args) == 2 and isinstance(args[1], list):
                    cluster = _static_value(args[0])
                    attributes = tuple(_static_value(item) for item in args[1])
                    if isinstance(cluster, (str, int)) and all(isinstance(item, (str, int)) for item in attributes):
                        actions.append(ConfigureAction("read", endpoint, cluster, attributes=attributes))
                    else:
                        unsupported = True
                else:
                    unsupported = True
            continue
        call = _call_name(statement)
        args = statement.get("args", [])
        if call and not call.startswith("reporting.") and call.rsplit(".", 1)[-1] in {"bind", "read", "configureReporting"}:
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
            if method_name == "read" and len(args) == 2 and isinstance(args[1], list):
                cluster = _static_value(args[0])
                attributes = tuple(_static_value(item) for item in args[1])
                if isinstance(cluster, (str, int)) and all(isinstance(item, (str, int)) for item in attributes):
                    actions.append(ConfigureAction("read", endpoint, cluster, attributes=attributes))
                else:
                    unsupported = True
                continue
            if method_name == "configureReporting" and len(args) == 2:
                reporting_actions = _configure_reporting_actions(endpoint, _static_value(args[0]), args[1])
                if reporting_actions is not None:
                    actions.extend(reporting_actions)
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
                actions.append(ConfigureAction("read", endpoint, "seMetering", attributes=("multiplier", "divisor")))
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
                actions.append(ConfigureAction("read", endpoint, "haElectricalMeasurement", attributes=attributes))
            continue
        helper_actions = _reporting_helper_actions(call, args, locals_)
        if helper_actions is not None:
            actions.extend(helper_actions)
            continue
        if call == "reporting.bind":
            clusters = args[2] if len(args) == 3 else None
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


def _device(raw: dict[str, Any], token: Token, filename: str, diagnostics: list[Diagnostic]) -> DeviceDefinition | None:
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
    exposes = [_expose(item) for item in raw_exposes] if isinstance(raw_exposes, list) else []
    exposes = [item for item in exposes if item is not None]
    from_zigbee = _bindings(raw.get("fromZigbee"), "report")
    to_zigbee = _bindings(raw.get("toZigbee"), "command")
    configure_actions, configure_unsupported = _configure_actions(raw.get("configure"))
    extends: list[str] = []
    unsupported_macros: list[str] = []
    dynamic_extend = False
    unsupported_fields = [
        str(key)
        for key, value in raw.items()
        if (isinstance(value, dict) and "__unsupported__" in value)
        or (key == "configure" and configure_unsupported)
    ]
    extend_values = raw.get("extend", []) if isinstance(raw.get("extend"), list) else []
    for item in extend_values:
        macro_name = _call_name(item)
        if macro_name:
            extends.append(macro_name)
        generated_exposes, generated_from, name, supported = _modern_extend(item)
        if name and not supported:
            unsupported_macros.append(name)
        if name == "electricityMeter":
            args = _call_args(item)
            dynamic_extend = dynamic_extend or any(
                key.startswith(("fz", "tz")) and value is not None for key, value in args.items()
            )
        exposes.extend(generated_exposes)
        from_zigbee.extend(generated_from)
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
        description=_string(raw.get("description")),
        exposes=exposes,
        from_zigbee=from_zigbee,
        to_zigbee=to_zigbee,
        extends=extends,
        configure_actions=configure_actions,
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


def parse_source(text: str, filename: str = "<memory>") -> ParseResult:
    """Parse definitions without importing or executing the source module."""
    result = ParseResult(syntax_validated=_validate_with_tree_sitter(text))
    tokens = tokenize(text)
    assignments = _find_assignments(tokens, {"definitions", "definition"})
    if not assignments:
        result.diagnostics.append(Diagnostic("warning", "no-definitions", "no static definitions assignment found", filename))
        return result
    for token, value in assignments:
        values = value if isinstance(value, list) else [value]
        if value is None:
            result.diagnostics.append(Diagnostic("warning", "unsupported-definition", "definition contains unsupported dynamic syntax", filename, token.line, token.column))
            result.rejected_definitions += 1
            continue
        for raw in values:
            if not isinstance(raw, dict):
                result.diagnostics.append(Diagnostic("warning", "unsupported-definition", "definition is not a static object", filename, token.line, token.column))
                continue
            if set(raw) == {"__unsupported__"}:
                # A spread entry from an aggregate index is not a device object.
                continue
            device = _device(raw, token, filename, result.diagnostics)
            if device:
                result.devices.append(device)
    return result


def parse_path(path: str | Path) -> ParseResult:
    """Parse every TypeScript device source in a file or converter snapshot."""
    combined = ParseResult()
    for source in load_sources(path):
        result = parse_source(source.text, source.filename)
        combined.source_files += 1
        combined.devices.extend(result.devices)
        combined.diagnostics.extend(result.diagnostics)
        combined.syntax_validated = combined.syntax_validated or result.syntax_validated
        combined.rejected_definitions += result.rejected_definitions
    return combined
