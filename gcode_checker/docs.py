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
| 坐标系 | `G54`-`G59`（各坐标系 X/Y/Z 偏置由机床配置 `wcs_offsets` 分别提供，叠加后做行程检查） |
| 平面 | `G17` XY（上电默认，联动轴 Z）、`G18` XZ（联动轴 Y）、`G19` YZ（联动轴 X），模态保持 |
| 运动 | `G0` 快速、`G1` 直线、`G2` 顺圆、`G3` 逆圆（当前平面内插补） |
| 固定循环 | `G80` 取消、`G81` 钻孔、`G82` 锪孔（`P` 孔底暂停）、`G83` 深孔啄钻（`Q` 分步）；`G98` 返回初始平面（默认）、`G99` 返回 R 平面；`R` R 平面、`L` 重复孔位；**仅允许在 `G17` 平面展开** |
| 圆弧参数 | `G17` 用 `I J`、`G18` 用 `I K`、`G19` 用 `J K`（起点相对圆心），或 `R`（正=劣弧，负=优弧）；垂直当前平面的轴随扫角线性联动（螺旋插补） |
| 工艺 | `F` 进给（按当前单位换算 mm/min）、`S` 主轴转速 rpm |
| 主轴 | `M3` 正转启动、`M5` 停止 |
| 词 | `X Y Z`、`I J K`（圆弧圆心）、`Q P L`（固定循环）、`N` 行号（忽略）；注释 `(...)` 与 `;...` |

### 工件坐标系（G54-G59）口径

- `G54`-`G59` 为**模态**切换，每个坐标系的 X/Y/Z 偏置在机床配置
  **`wcs_offsets`** 中分别设置；旧字段 `offset_x/offset_y/offset_z` 归入
  `G54`（显式 `wcs_offsets.G54` 优先），已有配置与作业可直接读取。
- **换系时刀具的机床位置不动**，工件坐标随新偏置重新换算
  （`新工件坐标 = 旧工件坐标 + 旧偏置 - 新偏置`）；后续直线、圆弧、
  螺旋和固定钻孔循环都按当前坐标系生成机床轨迹，安全 Z 仍按当前
  工件坐标判定。
- 程序引用**未配置偏置**的坐标系时**不沿用上一偏置**：报
  `UNKNOWN_WCS`（`details.reason=wcs_not_configured`），跳过行程检查，
  相关机床坐标、行程及包围盒结论标为未知（`bbox_machine_mm=null`，
  分工坐标系统计中该坐标系 `machine_bbox_mm=null`）。
- 每段轨迹记录 `wcs`、`offset_mm`、工件坐标（`start_mm/end_mm/points_mm`）
  与机床坐标（`start_machine_mm/end_machine_mm/points_machine_mm`，
  偏置未知时为 `null`）；固定循环的每个孔记录 `wcs` 与孔位机床坐标
  （`machine_x_mm/machine_y_mm`），每个展开动作带机床坐标。
- 偏置值非法时配置校验会**定位到坐标系与字段**，如
  `wcs_offsets.G55.x 必须是数值`。

### 圆弧与螺旋插补口径

- 平面为**模态**：`G17/G18/G19` 切换后保持，未写过按控制器上电默认 `G17`。
  旋向按“从垂直轴正向看向平面”判定（右手系：G17 看 XY、G18 看 XZ、G19 看 YZ）。
- 圆心词为**起点到圆心的增量**（不受 G90/G91 影响）：`G17`→`I/J`、
  `G18`→`I/K`、`G19`→`J/K`。起终点重合时用圆心词编程即**整圆**；
  省略 `XYZ` 终点词时终点即当前点，同样按整圆执行（如 `G2 I10 J0`）。
- `R` 编程：正值为劣弧（扫角 ≤180°），负值为优弧；**R 不能编程整圆**
  （起终点重合时阻断）。弦长 > 2R 同样无解。
- **螺旋**：垂直当前平面的第三轴随扫角线性联动，段长按三维螺旋长度
  `√(弧长² + 联动位移²)` 计入；整圆也可以带联动（如铣螺纹）。
