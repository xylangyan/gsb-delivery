import json

import jsonata
from jsonata.utils import Utils


NULL = Utils.NULL_VALUE


def evaluate(expression, rows):
    expr = jsonata.Jsonata(expression)
    expr.set_output_convert_nulls(False)
    return expr.evaluate({"rows": rows})


def render(value):
    if value is None:
        return "undefined"
    if value is NULL:
        return "null"
    return json.dumps(Utils.convert_nulls(value), ensure_ascii=False)


def replay(name, expression, rows):
    try:
        result = evaluate(expression, rows)
        print(f"{name}\n  expr: {expression}\n  result: {render(result)}")
    except jsonata.JException as exc:
        print(f"{name}\n  expr: {expression}\n  error: {exc.error}: {exc}")


sku_rows = [
    {"original_id": "W1", "mainSku": "A", "supplementSku": "M", "qty": 1},
    {"original_id": "W2", "mainSku": "B", "supplementSku": "A", "qty": 2},
]

position_rows = [
    {"original_id": "W1", "n": 1, "qty": 1},
    {"original_id": "W2", "n": 2, "qty": 2},
    {"original_id": "W3", "n": 3},
]

replay("main-sku-group", "rows{mainSku: $sum(qty)}", sku_rows)
replay("supplement-sku-group", "rows{supplementSku: $sum(qty)}", sku_rows)
replay(
    "same-group-collision",
    "rows{(mainSku): $sum(qty), (supplementSku): $sum(qty)}",
    sku_rows,
)
replay("duplicated-last-raw", "rows^(n)[[-1,-1]].original_id", position_rows)
replay("missing-last-qty", "rows^(n)[[-1,-1]].qty", position_rows)
replay("after-unreported-w3-deleted", "rows^(n)[[-1,-1]].qty", position_rows[:2])
replay(
    "explicit-null-last-qty",
    "rows^(n)[[-1,-1]].qty",
    [{**position_rows[2], "qty": None}],
)
