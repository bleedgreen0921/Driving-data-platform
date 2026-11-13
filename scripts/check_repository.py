"""Check links, portable runtime sources, public command references and git rules."""

import ast
import json
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKIP = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "driving_data_platform.egg-info",
}
RETIRED = r"(?:hms-register|hms-consume|embedding-trial-(?:up|stop)|embedding-api-(?:up|status|stop))"


def commands():
    tree = ast.parse((REPO / "scripts/manage_platform.py").read_text())
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "command"
        ):
            return set(
                ast.literal_eval(
                    next(k.value for k in node.keywords if k.arg == "choices")
                )
            )
    raise ValueError("CLI command registry missing")


def check(repo=REPO):
    errors = []
    public = commands()
    for path in repo.rglob("*"):
        if not path.is_file() or any(p in SKIP for p in path.parts):
            continue
        relative = path.relative_to(repo)
        history = relative.parts[:2] == ("docs", "history")
        if path.suffix not in {".md", ".py", ".yaml", ".sh", ".json", ".html"}:
            continue
        text = path.read_text()
        if path.suffix == ".md":
            for ref in re.findall(r"\]\(([^)]+)\)", text):
                if "://" not in ref and not (path.parent / ref.split("#")[0]).exists():
                    errors.append(f"{relative}: broken link {ref}")
            if not history and relative.as_posix() != "docs/command-migration.md":
                if re.search(
                    r"\./platform\.sh\s+"
                    + RETIRED
                    + r"\b|\./scripts/verify_(?:model|encoding)\.sh",
                    text,
                ):
                    errors.append(f"{relative}: retired public command")
                for cmd in re.findall(r"\./platform\.sh\s+([a-z][a-z-]+)", text):
                    if cmd not in public:
                        errors.append(f"{relative}: unknown command {cmd}")
        runtime = (
            relative.parts[0] in {"src", "scripts"}
            or relative.as_posix() == "platform.sh"
        )
        if (
            runtime
            and path.name != "check_repository.py"
            and re.search(r"/(?:Users|home)/[A-Za-z0-9_-]+/", text)
        ):
            errors.append(f"{relative}: machine-bound host path")
        if (
            not history
            and path.name != "check_repository.py"
            and re.search(
                r"ingestion\.hdfs|run_compatibility_probe|infra/(hadoop|hive)|compose\.compatibility",
                text,
            )
        ):
            errors.append(f"{relative}: retired reference")
    required = [
        "docs/current-status.md",
        "CHANGE_REQUESTS.md",
        "verify.sh",
        "requirements.lock",
        "configs/application.example.yaml",
    ]
    for path, should_ignore in [(p, False) for p in required] + [
        (".env", True),
        ("configs/runtime.yaml", True),
        ("models/private.gguf", True),
        ("artifacts/runtime.json", True),
    ]:
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "-q", "--", path],
            cwd=repo,
            check=False,
            capture_output=True,
        )
        if result.returncode not in (0, 1) or (result.returncode == 0) != should_ignore:
            errors.append(f"{path}: unexpected git ignore rule")
    return errors


def main():
    errors = check()
    print(json.dumps({"status": "failed" if errors else "passed", "errors": errors}))
    return 2 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
