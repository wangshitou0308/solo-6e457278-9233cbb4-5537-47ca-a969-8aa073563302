"""随服务附带的示例 .nc 程序（断网可下载）。

每个示例同时演示一类典型问题，方便首次启动后直接体验接口。
程序包示例（子程序展开）为 .json，见 PACKAGE_EXAMPLES。
"""

from __future__ import annotations

import json

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
G54.1 P1 X60 Y60   ; G54.1 附加坐标系未支持，整段阻断
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
    "plane_arc_demo": {
        "filename": "plane_arc_demo.nc",
        "title": "多平面圆弧与螺旋插补示例（G17/G18/G19）",
        "description": "G17 整圆与 Z 联动螺旋、G18(XZ) I/K 圆弧与 Y 联动、"
                       "G19(YZ) J/K 圆弧与 X 联动；含圆心参数混用、圆心词不属"
                       "当前平面、G18 下固定循环等阻断演示。建议配置：行程 "
                       "X[0,300] Y[0,200] Z[-50,60]、safe_z=10、F 上限 3000、"
                       "S 上限 12000。",
        "content": """\
; plane_arc_demo.nc —— 多平面圆弧与螺旋插补
; 建议配置：X[0,300] Y[0,200] Z[-50,60]，safe_z=10，F<=3000，S<=12000
G21 G90 G54
M3 S6000
G0 Z20
G0 X40 Y40
G1 Z-2 F300

; ---- G17(XY)：I/J 整圆 + Z 联动螺旋 ----
G2 X40 Y40 I20 J0        ; 整圆（圆心编程，回到起点）
G3 X80 Y40 I20 J0 Z-6    ; XY 半圆 + Z 联动 -2 -> -6（螺旋）

; ---- G18(XZ)：I/K 圆弧，Y 为垂直联动轴 ----
G18
G2 X82 Z-4 I0 K2         ; XZ 平面 1/4 圆（X80/Z-6 -> X82/Z-4）
G3 X122 Z-4 R20 Y80      ; R 半圆 + Y 联动 40 -> 80（螺旋）

; ---- G19(YZ)：J/K 圆弧，X 为垂直联动轴 ----
G19
G3 Y82 Z-6 J2 K0         ; YZ 平面 1/4 圆（Y80/Z-4 -> Y82/Z-6）
G3 Y122 Z-6 R20 X160     ; R 半圆 + X 联动 122 -> 160（螺旋）

; ---- 阻断演示（定位原行、整段回滚，进入行前的模态与位置不变）----
G0 Z20
G17
G2 X200 Y122 I10 J0 R15  ; 圆心参数混用（I/J 与 R）-> ARC_NO_SOLUTION
G2 X240 Y122 K5          ; K 不属于 G17 平面 -> ARC_NO_SOLUTION
G18
G81 R2 Z-8 F200          ; G18 下固定循环 -> CYCLE_PLANE_NOT_G17，孔阻断
G17
X260 Y122                ; G17 恢复后正常钻孔
G80
G0 Z20
M5
""",
    },
    "wcs_demo": {
        "filename": "wcs_demo.nc",
        "title": "多工件坐标系示例（G54/G55 切换与未配置坐标系）",
        "description": "G54/G55 模态切换：换系时机床位置不动、工件坐标按新偏置"
                       "重新换算；G59 未在配置中设置偏置，引用后机床坐标/行程/"
                       "包围盒结论标为未知（不沿用上一偏置）。建议配置：行程 "
                       "X[0,300] Y[0,200] Z[-50,60]、safe_z=10、F 上限 3000、"
                       "S 上限 12000；wcs_offsets：G54 {x:0,y:0,z:0}、"
                       "G55 {x:100,y:50,z:0}（G59 故意不配置）。",
        "content": """\
; wcs_demo.nc —— 多工件坐标系（G54-G59）
; 建议配置：X[0,300] Y[0,200] Z[-50,60]，safe_z=10，F<=3000，S<=12000
; wcs_offsets：G54 {x:0,y:0,z:0}，G55 {x:100,y:50,z:0}（G59 不配置）
G21 G90 G54
M3 S5000
G0 Z20
G0 X10 Y10
G1 Z-2 F300
G1 X30 Y30 F600      ; G54 下切削（机床坐标 = 工件 + G54 偏置）
G0 Z20
G55                  ; 换系：机床位置不动，工件坐标按 G55 偏置重算
G0 X10 Y10           ; 机床坐标 = 工件 + G55 偏置（+100/+50）
G1 Z-2 F300
G1 X30 Y30 F600      ; G55 下切削
G0 Z20
G59                  ; G59 未配置偏置 -> 后续运动 UNKNOWN_WCS，不沿用 G55
G0 X0 Y0
G1 Z-2 F300
G1 X20 Y20 F600
G0 Z20
G54                  ; 回到 G54（位置需重新建立）
G0 X0 Y0 Z50
M5
""",
    },
    "drill_cycle_demo": {
        "filename": "drill_cycle_demo.nc",
        "title": "固定钻孔循环示例（G81/G82/G83/G98/G99/L）",
        "description": "G81 排孔（G99 R 平面连续横移）、G91 G81 L4 连续孔、"
                       "G82 孔底暂停、G83 深孔啄钻，含缺 R、Q 非正、L 非法等"
                       "阻断演示。建议配置：行程 X[0,300] Y[0,200] "
                       "Z[-50,60]、safe_z=10、F 上限 3000、S 上限 12000。",
        "content": """\
; drill_cycle_demo.nc —— 固定钻孔循环
; 建议配置：X[0,300] Y[0,200] Z[-50,60]，safe_z=10，F<=3000，S<=12000
G21 G90 G54
M3 S4000
G0 X0 Y0 Z20

; ---- G81 普通钻孔：定义行即首孔，后续孔位模态触发 ----
G99 G81 R2 Z-8 F250   ; 孔1(0,0)，G99 孔后回 R 平面
X20 Y0                ; 孔2，R 高度横移（低于 safe_z=10 会告警）
X40 Y0                ; 孔3
G98 X60 Y0            ; 孔4，本行起返回初始平面 Z20

; ---- G91 增量连续孔：L 沿 XY 增量展开 4 个孔 ----
G90 G0 X80 Y0 Z20
G91 G99 G81 X20 L4 R-18 Z-10 F250  ; 孔5..8：X=100/120/140/160
G90 G80

; ---- G82 锪孔：孔底暂停 P500 = 0.5 s ----
G0 X0 Y30 Z20
G98 G82 R2 Z-6 P500 F200
X30 Y30
G80

; ---- G83 深孔啄钻：Q3 分步，每步退回 R 排屑 ----
G0 X0 Y60 Z20
G98 G83 R2 Z-11 Q3 F180
X40 Y60
X80 Y60
G80

; ---- 阻断演示（对应孔被阻断，原程序不变）----
G0 X0 Y90 Z20
G81 Z-8 F200          ; 缺 R：CYCLE_MISSING_PARAMS，本行孔阻断
G81 R2 Z-8            ; 参数补齐后下一行可正常触发
X120 Y90
G83 R2 Z-11 Q0        ; Q 非正：CYCLE_BAD_PARAM，本行孔阻断
G83 R2 Z-11 Q3
X160 Y90 L0           ; L=0 非法：CYCLE_BAD_PARAM
G80
M5
""",
    },
    "length_comp_demo": {
        "filename": "length_comp_demo.nc",
        "title": "刀长补偿示例（G43/G44/G49 + H 寄存器）",
        "description": "G43 加、G44 减、G49 取消；补偿-only 行主轴基准点不动、重算"
                       "刀尖工件 Z；同行运动使用新补偿。含 G43 缺 H、H 不在表、"
                       "G43/G44 同段冲突等阻断演示。建议配置：行程 "
                       "X[0,300] Y[0,200] Z[-50,60]、safe_z=10、F 上限 3000、"
                       "S 上限 12000；length_offsets：H1=10、H2=-3、H3=2。",
        "content": """\
; length_comp_demo.nc —— 刀长补偿 G43/G44/G49
; 建议配置：X[0,300] Y[0,200] Z[-50,60]，safe_z=10，F<=3000，S<=12000
; length_offsets：H1=10，H2=-3（G44 减去 -3 即主轴基准 +3），H3=2
G21 G90 G54
M3 S6000
G0 X0 Y0 Z20

; ---- G43 加补偿：补偿-only 行，主轴基准点保持 Z20，刀尖重算为 Z10 ----
G43 H1
G1 X20 Z5 F500          ; 同行运动使用新补偿：刀尖 Z5，主轴基准 Z15
G0 Z20                  ; 抬刀（刀尖 Z20，主轴基准 Z30）

; ---- G44 减补偿：H2 表值 -3，主轴基准 = 刀尖 - (-3) = 刀尖 + 3 ----
G44 H2
G1 X40 Z5 F500
G0 Z20

; ---- G49 取消补偿：主轴基准点不动，刀尖 Z 回到与基准一致 ----
G49
G1 X60 Z5 F500
G0 Z20

; ---- 阻断演示（整段阻断，不沿用旧补偿值，本行其他模态改动一并回滚）----
G43 Z5                  ; G43 缺 H：LENGTH_COMP_MISSING_H
G43 H9 Z5               ; H9 不在 length_offsets：LENGTH_COMP_H_NOT_FOUND
G43 G44 H1              ; 同段补偿指令冲突：LENGTH_COMP_CONFLICT
G43 H0                  ; H0 非正整数：LENGTH_COMP_H_NOT_FOUND
H3                      ; 无 G43/G44，H 不生效（仅规范化标注）
M5
""",
    },
}

