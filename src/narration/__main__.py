"""服务入口：python3 -m narration --db data/narration.db --port 8000"""
from __future__ import annotations

import argparse

from .api import make_server
from .service import NarrationService
from .store import Store


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="narration", description="讲解内容版本发布后端服务")
    parser.add_argument("--db", default=":memory:", help="SQLite 数据库路径，默认内存库")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    service = NarrationService(Store(args.db))
    server = make_server(service, args.host, args.port)
    print(f"讲解内容版本发布服务已启动：http://{args.host}:{args.port}/api/health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
