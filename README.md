# zha-zigbee-herdsman-converters

Safe declarative bridge between `zigbee-herdsman-converters` and ZHA.

The package does not execute JavaScript. It statically parses the supported
part of TypeScript definitions, builds its own IR, and can export it as a
regular Python module for ZHA.

## Quick start

```python
from zha_zhc import load_source, parse_source, export_python

source = load_source("vendor/zigbee-herdsman-converters")
result = parse_source(source.text, source.filename)
print(result.devices)
print(result.diagnostics)
export_python(result.devices, "generated/converters.py")
```

Install the optional dependency to enable AST validation for real TypeScript:

```shell
pip install -e '.[parser]'
```

Without `tree-sitter`, the package still supports the limited declarative
subset through its built-in static parser. No code path executes JavaScript.

## Coverage report

Run the report against the pinned upstream snapshot:

```shell
python3 scripts/coverage.py
python3 scripts/coverage.py --json > coverage.json
```

The report shows source files, total definitions, discovered definitions,
fully supported definitions, partial definitions, rejected definitions, and
aggregated unsupported converters or other problems. Use `--problem-limit` to
control how many device-level problem entries are printed. Unsupported
executable fields such as `configure` are retained as partial definitions when
their static metadata can still be recovered; they are never executed.

## Runtime binding plan

The runtime layer can build a controller-independent plan for basic ZHA
bindings and process reports without a JavaScript runtime:

```python
from zha_zhc import RuntimeReport, apply_report, build_runtime_plan, make_write

plan = build_runtime_plan(device_definition)
state = apply_report(plan, RuntimeReport("temperature_measurement", "measured_value", 2150))
write = make_write(plan, "state", True)
```

The current plan covers standard temperature, humidity, pressure, battery,
occupancy, and on/off attribute mappings. `register_with_zha` uses the same
plan when adapting entities to the installed ZHA `QuirkBuilder`.

Simple declarative configure callbacks are also represented in the plan. The
initial whitelist includes endpoint-to-coordinator cluster binds written as
`device.getEndpoint(1).bind(coordinatorEndpoint, "hvacThermostat")` or
`reporting.bind(endpoint, coordinatorEndpoint, ["genPowerCfg"])`, endpoint
attribute reads, and the common electrical-meter multiplier read helpers.
Other callback statements remain marked as partial and are never executed.