- 每段弧在轨迹中记录：平面、旋向、圆心（平面坐标 + 三维坐标）、半径、
  扫角、联动轴与联动位移、平面弧长与三维长度；包围盒与行程检查按
  **真实弧线**（端点 + 扫过的象限角极值点）精确计算，不做弦线近似。
- 以下情况报 `ARC_NO_SOLUTION`，**定位原行、整段阻断**，进入该行前的
  模态与位置保持不变（含本行写的平面/单位等全部回滚）：
  圆心词与 `R` 混用；圆心词不属于当前平面（如 `G17` 下给 `K`）；
  `R` 编程整圆；圆心词编程时起终半径不一致；圆心半径为 0；
  起终点坐标未知。

### 固定钻孔循环展开口径

- 循环为**模态**：`G81/G82/G83` 定义后，后续含 `X/Y`（或本行 `L`）的程序段
  连续触发孔加工；`G80` 或任意 `G0-G3` 取消循环。`Z/R/Q/P` 模态继承，
  可在定义时或后续段逐步给出。
- `L` 为孔位重复次数，默认 1，必须为**正整数**：`G90` 下在同一位置重复；
  `G91` 下按本行 XY 增量逐次平移，展开为连续孔。
- 坐标语义：`G90` 下 `R/Z` 为绝对坐标；`G91` 下 `R` 相对循环建立时锁定的
  **初始平面**，`Z` 相对 **R 平面**；`Q` 恒为正的无符号步进深度（mm）。
  `P` 为孔底暂停：整数按毫秒、小数按秒（`P500` = `P0.5` = 0.5 s）。
- 每个孔记录完整展开轨迹：孔间 `position` 快速定位 → 快速到 R（G99 连续孔
  可能是零长度）→ 进给下钻（G83 为 `Q` 分步 + 退回 R 排屑 + 快速接近 +
  进给走完预留量）→ G82 孔底 `dwell` → 快速返回初始平面/R 平面。
  每个动作带 `hole_no`，定位动作长度计入对应孔的 `expanded_path_mm`。
- 阻断规则（**只阻断对应孔**，登记孔记录与依据，原程序不变、后续模态可继承）：
  首次启用缺 `Z` 或 `R`（G83 还需正的 `Q`）→ `CYCLE_MISSING_PARAMS`；
  `Q<=0`、`P` 为负、`L` 非正整数 → `CYCLE_BAD_PARAM`；
  孔底高于 R 平面 → `CYCLE_PLANE_CONFLICT`；
  后续孔位缺少可继承的 XY/初始平面 → `CYCLE_NO_INHERITABLE_STATE`；
  `G18/G19` 平面下定义或触发 → `CYCLE_PLANE_NOT_G17`
  （固定循环仅允许在 `G17` 平面展开，循环模态仍登记，`G17` 恢复后可继续触发）。
- 循环展开轨迹复用行程/安全 Z/进给/主轴检查；G83 循环内部排屑快速动作豁免
  安全 Z 告警，孔间定位（尤其 G99 在 R 高度横移）仍报 `RAPID_BELOW_SAFE_Z`。
  主轴未转/无进给等工艺问题按**触发行**去重（一个 G83 孔只报一次）。

**未支持示例**（遇到即 `UNSUPPORTED_INSTRUCTION`，整段不执行、不改模态）：
`G28/30、G40-G43、G54.1、G84-G89、M4、M6、M7-M9、T/H/D` 等。
无法解析的残片（如 `X-`）报 `MALFORMED_LINE`；若同行还有未支持词（如 `G54.1 X-`），
两类问题都会列出。

> 注：`M98/M99/M2/M30` 与子程序号 `O` 只在**程序包静态展开**
> （`POST /api/packages`）中支持，单独提交给 `/api/analyze` 或 `/api/jobs`
> 时仍按未支持指令处理。变量/宏表达式（`#`、`[]`）在任何模式下都不支持。

## 2b. 程序包静态展开（主程序 + O 号子程序集）

程序包把**主程序**、**带 O 号的子程序集**和**机床配置**作为一个持久化作业，
先做静态展开，再交给上面的逐行轨迹与安全检查。

### 展开语义

