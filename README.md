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
fully supported definitions, partial definitions, and rejected definitions.
Partial definitions are split into `Usable partial` and `Unusable partial`:
the latter have no reliable supported data path after static extraction. For
example, a device whose readings depend entirely on an unsupported custom
datapoint converter is not counted as usable merely because some metadata or
exposes were recovered. `Rejected` is reserved for definitions that could not
be recovered by the parser at all. Use `--problem-limit` to control how many
device-level problem entries are printed. Unsupported executable fields such
as `configure` are retained as partial definitions when their static metadata
can still be recovered; they are never executed.

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
occupancy, on/off attribute mappings, and the declarative Tuya datapoint
subset. `register_with_zha` uses the same plan when adapting entities to the
installed ZHA `QuirkBuilder`.

Static device fingerprints such as `tuya.fingerprint("TS0001", ["_TZ..."])`
are preserved in the IR. The ZHA adapter registers one builder signature per
fingerprint, so vendor-specific manufacturer names and model IDs can match
without executing the converter module.

Direct fingerprint helpers and static `tuya.whitelabel(...)` manufacturer
aliases are also converted to exact ZHA signatures. This is important for
devices such as `Zbeacon / TS011F`: once the integration is filtered to that
manufacturer and model, its generated v2 quirk is registered for the exact
signature and takes precedence over a generic built-in model-only quirk.

Simple declarative configure callbacks are also represented in the plan. The
initial whitelist includes endpoint-to-coordinator cluster binds written as
`device.getEndpoint(1).bind(coordinatorEndpoint, "hvacThermostat")` or
`reporting.bind(endpoint, coordinatorEndpoint, ["genPowerCfg"])`, endpoint
attribute reads, static `endpoint.configureReporting(...)` payloads, and common
static reporting helpers such as `reporting.temperature(endpoint)`. Static
`endpoint.command(cluster, command, payload)` calls and the Tuya helpers
`configureQuery` and `configureBindBasic` are also represented as declarative
actions. Local constant arrays and indexed endpoint access are resolved without
evaluating expressions. Other callback statements remain marked as partial and
are never executed.

The safe Tuya subset currently includes `tuyaBase()` with its static `dp`
option, `dpOnOff`, `dpBinary`, `dpNumeric`, and `dpEnumLookup`, plus the
simple sensor/action wrappers built on them. They are supported when their
datapoint, type, lookup, and scalar or four-number range scale are static.
These macros become
declarative bindings for the Tuya MCU `dpValues` attribute; reports and writes
are represented by Python data, and the ZHA adapter creates a Python-only
`TuyaMCUCluster` subclass for those bindings. Dynamic skips, callbacks, and
other executable converter logic remain partial.

The subset also includes standard `tuyaOnOff()` switch bindings, static Tuya
fingerprints, `tuya.configureMagicPacket` as a `genBasic` read plan, and the
declarative `onOffCountdown` report/command plan. `RuntimeWrite` represents
`genOnOff.onWithTimedOff` as a command with an explicit payload. The ZHA
adapter installs a small Python-only custom cluster so the countdown number
entity can use the standard `number()` builder API while translating writes
to that command. Other device-specific features that require custom converter
behavior remain explicitly partial.

## Home Assistant installation

The package can run as a Home Assistant custom integration. Copy the package
to `custom_components/zha_zhc`, copy a converter snapshot to a readable path,
and add a YAML entry before starting Home Assistant:

```yaml
zha_zhc:
  source: /config/zha_zhc/converters/src/devices/tuya.ts
  devices:
    - manufacturer: _TZ3000_46t1rvdu
      model: TS0001
    - manufacturer: Zbeacon
      model: TS011F
```

The optional `devices` list is useful while validating selected devices. Each
entry may specify a manufacturer, a model, or both. Multiple entries are
combined. If `devices` is omitted, every definition from the selected source
file or directory is registered.
The integration registers definitions through the installed ZHA
`QuirkBuilder`; it does not run TypeScript or JavaScript.

For standard electrical measurement clusters, the adapter keeps ZHA's native
measurement entities instead of creating duplicate converter entities. This
allows the device's native scaling and reporting behavior to remain visible.
