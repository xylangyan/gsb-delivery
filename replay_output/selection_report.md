# 本地回放包选择结果

案例：`供货清单本机回归参考案例`；合规交错：`70` 种。

**选择**

- 已批准候选中的最小合规回放包：`approved-min`。
- 登记原文字节：`94380`；回包项：`8`；规范化状态：`25`。
- 逐项删除证书：`全部不可删`；检查 `10` 项，结果见 `replay_output/minimality_certificate.json`。

| 登记包 | 合规 | 字节 | 回包项 | 状态数 | 最早分歧 |
| --- | ---: | ---: | ---: | ---: | --- |
| `original-heavy` | 是 | 104440 | 10 | 25 | — |
| `approved-min` | 是 | 94380 | 8 | 25 | — |
| `shared-narrow` | 否 | 93740 | 7 | 24 | `BBBBAAAA#4` job beta step #4 differs |

**最早分歧**

- 登记包：`shared-narrow`；调度：`BBBBAAAA`；全局事件：`4`。
- 实际请求：`GET http://supply-test.local/detail?line=B&station=ST-7&station=ST-7`。
- 上一笔回包：原件 `ORIG_DETAIL_BUSY`；许可下一页/补件字段：`{"retry": true}`。
- 消耗位置：`[{"item_id": "shared-ok-a", "original_id": "ORIG_DETAIL_A", "position": 1, "registration": "detail-shared-sequence", "remaining_item_ids": ["shared-ok-b"]}]`。
- 期望：`{"attempt": 2, "exception": null, "method": "GET", "original_id": "ORIG_DETAIL_B", "status": 200, "step": "detail", "url": "http://supply-test.local/detail?line=B&station=ST-7&station=ST-7"}`。
- 实际：`{"attempt": 2, "exception": null, "method": "GET", "original_id": "ORIG_DETAIL_A", "status": 200, "step": "detail", "url": "http://supply-test.local/detail?line=B&station=ST-7&station=ST-7"}`。
- 原库重现脚本：`replay_output/repro_shared_narrow.py`。

**复跑**

- 校验：`./.venv/bin/python local_regression.py verify`。
- 重选：`./.venv/bin/python local_regression.py select`。
- 重现 `shared-narrow`：`./.venv/bin/python replay_output/repro_shared_narrow.py`。
