# 付料回执共同核对

实现在 `pyDatalog/reconcile.py`，根目录的 `reconcile.py`、`solution.py` 是兼容入口。

## 调用

```python
from pyDatalog.reconcile import reconcile_receipts

result = reconcile_receipts({
    "facts": [...],
    "choices": [
        {
            "id": "gap-1",
            "copies": {
                "copy-a": [
                    {"department": "issue", "part": "P-1", "amount": 3, "source": "line-1"},
                ],
                "copy-b": [
                    {"department": "issue", "part": "P-1", "amount": 7, "source": "line-1"},
                ],
            },
        }
    ],
    "signed_totals": [
        {"department": "quality", "total": 1, "receipt": "Q-1"},
    ],
    "transfers": [("P-1", "ACT-1", "edge-1")],
    "withheld": ["HELD-1"],
    "base_totals": {"issue": 3},
    "department": "issue",
    "interval": [6, 10],
})
```

也可从标准输入读 JSON：`uv run python solution.py < input.json`。

## 语义

- 每个 choice 恰好两份整档抄件；DP 中的一个二进制变量对应一处选择。
- 同一组二进制选择同时代入缺账部门和所有已签合计，不会按部门分别选择。
- `transfer/3` 经递归规则得到末端实际料号；`release_fact/2` 在递归完成后用否定排除 `withheld/1`。
- 两条路径到达同一实际料号时，`release_fact/2` 按事实去重；路径证明单独保留在 `release_paths`。
- 每份抄件的部门合计由 pyDatalog 现行 `sum_(Value, for_each=(Part, Source))` 计算。
- 可能总量是精确整数方程的离散解；不会把 6 和 10 之间的 7、8、9 视为可达。
- 不同选择得到同一目标总量时，结果字典只保留一个总量和一个可复读见证。
- 大账只共享一次递归闭包/缓存与一次聚合，再运行带残差边界的记忆化整数 DP；不按 `2^处数` 逐组合重跑 Datalog 规则。

返回对象包含 `count`、`minimum_reachable`、`minimum_unreachable`、`witness`、`included_lines`、`possible_totals`、`conflicts` 和 `explanation`，也支持字典键访问与 `as_dict()` JSON 序列化。
