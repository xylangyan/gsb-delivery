# Sample archive audit

这个模块处理“模板 + 次数 + 交接标记”的点检归档。它递归执行模板调用，不生成完整展开程序；窗口查询只保存实际采到的机床 XY 点，不预建格点明细表。

## 接口

```python
from pygcode import SampleArchive

archive = SampleArchive("archive.json")
count = archive.count_window([xmin, ymin, xmax, ymax])
first = archive.first_at(position=[machine_x, machine_y])
first_by_work = archive.first_at(
    work_position=[work_x, work_y],
    coordinate_system="G55",
)
check = archive.verify_short()
```

命令行：

```bash
python sample_audit.py audit archive.json \
  --window XMIN YMIN XMAX YMAX \
  --position MX MY \
  --output submission.json

python sample_audit.py verify-short short-archive.json
```

## 归档格式

```json
{
  "offsets": {
    "G54": [100, 200, 0],
    "G55": [-5, 7, 0]
  },
  "templates": {
    "Outer": {
      "lines": [
        {"line": 12, "gcode": "G90 G54 G0 X0 Y0"},
        {"line": 18, "gcode": "X2", "sample": true},
        {"line": 21, "call": "Inner", "repeat": 4}
      ]
    },
    "Inner": {
      "lines": [
        {"line": 31, "gcode": "G91 X1"},
        {"line": 32, "gcode": "Y1", "sample": true}
      ]
    }
  },
  "program": [
    {"template": "Outer", "repeat": 2}
  ],
  "window": [0, 0, 200, 200],
  "position": [102, 200]
}
```

交接单也可单独给出：

```json
{
  "handoff": {
    "Outer": [18],
    "Inner": [32]
  }
}
```

## 坐标语义

- 机床坐标按 pygcode `Machine.work2abs` 计算：`machine = work + G5x_offset`。
- `G90` 绝对模式只更新本段写出的轴；未写出的 X/Y 保持原机床位置，不会补零。
- `G91` 相对模式只对写出的轴加增量。
- 切换 `G54`/`G55` 不移动当前机床点；下一段绝对坐标使用新偏移，因此同样的工件坐标 `X2` 会得到不同机床位置。
- 去重键和窗口键都是实际机床 XY。

首采证明包含 `root_template`、`template`、完整 `stack`、各层 `repeat`、模板累计出现次数、`original_line`、显式偏移、工件坐标和机床坐标。

短档用 `verify_short()` 或 `verify-short` 同时驱动内置整数状态和未修改的 `pygcode.Machine`，逐个核对带标记行的 `abs_pos`。长档的普通 `count_window()` 走预编译轻量路径，不运行完整 `Machine`。
