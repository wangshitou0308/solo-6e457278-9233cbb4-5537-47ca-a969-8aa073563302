API_DOCS = r"""# G-code 上机前检查 — 本地 API 文档

> 纯 Python 标准库实现（`http.server` + `sqlite3`），断网可用。
> **本服务不连接、不控制任何机床**，只做离线静态检查。

## 1. 启动

```bash
python3 -m gcode_checker                 # 默认 127.0.0.1:8080，数据库 ./gcode_checker.db
python3 -m gcode_checker --host 0.0.0.0 --port 9000 --db /var/lib/gc.db
python3 -m gcode_checker --verbose       # 打印访问日志
```

## 2. 方言范围（只支持以下指令，其余显式报告并整段阻断）

| 类别 | 指令 |
|---|---|
| 单位 | `G21` 公制 mm、`G20` 英制 inch（内部 ×25.4 换算 mm） |
| 定位 | `G90` 绝对、`G91` 增量 |
| 坐标系 | `G54`（偏置 X/Y/Z 由机床配置提供，叠加后做行程检查） |
| 运动 | `G0` 快速、`G1` 直线、`G2` 顺圆、`G3` 逆圆（仅 G17/XY 平面） |
| 圆弧参数 | `I J`（起点相对圆心，优先）或 `R`（负值=优弧）；允许 Z 联动螺旋下刀 |
| 工艺 | `F` 进给（按当前单位换算 mm/min）、`S` 主轴转速 rpm |
| 主轴 | `M3` 正转启动、`M5` 停止 |
| 词 | `X Y Z`、`N` 行号（忽略）；注释 `(...)` 与 `;...` |

**未支持示例**（遇到即 `UNSUPPORTED_INSTRUCTION`，整段不执行、不改模态）：
`G17/18/19、G28/30、G40-G43、G54.1/G55-G59、G80-G89、M2/M30、M4、M6、M7-M9、T/H/D/P/Q/L` 等。
无法解析的残片（如 `X-`）报 `MALFORMED_LINE`；若同行还有未支持词（如 `G55 X-`），
两类问题都会列出。

## 3. 机床配置字段

```json
{
  "name": "桌面三轴机",
  "travel_x": [0, 300], "travel_y": [0, 200], "travel_z": [-100, 0],
  "safe_z": 10,
  "max_feed_mm_min": 3000,
  "max_spindle_rpm": 12000,
  "offset_x": -150, "offset_y": -100, "offset_z": 0
}
```

- `travel_*` 也可写成正数标量表示 `[0, v]`；或直接给 `x_min/x_max/y_min/...`。
- 工件坐标 → 机床坐标：`machine = program + offset`。
- `safe_z` 按**工件（程序）坐标**判定快速移动。

## 4. 接口

### 元信息

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET | `/api/dialect` | 支持的指令、问题代码、严重度表 |
| GET | `/api/docs` | 本文档（Markdown） |
| GET | `/api/examples` | 内置示例 .nc 清单 |
| GET | `/api/examples/<name>` | 下载示例（safe_demo/problems_demo/inch_demo/arc_demo） |

### 机床配置

- `POST /api/machines`：body 为配置对象，返回 `{id, config}`。
- `GET /api/machines` / `GET /api/machines/<id>`
- `PUT /api/machines/<id>`（整体更新）、`DELETE /api/machines/<id>`

### 同步分析（不落库）

`POST /api/analyze`，body：

```json
{ "machine_id": "…", "program_name": "part1.nc", "gcode": "G21 G90 G54\n…" }
```

或不建配置，直接内联：`{"config": { …机床配置… }, "gcode": "…"}`。
立即返回完整报告。

### 异步作业（推荐用于大程序，SQLite 持久化）

- `POST /api/jobs`：body 同 `/api/analyze`。返回 `202` 与作业 `id`，后台线程分析。
- `GET /api/jobs`：作业列表。
- `GET /api/jobs/<id>`：状态与进度
  `{status: queued|running|completed|failed, progress: 0-100, risk…}`。
- `GET /api/jobs/<id>/report`：完成后取完整报告，支持筛选参数：
  - `severity=critical,error`（多选逗号分隔；级别 critical/error/warning/info）
  - `code=OUT_OF_BOUNDS,ARC_NO_SOLUTION`
  - `line_from=10&line_to=80`
  - `trajectory=0`：省略逐行轨迹以减小响应
- `GET /api/jobs/<id>/report/download`：以 `attachment` 下载 JSON 报告（筛选参数同上）。
- `GET /api/jobs/<id>/gcode`：下载作业原始 .nc 文本。

### 双程序风险对比（同一机床配置）

`POST /api/compare`：

方式一（内联文本）：

```json
{
  "machine_id": "…",
  "label_a": "v1.nc", "gcode_a": "…",
  "label_b": "v2.nc", "gcode_b": "…",
  "save": true
}
```

方式二（引用已完成作业）：

```json
{ "job_a_id": "…", "job_b_id": "…" }
```

两个作业必须使用相同配置（否则 `409 CONFIG_MISMATCH`）。
结果按问题指纹多重集匹配，给出 `resolved_issues`（旧有新无）、
`introduced_issues`（新增）、`unchanged_issues`（仍在），以及
`by_code` 计数变化、风险分/级别变化、路径长度与包围盒变化。
- `GET /api/comparisons` / `GET /api/comparisons/<id>`：读取保存的对比。

## 5. 报告结构

```jsonc
{
  "program": { "physical_lines": 12, "executed_lines": 9,
               "blocked_lines": 2, "blank_or_comment_lines": 1 },
  "machine": { …回显配置… },
  "final_state": { "unit": "mm", "distance_mode": "absolute", "wcs": "G54",
    "motion_mode": "linear", "x/y/z": {"value_mm": …, "known": true},
    "feed_mm_per_min": {…}, "spindle_rpm": 6000, "spindle_on": true },
  "bbox_program_mm": { "x_mm": [0, 90], "y_mm": […], "z_mm": […],
                       "size_mm": [90, 20, 52] },
  "bbox_machine_mm": { …叠加偏置后的机床坐标包围盒… },
  "path_length_mm": { "rapid": 123.4, "cutting": 88.1,
                      "total": 211.5, "reliable": true, "unknown_segments": 0 },
  "risk": { "score": 35, "level": "high",
            "counts_by_severity": {"critical": 1, "error": 1, …},
            "total_issues": 6 },
  "issues": [ …见下… ],
  "trajectory": [ …逐行… ],
  "policies": { …各判定口径的文字说明… }
}
```

每个问题都附原行、规范化指令、进入/离开状态与判定依据：

```jsonc
{
  "code": "OUT_OF_BOUNDS",
  "title": "越出机床行程",
  "severity": "critical",
  "line_no": 7,
  "source_line": "G1 X320 Y-5 F9000",
  "normalized": "G21 G90 G54 G1 X320 Y-5 F9000",
  "state_in":  { …进入本行的完整模态快照… },
  "state_out": { …离开本行的完整模态快照… },
  "basis": "X 轴机床坐标 170 mm 越出行程边界 150 mm（超程 20 mm；已叠加 G54 偏置）",
  "details": { "axis": "X", "value_mm": 170, "bound_mm": 150,
               "overshoot_mm": 20, "side": "max" }
}
```

逐行轨迹元素：

```jsonc
{
  "line_no": 7,
  "source_line": "G2 X50 Y20 I10 J0",
  "normalized": "G2 X50 Y20 I10 J0",
  "type": "arc_cw | rapid | linear | setting | blank_or_comment | blocked",
  "executed": true,
  "physical_known": true,
  "state_in": {…}, "state_out": {…},
  "issue_codes": ["…"],
  "segment": {
    "kind": "arc_cw",
    "start_mm": [10, 10, -2], "end_mm": [50, 20, -2],
    "length_mm": 32.1,
    "points_mm": [ …离散轨迹（直线取端点，圆弧自动加密）… ],
    "arc": { "plane": "G17(XY)", "programming": "I/J",
             "center_mm": [20, 10], "radius_mm": 40,
             "sweep_deg": -90, "helical": false, "z_change_mm": 0 }
  }
}
```

## 6. 问题代码与严重度

| 代码 | 默认严重度 | 触发条件 |
|---|---|---|
| `MALFORMED_LINE` | critical | 残片/非法数字（如 `X-`），整段阻断 |
| `UNSUPPORTED_INSTRUCTION` | error | 不在方言表内的指令/地址词，整段阻断 |
| `ARC_NO_SOLUTION` | critical | 半径 0、终点不落在圆上、弦长 > 2R 等；整段阻断并回滚本行模态 |
| `OUT_OF_BOUNDS` | critical | 轨迹上任意点叠加 G54 偏置后越出行程 |
| `RAPID_BELOW_SAFE_Z` | error | G0 轨迹上任意点 Z < safe_z（工件坐标） |
| `SPINDLE_NOT_RUNNING` | error | G1/G2/G3 切削时 `spindle_on=false` |
| `FEED_OVER_LIMIT` | error | F（换算 mm/min）> max_feed_mm_min |
| `SPINDLE_OVER_LIMIT` | error | S > max_spindle_rpm |
| `FEED_UNSET` | warning | 切削段前未建立有效 F |
| `NO_MOTION_MODE` | warning | 有轴词但没有任何 G0-G3 模态（不猜测运动） |
| `UNKNOWN_UNITS` | warning | G20/G21 建立前出现 F 或运动，位置标记未知 |
| `UNKNOWN_DISTANCE_MODE` | warning | G90/G91 建立前出现轴坐标，位置标记未知 |
| `UNKNOWN_WCS` | warning | G54 建立前运动：跳过行程检查，机床包围盒不可用 |

风险分：critical 25 / error 10 / warning 3 / info 1（封顶 100），
级别 `none/low(≤10)/medium(≤30)/high(≤60)/critical`。

## 7. 判定口径（关键约定）

1. **同组模态取同行最后一个**：如 `G20 G21 G90 G91` 离开状态为 mm + 增量，
   规范化输出 `G21 G91`。
2. **阻断即无痕**：含残缺或未支持指令的程序段不执行、不改变任何模态；
   圆弧无解时连同本行已改模态一起回滚。
3. **位置未知**：单位或定位模式不明时，不按默认值蒙算，相关轴 `known=false`，
   该段不进入包围盒/长度/行程统计，并给出对应 warning。
4. **路径长度**：仅累计物理已知段；圆弧按弧长（含 Z 联动的螺旋长度），
   直线按 3D 弦长。`reliable=false` 时报告会标注存在未知段。
5. **安全 Z 按工件坐标**判定；**行程按机床坐标**（叠加 G54 偏置）判定。
6. 服务监听本地回环，数据库为单个 SQLite 文件，全程无任何网络外联。

## 8. curl 快速上手

```bash
# 1) 建配置
curl -s localhost:8080/api/machines -H 'Content-Type: application/json' -d '{
  "name":"demo", "travel_x":[0,300], "travel_y":[0,200], "travel_z":[-100,0],
  "safe_z":10, "max_feed_mm_min":3000, "max_spindle_rpm":12000}'

# 2) 下载示例并建作业
curl -s localhost:8080/api/examples/problems_demo -o problems.nc
python3 - <<'PY'
import json,urllib.request
g=open('problems.nc',encoding='utf-8').read()
req=urllib.request.Request('http://localhost:8080/api/jobs',
  data=json.dumps({'machine_id':'<上一步id>','gcode':g}).encode(),
  headers={'Content-Type':'application/json'})
print(urllib.request.urlopen(req).read().decode())
PY

# 3) 查进度 / 筛选严重问题 / 下载
curl -s 'localhost:8080/api/jobs/<id>'
curl -s 'localhost:8080/api/jobs/<id>/report?severity=critical,error'
curl -s -OJ 'localhost:8080/api/jobs/<id>/report/download'
```
"""
