"""检查活动文档链接和已退役模块引用。"""

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def main():
    errors = []
    for path in REPO.rglob("*"):
        if not path.is_file() or any(
            p
            in {
                ".git",
                "__pycache__",
                ".pytest_cache",
                ".ruff_cache",
                "driving_data_platform.egg-info",
            }
            for p in path.parts
        ):
            continue
        if path.suffix == ".md":
            for ref in re.findall(r"\]\(([^)]+)\)", path.read_text()):
                if "://" not in ref and not (path.parent / ref.split("#")[0]).exists():
                    errors.append(f"{path.relative_to(REPO)}: broken link {ref}")
        if (
            path.suffix in {".py", ".yaml", ".sh", ".md", ".json"}
            and path.name != "check_repository.py"
        ):
            text = path.read_text()
            if re.search(
                r"/home/qjj|ingestion\.hdfs|run_compatibility_probe|infra/(hadoop|hive)|compose\.compatibility",
                text,
            ):
                errors.append(f"{path.relative_to(REPO)}: retired reference")
    print(json.dumps({"status": "failed" if errors else "passed", "errors": errors}))
    return 2 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
