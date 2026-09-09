# zha-z2m-converters

`zha-z2m-converters` is a safe, declarative bridge between
[zigbee-herdsman-converters](https://github.com/Koenkk/zigbee-herdsman-converters)
and Home Assistant ZHA.

It reads a fresh converter snapshot, extracts the supported static parts, and
turns them into Python-side ZHA definitions. It does not execute JavaScript or
TypeScript and does not require V8, Node.js, or another JavaScript runtime.

## Current coverage

Coverage is measured against the bundled converter snapshot in
`custom_components/zha_z2m_converters/converters/`. The numbers below are the
current baseline and can change when the snapshot is updated.

| Status | Definitions | Share | Meaning |
|:---:|---:|---:|---|
| ✅ Fully supported | **2,347** | **52.4%** | Static definition and all extracted features are supported. |
| 🟡 Usable partial | **1,810** | **40.4%** | Some features are missing, but at least one reliable data path remains. |
| 🟠 Metadata only | **10** | **0.2%** | Device metadata was recovered, but no usable entity or data binding exists. |
| 🔴 Unusable partial | **307** | **6.9%** | The remaining functionality depends on unsupported converter logic or has no usable data path. |
| ⛔ Rejected | **4** | **0.1%** | The definition could not be recovered by the static parser. |
| **Total** | **4,478** | **100%** | All definitions found in the snapshot, including rejected definitions. |

### At a glance

- **4,157 definitions (92.8%)** have either full support or at least one usable
  supported data path.
- **317 definitions** still need additional implementation or have no usable
  entity path (`metadata-only` + `unusable partial`).
- **4 definitions** are currently rejected by the parser.

Run the report yourself:

```shell
python3 scripts/coverage.py
python3 scripts/coverage.py --problem-limit 10
python3 scripts/coverage.py --json > coverage.json
```

The report also lists unsupported extend macros, converter bindings, definition
fields, and device-level problems. `--problem-limit 0` prints every problem
entry.

## Design goals

- Use declarative converter data wherever possible.
- Never execute JavaScript or TypeScript from a device definition.
- Keep unsupported behavior visible instead of silently pretending it works.
- Generate normal Python/ZHA runtime objects without a JavaScript engine.
- Make it possible to add small, auditable macros without adding arbitrary code
  execution.

The project is intentionally not a JavaScript compatibility layer. Dynamic
callbacks and device-specific executable converter code remain partial until
they can be represented safely as data or a constrained macro.

## Quick start

```python
from zha_z2m_converters import export_python, load_source, parse_source

source = load_source(
    "custom_components/zha_z2m_converters/converters/zigbee-herdsman-converters"
)
result = parse_source(source.text, source.filename)

print(result.devices)
print(result.diagnostics)
export_python(result.devices, "generated/converters.py")
```

Install the optional TypeScript parser for AST validation:

```shell
pip install -e '.[parser]'
```

The built-in parser still supports the safe declarative subset when the
optional `tree-sitter` dependency is not installed.

## Supported declarative features

The supported subset currently includes:

- standard ZHA cluster bindings for common sensors, switches, lights, and
  electrical measurements;
- static device fingerprints and Tuya fingerprints;
- static `tuya.whitelabel(...)` manufacturer aliases;
- `tuyaBase({dp: true})` and common Tuya datapoint macros:
  `dpOnOff`, `dpBinary`, `dpNumeric`, and `dpEnumLookup`;
- simple Tuya datapoint sensor and action wrappers;
- `tuyaBase({queryOnConfigure: true})`;
- `tuyaBase({bindBasicOnConfigure: true})`;
- `tuya.configureMagicPacket`, `tuya.configureQuery`, and
  `tuya.configureBindBasic` as declarative configure actions;
- safe static configure actions such as endpoint binds, attribute reads,
  reporting configuration, and whitelisted commands;
- `tuyaOnOff` and the declarative `onOffCountdown` plan;
- declarative `lumiZigbeeOTA` endpoint support.

Unsupported dynamic expressions, custom JavaScript converters, and complex
custom clusters are retained as partial definitions and are never evaluated.

## Runtime binding plan

The runtime layer builds a controller-independent plan for basic ZHA bindings
and processes reports without a JavaScript runtime:

```python
from zha_z2m_converters import RuntimeReport, apply_report, build_runtime_plan, make_write

plan = build_runtime_plan(device_definition)
state = apply_report(plan, RuntimeReport("temperature_measurement", "measured_value", 2150))
write = make_write(plan, "state", True)
```

`register_with_zha` adapts the same plan to the installed Home Assistant ZHA
`QuirkBuilder`. Fingerprints are registered as exact manufacturer/model
signatures, which allows vendor-specific definitions to take precedence over
generic built-in quirks.

## Home Assistant installation

Copy the package to:

```text
config/custom_components/zha_z2m_converters
```

Copy a converter snapshot to a readable path, then configure the integration:

```yaml
zha_z2m_converters:
  devices:
    - manufacturer: _TZ3000_46t1rvdu
      model: TS0001
    - manufacturer: Zbeacon
      model: TS011F
```

The bundled converter snapshot is installed with the integration and is used
automatically when `source` is omitted. Additional
user-provided TypeScript definitions can be placed in:

```text
<Home Assistant config directory>/external_converters/
```

All `.ts` files in that directory are loaded together with the bundled
snapshot. The component resolves this path through Home Assistant's actual
configuration directory, so it is not tied to `/config`. The directory is
outside `custom_components`, so HACS updates do not modify it. A custom
`external_source` path can be configured when user converters are stored
elsewhere.

The `devices` list is optional and is useful while validating selected devices.
Each entry may specify a manufacturer, a model, or both. If it is omitted, all
definitions from the selected source paths are registered.

The integration creates Python-side ZHA quirks through the installed
`QuirkBuilder`. It does not run TypeScript or JavaScript.

## Testing

Run the complete test suite with:

```shell
PYTHONPATH=custom_components python3 -m unittest discover -s tests -q
```

Run the coverage report against the bundled converter snapshot with:

```shell
python3 scripts/coverage.py --problem-limit 10
```

## License

This project is licensed under the [Apache License 2.0](LICENSE).
