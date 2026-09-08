export const definitions = [
    {
        zigbeeModel: "TEST-PLUG",
        model: "Test Plug",
        vendor: "Example",
        description: "A static fixture",
        exposes: [
            {
                type: "switch",
                name: "state",
                property: "state",
                access: ["state", "set"],
            },
            {
                type: "numeric",
                name: "temperature",
                property: "temperature",
                unit: "°C",
                valueMin: -40,
                valueMax: 125,
            },
        ],
        fromZigbee: [fz.on_off, fz.temperature],
        toZigbee: [tz.on_off],
    },
];