- 子程序以 `O<n>` 行开头（必须是第一条指令）、以 `M99` 结尾；
  `M98 P<n> L<k>` 调用子程序 `O<n>` 共 `k` 次（`L` 默认 1，必须为正整数）。
- **调用时继承模态**：子程序看到的单位/定位/坐标系/平面/运动/进给/主轴/
  固定循环等全部模态均来自调用点。
- **返回后从调用点继续**：`M99` 回到 `M98` 的下一程序段。
- **重复调用不重置状态**：`L<k>` 的 k 次展开共享同一段连续模态流
  （例如子程序内用 `G91 G81 X.. L3`，连续两次调用沿同一排孔继续）。
- `M2/M30` 结束整个程序（子程序中出现也终止全部执行）。
- 每个展开块保留 `source_program`（`main` 或 `O100`）、来源文件、原行号、
  原行文本、`depth` 与完整 `call_stack`（每级含调用行、`repeat_index`/
  `repeat_total`）；轨迹条目和问题都带这些字段。
- 调用图节点含 `defined`（子程序是否提供）与 `reachable`（从主程序是否可达）；
  边汇总静态调用点数 `sites`、遍历执行次数 `executions` 与展开调用次数
  `invocations`（含 L 重复）。

### 展开阶段阻断（不生成部分安全结论）

下列情况整体阻断：作业 `status=blocked`，报告只含 `call_graph` 与
`expansion_errors`，**没有** risk/trajectory/issues，也不生成展开块：

| 代码 | 触发条件 |
|---|---|
| `PACKAGE_DUPLICATE_O` | 两个子程序使用同一 O 号 |
| `PACKAGE_SUBPROGRAM_NOT_FOUND` | `M98 P<n>` 的目标不在程序包中 |
| `PACKAGE_M99_IN_MAIN` | 主程序中出现 M99 |
| `PACKAGE_SUBPROGRAM_MISSING_M99` | 子程序最后一条非空指令不是 M99 |
| `PACKAGE_RECURSIVE_CALL` | 调用图存在递归环（含间接递归） |
| `PACKAGE_DEPTH_LIMIT` | 调用嵌套深度超过 `max_depth`（默认 50，请求可设 1..1000） |
| `PACKAGE_BLOCK_LIMIT` | 展开块数超过 100,000（硬上限） |
| `PACKAGE_DYNAMIC_P` | 动态子程序号：`M98 P#100`、`M98 P[..]` |
| `PACKAGE_VARIABLE_EXPRESSION` | 任意行含变量 `#` 或方括号表达式 |
| `PACKAGE_INVALID_P` | M98 无 P / P 非正整数 / 同段多个程序流指令 |
| `PACKAGE_INVALID_L` | M98 的 L 非正整数或给出多个 |
| `PACKAGE_O_NUMBER_MISMATCH` | 子程序缺 O 号、多个 O 号、O 号与声明不一致、O 行非首行 |
| `PACKAGE_MAIN_HAS_O` | 主程序中出现 O 号 |
| `PACKAGE_UNSUPPORTED_RETURN` | `M99 P<n>` 行号返回跳转 |

每个错误都带 `source_program/source_file/line_no/source_line` 与（遍历时
错误的）`call_stack`、`repeat_index`；错误清单通过 `GET /api/dialect`
的 `expansion_error_titles` 获取中文标题。

### 请求体（POST /api/packages）

```json
{
  "machine_id": "…",
  "name": "part_package",
  "main": "G21 G90 G54\nM3 S4000\nM98 P100 L2\nM30\n",
  "main_file": "main.nc",
  "subprograms": [
    {"name": "o100.nc", "content": "O100\nG91 G99 G81 X20 Z-10 R-18 L3 F250\nG90 G80\nM99\n"}
  ],
  "max_depth": 50
}
```

也可以用内联 `"config": {…}` 代替 `machine_id`；子程序的 O 号默认从内容
首行解析，也可显式给 `"o_number": 100`（与内容不一致时阻断）。
可直接下载 `GET /api/examples/subprogram_demo` 的 JSON 作为请求体模板
（补上 `machine_id`/`config`）；`subprogram_errors_demo` 演示全部阻断错误。

## 3. 机床配置字段

