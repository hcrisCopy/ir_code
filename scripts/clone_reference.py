"""参考仓库：把论文官方仓库浅克隆到 ir_code/reference/，并生成索引。

仓库只作证据用（确认 split、候选构造、字段含义），不参与任何统计。
索引写在 reference/REPOS.md，方便对接时说明每个仓库用来核对什么。

用法（在 ir_code 下执行）：
    python scripts/clone_reference.py --list
    python scripts/clone_reference.py --all
    python scripts/clone_reference.py --name bergen
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import IR_CODE, banner, load_manifest, to_display  # noqa: E402

REFERENCE_DIR = IR_CODE / "reference"


def git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd) if cwd else None, capture_output=True, text=True
    )


def commit_of(path: Path) -> str:
    result = git("rev-parse", "--short", "HEAD", cwd=path)
    return result.stdout.strip() if result.returncode == 0 else "未知"


def clone(repo: dict, refresh: bool) -> str:
    target = REFERENCE_DIR / repo["name"]
    if (target / ".git").exists():
        if refresh:
            git("pull", "--ff-only", cwd=target)
            return f"已更新 -> {commit_of(target)}"
        return f"已存在 -> {commit_of(target)}"
    target.parent.mkdir(parents=True, exist_ok=True)
    result = git("clone", "--depth", "1", repo["url"], str(target))
    if result.returncode != 0:
        return f"失败：{result.stderr.strip().splitlines()[-1] if result.stderr else '未知错误'}"
    return f"已克隆 -> {commit_of(target)}"


def write_index(manifest: dict) -> Path:
    repos = manifest.get("reference_repos") or []
    lines = [
        "# 参考仓库索引",
        "",
        "论文官方仓库的浅克隆，只用来核对 split、候选构造和字段含义，**不参与统计**。",
        "重新克隆或更新：`python scripts/clone_reference.py --all`（加 `--refresh` 会 git pull）。",
        "",
        "| 仓库 | 用途 | 核对哪些配置 | commit |",
        "| --- | --- | --- | --- |",
    ]
    for repo in repos:
        target = REFERENCE_DIR / repo["name"]
        state = commit_of(target) if (target / ".git").exists() else "未克隆"
        feeds = "；".join(repo.get("feeds") or [])
        lines.append(f"| [{repo['name']}]({repo['url']}) | {repo['why']} | {feeds} | `{state}` |")
    lines.append("")
    lines.append("记录 commit 是为了以后能回到同一份代码核对，避免仓库更新后结论对不上。")
    lines.append("")
    path = REFERENCE_DIR / "REPOS.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="克隆论文参考仓库并生成索引")
    parser.add_argument("--name", nargs="*", default=None, help="只处理指定仓库")
    parser.add_argument("--all", action="store_true", help="处理全部")
    parser.add_argument("--list", action="store_true", help="只列清单")
    parser.add_argument("--refresh", action="store_true", help="已存在的仓库执行 git pull")
    args = parser.parse_args()

    manifest = load_manifest()
    repos = manifest.get("reference_repos") or []
    if not repos:
        print("manifest 里没有 reference_repos")
        return 1

    if args.list:
        banner("参考仓库")
        for repo in repos:
            target = REFERENCE_DIR / repo["name"]
            state = "已克隆 " + commit_of(target) if (target / ".git").exists() else "未克隆"
            print(f"  {repo['name']:20s} {state:22s} {repo['url']}")
        return 0

    if not args.all and not args.name:
        parser.error("请用 --name 指定仓库，或用 --all")

    selected = repos if args.all else [r for r in repos if r["name"] in (args.name or [])]
    banner("克隆参考仓库")
    for repo in selected:
        print(f"  {repo['name']:20s} {clone(repo, args.refresh)}")

    path = write_index(manifest)
    print(f"\n  索引已写入 {to_display(path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
