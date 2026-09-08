# Upstream snapshot

Place a pinned checkout of
[`Koenkk/zigbee-herdsman-converters`](https://github.com/Koenkk/zigbee-herdsman-converters)
in this directory. The loader reads `src/devices/*.ts` from the checkout and
does not import or execute the package.

Record the exact upstream commit and package version in `manifest.json` when a
snapshot is added. Snapshot updates must be reviewed together with the parser
coverage report and golden test changes.