```json
{
  "name": "桌面三轴机",
  "travel_x": [0, 300], "travel_y": [0, 200], "travel_z": [-100, 0],
  "safe_z": 10,
  "max_feed_mm_min": 3000,
  "max_spindle_rpm": 12000,
  "offset_x": -150, "offset_y": -100, "offset_z": 0,
  "wcs_offsets": {
    "G54": {"x": -150, "y": -100, "z": 0},
    "G55": {"x": 50, "y": 20, "z": -5}
  }
}
```

- `travel_*` 也可写成正数标量表示 `[0, v]`；或直接给 `x_min/x_max/y_min/...`。
- 工件坐标 → 机床坐标：`machine = program + 当前坐标系偏置`。
- `wcs_offsets` 分别设置 `G54`-`G59` 的 X/Y/Z 偏置（缺省轴按 0）；
  旧字段 `offset_x/offset_y/offset_z` 归入 `G54`（显式 `wcs_offsets.G54`
  优先），未出现在 `wcs_offsets` 中的坐标系视为**未配置**。
- 偏置非法时按坐标系与字段报错，如 `wcs_offsets.G55.x 必须是数值`。
- `safe_z` 按**工件（程序）坐标**判定快速移动。

## 4. 接口

### 元信息

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET | `/api/dialect` | 支持的指令、问题代码、严重度表 |
| GET | `/api/docs` | 本文档（Markdown） |
| GET | `/api/examples` | 内置示例 .nc 清单 |
| GET | `/api/examples/<name>` | 下载示例（safe_demo/problems_demo/inch_demo/arc_demo/plane_arc_demo/wcs_demo/drill_cycle_demo；程序包示例 subprogram_demo/subprogram_errors_demo 为 JSON） |

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
  - `cycle=G81,G83`：只保留指定循环类型的孔/分组/轨迹（G81/G82/G83）
  - `hole_from=3&hole_to=8`：按全程序孔序（阻断孔也占位）筛选
  - `plane=G17,G18`：按圆弧平面筛选（G17/G18/G19）；只保留带平面信息的
    问题（弧段的越界/主轴/进给、圆弧无解、循环平面限制），轨迹中其他
    平面的弧段条目被剔除，`arcs` 汇总与风险计数随之重算
  - `wcs=G54,G55`：按工件坐标系筛选（G54-G59）；只保留该坐标系下产生的
    问题与轨迹段（无轨迹段的设定/注释行保留），`wcs` 汇总节只保留命中
    坐标系，固定循环分组按孔的 `wcs` 裁剪，风险计数随之重算
  - `trajectory=0`：省略逐行轨迹以减小响应；`trajectory=all`：循环/平面/坐标系筛选时保留完整轨迹

  指定 `cycle`/`hole_*` 后，报告中的 `drill_cycles`（groups、by_cycle、
  summary 的孔数/钻深/暂停/展开路径）与逐行轨迹的孔及动作（含孔间定位）
  都只反映命中孔；程序顶层统计保持完整。
- `GET /api/jobs/<id>/report/download`：以 `attachment` 下载 JSON 报告（筛选参数同上）。
- `GET /api/jobs/<id>/gcode`：下载作业原始 .nc 文本。

### 程序包静态展开作业（主程序 + O 号子程序集）

- `POST /api/packages`：body 见 2b。返回 `202` 与程序包作业 `id`，后台展开+分析。
  程序包结构非法（缺主程序、子程序无 content、`max_depth` 越界等）返回
  `400 BAD_PACKAGE`，错误明细在 `error.details.errors`。
- `GET /api/packages`：程序包列表（含状态、`expansion` 概要与调用图）。
- `GET /api/packages/<id>`：状态与进度；`completed` 含 `risk`，`blocked`
  含 `expansion_errors`（调用错误清单）。
- `GET /api/packages/<id>/report`：完成后取完整报告（普通分析报告 +
  `package`/`program` 展开节）；阻断时返回阻断报告（无安全结论）。支持：
  - `source=main,O100`：**按来源程序筛选轨迹**（可多选逗号分隔）；
    逐行轨迹与问题只保留命中来源程序，风险计数随之重算；非法来源返回 400。
  - `trajectory=0`：省略逐行轨迹（顶层 package/call_graph 保留）。
