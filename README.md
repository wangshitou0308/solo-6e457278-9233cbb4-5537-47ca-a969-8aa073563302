# gcode-checker — 小型数控工作室离线 G-code 上机前检查 API

纯 Python 标准库实现（`http.server` + `sqlite3`），**断网可用**；
**不连接、不控制任何机床**，只对 `.nc` 文本做静态检查。

## 启动

```bash
python3 -m gcode_checker                 # http://127.0.0.1:8080，SQLite: ./gcode_checker.db
python3 -m gcode_checker --port 9000 --db /var/lib/gc.db --verbose
```

启动后：

- 接口文档：`GET http://127.0.0.1:8080/api/docs`
- 支持的指令方言 / 问题代码：`GET /api/dialect`
- 内置示例程序：`GET /api/examples`（可直接下载建作业）

## 支持的 G-code 方言

| 类别 | 指令 |
|---|---|
| 单位 | `G20` inch（内部 ×25.4）/ `G21` mm |
| 定位 | `G90` 绝对 / `G91` 增量 |
| 坐标系 | `G54`-`G59`（各坐标系 X/Y/Z 偏置由机床配置 `wcs_offsets` 分别提供，叠加后做行程检查；旧字段 `offset_x/y/z` 归入 `G54`） |
| 平面 | `G17` XY（默认，联动轴 Z）/ `G18` XZ（联动轴 Y）/ `G19` YZ（联动轴 X），模态保持 |
| 运动 | `G0` 快速、`G1` 直线、`G2/G3` 顺/逆圆弧（当前平面内插补，`G17` 用 `I/J`、`G18` 用 `I/K`、`G19` 用 `J/K` 或 `R`；垂直轴随扫角线性联动成螺旋；无 XYZ 终点词的圆心编程为整圆） |
| 固定循环 | `G80` 取消、`G81` 钻孔、`G82` 锪孔（`P` 暂停）、`G83` 深孔啄钻（`Q` 分步），`G98`/`G99` 返回初始/R 平面，`R`、`L` 重复孔位；仅允许在 `G17` 平面展开 |
| 工艺 | `F` 进给（换算 mm/min）、`S` 主轴转速 |
| 主轴 | `M3` 正转、`M5` 停止 |
| 注释 | `(…)` 与 `;…`；行号 `N` 忽略 |

**固定循环展开口径**：循环为模态，`G80` 或 `G0-G3` 取消；`Z/R/Q/P` 模态继承，
`L` 为孔位重复次数（默认 1，正整数）。`G90` 下 `Z/R` 为绝对坐标、`L` 在同位
重复；`G91` 下 `R` 相对循环初始平面、`Z` 相对 R 平面，`L` 沿 XY 增量展开连续孔。
逐孔记录定位、到 R、进刀、孔底暂停、G83 分步下钻/排屑回退以及返回初始平面
或 R 平面的完整轨迹，并保留循环定义行、触发行与每个参数的来源行。
首次启用缺 `Z`/`R`、G83 的 `Q<=0`、`P` 为负、`L` 非正整数、孔底高于 R 平面、
或后续孔位缺少可继承的 XY/初始平面状态时，**阻断对应孔并写明依据，原程序不变**。

**多工件坐标系口径**：`G54`-`G59` 模态切换，换系时机床位置不动、工件坐标按
新偏置重新换算；后续直线/圆弧/螺旋/固定循环都按当前坐标系生成机床轨迹，
安全 Z 仍按当前工件坐标判定。程序引用未配置偏置的坐标系时不沿用上一偏置：
报 `UNKNOWN_WCS`，相关机床坐标、行程及包围盒结论标为未知。每段轨迹记录
坐标系、偏置、工件坐标与机床坐标，报告含分工坐标系的路径/机床包围盒/问题
汇总，可按 `wcs=G54,G55` 筛选。

**未支持的指令显式列出且整段阻断**（不猜测执行、不改模态），例如
`G28/G40-G43/G54.1/G84-G89、M4/M6/M8/T/H/D` 等；
无法解析的残片（如 `X-`）报 `MALFORMED_LINE`，同行若含未支持指令（如
`G54.1 X-`）两类问题都会列出。
（`M98/M99/M2/M30/O` 仅在程序包静态展开中支持，见下。）

## 程序包静态展开（主程序 + O 号子程序集）

`POST /api/packages` 把**主程序**、**带 O 号的子程序集**和机床配置作为一个
持久化作业：解析 O 号、`M98 P<n> L<k>` 的子程序号与重复次数、`M99` 返回及
`M2/M30` 结束，先做静态展开，再交给同一套轨迹与安全检查。

