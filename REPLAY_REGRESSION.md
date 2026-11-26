# 调度感知的 requests-mock 回归入口

入口是 `local_regression.py`，也可通过 `./run_regression.sh` 自动选择工作区虚拟环境。它会用真实的 `requests_mock.Mocker(case_sensitive=True)` 执行两个声明式作业，并枚举两个作业、每作业最多四笔请求的全部 70 种交错。

当前工作区只有 `requests-mock` 上游仓库，没有题述的真实原夹具文件；因此 `replay_cases/supply_chain.json` 是全测试地址的可运行参考案例。把真实夹具按文末 schema 落成 JSON 后，用 `--case /path/to/real-case.json` 即可得到真实编号的最小包和分歧脚本。

**命令**
- `./.venv/bin/python local_regression.py list`：列出作业、候选登记和字节成本。
- `./.venv/bin/python local_regression.py verify`：对照基线校验全部候选。
- `./.venv/bin/python local_regression.py select`：选择最小合规包并写出报告。
- `./.venv/bin/python local_regression.py verify --plan shared-narrow`：只校验一个登记包。
- `./.venv/bin/python local_regression.py repro --plan shared-narrow`：生成最早分歧重现脚本。

**产物**
- `replay_output/minimal_replay_case.json`：只含选中登记的自包含案例，可直接传给 `--case`。
- `replay_output/selected_package.json`：选择结果和选中的登记。
- `replay_output/evaluation_results.json`：全部候选的机器可读比对结果。
- `replay_output/selection_report.md`：中文选择报告和最早分歧。
- `replay_output/repro_shared_narrow.py`：可在本库直接复跑的分歧脚本。

**比对口径**
- 只把实际发出的请求记入轨迹；不会用另一种顺序的 URL 清单代替回执。
- 每个作业按自己的第 N 步比较：实际 URL、方法、状态或异常、原回包编号和结束分支。
- 全局事件额外记录调度串、`global_index`、登记消耗位置和消耗后的 `remaining_item_ids`。
- 成本按登记中的每个回包项计算；同一原件配到两个登记时，原文字节在两个登记中各计一次。
- `replay_lab/graph.py` 用规范化的作业状态、变量和登记余项合并等价状态，避免同一状态重复展开。

**案例格式**

真实夹具未随当前工作区提供时，可把原夹具整理成 `replay_cases/*.json`。字段包括：

- `schedule.attempts_per_job`：每种作业纳入交错枚举的请求尝试数；暂时失败后的重试也计为一次尝试。
- `jobs.<id>.requests`：首请求或由回包字段得到的 `url_var`。
- `jobs.<id>.transitions`：只读 `status`、`exception` 和白名单 JSON 字段，决定下一页、补件入口、重试或结束分支。
- `plans[].registrations[]`：按注册顺序写 URL、`complete_qs`、`request_headers`、纯请求谓词 `additional_matcher` 和 `responses`。
- `originals`：原回包编号及 `raw_bytes`。
- `required_receipts`：必须保留的原回包编号。

当前 `replay_cases/supply_chain.json` 是全测试地址的参考案例，覆盖单次/重复 `station=ST-7`、后注册窄登记、由当前请求中的补件来源触发的 `additional_matcher`、首项 503 消耗，以及有状态共享序列导致的交错分歧。
