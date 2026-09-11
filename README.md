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
| 坐标系 | `G54`（X/Y/Z 偏置由机床配置提供，叠加后做行程检查） |
| 运动 | `G0` 快速、`G1` 直线、`G2/G3` 顺/逆圆弧（G17 XY，`I/J` 或 `R`，允许 Z 联动） |
| 工艺 | `F` 进给（换算 mm/min）、`S` 主轴转速 |
| 主轴 | `M3` 正转、`M5` 停止 |
| 注释 | `(…)` 与 `;…`；行号 `N` 忽略 |

**未支持的指令显式列出且整段阻断**（不猜测执行、不改模态），例如
`G17/G28/G40-G43/G55-G59/G80-G89、M2/M4/M6/M8/M30、T/H/D/P/Q/L` 等；
无法解析的残片（如 `X-`）报 `MALFORMED_LINE`，同行若含未支持指令（如
`G55 X-`）两类问题都会列出。

## 检查内容（每个问题附原行、规范化指令、进入/离开状态、判定依据）

- 加工包围盒（程序坐标 + 叠加 G54 偏置的机床坐标）
- 路径长度估算（快速 / 切削分开；圆弧按弧长，含螺旋 Z 联动）
- 逐行轨迹（含圆弧加密采样点、圆心、半径、扫角）
- 越界 `OUT_OF_BOUNDS`（critical）
- 单位 / 定位模式 / WCS 不明（warning；位置标记未知，不按默认值蒙算）
- 圆弧几何无解 `ARC_NO_SOLUTION`（半径为 0、终点不落圆、弦长 > 2R；
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
GET    /api/jobs/<id>/report/download   下载 JSON 报告
GET    /api/jobs/<id>/gcode             下载原始 .nc

POST   /api/compare            同一机床配置比较两个程序
                               （内联 gcode_a/gcode_b，或 job_a_id/job_b_id）
GET    /api/comparisons        /api/comparisons/<id>
```

对比结果把问题按指纹多重集匹配为 `resolved / introduced / unchanged`，
并给出按代码计数变化、风险分/级别变化、路径长度与包围盒变化。
两个作业的机床配置不一致时返回 `409 CONFIG_MISMATCH`。

## 目录结构

```text
gcode_checker/
  parser.py     词法解析（保守，不补全）
  analyzer.py   模态还原、圆弧几何、安全检查、报告结构
  compare.py    双程序风险对比
  database.py   SQLite 持久化 + 后台作业线程
  server.py     http.server REST API
  examples.py   内置 .nc 示例
  docs.py       /api/docs 的 Markdown 文本
  __main__.py   命令行入口
tests/
  test_analyzer.py   解析/几何/检查/对比单元测试（25 项）
  test_api.py        HTTP 端到端测试（6 项）
```

## 测试

```bash
python3 -m tests.test_analyzer
python3 -m tests.test_api
```

## 判定口径（关键约定）

1. 同组模态取同行最后一个：`G20 G21 G90 G91` → mm + 增量，规范化 `G21 G91`。
2. 含残缺或未支持指令的程序段不执行、不留模态痕迹；圆弧无解连同本行已改模态回滚。
3. 单位或定位模式不明时，位置 `known=false`，不进入包围盒/长度/行程统计，
   并给出对应 warning；路径长度在存在未知段时标记 `reliable=false`。
4. 安全 Z 按**工件（程序）坐标**判定；行程按**机床坐标**（叠加 G54 偏置）判定。
5. 无任何网络外联；监听默认仅本机回环。
