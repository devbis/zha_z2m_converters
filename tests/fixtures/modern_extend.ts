export const definitions = [
    {
        zigbeeModel: ["MODERN-LIGHT"],
        model: "Modern Light",
        vendor: "Example",
        extend: [
            m.light({colorTemp: {range: [200, 400]}}),
            m.battery({voltage: true}),
            m.identify(),
        ],
    },
];