- `GET /api/packages/<id>/report/download`：以 `attachment` 下载程序包
  完整 JSON（展开状态、调用图、调用错误、轨迹与分析结果）。
- `GET /api/packages/<id>/blocks?limit=100&offset=0&source=O100`：
  **展开块分页预览**。返回 `{total, limit, offset, count, has_more, blocks}`，
  每块含 `seq/program/file/line_no/source_line/depth/repeat_index/repeat_total/
  call_stack`（不含状态快照，预览轻量）。`limit` 1..1000；阻断中的程序包
  返回 `409 PACKAGE_BLOCKED`。
- `GET /api/packages/<id>/package`：下载原始程序包 JSON（主程序与子程序原文）。
- `POST /api/package-compare`：程序对比汇总调用与展开块变化（同一机床配置）：
  - 内联：`{"machine_id":…, "package_a":{…2b 请求体…}, "package_b":{…},
    "label_a":"v1", "label_b":"v2", "save":true}`
  - 引用：`{"package_a_id":"…", "package_b_id":"…"}`
  - 返回标准安全对比（resolved/introduced/unchanged、风险/路径变化）外加：
    `expansion`（子程序数、展开块、调用点数/执行次数/调用次数/重复次数、
    最大深度的两侧值与 delta）与 `call_graph_diff`（`programs_added/removed`、
    `edges_added/removed/changed`，变化边给出调用次数 delta）。
  - 任一程序包被展开错误阻断时返回 `409 PACKAGE_BLOCKED/PACKAGE_NOT_COMPARABLE`，
    不产生部分对比结论；配置不一致返回 409。

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
`by_code` 计数变化、风险分/级别变化、路径长度与包围盒变化，
并在 `drill_cycles` 中汇总孔数/阻断孔/钻深/展开路径的增减
（总计及按 G81/G82/G83 分类的 `by_cycle`），在 `arcs` 中汇总
各平面（G17/G18/G19）的弧段数、弧长、螺旋段数与阻断弧数的增减，
以及该平面问题的新增（`introduced_issues`）/解决（`resolved_issues`）/
净变化（`delta_issues`），并在 `wcs` 中汇总各工件坐标系（G54-G59）的
路径长度、机床坐标包围盒（行程占用）与问题的新增/解决/净变化。
- `GET /api/comparisons` / `GET /api/comparisons/<id>`：读取保存的对比。

## 5. 报告结构

```jsonc
{
  "program": { "physical_lines": 12, "executed_lines": 9,
               "blocked_lines": 2, "blank_or_comment_lines": 1,
               "drill_holes": 8, "drill_holes_blocked": 1,
               "drill_holes_total": 9, "drill_cycle_groups": 2 },
  "machine": { …回显配置… },
  "final_state": { "unit": "mm", "distance_mode": "absolute", "wcs": "G54",
    "motion_mode": "linear", "plane": "G17",
    "x/y/z": {"value_mm": …, "known": true},
    "feed_mm_per_min": {…}, "spindle_rpm": 6000, "spindle_on": true,
    "canned_cycle": { …当前激活循环的参数与来源，无则 null… },
    "cycle_return_plane": "G98" },
  "arcs": {
    "by_plane": { "G17": {"count": 2, "arc_length_mm": 125.6,
        "length_3d_mm": 130.1, "helical_count": 1, "full_circle_count": 1},
                  "G18": {…}, "G19": {…} },
    "total": { "count": 4, "arc_length_mm": …, "length_3d_mm": …,
               "helical_count": 2, "full_circle_count": 1 },
    "blocked_count": 1 },
  "wcs": {
    "offsets_mm": { "G54": {"x":-150,"y":-100,"z":0}, "G55": {…} },
    "used": ["G54", "G55"],
    "by_wcs": {
      "G54": { "configured": true, "offset_mm": {"x":-150,"y":-100,"z":0},
        "path_length_mm": {"rapid":…,"cutting":…,"total":…,
                           "unknown_segments":0},
        "machine_bbox_mm": { …该坐标系下机床坐标包围盒… },
        "issues": 2 },
      "G59": { "configured": false, "offset_mm": null,
        "path_length_mm": {…}, "machine_bbox_mm": null, "issues": 3 }
    }
  },
  "drill_cycles": {
    "summary": { "cycle_groups": 2, "holes_total": 9, "holes_drilled": 8,
      "holes_blocked": 1, "total_drill_depth_mm": 63.0, "total_dwell_s": 0.5,
      "expanded_path_mm": {"rapid": …, "cutting": …, "total": …} },
    "by_cycle": { "G81": {"groups":1,"holes":6,…}, "G83": {"groups":1,"holes":3,…} },
    "groups": [ { "cycle":"G83", "definition_line_no":4, "cancel_line_no":9,
      "initial_plane_z_mm":20, "parameters":{ …Z/R/Q/P 及来源行历史… },
      "hole_count":3, "executed_holes":3, "blocked_holes":0,
      "total_drill_depth_mm":33, "expanded_path_mm":{…},
      "holes":[ …逐孔（含定位动作）… ] } ]
  },
  "bbox_program_mm": { "x_mm": [0, 90], "y_mm": […], "z_mm": […],
                       "size_mm": [90, 20, 52] },
  "bbox_machine_mm": { …叠加当前坐标系偏置后的机床坐标包围盒… },
  "path_length_mm": { "rapid": 123.4, "cutting": 88.1,
                      "total": 211.5, "reliable": true, "unknown_segments": 0,
                      "canned_cycle_rapid": 40.2, "canned_cycle_cutting": 33.0 },
  "risk": { "score": 35, "level": "high",
            "counts_by_severity": {"critical": 1, "error": 1, …},
            "total_issues": 6 },
  "issues": [ …见下… ],
  "trajectory": [ …逐行（循环段含逐孔展开动作）… ],
  "policies": { …各判定口径的文字说明… }
}
```

