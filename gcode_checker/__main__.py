"""命令行入口：python3 -m gcode_checker

启动本地离线 G-code 检查 API（默认 127.0.0.1:8080）。
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .server import make_server


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python3 -m gcode_checker",
        description="本地离线 G-code 上机前检查 API（不连接/控制机床）")
    p.add_argument("--host", default="127.0.0.1",
                   help="监听地址（默认 127.0.0.1，仅本机访问）")
    p.add_argument("--port", type=int, default=8080, help="端口（默认 8080）")
    p.add_argument("--db", default="gcode_checker.db",
                   help="SQLite 数据库路径（默认 ./gcode_checker.db）")
    p.add_argument("--verbose", action="store_true", help="打印访问日志")
    p.add_argument("--version", action="version",
                   version=f"gcode-checker {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        srv = make_server(args.host, args.port, args.db, args.verbose)
    except OSError as e:
        print(f"[gcode-checker] 无法绑定 {args.host}:{args.port}: {e}",
              file=sys.stderr)
        return 2
    print(f"[gcode-checker] v{__version__} 已启动（离线模式，不连接机床）")
    print(f"[gcode-checker] 监听: http://{args.host}:{args.port}")
    print(f"[gcode-checker] 数据库: {args.db}")
    print("[gcode-checker] 文档: GET /api/docs  方言: GET /api/dialect  "
          "示例: GET /api/examples")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[gcode-checker] 收到中断，正在关闭…")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
