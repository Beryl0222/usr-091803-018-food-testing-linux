"""糕点抽检实验室协作的运行入口。

* python3 service.py --check       检查基础配置（种子数据与证据链）
* python3 service.py --port 8000   启动 HTTP 服务，/health 确认服务身份
"""

import argparse
from http.server import ThreadingHTTPServer

from foodtesting import CollabEngine, Store
from foodtesting.api import build_handler
from foodtesting.bootstrap import bootstrap
from foodtesting.service_identity import SERVICE_ID, SERVICE_NAME, health_payload


def build_engine(seed: bool = True) -> CollabEngine:
    """构造引擎并按需播种基础标准、方法与角色。"""
    engine = CollabEngine(Store())
    if seed:
        bootstrap(engine)
    return engine


Handler = build_handler(build_engine(seed=True))


def run_checks() -> None:
    assert health_payload()["service"] == SERVICE_ID
    engine = build_engine(seed=True)
    verified = engine.store.verify_chain()
    assert verified["ok"], f"种子事件链校验失败：{verified}"
    assert engine.store.standards, "缺少判定标准种子数据"
    assert engine.store.methods, "缺少检测方法版本种子数据"
    print(f"基础检查通过（标准 {len(engine.store.standards)} 项，方法 {len(engine.store.methods)} 项，"
          f"事件链 {verified['events']} 条）")


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--no-seed", action="store_true", help="不播种演示数据")
    args = parser.parse_args()
    if args.check:
        run_checks()
        return
    handler = Handler if not args.no_seed else build_handler(build_engine(seed=False))
    ThreadingHTTPServer(("0.0.0.0", args.port), handler).serve_forever()


if __name__ == "__main__":
    main()
