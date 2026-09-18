"""糕点抽检实验室协作的服务入口。"""

import argparse
from http.server import ThreadingHTTPServer

from foodtesting import SERVICE_ID, SERVICE_NAME, health_payload
from foodtesting.api import App, make_handler
from foodtesting.seed import seed_demo

__all__ = ["SERVICE_ID", "SERVICE_NAME", "health_payload", "Handler", "main"]

app = App()
Handler = make_handler(app)


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--seed", action="store_true", help="载入中秋糕点抽检演示数据")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        print("基础检查通过")
        return
    if args.seed:
        seed_demo(app.store)
        print("已载入演示数据")
    print(f"{SERVICE_NAME} 监听端口 {args.port}")
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