# 程序包示例（POST /api/packages 请求体格式）。JSON 字符串可直接作为
# 请求体（仅需补 machine_id 或 config）。
PACKAGE_EXAMPLES: dict[str, dict] = {
    "subprogram_demo": {
        "filename": "subprogram_demo.json",
        "title": "程序包子程序展开示例（O/M98/M99/L）",
        "description": "主程序两次调用 O100（G81 排孔子程序，L2 重复展开且不重置"
                       "模态），并经 O200 嵌套调用；含调用图、调用栈与按来源程序"
                       "筛选。建议配置：行程 X[0,300] Y[0,200] Z[-50,60]、"
                       "safe_z=10、F 上限 3000、S 上限 12000。",
        "package": {
            "name": "subprogram_demo",
            "main": """\
; subprogram_demo 主程序
G21 G90 G54
M3 S4000
G0 X0 Y0 Z20
M98 P100 L2        ; 连续两次调用 O100（模态不重置）
G0 X0 Y60 Z20
M98 P200           ; 经 O200 嵌套调用 O100
G0 Z50
M30
""",
            "main_file": "main.nc",
            "subprograms": [
                {
                    "name": "o100.nc",
                    "content": """\
O100 (排孔子程序：G91 增量 G81，L3 沿 X 展开 3 个孔)
G91 G99 G81 X20 Z-10 R-18 L3 F250
G90 G80
M99
""",
                },
                {
                    "name": "o200.nc",
                    "content": """\
O200 (定位到第二工位后调用 O100)
G0 X100 Y60
M98 P100
G0 X0 Y60
M99
""",
                },
            ],
        },
    },
    "subprogram_errors_demo": {
        "filename": "subprogram_errors_demo.json",
        "title": "程序包展开错误演示（全部在展开阶段阻断）",
        "description": "重复 O300、主程序 M99、O100 递归、M98 P999 目标不存在、"
                       "M98 P#.. 动态子程序号：展开阶段阻断，不生成安全结论。",
        "package": {
            "name": "subprogram_errors_demo",
            "main": """\
G21 G90 G54
M99                ; 主程序中出现 M99（阻断）
M98 P100           ; O100 递归（阻断）
M98 P999           ; 目标不存在（阻断）
M98 P#200          ; 动态 P（不支持，阻断）
M30
""",
            "main_file": "main.nc",
            "subprograms": [
                {"name": "o100.nc",
                 "content": "O100\nM98 P100\nM99\n"},
                {"name": "o300a.nc",
                 "content": "O300\nM99\n"},
                {"name": "o300b.nc",
                 "content": "O300\nM99\n"},
            ],
        },
    },
}

