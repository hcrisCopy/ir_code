"""Download the public datasets listed in README sections A2.3--A2.14.

Run this script from the ``ir_code`` Conda environment.  Files are placed in
``../ir_data/datasets`` by default.  HTTP downloads use aria2 when available
for parallel, resumable transfers and fall back to requests + tqdm.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import math
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import zipfile
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import requests
from requests.adapters import HTTPAdapter
from tqdm import tqdm


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_ROOT = (SCRIPT_DIR / "../ir_data/datasets").resolve()
DEFAULT_TOOLS_ROOT = (SCRIPT_DIR / "../ir_data/_tools").resolve()

# ir_datasets 0.6.3 在 Windows 上有个临时文件句柄不释放的 bug，
# 一碰就栽在 os.replace（WinError 5）。这个入口负责先打补丁再调它。
# 非 Windows 上它是透明透传，所以可以无条件这么调。
IR_DATASETS_ENTRY = SCRIPT_DIR / "scripts" / "ir_datasets_win_fix.py"

# 小于这个大小就用单连接，分段反而更慢
PARALLEL_MIN_BYTES = 8 * 1024 * 1024
# 单段不小于这个大小，避免切得太碎
MIN_SEGMENT_BYTES = 4 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class Context:
    data_root: Path
    tools_root: Path
    connections: int
    repair: bool
    extract: bool
    files_in_parallel: int = 4
    use_aria2: bool = False


@dataclass(frozen=True)
class Task:
    section: str
    name: str
    description: str
    approximate_size: str
    runner: Callable[[Context], None]


def run(command: list[str], *, cwd: Path | None = None, stdout=None) -> None:
    """Run a command without hiding its progress output."""
    printable = subprocess.list2cmdline(command)
    print(f"\n> {printable}", flush=True)
    result = subprocess.run(command, cwd=cwd, stdout=stdout, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed ({result.returncode}): {printable}")


def remove_download_state(path: Path) -> None:
    """Remove one explicitly selected target and its resumable sidecars."""
    candidates = [path, Path(f"{path}.aria2"), Path(f"{path}.part")]
    candidates.extend(sorted(path.parent.glob(f"{path.name}.dl.*")))
    for candidate in candidates:
        if candidate.exists():
            print(f"[repair] removing incomplete target: {candidate}")
            candidate.unlink()


def make_session(connections: int) -> requests.Session:
    """连接池要够大，否则多线程会被 requests 自己的池子卡住。"""
    session = requests.Session()
    adapter = HTTPAdapter(
        pool_connections=max(connections, 10),
        pool_maxsize=max(connections * 2, 20),
        max_retries=0,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def remote_size(url: str, headers: dict[str, str] | None = None) -> int | None:
    # 必须 identity：CDN（如 raw.githubusercontent.com）对文本文件返回 gzip 压缩的
    # Content-Length，比真实文件小 2-5 倍，会被误判为"大小不符"。
    probe_headers = {**(headers or {}), "Accept-Encoding": "identity"}
    try:
        response = requests.head(
            url, headers=probe_headers, allow_redirects=True, timeout=(15, 30)
        )
        response.raise_for_status()
        value = response.headers.get("Content-Length")
        if value:
            return int(value)
    except (requests.RequestException, ValueError):
        pass
    # HEAD 被拒时退回一次只取 1 字节的 GET，从 Content-Range 里读总大小
    try:
        with requests.get(
            url,
            headers=probe_headers | {"Range": "bytes=0-0"},
            stream=True,
            allow_redirects=True,
            timeout=(15, 30),
        ) as response:
            content_range = response.headers.get("Content-Range", "")
            if "/" in content_range:
                tail = content_range.rsplit("/", 1)[-1]
                if tail.isdigit():
                    return int(tail)
            value = response.headers.get("Content-Length")
            if value and response.status_code == 200:
                return int(value)
    except (requests.RequestException, ValueError):
        pass
    return None


def supports_range(url: str, headers: dict[str, str] | None = None) -> bool:
    try:
        with requests.get(
            url,
            headers={**(headers or {}), "Range": "bytes=0-0"},
            stream=True,
            allow_redirects=True,
            timeout=(15, 30),
        ) as response:
            if response.status_code == 206:
                return True
            return response.headers.get("Accept-Ranges", "").lower() == "bytes"
    except requests.RequestException:
        return False


def verify_hash(path: Path, algorithm: str, expected: str) -> None:
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream, tqdm(
        total=path.stat().st_size,
        unit="B",
        unit_scale=True,
        desc=f"verify {path.name}",
        leave=False,
    ) as progress:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
            progress.update(len(chunk))
    actual = digest.hexdigest().lower()
    if actual != expected.lower():
        raise RuntimeError(
            f"Checksum mismatch for {path}: expected {expected}, got {actual}. "
            "Run this task again with --repair."
        )


def requests_download(
    url: str,
    destination: Path,
    *,
    headers: dict[str, str] | None = None,
) -> None:
    """单连接可续传下载，作为兜底路径。"""
    part = Path(f"{destination}.part")
    downloaded = part.stat().st_size if part.exists() else 0
    request_headers = dict(headers or {})
    request_headers.setdefault("Accept-Encoding", "identity")  # 与 remote_size 口径一致
    if downloaded:
        request_headers["Range"] = f"bytes={downloaded}-"

    with requests.get(
        url,
        headers=request_headers,
        stream=True,
        allow_redirects=True,
        timeout=(30, 120),
    ) as response:
        if downloaded and response.status_code == 200:
            downloaded = 0
            part.unlink(missing_ok=True)
        response.raise_for_status()
        if downloaded:
            content_range = response.headers.get("Content-Range", "")
            if response.status_code != 206 or not content_range.startswith(
                f"bytes {downloaded}-"
            ):
                raise RuntimeError("Server returned an invalid resume range")
        remaining = int(response.headers.get("Content-Length", 0))
        total = downloaded + remaining if remaining else None
        mode = "ab" if downloaded else "wb"
        with part.open(mode) as stream, tqdm(
            total=total,
            initial=downloaded,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            desc=destination.name,
        ) as progress:
            for chunk in response.iter_content(chunk_size=CHUNK_BYTES):
                if chunk:
                    stream.write(chunk)
                    progress.update(len(chunk))
    os.replace(part, destination)
    # aria2 的分片文件可能有空洞，不能当续传源；这里完成后清掉它的控制文件。
    Path(f"{destination}.aria2").unlink(missing_ok=True)


def _fetch_range(
    session: requests.Session,
    url: str,
    headers: dict[str, str] | None,
    start: int,
    end: int,
    target: Path,
    progress: tqdm,
    lock: threading.Lock,
) -> None:
    """下载半开区间 [start, end)，可断点续传。end 为排他上界。"""
    expected = end - start
    have = target.stat().st_size if target.exists() else 0
    if have > expected:
        with target.open("r+b") as stream:
            stream.truncate(expected)
        have = expected
    if have == expected:
        return

    request_headers = dict(headers or {})
    request_headers.setdefault("Accept-Encoding", "identity")  # Range 必须按未压缩字节算
    request_headers["Range"] = f"bytes={start + have}-{end - 1}"
    with session.get(
        url, headers=request_headers, stream=True, allow_redirects=True, timeout=(30, 180)
    ) as response:
        if response.status_code == 200 and (start + have) > 0:
            # 服务器忽略了 Range，分段模式失效，交给调用方回退
            raise RuntimeError("Server ignored the Range header")
        if response.status_code not in (200, 206):
            response.raise_for_status()
        with target.open("ab") as stream:
            for chunk in response.iter_content(chunk_size=CHUNK_BYTES):
                if not chunk:
                    continue
                stream.write(chunk)
                with lock:
                    progress.update(len(chunk))
    if target.stat().st_size != expected:
        raise RuntimeError(
            f"Segment {start}-{end} incomplete: {target.stat().st_size} != {expected}"
        )


def segmented_download(
    url: str,
    destination: Path,
    *,
    headers: dict[str, str] | None = None,
    connections: int = 16,
    size: int | None = None,
) -> bool:
    """多连接分片下载。成功返回 True；服务器不支持分段则返回 False。

    续传方式：`.part` 始终是文件的一段连续前缀；每个分片先落到
    `.<名字>.dl.<起始偏移>`，按偏移升序拼接进 `.part`，拼完即删。
    这样中断后连分片级别都不用重下。
    """
    if size is None:
        size = remote_size(url, headers)
    if not size or size < PARALLEL_MIN_BYTES:
        return False

    part = Path(f"{destination}.part")
    merged = part.stat().st_size if part.exists() else 0
    if merged > size:
        print(f"[warn] {part.name} 比目标文件还大，丢弃重下")
        part.unlink(missing_ok=True)
        merged = 0

    segment_count = max(1, min(connections, size // MIN_SEGMENT_BYTES))
    segment_length = math.ceil(size / segment_count)

    # 已合并的部分不用管，只下 [merged, size)
    ranges: list[tuple[int, int]] = []
    if merged < size:
        first_index = merged // segment_length
        ranges.append((merged, min(size, (first_index + 1) * segment_length)))
        for index in range(first_index + 1, segment_count):
            start = index * segment_length
            if start >= size:
                break
            ranges.append((start, min(size, start + segment_length)))
    ranges = sorted(set(ranges))

    if not ranges:
        if part.stat().st_size == size:
            os.replace(part, destination)
            return True
        return False

    session = make_session(len(ranges))
    lock = threading.Lock()
    already = sum(
        min(path.stat().st_size, end - start)
        for start, end in ranges
        if (path := Path(f"{destination}.dl.{start}")).exists()
    )
    print(
        f"[download] {destination.name}: {size / 1024**3:.2f} GiB，"
        f"{len(ranges)} 个分片并行（已有 {merged / 1024**2:.0f} MiB）"
    )
    try:
        with tqdm(
            total=size,
            initial=merged + already,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            desc=destination.name,
        ) as progress:
            with ThreadPoolExecutor(max_workers=len(ranges)) as pool:
                futures = {
                    pool.submit(
                        _fetch_range,
                        session,
                        url,
                        headers,
                        start,
                        end,
                        Path(f"{destination}.dl.{start}"),
                        progress,
                        lock,
                    ): (start, end)
                    for start, end in ranges
                }
                for future in as_completed(futures):
                    future.result()
    except RuntimeError as error:
        if "Range" in str(error):
            print("[fallback] 服务器不支持分段下载，改用单连接。")
            for start, _ in ranges:
                Path(f"{destination}.dl.{start}").unlink(missing_ok=True)
            return False
        raise
    finally:
        session.close()

    # 按偏移升序拼进 .part，拼一个删一个，峰值占用只多一个分片
    with part.open("ab") as sink:
        for start, end in ranges:
            chunk_path = Path(f"{destination}.dl.{start}")
            if not chunk_path.exists():
                raise RuntimeError(f"缺少分片文件 {chunk_path.name}")
            with chunk_path.open("rb") as source:
                shutil.copyfileobj(source, sink, CHUNK_BYTES)
            chunk_path.unlink(missing_ok=True)

    if part.stat().st_size != size:
        raise RuntimeError(
            f"{part.name} 拼接后大小不对：{part.stat().st_size:,} != {size:,}"
        )
    os.replace(part, destination)
    Path(f"{destination}.aria2").unlink(missing_ok=True)
    return True


def aria2_download(
    ctx: Context,
    url: str,
    destination: Path,
    headers: dict[str, str] | None = None,
) -> bool:
    aria2 = shutil.which("aria2c")
    if not aria2:
        return False
    command = [
        aria2,
        "--continue=true",
        f"--max-connection-per-server={ctx.connections}",
        f"--split={ctx.connections}",
        "--min-split-size=1M",
        "--file-allocation=none",
        "--max-tries=5",
        "--retry-wait=3",
        "--summary-interval=1",
        "--console-log-level=notice",
        # Windows 版 aria2 对部分 CDN 会 TLS 握手失败，放宽这两项能救回一部分
        "--check-certificate=false",
        "--min-tls-version=TLSv1.2",
        f"--dir={destination.parent}",
        f"--out={destination.name}",
    ]
    for key, value in (headers or {}).items():
        command.append(f"--header={key}: {value}")
    command.append(url)
    try:
        run(command)
    except RuntimeError:
        print("[fallback] aria2 失败，改用 Python 多连接下载。")
        return False
    return True


def download(
    ctx: Context,
    url: str,
    destination: Path,
    *,
    headers: dict[str, str] | None = None,
    checksum: tuple[str, str] | None = None,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if ctx.repair:
        remove_download_state(destination)

    expected_size = remote_size(url, headers)
    # 未完成的部分文件/分片残留：有则说明上次没下完，必须续传，不能只看目标文件在不在
    sidecars = [p for p in (Path(f"{destination}.part"), Path(f"{destination}.aria2"))
                if p.exists()]
    sidecars += sorted(destination.parent.glob(f"{destination.name}.dl.*"))
    if (
        destination.exists()
        and not sidecars
        and expected_size is not None
        and expected_size == destination.stat().st_size
    ):
        print(f"[skip] complete: {destination.name} ({expected_size:,} bytes)")
    elif destination.exists() and not sidecars and expected_size is None:
        # 远端大小探测失败（网络抖动）。目标文件存在且无任何分片残留 = 上次已完整落盘
        # （分段合并完成才 os.replace），按已完成跳过；有校验和的话后面仍会本地校验。
        print(f"[skip] exists（远端大小探测失败，按本地完成状态跳过）: {destination.name}")
    else:
        done = False
        if ctx.use_aria2:
            done = aria2_download(ctx, url, destination, headers)
        if not done:
            done = segmented_download(
                url,
                destination,
                headers=headers,
                connections=ctx.connections,
                size=expected_size,
            )
        if not done:
            print("[download] 单连接模式（支持断点续传）")
            requests_download(url, destination, headers=headers)

    if expected_size is not None and destination.stat().st_size != expected_size:
        raise RuntimeError(
            f"Size mismatch for {destination}: expected {expected_size:,}, "
            f"got {destination.stat().st_size:,}. Run again with --repair."
        )
    if checksum:
        verify_hash(destination, checksum[0], checksum[1])


@dataclass(frozen=True)
class FetchItem:
    url: str
    destination: Path
    headers: dict[str, str] | None = None
    checksum: tuple[str, str] | None = None


def download_many(ctx: Context, items: Iterable[FetchItem]) -> None:
    """一个任务里的多个文件并行下载，谁先好谁先过。"""
    queue = list(items)
    if not queue:
        return
    workers = max(1, min(ctx.files_in_parallel, len(queue)))
    if workers == 1:
        for item in queue:
            download(ctx, item.url, item.destination, headers=item.headers, checksum=item.checksum)
        return

    print(f"[parallel] {len(queue)} 个文件，同时下 {workers} 个")
    errors: list[tuple[Path, BaseException]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                download, ctx, item.url, item.destination,
                headers=item.headers, checksum=item.checksum,
            ): item
            for item in queue
        }
        for future in as_completed(futures):
            item = futures[future]
            try:
                future.result()
            except BaseException as error:  # noqa: BLE001 - 收集后统一抛出
                errors.append((item.destination, error))
                print(f"[error] {item.destination.name}: {error}")
    if errors:
        names = ", ".join(path.name for path, _ in errors)
        raise RuntimeError(f"以下文件下载失败：{names}（可重跑同一命令续传）")


def extract_tar(archive: Path, destination: Path, marker_name: str) -> None:
    marker = destination / marker_name
    if marker.exists():
        print(f"[skip] already extracted: {archive.name}")
        return
    print(f"[extract] {archive.name} -> {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:*") as bundle:
        members = bundle.getmembers()
        for member in tqdm(members, desc=f"extract {archive.name}", unit="file"):
            bundle.extract(member, destination, filter="data")
    marker.touch()


def extract_zip(archive: Path, destination: Path) -> None:
    marker = destination / f".{archive.stem}.extracted"
    if marker.exists():
        print(f"[skip] already extracted: {archive.name}")
        return
    print(f"[extract] {archive.name} -> {destination}")
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        for member in tqdm(members, desc=f"extract {archive.name}", unit="file"):
            bundle.extract(member, destination)
    marker.touch()


def clone_repository(url: str, destination: Path) -> None:
    if (destination / ".git").exists():
        print(f"[skip] repository exists: {destination}")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "clone", "--depth", "1", url, str(destination)])


def a23_msmarco_passage_v1(ctx: Context) -> None:
    destination = ctx.data_root / "msmarco_passage_v1"
    base = "https://msmarco.z22.web.core.windows.net/msmarcoranking"
    specs = [
        ("collection.tar.gz", ("md5", "87dd01826da3e2ad45447ba5af577628")),
        ("queries.tar.gz", None),
        ("qrels.train.tsv", None),
        ("qrels.dev.tsv", None),
    ]
    download_many(
        ctx,
        [
            FetchItem(f"{base}/{name}", destination / name, checksum=checksum)
            for name, checksum in specs
        ],
    )
    if ctx.extract:
        extract_tar(
            destination / "collection.tar.gz", destination, ".collection.extracted"
        )
        extract_tar(destination / "queries.tar.gz", destination, ".queries.extracted")


def a24_msmarco_document_v1(ctx: Context) -> None:
    destination = ctx.data_root / "msmarco_document_v1" / "msmarco-docs.tsv.gz"
    url = (
        "https://msmarco.z22.web.core.windows.net/msmarcoranking/"
        "msmarco-docs.tsv.gz"
    )
    download(ctx, url, destination)


def a25_msmarco_passage_v2(ctx: Context) -> None:
    destination = ctx.data_root / "msmarco_passage_v2"
    base = "https://msmarco.z22.web.core.windows.net/msmarcoranking"
    headers = {"X-Ms-Version": "2019-12-12"}
    names = (
        "msmarco_v2_passage.tar",
        "passv2_train_queries.tsv",
        "passv2_train_qrels.tsv",
        "passv2_train_top100.txt.gz",
    )
    download_many(
        ctx,
        [FetchItem(f"{base}/{name}", destination / name, headers=headers) for name in names],
    )


def export_ir_dataset(dataset: str, record_type: str, destination: Path) -> None:
    if destination.exists() and destination.stat().st_size > 0:
        print(f"[skip] export exists: {destination.name}")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(f"{destination}.part")
    print(f"[export] {dataset} {record_type} -> {destination.name}")
    with temporary.open("wb") as output:
        # 注意：不要直接调 `ir_datasets`，要走补丁入口。
        # ir_datasets 0.6.3 的 util/download.py:264 用 NamedTemporaryFile 建的临时文件
        # 句柄不会释放，Windows 上紧接着的 os.replace 会报 WinError 5「拒绝访问」，
        # 兜底逻辑再把下好的数据删掉，最后只留一个 0 字节文件。
        # 详见 scripts/ir_datasets_win_fix.py。
        run(
            [sys.executable, str(IR_DATASETS_ENTRY), "export", dataset, record_type],
            stdout=output,
        )
    os.replace(temporary, destination)


def a26_trec_dl(ctx: Context) -> None:
    destination = ctx.data_root / "trec_dl"
    for kind in ("passage", "document"):
        for year in (2019, 2020):
            dataset = f"msmarco-{kind}/trec-dl-{year}/judged"
            for record_type in ("queries", "qrels"):
                target = destination / f"trec_dl_{year}_{kind}_{record_type}.tsv"
                if ctx.repair:
                    remove_download_state(target)
                export_ir_dataset(dataset, record_type, target)


def a27_kilt(ctx: Context) -> None:
    destination = (
        ctx.data_root / "kilt_wikipedia_passages" / "kilt_knowledgesource.json"
    )
    download(
        ctx,
        "http://dl.fbaipublicfiles.com/KILT/kilt_knowledgesource.json",
        destination,
    )


def a28_echo_e5(ctx: Context) -> None:
    destination = ctx.data_root / "echo_e5_training_data"
    archive = destination / "echo-data.tar"
    destination.mkdir(parents=True, exist_ok=True)
    if ctx.repair:
        remove_download_state(archive)
    if not archive.exists():
        run(
            [
                sys.executable,
                "-m",
                "gdown",
                # 注意：这里**不能**加 --fuzzy。
                # gdown 6.x 已移除该参数（base 的 6.1.0 和 ir_stats 的 6.2.0 都没有，
                # 报 `unrecognized arguments: --fuzzy`）。而我们给的是完整的
                # /file/d/<id>/view 链接，gdown 自己就能用 parse_url 解析出 file id
                # （实测 parse_url 返回 ('1YqgaJIzmBIH37XBxpRPCVzV_CLh6aOI4', False)），
                # 本来也不需要用 --fuzzy 去模糊匹配。
                # --continue 用于大文件中途断了能续传。
                "--continue",
                "-O",
                str(archive),
                "https://drive.google.com/file/d/1YqgaJIzmBIH37XBxpRPCVzV_CLh6aOI4/view",
            ]
        )
    else:
        print(f"[skip] file exists: {archive}")
    if ctx.extract:
        extract_tar(archive, destination, ".echo-data.extracted")


# ATLAS 官方语料库的 S3 直链（download_tools.py 里 BASE_URL + corpus 路径）
ATLAS_CORPUS_BASE = "https://dl.fbaipublicfiles.com/atlas/corpora/wiki/enwiki-dec2020"


def a29_atlas(ctx: Context) -> None:
    repository = ctx.tools_root / "atlas"
    destination = ctx.data_root / "atlas_wikipedia_2020_12"
    # 官方仓库照旧克隆：它是这个语料的来源说明，也方便对照 ATLAS 的预处理口径
    clone_repository("https://github.com/facebookresearch/atlas.git", repository)
    destination.mkdir(parents=True, exist_ok=True)

    # 注意：这里**故意不调用**官方的 preprocessing/download_corpus.py，改用自家下载器。两个原因：
    #
    # 1) 官方脚本依赖 PyPI 的 wget 模块（preprocessing/download_tools.py 第 9 行
    #    `import wget`），ir_stats 环境里没有。而且 wget 是**单连接、不支持续传**，
    #    而这两个文件合计约 20.5 GiB（实测 Content-Length：
    #    text-list-100-sec.jsonl = 19,833,317,807；infobox.jsonl = 2,186,510,109）。
    #    自己的下载器是 16 分片 + 分片级续传，和其余数据集口径一致。
    #
    # 2) 官方脚本写的是 <输出目录>/corpora/wiki/enwiki-dec2020/<文件>（见
    #    download_tools.py 的 get_download_path = os.path.join(output_dir, path)，path 带子目录）。
    #    但本项目 manifest 里 files[].path 与 text_source.file 都是**相对数据集根目录**的，
    #    check_data.py 按顶层找，readers.py 的 _pattern_files → resolve_file 也不递归。
    #    所以这里直接平铺到数据集根目录，与清单保持一致。
    #    官方脚本的输出路径结构不是我们丢失的东西——它只是把 S3 的目录树搬了下来。
    names = ("text-list-100-sec.jsonl", "infobox.jsonl")
    if ctx.repair:
        for name in names:
            remove_download_state(destination / name)
    download_many(
        ctx,
        [FetchItem(f"{ATLAS_CORPUS_BASE}/{name}", destination / name) for name in names],
    )


BEIR_PUBLIC = (
    "trec-covid",
    "nfcorpus",
    "webis-touche2020",
    "dbpedia-entity",
    "scifact",
    "nq",
    "hotpotqa",
    "fiqa",
    "quora",
    "scidocs",
    "fever",
    "climate-fever",
)


def a210_beir_public(ctx: Context) -> None:
    destination = ctx.data_root / "beir/public"
    download_many(
        ctx,
        [
            FetchItem(
                f"https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/"
                f"datasets/{name}.zip",
                destination / f"{name}.zip",
            )
            for name in BEIR_PUBLIC
        ],
    )
    if ctx.extract:
        for name in BEIR_PUBLIC:
            archive = destination / f"{name}.zip"
            if archive.exists():
                extract_zip(archive, destination)


def gunzip_keep(source: Path) -> None:
    destination = source.with_suffix("")
    if destination.exists() and destination.stat().st_size > 0:
        print(f"[skip] decompressed file exists: {destination.name}")
        return
    print(f"[extract] {source.name} -> {destination.name}")
    with gzip.open(source, "rb") as input_stream, destination.open("wb") as output:
        with tqdm(
            total=source.stat().st_size,
            unit="B",
            unit_scale=True,
            desc=source.name,
        ) as progress:
            while chunk := input_stream.read(4 * 1024 * 1024):
                output.write(chunk)
                progress.update(min(len(chunk), source.stat().st_size - progress.n))


def a211_beir_indexes(ctx: Context) -> None:
    root = ctx.data_root / "beir/prebuilt_indexes"
    archives = root / "archives"
    indexes = root / "indexes"
    metadata = root / "metadata"
    indexes.mkdir(parents=True, exist_ok=True)
    specs = (
        (
            "signal1m",
            "lucene-inverted.beir-v1.0.0-signal1m.flat.20221116.505594.tar.gz",
            "signal1m_beir_flat.tar.gz",
            None,
        ),
        (
            "robust04",
            "lucene-inverted.beir-v1.0.0-robust04.flat.20221116.505594.tar.gz",
            "robust04_beir_flat.tar.gz",
            ("md5", "d508fc770002a99a5dc3da3d0fa001b7"),
        ),
        (
            "trec-news",
            "lucene-inverted.beir-v1.0.0-trec-news.flat.20221116.505594.tar.gz",
            "trec-news_beir_flat.tar.gz",
            ("md5", "22e7752c3d0122c28013b33e5e2134ae"),
        ),
    )
    base = (
        "https://huggingface.co/datasets/castorini/prebuilt-indexes-beir/"
        "resolve/main/lucene-inverted/flat"
    )
    download_many(
        ctx,
        [
            FetchItem(
                f"{base}/{remote_name}",
                archives / local_name,
                checksum=checksum,
            )
            for _, remote_name, local_name, checksum in specs
        ],
    )
    if ctx.extract:
        for _, _, local_name, _ in specs:
            archive = archives / local_name
            if archive.exists():
                extract_tar(archive, indexes, f".{local_name}.extracted")

    commit = "0b4acbd929edd11edfd16250457fb70ff69e9b4f"
    eval_base = f"https://raw.githubusercontent.com/castorini/eval/{commit}"
    metadata_items = []
    for name, _, _, _ in specs:
        topics_name = f"topics.beir-v1.0.0-{name}.test.tsv.gz"
        qrels_name = f"qrels.beir-v1.0.0-{name}.test.txt"
        metadata_items.append(FetchItem(f"{eval_base}/topics/{topics_name}", metadata / topics_name))
        metadata_items.append(FetchItem(f"{eval_base}/qrels/{qrels_name}", metadata / qrels_name))
    download_many(ctx, metadata_items)
    if ctx.extract:
        for name, _, _, _ in specs:
            topics = metadata / f"topics.beir-v1.0.0-{name}.test.tsv.gz"
            if topics.exists():
                gunzip_keep(topics)


def a212_mteb(ctx: Context) -> None:
    destination = ctx.data_root / "mteb_english"
    destination.mkdir(parents=True, exist_ok=True)
    os.environ["HF_DATASETS_CACHE"] = str(destination)
    import mteb  # Imported lazily because this task alone needs it.

    expected = {"MTEB(eng, v1)": 56, "MTEB(eng, v2)": 41}
    for benchmark_name, expected_count in expected.items():
        benchmark = mteb.get_benchmark(benchmark_name)
        if len(benchmark.tasks) != expected_count:
            raise RuntimeError(
                f"{benchmark_name}: expected {expected_count} tasks, "
                f"found {len(benchmark.tasks)}"
            )
        for task in tqdm(benchmark.tasks, desc=benchmark_name, unit="task"):
            task.load_data()


def a213_mirage(ctx: Context) -> None:
    clone_repository(
        "https://github.com/gzxiong/MIRAGE.git", ctx.data_root / "mirage"
    )


def a214_pmc_patients(ctx: Context) -> None:
    destination = ctx.data_root / "pmc_patients_recds"
    specs = (
        ("43050394", "PMC-Patients.json.tar.gz"),
        ("43054744", "ReCDS_benchmark.tar.gz"),
        ("43055212", "Meta_data.tar.gz"),
    )
    download_many(
        ctx,
        [
            FetchItem(f"https://ndownloader.figshare.com/files/{file_id}", destination / name)
            for file_id, name in specs
        ],
    )


TASKS: OrderedDict[str, Task] = OrderedDict(
    (
        task.section.lower(),
        task,
    )
    for task in (
        Task("A2.3", "msmarco-passage-v1", "MS MARCO Passage v1", "~1.1 GB", a23_msmarco_passage_v1),
        Task("A2.4", "msmarco-document-v1", "MS MARCO Document v1", "~8.5 GB", a24_msmarco_document_v1),
        Task("A2.5", "msmarco-passage-v2", "MS MARCO Passage v2", ">20 GB", a25_msmarco_passage_v2),
        Task("A2.6", "trec-dl", "TREC DL 2019/2020 query and qrels", "small", a26_trec_dl),
        Task("A2.7", "kilt", "KILT Wikipedia knowledge source", "~35 GiB", a27_kilt),
        Task("A2.8", "echo-e5", "Echo E5 training data", "unknown", a28_echo_e5),
        Task("A2.9", "atlas", "Atlas Wikipedia 2020-12", "large", a29_atlas),
        Task("A2.10", "beir-public", "BEIR public datasets", "several GB", a210_beir_public),
        Task("A2.11", "beir-indexes", "BEIR Signal-1M/Robust04/TREC-News indexes", "~4.6 GiB", a211_beir_indexes),
        Task("A2.12", "mteb", "MTEB English v1/v2", "task-dependent", a212_mteb),
        Task("A2.13", "mirage", "MIRAGE benchmark repository", "small", a213_mirage),
        Task("A2.14", "pmc-patients", "PMC-Patients and ReCDS", "several GB", a214_pmc_patients),
    )
)

NAME_TO_SECTION = {task.name.lower(): key for key, task in TASKS.items()}


def show_tasks() -> None:
    print("Available download tasks:\n")
    for task in TASKS.values():
        print(
            f"  {task.section:<6} {task.name:<22} "
            f"{task.approximate_size:<14} {task.description}"
        )


def resolve_tasks(values: Iterable[str]) -> list[Task]:
    selected: list[Task] = []
    seen: set[str] = set()
    for raw in values:
        key = raw.lower()
        key = NAME_TO_SECTION.get(key, key)
        if key not in TASKS:
            raise ValueError(f"Unknown dataset task: {raw}")
        if key not in seen:
            selected.append(TASKS[key])
            seen.add(key)
    return selected


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resumable downloader for README A2.3--A2.14 datasets."
    )
    parser.add_argument(
        "tasks",
        nargs="*",
        help="Section numbers or names, for example: A2.3 beir-public",
    )
    parser.add_argument("--list", action="store_true", help="List available tasks")
    parser.add_argument("--all", action="store_true", help="Run every task")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm the very large --all download",
    )
    parser.add_argument(
        "--repair",
        action="store_true",
        help="Delete and redownload target files in the selected tasks",
    )
    parser.add_argument(
        "--connections",
        type=int,
        default=16,
        help="每个文件的分片连接数 (default: 16)",
    )
    parser.add_argument(
        "--files-in-parallel",
        type=int,
        default=4,
        help="同一个任务里同时下载几个文件 (default: 4)",
    )
    parser.add_argument(
        "--use-aria2",
        action="store_true",
        help="改用 aria2c 下载（默认用内置的多连接下载器）",
    )
    parser.add_argument(
        "--no-extract",
        action="store_true",
        help="Download archives without extracting them",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=DEFAULT_DATA_ROOT,
        help="Dataset root (default: ../ir_data/datasets)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.list or (not args.tasks and not args.all):
        show_tasks()
        if not args.list:
            print("\nNothing downloaded. Select a task, e.g.:\n"
                  "  python download_datasets.py A2.3 --repair")
        return 0
    if args.all and args.tasks:
        raise ValueError("Use either --all or explicit task names, not both")
    if args.all and not args.yes:
        raise ValueError(
            "--all includes multiple very large corpora (well over 70 GB). "
            "Add --yes after checking disk space."
        )
    if not 1 <= args.connections <= 64:
        raise ValueError("--connections must be between 1 and 64")
    if not 1 <= args.files_in_parallel <= 16:
        raise ValueError("--files-in-parallel must be between 1 and 16")

    selected = list(TASKS.values()) if args.all else resolve_tasks(args.tasks)
    context = Context(
        data_root=args.data_root.resolve(),
        tools_root=DEFAULT_TOOLS_ROOT,
        connections=args.connections,
        repair=args.repair,
        extract=not args.no_extract,
        files_in_parallel=args.files_in_parallel,
        use_aria2=args.use_aria2,
    )
    print(f"Dataset root: {context.data_root}")
    print("Selected:", ", ".join(task.section for task in selected))
    print(
        f"下载方式: 每个文件 {context.connections} 个分片，"
        f"同时下 {context.files_in_parallel} 个文件"
        + ("（aria2c）" if context.use_aria2 else "（内置多连接）")
    )
    if context.repair:
        print("Repair mode: selected target files will be redownloaded.")

    failures: list[tuple[Task, Exception]] = []
    for index, task in enumerate(selected, start=1):
        print("\n" + "=" * 78)
        print(f"[{index}/{len(selected)}] {task.section} {task.description}")
        print("=" * 78)
        try:
            task.runner(context)
            print(f"[done] {task.section} {task.description}")
        except KeyboardInterrupt:
            raise
        except Exception as error:
            # 单个任务失败不中断整轮：网络抖动很常见，后面的任务继续跑，
            # 最后统一汇总，重跑同一命令即可续传。
            failures.append((task, error))
            print(f"\n[fail] {task.section} {task.description}: {error}", file=sys.stderr)
    if failures:
        summary = "；".join(f"{task.section}（{error}）" for task, error in failures)
        raise RuntimeError(f"{len(failures)} 个任务失败，可重跑同一命令续传：{summary}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted. Run the same command again to resume.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as error:
        print(f"\nERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