- **调用时继承模态**，M99 返回后从调用点下一程序段继续，`L<k>` 重复调用
  之间不重置状态（连续模态流）；`M2/M30` 结束整个程序。
- 每个展开块保留**来源程序、原行号/原行、调用栈（含每级调用行与重复序号）、
  深度与重复序号**；轨迹条目与问题都带这些字段。
- 展开块分页预览：`GET /api/packages/<id>/blocks?limit=&offset=&source=`。
- 调用图节点标注 `defined/reachable`，边汇总静态调用点数、执行次数与
  调用次数（含 L 重复）。
- **展开阶段整体阻断、不生成部分安全结论**：重复 O 号、M98 目标不存在、
  主程序中的 M99、子程序末尾缺少 M99、递归调用、调用深度超限（默认 50）、
  展开量超过 100,000 块（硬上限）；动态 P（`P#100`/`P[..]`）与变量表达式
  （`#`/`[]`）、M99 P 行号跳转为**未支持**。
- 报告提供**调用图、调用错误、按来源程序筛选的轨迹**（
  `?source=O100,main`）以及程序包 JSON 下载。
- 示例：`GET /api/examples/subprogram_demo`（可直接作请求体模板）、
  `subprogram_errors_demo`（全部阻断错误）。
- `POST /api/package-compare` 对比两个程序包，在标准风险对比之外汇总
  **调用与展开块变化**（子程序数、展开块、调用点/执行/调用次数、最大深度
  delta，调用图边的新增/删除/调用次数变化）。

## 检查内容（每个问题附原行、规范化指令、进入/离开状态、判定依据）

- 加工包围盒（程序坐标 + 叠加当前坐标系偏置的机床坐标；圆弧按真实弧线极值点）
- 分工件坐标系（G54-G59）汇总：路径长度、机床坐标包围盒（行程占用）、问题数
- 路径长度估算（快速 / 切削分开；圆弧按弧长，含垂直轴联动的三维螺旋长度；
  固定循环展开后的定位/接近/下钻/排屑/回退动作计入）
- 逐行轨迹（含圆弧加密采样点、平面、旋向、圆心、半径、扫角、联动位移）
- 固定钻孔循环逐孔展开轨迹（G81/G82/G83，含 G90/G91、L 连续/重复孔位）
- 越界 `OUT_OF_BOUNDS`（critical）
- 循环阻断 `CYCLE_MISSING_PARAMS` / `CYCLE_BAD_PARAM` /
  `CYCLE_PLANE_CONFLICT` / `CYCLE_NO_INHERITABLE_STATE` /
  `CYCLE_PLANE_NOT_G17`（error，仅阻断对应孔）
- 单位 / 定位模式 / WCS 不明（warning；位置标记未知，不按默认值蒙算）
- 圆弧几何无解 `ARC_NO_SOLUTION`（圆心词与 R 混用、圆心词不属于当前平面、
  R 编程整圆、起终半径不一致、半径为 0、弦长 > 2R；
  整段阻断并回滚本行全部模态改动）
- 进给超限、转速超限、切削无 F
- 主轴未启动即切削
- 低于安全 Z 的快速移动（终点低于安全 Z，或安全 Z 以下水平快速移动；
  纯垂直抬刀不误报）

## 主要接口

```text
GET    /api/health
GET    /api/dialect
GET    /api/docs
GET    /api/examples            /api/examples/<name>

GET/POST    /api/machines                  列表 / 新建配置
GET/PUT/DELETE /api/machines/<id>
POST   /api/analyze            同步分析（不落库）

POST   /api/jobs               创建分析作业（后台线程、SQLite 持久化）
GET    /api/jobs               作业列表
GET    /api/jobs/<id>          状态与进度 0-100
GET    /api/jobs/<id>/report   完整报告；?severity=critical,error
                               &code=OUT_OF_BOUNDS&line_from=&line_to=&trajectory=0
                               &cycle=G81,G83&hole_from=&hole_to=&plane=G17,G18
                               &wcs=G54,G55
GET    /api/jobs/<id>/report/download   下载 JSON 报告
GET    /api/jobs/<id>/gcode             下载原始 .nc

POST   /api/compare            同一机床配置比较两个程序
                               （内联 gcode_a/gcode_b，或 job_a_id/job_b_id）
GET    /api/comparisons        /api/comparisons/<id>

POST   /api/packages           创建程序包静态展开作业（主程序 + O 号子程序集）
GET    /api/packages           /api/packages/<id>
GET    /api/packages/<id>/report       ?source=O100,main 按来源程序筛选轨迹
GET    /api/packages/<id>/report/download
GET    /api/packages/<id>/blocks       展开块分页预览（limit/offset/source）
GET    /api/packages/<id>/package      下载原始程序包 JSON
POST   /api/package-compare    比较两个程序包（调用图/展开块变化）
```

