"""python3 -m guide_versioning [--host H] [--port P] [--db PATH]"""
from __future__ import annotations

import argparse
import os
import sys

from .api import build_server
from .clock import Clock
from .service import GuideService
from .store import Store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="讲解内容版本发布后端")
    parser.add_argument("--host", default=os.environ.get("GUIDE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("GUIDE_PORT", "8080")))
    parser.add_argument("--db", default=os.environ.get("GUIDE_DB", "guide_versioning.sqlite3"))
    args = parser.parse_args(argv)

    store = Store(args.db)
    service = GuideService(store, Clock())
    server = build_server(args.host, args.port, service)
    print(f"讲解内容版本发布服务已启动：http://{args.host}:{args.port}（数据库 {args.db}）", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
