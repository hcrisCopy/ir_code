"""Download the public datasets listed in README sections A2.3--A2.14.

Run this script from the ``ir_code`` Conda environment.  Files are placed in
``../ir_data/datasets`` by default.  HTTP downloads use aria2 when available
for parallel, resumable transfers and fall back to requests + tqdm.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import requests
from tqdm import tqdm


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_ROOT = (SCRIPT_DIR / "../ir_data/datasets").resolve()
DEFAULT_TOOLS_ROOT = (SCRIPT_DIR / "../ir_data/_tools").resolve()


@dataclass(frozen=True)
class Context:
    data_root: Path
    tools_root: Path
    connections: int
    repair: bool
    extract: bool


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
    for candidate in candidates:
        if candidate.exists():
            print(f"[repair] removing incomplete target: {candidate}")
            candidate.unlink()


def remote_size(url: str, headers: dict[str, str] | None = None) -> int | None:
    try:
        response = requests.head(
            url, headers=headers, allow_redirects=True, timeout=(15, 30)
        )
        response.raise_for_status()
        value = response.headers.get("Content-Length")
        return int(value) if value else None
    except (requests.RequestException, ValueError):
        return None


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
    """Single-connection resumable fallback when aria2 is unavailable."""
    part = Path(f"{destination}.part")
    downloaded = part.stat().st_size if part.exists() else 0
    request_headers = dict(headers or {})
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
            for chunk in response.iter_content(chunk_size=4 * 1024 * 1024):
                if chunk:
                    stream.write(chunk)
                    progress.update(len(chunk))
    os.replace(part, destination)


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
    if destination.exists() and expected_size == destination.stat().st_size:
        print(f"[skip] complete: {destination.name} ({expected_size:,} bytes)")
    else:
        aria2 = shutil.which("aria2c")
        if aria2:
            command = [
                aria2,
                "--continue=true",
                f"--max-connection-per-server={ctx.connections}",
                f"--split={ctx.connections}",
                "--min-split-size=1M",
                "--file-allocation=none",
                "--max-tries=0",
                "--retry-wait=3",
                "--summary-interval=1",
                "--console-log-level=notice",
                f"--dir={destination.parent}",
                f"--out={destination.name}",
            ]
            for key, value in (headers or {}).items():
                command.append(f"--header={key}: {value}")
            command.append(url)
            run(command)
        else:
            print(
                "[warning] aria2c was not found; using the slower requests fallback.\n"
                "          Install the fast downloader with:\n"
                "          conda install -c conda-forge aria2 -y"
            )
            requests_download(url, destination, headers=headers)

    if expected_size is not None and destination.stat().st_size != expected_size:
        raise RuntimeError(
            f"Size mismatch for {destination}: expected {expected_size:,}, "
            f"got {destination.stat().st_size:,}. Run again with --repair."
        )
    if checksum:
        verify_hash(destination, checksum[0], checksum[1])


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
        (
            "collection.tar.gz",
            ("md5", "87dd01826da3e2ad45447ba5af577628"),
        ),
        ("queries.tar.gz", None),
        ("qrels.train.tsv", None),
        ("qrels.dev.tsv", None),
    ]
    for name, checksum in specs:
        download(ctx, f"{base}/{name}", destination / name, checksum=checksum)
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
    for name in (
        "msmarco_v2_passage.tar",
        "passv2_train_queries.tsv",
        "passv2_train_qrels.tsv",
        "passv2_train_top100.txt.gz",
    ):
        download(ctx, f"{base}/{name}", destination / name, headers=headers)


def export_ir_dataset(dataset: str, record_type: str, destination: Path) -> None:
    if destination.exists() and destination.stat().st_size > 0:
        print(f"[skip] export exists: {destination.name}")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(f"{destination}.part")
    print(f"[export] {dataset} {record_type} -> {destination.name}")
    with temporary.open("wb") as output:
        run(["ir_datasets", "export", dataset, record_type], stdout=output)
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
                "--fuzzy",
                "https://drive.google.com/file/d/1YqgaJIzmBIH37XBxpRPCVzV_CLh6aOI4/view",
                "-O",
                str(archive),
            ]
        )
    else:
        print(f"[skip] file exists: {archive}")
    if ctx.extract:
        extract_tar(archive, destination, ".echo-data.extracted")


def a29_atlas(ctx: Context) -> None:
    repository = ctx.tools_root / "atlas"
    destination = ctx.data_root / "atlas_wikipedia_2020_12"
    clone_repository("https://github.com/facebookresearch/atlas.git", repository)
    destination.mkdir(parents=True, exist_ok=True)
    run(
        [
            sys.executable,
            str(repository / "preprocessing/download_corpus.py"),
            "--corpus",
            "corpora/wiki/enwiki-dec2020",
            "--output_directory",
            str(destination),
        ]
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
    for name in BEIR_PUBLIC:
        archive = destination / f"{name}.zip"
        download(
            ctx,
            f"https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/"
            f"datasets/{name}.zip",
            archive,
        )
        if ctx.extract:
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
    for _, remote_name, local_name, checksum in specs:
        archive = archives / local_name
        download(ctx, f"{base}/{remote_name}", archive, checksum=checksum)
        if ctx.extract:
            extract_tar(archive, indexes, f".{local_name}.extracted")

    commit = "0b4acbd929edd11edfd16250457fb70ff69e9b4f"
    eval_base = f"https://raw.githubusercontent.com/castorini/eval/{commit}"
    for name, _, _, _ in specs:
        topics_name = f"topics.beir-v1.0.0-{name}.test.tsv.gz"
        qrels_name = f"qrels.beir-v1.0.0-{name}.test.txt"
        topics = metadata / topics_name
        download(ctx, f"{eval_base}/topics/{topics_name}", topics)
        download(ctx, f"{eval_base}/qrels/{qrels_name}", metadata / qrels_name)
        if ctx.extract:
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
    for file_id, name in specs:
        download(
            ctx,
            f"https://ndownloader.figshare.com/files/{file_id}",
            destination / name,
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
        help="aria2 connections per file (default: 16)",
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
    if not 1 <= args.connections <= 32:
        raise ValueError("--connections must be between 1 and 32")

    selected = list(TASKS.values()) if args.all else resolve_tasks(args.tasks)
    context = Context(
        data_root=args.data_root.resolve(),
        tools_root=DEFAULT_TOOLS_ROOT,
        connections=args.connections,
        repair=args.repair,
        extract=not args.no_extract,
    )
    print(f"Dataset root: {context.data_root}")
    print("Selected:", ", ".join(task.section for task in selected))
    if context.repair:
        print("Repair mode: selected target files will be redownloaded.")

    for index, task in enumerate(selected, start=1):
        print("\n" + "=" * 78)
        print(f"[{index}/{len(selected)}] {task.section} {task.description}")
        print("=" * 78)
        task.runner(context)
        print(f"[done] {task.section} {task.description}")
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
