"""元数据候选的显式准备与服务管理入口。"""

import argparse
import json
import os
import subprocess
from pathlib import Path

import psycopg
from metastore_ops import prepare

from driving_data_platform.warehouse.catalog import command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "up", "stop", "validate"])
    args = parser.parse_args()
    root = Path(
        os.environ.get(
            "DDP_OUTPUT_ROOT",
            "/Users/qiujiajun/Project/driving-data-platform-artifacts",
        )
    )
    repo = Path(__file__).resolve().parents[1]
    try:
        if args.action == "prepare":
            report = prepare(root, repo)
        elif args.action == "validate":
            subprocess.run(
                [
                    *command(root, repo),
                    "run",
                    "--rm",
                    "--no-deps",
                    "metastore",
                    "validate",
                ],
                check=True,
            )
            report = {"status": "passed", "action": "validate"}
        else:
            options = (
                ["up", "-d", "--wait", "--no-deps", "metastore"]
                if args.action == "up"
                else ["stop", "metastore"]
            )
            subprocess.run([*command(root, repo), *options], check=True)
            report = {"status": "passed", "action": args.action}
        print(json.dumps(report))
        return 0
    except (OSError, ValueError, psycopg.Error, subprocess.SubprocessError):
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "error": "元数据候选操作失败；查看本轮日志，不自动修改数据库 schema 或清理缓存。",
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
