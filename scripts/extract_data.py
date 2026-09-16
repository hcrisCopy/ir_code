"""② 解压数据：解压 -> 逐个文件校验 -> 通过才删压缩包。

设计要点：
  - 只处理 manifest 里标了 extract 的文件；标 keep / none 的一律不动
    （例如 msmarco_v2_passage.tar 里面已经是 gzip JSONL 分片，解开纯属浪费空间）。
  - 校验方式：压缩包内每个成员都能在解压目录里找到同名文件且大小一致。
  - 只有校验全部通过才删压缩包；加 --keep-archive 可以保留。
  - 重复执行安全：解压成功会留下 <压缩包名>.extracted 标记，下次直接跳过。

用法（在 ir_code 下执行）：
    python scripts/extract_data.py --dataset msmarco_passage_v1 --dry-run
    python scripts/extract_data.py --dataset msmarco_passage_v1
    python scripts/extract_data.py --all
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    PARTIAL_SUFFIXES,
    banner,
    dataset_files,
    dataset_path,
    expand_files,
    hsize,
    iter_datasets,
    load_manifest,
    progress,
    to_display,
)

MANAGED_SUFFIXES = {
    ".tar.gz": "tar",
    ".tgz": "tar",
    ".tar": "tar",
    ".zip": "zip",
    ".gz": "gz",
}


def kind_of(archive: Path) -> str | None:
    name = archive.name.lower()
    for suffix, kind in MANAGED_SUFFIXES.items():
        if name.endswith(suffix):
            return kind
    return None


def safe_members(names: list[str], target: Path) -> list[str]:
    escaped = []
    for name in names:
        candidate = (target / name).resolve()
        if not candidate.is_relative_to(target.resolve()) or Path(name).is_absolute():
            escaped.append(name)
    return escaped


def extract_tar(archive: Path, target: Path) -> tuple[list[tuple[str, int]], list[str]]:
    with tarfile.open(archive, "r:*") as bundle:
        members = [m for m in bundle.getmembers() if m.isfile()]
        escaped = safe_members([m.name for m in members], target)
        if escaped:
            raise RuntimeError(f"压缩包里有越界路径：{escaped[:3]}")
        expected = [(m.name, m.size) for m in members]
        for member in progress(members, total=len(members), desc=archive.name, unit="file"):
            try:
                bundle.extract(member, target, filter="data")
            except TypeError:
                bundle.extract(member, target)
    return expected, []


def extract_zip(archive: Path, target: Path) -> tuple[list[tuple[str, int]], list[str]]:
    with zipfile.ZipFile(archive) as bundle:
        infos = [i for i in bundle.infolist() if not i.is_dir()]
        escaped = safe_members([i.filename for i in infos], target)
        if escaped:
            raise RuntimeError(f"压缩包里有越界路径：{escaped[:3]}")
        expected = [(i.filename, i.file_size) for i in infos]
        for info in progress(infos, total=len(infos), desc=archive.name, unit="file"):
            bundle.extract(info, target)
    return expected, []


def gunzip_keep(archive: Path) -> tuple[list[tuple[str, int]], list[str]]:
    target = archive.with_suffix("")
    print(f"  [解压] {archive.name} -> {target.name}")
    temporary=target.with_name(target.name+'.tmp')
    with gzip.open(archive, "rb") as source, temporary.open("wb") as sink:
        shutil.copyfileobj(source, sink, 8 * 1024 * 1024)
    os.replace(temporary,target)
    return [(target.name, target.stat().st_size)], []


def verify(expected: list[tuple[str, int]], target: Path) -> tuple[bool, str]:
    """逐个成员核对：存在、大小一致。"""
    missing = []
    mismatched = []
    for name, size in expected:
        path = target / name
        if not path.exists():
            missing.append(name)
        elif size and path.stat().st_size != size:
            mismatched.append(f"{name}({path.stat().st_size} != {size})")
    if missing or mismatched:
        detail = []
        if missing:
            detail.append(f"缺 {len(missing)} 个：{missing[:3]}")
        if mismatched:
            detail.append(f"大小不符 {len(mismatched)} 个：{mismatched[:3]}")
        return False, "；".join(detail)
    return True, f"{len(expected)} 个成员全部核对通过"


def process_archive(archive: Path, base: Path, declaration: dict, keep: bool, dry_run: bool) -> dict:
    kind = kind_of(archive)
    mode = declaration.get("extract", "none")
    marker = archive.with_name(archive.name + ".extracted")
    result = {"archive": to_display(archive), "kind": kind, "action": "", "ok": True, "detail": ""}

    if mode in ("keep", "none"):
        result["action"] = "跳过（manifest 标为 " + mode + "）"
        return result
    if kind is None:
        result["action"] = "跳过（不认识的压缩格式）"
        return result
    if marker.exists():
        try:
            metadata=json.loads(marker.read_text(encoding='utf-8'))
            if all((base/item['path']).is_file() and (base/item['path']).stat().st_size==item['size'] for item in metadata['members']):
                result['action']='跳过（解压产物清单核对通过）'
                return result
        except (ValueError,KeyError): pass

    target = (base / declaration.get('extract_to','.')).resolve()
    if kind == 'gz': target = archive.parent
    if not target.is_relative_to(base): raise ValueError('解压目标必须在数据集目录内')
    target.mkdir(parents=True,exist_ok=True)
    print(f"\n  {archive.name}  [{kind}]  {hsize(archive.stat().st_size)} -> {to_display(target)}")
    if dry_run:
        result["action"] = "仅预览，未执行"
        return result

    if kind == "tar":
        expected, _ = extract_tar(archive, target)
    elif kind == "zip":
        expected, _ = extract_zip(archive, target)
    else:
        expected, _ = gunzip_keep(archive)

    ok, detail = verify(expected, target)
    result["detail"] = detail
    if not ok:
        result["ok"] = False
        result["action"] = "校验失败，保留压缩包待查"
        print(f"  [失败] {detail}")
        return result

    metadata={'members':[{'path':(target/name).relative_to(base).as_posix(),'size':size} for name,size in expected]}
    marker.write_text(json.dumps(metadata,ensure_ascii=False),encoding='utf-8')
    if keep:
        result["action"] = "解压完成，按要求保留压缩包"
    else:
        archive.unlink()
        result["action"] = "解压完成，压缩包已删除"
    print(f"  [完成] {detail}；{result['action']}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="解压数据集并删除压缩包")
    parser.add_argument("--dataset", nargs="*", default=None, help="数据集名，可多个")
    parser.add_argument("--all", action="store_true", help="处理全部数据集")
    parser.add_argument("--dry-run", action="store_true", help="只列清单，不动文件")
    parser.add_argument("--keep-archive", action="store_true", help="解压后保留压缩包")
    args = parser.parse_args()

    if not args.all and not args.dataset:
        parser.error("请用 --dataset 指定数据集，或用 --all")
    if args.all and args.dataset:
        parser.error("--all 和 --dataset 不能同时用")

    manifest = load_manifest()
    results = []

    for name, entry in iter_datasets(manifest, None if args.all else args.dataset):
        banner(f"{name}   {entry.get('display_name','')}")
        base = dataset_path(entry)
        if not base.exists():
            print(f"  目录不存在，跳过：{to_display(base)}")
            continue

        tasks = []
        for declaration in dataset_files(entry):
            if declaration.get("extract") not in ("tar.gz", "tar", "zip", "gz"):
                continue
            pattern = declaration["path"]
            for archive in expand_files(pattern, base):
                if archive.name.endswith(PARTIAL_SUFFIXES):
                    continue
                tasks.append((archive, declaration))

        if not tasks:
            print("  没有需要解压的压缩包（manifest 里没有标 extract，或文件还没下载）")
            continue

        for archive, declaration in tasks:
            results.append(process_archive(archive, base, declaration, args.keep_archive, args.dry_run))

    banner("汇总")
    done = [r for r in results if r["action"].startswith("解压完成")]
    skipped = [r for r in results if r["action"].startswith("跳过")]
    failed = [r for r in results if not r["ok"]]
    for row in done:
        print(f"  [完成] {row['archive']}  {row['detail']}  {row['action']}")
    for row in skipped:
        print(f"  [跳过] {row['archive']}  {row['action']}")
    for row in failed:
        print(f"  [失败] {row['archive']}  {row['detail']}")
    print(f"\n  解压 {len(done)}，跳过 {len(skipped)}，失败 {len(failed)}")
    if args.dry_run:
        print("  这是预览模式，没有改动任何文件。确认无误后去掉 --dry-run 再跑一次。")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
