"""随服务附带的示例 .nc 程序（断网可下载）。

每个示例同时演示一类典型问题，方便首次启动后直接体验接口。
"""

from __future__ import annotations

EXAMPLES: dict[str, dict] = {
    "safe_demo": {
        "filename": "safe_demo.nc",
        "title": "合规示例（无严重问题）",
        "description": "单位/绝对/G54 建立完整，先抬安全高度，主轴启动后切削。",
        "content": """\
; safe_demo.nc —— 合规示例
(单位 mm，绝对定位，G54，工件偏置由配置提供)
G21 G90 G54
M3 S6000          ; 主轴启动
G0 Z50            ; 抬到安全高度
G0 X0 Y0
G0 Z5
G1 Z-2 F300       ; 下刀切削
G1 X40 Y10 F600
G2 X50 Y20 I10 J0 ; 顺时针半圆
G1 X90
G0 Z50            ; 抬刀
M5                ; 主轴停止
""",
    },
    "problems_demo": {
        "filename": "problems_demo.nc",
        "title": "问题示例（覆盖各类检查）",
        "description": "未启动主轴切削、越界、安全 Z 以下水平快速移动、进给/转速"
                       "超限、未支持指令、圆弧无解、单位/模式不明等。"
                       "建议配置：行程 X[0,300] Y[0,200] Z[-50,60]、safe_z=10、"
                       "F 上限 3000、S 上限 12000。",
        "content": """\
; problems_demo.nc —— 问题演示程序
; 建议配置：X[0,300] Y[0,200] Z[-50,60]，safe_z=10，F<=3000，S<=12000
G21 G90 G54
M3 S1000
G0 Z20
G0 X0 Y0
G1 Z-2 F300
M5
G1 X10 Y10 F200     ; 主轴未启动即直线切削（M5 之后）
M3 S24000           ; 超过演示机床 12000 rpm 上限
G1 X320 Y-5 F9000  ; X 越界(行程到300)、进给超 3000 上限
G2 X30 Y30 I0 J0   ; 半径为 0，圆弧无解
G0 Z-3             ; 快速移动终点低于安全 Z(10)
G55 X60 Y60        ; G55 未支持，整段阻断
G91
G1 X-10            ; 切换增量（主轴仍转）
M8                 ; 冷却指令未支持，整段阻断
M5
""",
    },
    "inch_demo": {
        "filename": "inch_demo.nc",
        "title": "英制单位示例",
        "description": "G20 输入 inch，服务内部乘 25.4 换算 mm 后做全部检查。",
        "content": """\
; inch_demo.nc —— G20 英制（坐标与 F 按 inch/inch/min 给出）
G20 G90 G54
M3 S4000
G0 Z2.0
G0 X0.5 Y0.5
G1 Z-0.05 F20.0      ; 20 inch/min = 508 mm/min
G1 X2.0 Y1.0 F30.0
G0 Z2.0
M5
""",
    },
    "arc_demo": {
        "filename": "arc_demo.nc",
        "title": "圆弧 I/J 与 R 示例",
        "description": "整圆 I/J、R 半圆、无解 R（弦长大于 2R）各一段。",
        "content": """\
; arc_demo.nc —— 圆弧编程（建议 safe_z=2，行程 X/Y 至少 0..100、Z -50..60）
G21 G90 G54
M3 S8000
G0 Z20
G0 X20 Y20
G1 Z-1 F200
G2 X20 Y20 I10 J0   ; 整圆回到起点（I/J）
G3 X40 Y20 R10      ; 逆时针半圆（R）
G1 X70 Y20
G2 X90 Y20 R5       ; 弦长 20 > 2R=10，几何无解，整段阻断
G0 Z20
M5
""",
    },
}


def list_examples() -> list[dict]:
    return [{
        "name": name,
        "filename": meta["filename"],
        "title": meta["title"],
        "description": meta["description"],
        "download_url": f"/api/examples/{name}",
    } for name, meta in EXAMPLES.items()]


def get_example(name: str) -> tuple[str, str]:
    """返回 (文件内容, 文件名)。"""
    meta = EXAMPLES[name]
    return meta["content"], meta["filename"]
