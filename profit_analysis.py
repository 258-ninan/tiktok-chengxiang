"""Calculate order scenarios using explicitly supplied costs in one currency."""

import csv
from decimal import Decimal, InvalidOperation

from report_output import write_csv


NUMBER_FIELDS = """order_kg sale_per_kg purchase_per_kg loss_percent packaging_per_order freight_per_order
                   import_charges_per_order payment_percent acquisition_per_order other_per_order
                   fixed_cost_allocation_per_order""".split()
INPUT_FIELDS = ["scenario", "material", "grade", "destination", "currency"] + NUMBER_FIELDS + ["evidence"]
COMPUTED_FIELDS = [
    "revenue", "product_cost", "variable_cost", "contribution",
    "estimated_profit", "profit_per_kg", "profit_margin_percent",
]
OUTPUT_FIELDS = INPUT_FIELDS + ["status", "missing_fields"] + COMPUTED_FIELDS


def calculate(row):
    result = {key: (row.get(key) or "").strip() for key in INPUT_FIELDS}
    required = ["currency", "order_kg", "sale_per_kg", "purchase_per_kg"] + NUMBER_FIELDS[3:]
    missing = [key for key in required if not result[key]]
    if missing:
        return result | {"status": "needs_input", "missing_fields": "|".join(missing)}
    try:
        values = {key: Decimal(result[key]) for key in NUMBER_FIELDS}
    except InvalidOperation:
        raise ValueError(f"Invalid number in profit scenario {result['scenario']}") from None
    if (any(not value.is_finite() or value < 0 for value in values.values())
            or values["order_kg"] <= 0 or values["sale_per_kg"] <= 0
            or values["loss_percent"] >= 100 or values["payment_percent"] > 100):
        raise ValueError(f"Invalid quantities or percentages in profit scenario {result['scenario']}")
    revenue = values["order_kg"] * values["sale_per_kg"]
    product = values["order_kg"] * values["purchase_per_kg"] / (1 - values["loss_percent"] / 100)
    variable = product + revenue * values["payment_percent"] / 100 + sum(
        values[key] for key in (
            "packaging_per_order", "freight_per_order", "import_charges_per_order",
            "acquisition_per_order", "other_per_order",
        )
    )
    contribution = revenue - variable
    profit = contribution - values["fixed_cost_allocation_per_order"]
    computed = dict(revenue=revenue, product_cost=product, variable_cost=variable,
                    contribution=contribution, estimated_profit=profit,
                    profit_per_kg=profit / values["order_kg"], profit_margin_percent=profit / revenue * 100)
    return result | {"status": "calculated", "missing_fields": ""} | {
        key: str(value.quantize(Decimal("0.01"))) for key, value in computed.items()
    }


def export_profit(inputs, output):
    with inputs.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not set(INPUT_FIELDS) <= set(reader.fieldnames or []):
            raise ValueError("profit_inputs.csv has missing columns")
        scenarios = []
        for row in reader:
            if None in row:
                raise ValueError("Unexpected columns in profit_inputs.csv")
            scenarios.append(calculate(row))
    write_csv(output, OUTPUT_FIELDS, (
        row | {field: Decimal(row[field]) for field in COMPUTED_FIELDS if field in row}
        for row in scenarios
    ))
    return scenarios