固定循环每个孔的记录：

```jsonc
{
  "hole_no": 2, "cycle": "G83", "repeat_index": 1, "l_repeat": 1,
  "status": "drilled",                 // drilled | blocked
  "trigger_line_no": 5, "trigger_source_line": "X20 Y0",
  "definition_line_no": 4,
  "wcs": "G54",                        // 触发孔时的工件坐标系
  "x_mm": 20, "y_mm": 0,
  "machine_x_mm": -130, "machine_y_mm": -100,   // 孔位机床坐标（偏置未知为 null）
  "initial_plane_z_mm": 20, "r_plane_z_mm": 2, "z_bottom_mm": -11,
  "return_plane": "G99", "retract_z_mm": 2,
  "drill_depth_mm": 13, "dwell_s": 0,
  "expanded_path_mm": {"rapid": 25.0, "cutting": 13.2, "total": 38.2},
  "parameter_sources": {               // 每个参数的取值行/触发行/默认
    "Z_bottom": {"value_mm":-11,"line_no":4,"source_line":"…"},
    "R_plane":  {…}, "Q_peck": {…}, "P_dwell_s": null,
    "return_plane": {"code":"G99","line_no":5,"default":false} },
  "moves": [                           // 首段为孔间定位 position
    {"action":"position","motion":"rapid","hole_no":2,
     "start_mm":[0,0,2],"end_mm":[20,0,2],"length_mm":20,…},
    {"action":"approach_r","motion":"rapid", …},
    {"action":"peck_drill_1","motion":"feed", …},
    {"action":"peck_retract_1","motion":"rapid","internal_cycle":true, …},
    {"action":"peck_reapproach_1","motion":"rapid","internal_cycle":true, …},
    {"action":"peck_feed_approach_1","motion":"feed", …},
    {"action":"retract_r","motion":"rapid", …}
  ],
  "block_codes": ["CYCLE_MISSING_PARAMS"],   // 阻断孔
  "basis": "首次启用缺少 Z/R（G83 还需 Q）"
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
               "overshoot_mm": 20, "side": "max", "wcs": "G54" }
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
    "wcs": "G55",                       // 本段所属的工件坐标系
    "wcs_configured": true,
    "offset_mm": {"x": 50, "y": 20, "z": -5},
    "start_mm": [10, 10, -2], "end_mm": [50, 20, -2],
    "start_machine_mm": [60, 30, -7],   // 叠加当前坐标系偏置的机床坐标
    "end_machine_mm": [100, 40, -7],    // （偏置未知时这些机床坐标为 null）
    "length_mm": 32.1,
    "points_mm": [ …离散轨迹（直线取端点，圆弧自动加密）… ],
    "points_machine_mm": [ …对应的机床坐标轨迹… ],
    "arc": { "plane": "G17(XY)", "plane_code": "G17", "direction": "CW",
             "programming": "I/J",
             "center_mm": [20, 10], "center_axes": ["X", "Y"],
             "center_3d_mm": [20, 10, -2],
             "radius_mm": 40, "sweep_deg": -90, "full_circle": false,
             "helical": false, "perp_axis": "Z", "perp_change_mm": 0,
             "arc_length_mm": 62.8 }
  }
}
```

