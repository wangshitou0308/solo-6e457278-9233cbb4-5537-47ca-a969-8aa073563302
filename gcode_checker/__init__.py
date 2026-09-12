"""本地 G-code 上机前检查 API（仅使用 Python 标准库）。

模块组成：
- parser:   .nc 文本词法解析
- analyzer: 模态还原、几何计算与安全检查
- database: SQLite 持久化（配置 / 作业 / 报告 / 对比）
- compare:  两个程序的风险对比
- server:   http.server 实现的 REST API
"""

__version__ = "1.2.0"