循环报告可按 `cycle=G81,G82,G83` 与 `hole_from/hole_to`（全程序孔序）筛选，
筛选后的孔明细、分组、`by_cycle` 与汇总（孔数/钻深/展开路径）只反映命中孔，
逐行轨迹中的孔与动作（含孔间定位）同步裁剪；`trajectory=all` 可保留完整轨迹。

圆弧报告可按 `plane=G17,G18,G19` 筛选：弧段相关问题（越界、主轴/进给、
圆弧无解、循环平面限制）只保留命中平面，`arcs` 汇总与风险计数随之重算，
轨迹中其他平面的弧段条目被剔除。

坐标系报告可按 `wcs=G54..G59` 筛选：只保留命中坐标系下产生的问题与轨迹段，
`wcs` 汇总节只保留命中坐标系，固定循环分组按孔的坐标系裁剪，风险计数随之
重算。

对比结果把问题按指纹多重集匹配为 `resolved / introduced / unchanged`，
并给出按代码计数变化、风险分/级别变化、路径长度与包围盒变化，
固定循环的孔数/阻断孔/钻深/展开路径（总计与按 G81/G82/G83 分类）增减，
`arcs` 中各平面（G17/G18/G19）的弧段数、弧长、螺旋段数、阻断弧数
与该平面问题的新增/解决/净变化，以及 `wcs` 中各坐标系（G54-G59）的
路径长度、机床坐标包围盒（行程占用）与问题的新增/解决/净变化。
两个作业的机床配置不一致时返回 `409 CONFIG_MISMATCH`。

## 目录结构

```text
gcode_checker/
  parser.py     词法解析（保守，不补全）
  cycles.py     固定钻孔循环 G81/G82/G83 逐孔展开
  analyzer.py   模态还原、多平面圆弧/螺旋几何、循环分析、安全检查、报告结构
  packages.py   程序包静态展开（O/M98/M99/M2/M30、调用图、调用栈、阻断错误）
  compare.py    双程序/双程序包对比（问题匹配、循环/平面/WCS、调用与展开块）
  database.py   SQLite 持久化 + 后台作业线程（作业/程序包/展开块分页/对比）
  server.py     http.server REST API（含循环/孔序/平面/坐标系/来源程序筛选）
  examples.py   内置 .nc 与程序包 .json 示例
  docs.py       /api/docs 的 Markdown 文本
  __main__.py   命令行入口
tests/
  test_analyzer.py    解析/几何/检查/循环/对比单元测试
  test_packages.py    程序包展开/阻断/模态继承/程序包对比单元测试
  test_api.py         HTTP 端到端测试（作业与程序包）
```

## 测试

```bash
python3 -m tests.test_analyzer
python3 -m tests.test_packages
python3 -m tests.test_api
```

## 判定口径（关键约定）

1. 同组模态取同行最后一个：`G20 G21 G90 G91` → mm + 增量，规范化 `G21 G91`；
   平面 `G17/G18/G19` 同为模态（未写过默认 `G17`）。
2. 含残缺或未支持指令的程序段不执行、不留模态痕迹；圆弧无解连同本行已改模态回滚。
3. 单位或定位模式不明时，位置 `known=false`，不进入包围盒/长度/行程统计，
   并给出对应 warning；路径长度在存在未知段时标记 `reliable=false`。
4. 安全 Z 按**工件（程序）坐标**判定；行程按**机床坐标**（叠加当前坐标系偏置）判定；
   圆弧的包围盒与行程检查按真实弧线极值点（端点 + 扫过的象限角）计算。
   `G54`-`G59` 换系时机床位置不动、工件坐标按新偏置重新换算；引用未配置
   偏置的坐标系时不沿用上一偏置，相关机床坐标/行程/包围盒结论标为未知。
5. 固定循环参数非法或缺项时只阻断对应孔（登记孔记录与依据，不产生位移、
   不改后续模态）；固定循环仅允许在 `G17` 平面展开；
   循环内部 G83 排屑快速动作豁免安全 Z 告警，孔间定位仍检查。
6. 无任何网络外联；监听默认仅本机回环。