# 合并示例清单（程序包示例的 content 为 JSON 文本）
_EXAMPLE_PACKAGE_BLOB: dict[str, tuple[str, str]] = {}
for _name, _meta in PACKAGE_EXAMPLES.items():
    _EXAMPLE_PACKAGE_BLOB[_name] = (
        json.dumps(_meta["package"], ensure_ascii=False, indent=2) + "\n",
        _meta["filename"])
PACKAGE_EXAMPLE_NAMES = set(PACKAGE_EXAMPLES)


def list_examples() -> list[dict]:
    out = [{
        "name": name,
        "filename": meta["filename"],
        "title": meta["title"],
        "description": meta["description"],
        "kind": "program",
        "download_url": f"/api/examples/{name}",
    } for name, meta in EXAMPLES.items()]
    for name, meta in PACKAGE_EXAMPLES.items():
        out.append({
            "name": name,
            "filename": meta["filename"],
            "title": meta["title"],
            "description": meta["description"],
            "kind": "package",
            "download_url": f"/api/examples/{name}",
        })
    return out


def get_example(name: str) -> tuple[str, str]:
    """返回 (文件内容, 文件名)。"""
    if name in _EXAMPLE_PACKAGE_BLOB:
        return _EXAMPLE_PACKAGE_BLOB[name]
    meta = EXAMPLES[name]
    return meta["content"], meta["filename"]
