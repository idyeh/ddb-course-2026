#!/usr/bin/env python3
"""课堂集群实验室：一条命令启动演示，或在命令行里复现同一次运行。

    python3 lab/run.py serve                     打开网页演示台
    python3 lab/run.py list                      列出全部演示
    python3 lab/run.py run raft-replication      在终端跑一次并打印检查结果
    python3 lab/run.py sweep ack-meaning         扫描多个种子，找出反例
    python3 lab/run.py export raft-election      把轨迹写成 JSON，供报告引用
"""

from __future__ import annotations

import argparse
import json
import sys
import unittest
import webbrowser
from pathlib import Path

LAB_ROOT = Path(__file__).resolve().parent
if str(LAB_ROOT) not in sys.path:
    sys.path.insert(0, str(LAB_ROOT))

from cluster_lab import registry, server  # noqa: E402


def _params_from(scenario, overrides: list[str]) -> dict:
    raw = {}
    for item in overrides:
        if "=" not in item:
            raise SystemExit(f"参数需要写成 name=value，收到 {item!r}")
        name, _, value = item.partition("=")
        raw[name.strip()] = value.strip()
    return scenario.params.resolve(raw)


def _print_result(result) -> None:
    print(f"  种子 {result.trace.seed}  事件 {len(result.trace.events)}  "
          f"逻辑时长 {result.trace.duration} ticks")
    for key, value in result.metrics.items():
        print(f"    {key}: {value}")
    for check in result.checks:
        mark = "通过" if check.ok else "违反"
        print(f"    [{mark}] {check.name} — {check.detail}")


def command_list(args: argparse.Namespace) -> int:
    lecture = ""
    for scenario in registry.all_scenarios():
        if scenario.lecture != lecture:
            lecture = scenario.lecture
            print(f"\n{lecture}")
        print(f"  {scenario.id:<20} {scenario.title} — {scenario.subtitle}")
    print()
    return 0


def command_run(args: argparse.Namespace) -> int:
    scenario = registry.get(args.scenario)
    params = _params_from(scenario, args.set)
    if args.seed is not None:
        params["seed"] = args.seed
    print(f"{scenario.title} ({scenario.id})")
    result = scenario.run(params, int(params.get("seed", 0)))
    _print_result(result)
    return 0 if all(check.ok for check in result.checks) else 1


def command_compare(args: argparse.Namespace) -> int:
    scenario = registry.get(args.scenario)
    params = _params_from(scenario, args.set)
    for value in args.values:
        print(f"\n{scenario.title} — {args.axis}={value}")
        result = scenario.run({**params, args.axis: value}, int(params.get("seed", 0)))
        _print_result(result)
    return 0


def command_sweep(args: argparse.Namespace) -> int:
    scenario = registry.get(args.scenario)
    params = _params_from(scenario, args.set)
    report = server.sweep(args.scenario, params, args.first, args.count)
    print(f"{scenario.title}：扫描 {report['checked']} 个种子，{report['failing']} 个出现反例")
    for row in report["rows"]:
        if row["violations"]:
            print(f"  种子 {row['seed']}: " + "；".join(row["violations"]))
    print("\n固定种子提供重放，不提供覆盖率。请把每个反例种子加入回归测试。")
    return 0


def command_export(args: argparse.Namespace) -> int:
    scenario = registry.get(args.scenario)
    params = _params_from(scenario, args.set)
    payload = scenario.run(params, int(params.get("seed", 0))).to_dict()
    target = Path(args.out or f"{scenario.id}-{params.get('seed', 0)}.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"轨迹已写入 {target}")
    return 0


def command_serve(args: argparse.Namespace) -> int:
    httpd = server.serve(port=args.port, host=args.host)
    url = f"http://{args.host}:{args.port}/"
    print(f"课堂集群实验室已启动：{url}")
    print("按 Ctrl+C 结束。")
    if not args.no_open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


def command_test(args: argparse.Namespace) -> int:
    loader = unittest.TestLoader()
    suite = loader.discover(str(LAB_ROOT / "tests"), pattern="test_*.py", top_level_dir=str(LAB_ROOT))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="课堂集群实验室")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("list", help="列出全部演示").set_defaults(handler=command_list)

    def add_common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("scenario")
        sub.add_argument("--set", action="append", default=[], metavar="name=value")

    runner = subparsers.add_parser("run", help="运行一次并打印检查结果")
    add_common(runner)
    runner.add_argument("--seed", type=int)
    runner.set_defaults(handler=command_run)

    comparer = subparsers.add_parser("compare", help="沿对比轴运行两次")
    add_common(comparer)
    comparer.add_argument("--axis", required=True)
    comparer.add_argument("--values", nargs="+", required=True)
    comparer.set_defaults(handler=command_compare)

    sweeper = subparsers.add_parser("sweep", help="扫描多个种子寻找反例")
    add_common(sweeper)
    sweeper.add_argument("--first", type=int, default=2026)
    sweeper.add_argument("--count", type=int, default=20)
    sweeper.set_defaults(handler=command_sweep)

    exporter = subparsers.add_parser("export", help="导出轨迹 JSON")
    add_common(exporter)
    exporter.add_argument("--out")
    exporter.set_defaults(handler=command_export)

    serve = subparsers.add_parser("serve", help="启动网页演示台")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--no-open", action="store_true", help="不要自动打开浏览器")
    serve.set_defaults(handler=command_serve)

    subparsers.add_parser("test", help="运行 lab 自带测试").set_defaults(handler=command_test)

    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    if not getattr(args, "handler", None):
        args = parser.parse_args(["serve"])  # bare `run.py` opens the demo table
    try:
        return args.handler(args)
    except (ValueError, KeyError) as error:
        # Parameter problems are the user's to fix, so show the message, not a traceback.
        print(f"参数有误：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