## 6. 问题代码与严重度

| 代码 | 默认严重度 | 触发条件 |
|---|---|---|
| `MALFORMED_LINE` | critical | 残片/非法数字（如 `X-`），整段阻断 |
| `UNSUPPORTED_INSTRUCTION` | error | 不在方言表内的指令/地址词，整段阻断 |
| `ARC_NO_SOLUTION` | critical | 圆心词与 R 混用、圆心词不属于当前平面、R 编程整圆、起终半径不一致、半径 0、弦长 > 2R 等；整段阻断并回滚本行模态 |
| `OUT_OF_BOUNDS` | critical | 轨迹上任意点（圆弧取真实弧线极值点）叠加当前坐标系偏置后越出行程 |
| `RAPID_BELOW_SAFE_Z` | error | G0 轨迹上任意点 Z < safe_z（工件坐标） |
| `SPINDLE_NOT_RUNNING` | error | G1/G2/G3 切削时 `spindle_on=false` |
| `FEED_OVER_LIMIT` | error | F（换算 mm/min）> max_feed_mm_min |
| `SPINDLE_OVER_LIMIT` | error | S > max_spindle_rpm |
| `FEED_UNSET` | warning | 切削段前未建立有效 F |
| `NO_MOTION_MODE` | warning | 有轴词但没有任何 G0-G3 模态（不猜测运动） |
| `UNKNOWN_UNITS` | warning | G20/G21 建立前出现 F 或运动，位置标记未知 |
| `UNKNOWN_DISTANCE_MODE` | warning | G90/G91 建立前出现轴坐标，位置标记未知 |
| `UNKNOWN_WCS` | warning | G54-G59 建立前运动，或引用了未配置偏置的坐标系：跳过行程检查，机床包围盒不可用 |
| `CYCLE_MISSING_PARAMS` | error | 循环首次启用缺 Z/R（G83 还需正 Q），对应孔阻断、模态可补齐 |
| `CYCLE_BAD_PARAM` | error | G83 的 Q<=0、P 为负、L 非正整数，对应孔阻断 |
| `CYCLE_PLANE_CONFLICT` | error | 孔底高于 R 平面，对应孔阻断 |
| `CYCLE_NO_INHERITABLE_STATE` | error | 后续孔位的 XY/初始平面状态未知，对应孔阻断 |
| `CYCLE_PLANE_NOT_G17` | error | G18/G19 平面下展开固定循环，对应孔阻断（G17 恢复后可继续） |

风险分：critical 25 / error 10 / warning 3 / info 1（封顶 100），
级别 `none/low(≤10)/medium(≤30)/high(≤60)/critical`。

## 7. 判定口径（关键约定）

1. **同组模态取同行最后一个**：如 `G20 G21 G90 G91` 离开状态为 mm + 增量，
   规范化输出 `G21 G91`；平面 `G17/G18/G19` 同理（未写过默认 `G17`）。
2. **阻断即无痕**：含残缺或未支持指令的程序段不执行、不改变任何模态；
   圆弧无解时连同本行已改模态（含平面）一起回滚。
3. **位置未知**：单位或定位模式不明时，不按默认值蒙算，相关轴 `known=false`，
   该段不进入包围盒/长度/行程统计，并给出对应 warning。
4. **路径长度**：仅累计物理已知段；圆弧按弧长（含垂直轴联动的三维螺旋
   长度），直线按 3D 弦长。`reliable=false` 时报告会标注存在未知段。
