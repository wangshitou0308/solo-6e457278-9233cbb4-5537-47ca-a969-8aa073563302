"""本地 G-code 上机前检查 API（仅使用 Python 标准库）。

模块组成：
- parser:   .nc 文本词法解析
- analyzer: 模态还原、几何计算与安全检查
- packages: 程序包静态展开（主程序 + O 号子程序集、调用图、展开块）
- database: SQLite 持久化（配置 / 作业 / 程序包 / 展开块 / 对比）
- compare:  两个程序或程序包的风险/展开对比
- server:   http.server 实现的 REST API
"""

__version__ = "1.3.0"
