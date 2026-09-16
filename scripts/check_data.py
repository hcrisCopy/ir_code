"""① 检查数据齐全：服务器上有哪些文件、还缺哪个数据集。

用法（在 ir_code 下执行）：
    python scripts/check_data.py --list
    python scripts/check_data.py --dataset rearank_12k bright
    python scripts/check_data.py --all
结果同时写入 outputs/inventory.csv。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    OUTPUTS_DIR,
    PARTIAL_SUFFIXES,
    banner,
    dataset_files,
    dataset_path,
    ensure_dirs,
    expand_files,
    hsize,
    is_opaque_pattern,
    iter_datasets,
    load_manifest,
    real_files,
    to_display,
)

STATUS_ORDER = {
    "missing": 0,
    "partial": 1,
    "junk": 2,
    "size": 3,
    "md5": 4,
    "ok": 5,
    "n/a": 6,
}
STATUS_LABEL = {
    "ok": "完整",
    "missing": "缺失",
    "partial": "未下完",
    "junk": "残留",
    "size": "大小不符",
    "md5": "校验不符",
    "n/a": "无法核对",
}


def md5_of(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def is_partial(path: Path) -> bool:
    name = path.name
    return name.endswith(PARTIAL_SUFFIXES) or ".before-aria2" in name or ".aria2" in name or ".dl." in name


def check_dataset(name: str, entry: dict) -> tuple[list[dict], list[dict]]:
    base = dataset_path(entry)
    expected_rows: list[dict] = []
    for spec in dataset_files(entry):
        pattern = spec["path"]
        note = spec.get("note", "")
        if is_opaque_pattern(pattern):
            expected_rows.append({"path": pattern, "status": "n/a", "detail": note, "files": 0, "bytes": 0})
            continue

        found = [p for p in expand_files(pattern, base) if not is_partial(p)]
        if not found:
            alternative = spec.get('extracted_pattern')
            extracted = expand_files(alternative, base) if alternative else []
            marker = (base / (pattern + '.extracted'))
            if extracted and len(extracted) >= spec.get('min_files',1):
                expected_rows.append({'path':pattern,'status':'ok','detail':'已解压：原包可删除；产物已存在','files':len(extracted),'bytes':sum(p.stat().st_size for p in extracted)})
                continue
            if marker.exists():
                import json
                try:
                    metadata = json.loads(marker.read_text(encoding='utf-8'))
                    if all((base/p['path']).is_file() and (base/p['path']).stat().st_size == p['size'] for p in metadata['members']):
                        expected_rows.append({'path':pattern,'status':'ok','detail':'原包已删除，解压清单校验通过','files':len(metadata['members']),'bytes':0})
                        continue
                except (ValueError,KeyError): pass
            expected_rows.append({"path": pattern, "status": "missing", "detail": note or "没找到", "files": 0, "bytes": 0})
            continue

        total = sum(p.stat().st_size for p in found)
        status = "ok"
        detail = f"{len(found)} 个文件"
        if len(found) < spec.get('min_files',1):
            status='missing'
            detail=f'只有 {len(found)} / {spec["min_files"]} 个文件'
        if len(found) == 1 and "bytes" in spec:
            actual = found[0].stat().st_size
            if actual != spec["bytes"]:
                status = "size"
                detail = f"实际 {actual:,} / 期望 {spec['bytes']:,}"
        if len(found) == 1 and 'md5' in spec and status=='ok':
            actual_md5=md5_of(found[0])
            if actual_md5 != spec['md5']:
                status='md5'; detail=f'实际 MD5 {actual_md5}'
            else: detail='MD5 一致'
        expected_rows.append(
            {
                "path": pattern,
                "status": status,
                "detail": detail,
                "files": len(found),
                "bytes": total,
                "resolved": " | ".join(to_display(p) for p in found[:3]),
            }
        )

    partial_rows = []
    for path in real_files(base):
        if not is_partial(path):
            continue
        is_junk = ".before-aria2" in path.name
        partial_rows.append(
            {
                "path": to_display(path),
                "status": "junk" if is_junk else "partial",
                "detail": f"{hsize(path.stat().st_size)}，"
                + ("下载器切换时的残留备份，可以删" if is_junk else "未下完，需要续传"),
                "files": 1,
                "bytes": path.stat().st_size,
            }
        )
    return expected_rows, partial_rows


def main() -> int:
    parser = argparse.ArgumentParser(description="检查数据集文件是否齐全")
    parser.add_argument("--dataset", nargs="*", default=None, help="数据集名（manifest 的键），可多个")
    parser.add_argument("--all", action="store_true", help="检查全部数据集")
    parser.add_argument("--list", action="store_true", help="只列出数据集名")
    parser.add_argument("--no-csv", action="store_true", help="不写 outputs/inventory.csv")
    args = parser.parse_args()

    manifest = load_manifest()

    if args.list:
        banner("manifest 里的数据集")
        for name, entry in manifest["datasets"].items():
            configs = len(entry.get("configs") or [])
            print(f"  {name:32s} {entry.get('status',''):20s} config={configs}  {entry.get('display_name','')}")
        return 0

    if not args.all and not args.dataset:
        parser.error("请用 --dataset 指定数据集，或用 --all 检查全部；--list 可看名字")

    ensure_dirs()
    records: list[dict] = []
    summary: list[tuple[str, str, str]] = []

    for name, entry in iter_datasets(manifest, None if args.all else args.dataset):
        banner(f"{name}   {entry.get('display_name','')}   [{entry.get('status','')}]")
        base = dataset_path(entry)
        if not base.exists():
            print(f"  目录不存在：{to_display(base)}")
            summary.append((name, "missing", "目录不存在"))
            records.append({'dataset':name,'layer':'expected','path':to_display(base),'status':'missing','detail':'目录不存在','resolved':''})
            continue

        expected_rows, partial_rows = check_dataset(name, entry)
        worst = max(
            expected_rows + partial_rows,
            key=lambda row: -STATUS_ORDER[row["status"]],
            default=None,
        )
        for layer, rows in (("expected", expected_rows), ("partial", partial_rows)):
            for row in rows:
                flag = STATUS_LABEL[row["status"]]
                print(f"  [{flag:4s}] {row['path']}")
                if row.get("detail"):
                    print(f"           {row['detail']}")
                if row.get("resolved"):
                    print(f"           -> {row['resolved']}")
                records.append(
                    {
                        "dataset": name,
                        "layer": layer,
                        "path": row["path"],
                        "status": row["status"],
                        "detail": row.get("detail", ""),
                        "resolved": row.get("resolved", ""),
                    }
                )

        files = real_files(base)
        total = sum(path.stat().st_size for path in files)
        print(f"  当前磁盘：{len(files)} 个文件，{hsize(total)}")
        dataset_status = worst["status"] if worst else "n/a"
        summary.append((name, dataset_status, f"{len(files)} 文件 / {hsize(total)}"))

    banner("汇总")
    ok = [row for row in summary if row[1] in ("ok", "n/a")]
    bad = [row for row in summary if row[1] not in ("ok", "n/a")]
    print(f"  齐全或无需核对：{len(ok)}")
    for name, status, detail in ok:
        print(f"    {name:32s} {STATUS_LABEL[status]:6s} {detail}")
    print(f"  还缺：{len(bad)}")
    for name, status, detail in bad:
        print(f"    {name:32s} {STATUS_LABEL[status]:6s} {detail}")

    if not args.no_csv:
        target = OUTPUTS_DIR / "inventory.csv"
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=["dataset", "layer", "path", "status", "detail", "resolved"])
            writer.writeheader()
            writer.writerows(records)
        print(f"\n  明细已写入 {to_display(target)}")

    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