5. **安全 Z 按工件坐标**判定；**行程按机床坐标**（叠加当前坐标系偏置）判定；
   圆弧的包围盒与行程检查按真实弧线极值点（端点 + 扫过的象限角）计算。
   **坐标系切换**（G54-G59）时机床位置不动、工件坐标按新偏置重新换算；
   引用未配置偏置的坐标系时不沿用上一偏置，相关机床坐标/行程/包围盒
   结论标为未知。
6. **固定循环**：循环参数非法/缺项只阻断对应孔并写明依据，不产生位移、
   不改写程序；固定循环仅允许在 `G17` 平面展开（`G18/G19` 下对应孔阻断）；
   G83 内部排屑快速动作豁免安全 Z 告警，孔间定位仍检查；
   工艺问题（主轴未转/无进给）按触发行去重。
7. 服务监听本地回环，数据库为单个 SQLite 文件，全程无任何网络外联。

## 8. curl 快速上手

```bash
# 1) 建配置（含 G54/G55 两套工件坐标偏置）
curl -s localhost:8080/api/machines -H 'Content-Type: application/json' -d '{
  "name":"demo", "travel_x":[0,300], "travel_y":[0,200], "travel_z":[-100,0],
  "safe_z":10, "max_feed_mm_min":3000, "max_spindle_rpm":12000,
  "wcs_offsets":{"G54":{"x":0,"y":0,"z":0},"G55":{"x":100,"y":50,"z":0}}}'

# 2) 下载示例并建作业（含钻孔循环示例 drill_cycle_demo）
curl -s localhost:8080/api/examples/problems_demo -o problems.nc
python3 - <<'PY'
import json,urllib.request
g=open('problems.nc',encoding='utf-8').read()
req=urllib.request.Request('http://localhost:8080/api/jobs',
  data=json.dumps({'machine_id':'<上一步id>','gcode':g}).encode(),
  headers={'Content-Type':'application/json'})
print(urllib.request.urlopen(req).read().decode())
PY

# 3) 查进度 / 筛选严重问题 / 按循环类型与孔序筛选 / 按圆弧平面筛选 / 下载
curl -s 'localhost:8080/api/jobs/<id>'
curl -s 'localhost:8080/api/jobs/<id>/report?severity=critical,error'
curl -s 'localhost:8080/api/jobs/<id>/report?cycle=G83&hole_from=11&hole_to=13'
curl -s 'localhost:8080/api/jobs/<id>/report?plane=G18,G19'
curl -s -OJ 'localhost:8080/api/jobs/<id>/report/download'

# 4) 钻孔循环示例（G81/G82/G83/G98/G99/L，含阻断演示）
curl -s localhost:8080/api/examples/drill_cycle_demo -o drill.nc

# 5) 多平面圆弧与螺旋插补示例（G17/G18/G19、I/J、I/K、J/K、R、联动轴）
curl -s localhost:8080/api/examples/plane_arc_demo -o planes.nc

# 6) 多工件坐标系示例（G54/G55 切换、G59 未配置），按坐标系筛选报告
curl -s localhost:8080/api/examples/wcs_demo -o wcs.nc
curl -s 'localhost:8080/api/jobs/<id>/report?wcs=G55'

# 7) 程序包静态展开（O/M98/M99/L、调用图、调用栈、按来源筛选、分页预览）
curl -s localhost:8080/api/examples/subprogram_demo -o pkg.json
python3 - <<'PY'
import json,urllib.request
pkg=json.load(open('pkg.json',encoding='utf-8'))
pkg['machine_id']='<配置id>'
req=urllib.request.Request('http://localhost:8080/api/packages',
  data=json.dumps(pkg).encode(),
  headers={'Content-Type':'application/json'})
print(urllib.request.urlopen(req).read().decode())
PY
curl -s localhost:8080/api/packages/<id>                    # 状态/调用图
curl -s 'localhost:8080/api/packages/<id>/blocks?limit=20'  # 展开块分页
curl -s 'localhost:8080/api/packages/<id>/report?source=O100'
curl -s localhost:8080/api/examples/subprogram_errors_demo  # 全部阻断错误演示
```
"""
