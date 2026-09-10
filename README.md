# zha-z2m-converters

`zha-z2m-converters` is a safe, declarative bridge between
[zigbee-herdsman-converters](https://github.com/Koenkk/zigbee-herdsman-converters)
and Home Assistant ZHA.

It reads a fresh converter snapshot, extracts the supported static parts, and
turns them into Python-side ZHA definitions. It does not execute JavaScript or
TypeScript and does not require V8, Node.js, or another JavaScript runtime.

## Coverage

Coverage is measured against the bundled converter snapshot in
`custom_components/zha_z2m_converters/converters/`. The generated block below
is refreshed from the snapshot by `scripts/update_readme_coverage.py`.

<!-- coverage:start -->
| Status | Definitions | Share | Meaning |
|:---:|---:|---:|---|
| ✅ Fully supported | **2,544** | **56.8%** | Static definition and all extracted features are supported. |
| 🟡 Usable partial | **1,614** | **36.0%** | Some features are missing, but at least one reliable data path remains. |
| 🟠 Metadata only | **11** | **0.2%** | Device metadata was recovered, but no usable entity or data binding exists. |
| 🔴 Unusable partial | **305** | **6.8%** | The remaining functionality depends on unsupported converter logic or has no usable data path. |
| ⛔ Rejected | **4** | **0.1%** | The definition could not be recovered by the static parser. |
| **Total** | **4,478** | **100%** | All definitions found in the snapshot, including rejected definitions. |

### At a glance

- **4,158 definitions (92.9%)** have either full support or at least one usable
  supported data path.
- **316 definitions** still need additional implementation or have no usable
  entity path (`metadata-only` + `unusable partial`).
- **4 definitions** are currently rejected by the parser.
<!-- coverage:end -->

## Installation

### HACS

1. Open **HACS → Integrations** in Home Assistant.
2. Search for **ZHA Z2M Converters** and install it.
3. Restart Home Assistant.

If the project is not available in the HACS index yet, add the GitHub
repository as a custom HACS repository with category **Integration**, then
install it and restart Home Assistant.

### Basic configuration

Add this single entry to `configuration.yaml`:

```yaml
zha_z2m_converters:
```

Restart Home Assistant again. The integration will load all definitions from
the converter snapshot bundled with the component.

## Configuration

The default configuration is enough for normal use. Optional settings are
available when testing selected devices or adding local converter definitions.

### Select specific devices

Use `devices` to register only selected definitions while validating a device:

```yaml
zha_z2m_converters:
  devices:
    - manufacturer: _TZ3000_46t1rvdu
      model: TS0001
    - manufacturer: Zbeacon
      model: TS011F
```

Each selector may contain only `manufacturer`, only `model`, or both. If
`devices` is omitted, all definitions from the selected source paths are
registered.

### External converters

Place user-provided TypeScript definitions in:

```text
<Home Assistant configuration directory>/external_converters/
```

All `.ts` files in that directory are loaded together with the bundled
snapshot. The path is resolved through Home Assistant's actual configuration
directory and is not hardcoded to `/config`.

To use another location, set `external_source`:

```yaml
zha_z2m_converters:
  external_source: /path/to/external_converters
```

Relative paths are resolved from Home Assistant's configuration directory.
The external directory is outside `custom_components`, so HACS updates do not
modify user-provided converters.

### Custom bundled source

The bundled snapshot is used automatically. A different source directory can
be selected with `source`:

```yaml
zha_z2m_converters:
  source: /path/to/zigbee-herdsman-converters
```

The selected source must contain TypeScript converter files.

## How it works

The integration follows a small, safe pipeline:

1. It reads the bundled converter snapshot and any configured external
   converter files.
2. It parses only the declarative subset into a Python intermediate
   representation.
3. It maps that representation to ZHA entities, clusters, fingerprints, and
   configure actions.
4. It registers the generated definitions with Home Assistant's ZHA runtime.

JavaScript and TypeScript callbacks are never executed. Unsupported dynamic
parts are reported in coverage results instead of being evaluated. This keeps
the integration independent of Node.js and V8 while leaving room for future
safe AST-based translations of small, explicitly supported code patterns.

## Design goals

- Use declarative converter data wherever possible.
- Never execute JavaScript or TypeScript from a device definition.
- Keep unsupported behavior visible instead of silently pretending it works.
- Generate normal Python/ZHA runtime objects without a JavaScript engine.
- Add small, auditable macros only when they can be represented safely as
  data.

The project is intentionally not a JavaScript compatibility layer. Dynamic
callbacks and device-specific executable converter code remain partial until
they can be represented safely as data or a constrained macro.

### Python API

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

## Supported features

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

## Runtime details

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

## Testing and coverage reports

Run the complete test suite with:

```shell
PYTHONPATH=custom_components python3 -m unittest discover -s tests -q
```

Generate the device support report against the bundled converter snapshot:

```shell
python3 scripts/coverage.py
```

Show the first ten problems for investigation:

```shell
python3 scripts/coverage.py --problem-limit 10
```

Export machine-readable results for automation:

```shell
python3 scripts/coverage.py --json > coverage.json
```

The report separates fully supported, usable partial, metadata-only,
unusable partial, and rejected definitions. A partial definition may still be
usable when at least one entity or data path was recovered; the problem list
explains which features were not translated. Use `--problem-limit 0` to print
all problems.

The README coverage table is generated from the same report:

```shell
python3 scripts/update_readme_coverage.py
```

The repository pre-commit hook checks that the generated table is current.
Install and run it with:

```shell
pip install pre-commit
pre-commit install
pre-commit run --all-files
```

## License

This project is licensed under the [Apache License 2.0](LICENSE).
