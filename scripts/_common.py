"""第二阶段公共模块：路径、manifest、读取器、长度统计、进度条。

所有脚本都从这里取公共能力，路径一律相对项目根目录。
"""

from __future__ import annotations

import gzip
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Iterator

# --- 路径 ---------------------------------------------------------------

SCRIPTS_DIR = Path(__file__).resolve().parent
IR_CODE = SCRIPTS_DIR.parent
PROJECT_ROOT = IR_CODE.parent
IR_DATA = PROJECT_ROOT / "ir_data"
MANIFEST_PATH = IR_CODE / "configs" / "manifest.json"
# 输出产物统一放到 ir_data 下，和数据集同级，方便整包上传服务器
OUTPUTS_DIR = IR_DATA / "outputs"
CACHE_DIR = OUTPUTS_DIR / "cache"
REPORT_DIR = OUTPUTS_DIR / "reports"

SKIP_DIR_NAMES = {".cache", ".git", "__pycache__", ".ipynb_checkpoints"}
PARTIAL_SUFFIXES = (".part", ".aria2", ".tmp", ".incomplete")


def ensure_dirs() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def to_display(path: Path) -> str:
    """把绝对路径转成相对项目根目录的写法，方便对接。"""
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


# --- manifest ----------------------------------------------------------


def load_manifest() -> dict[str, Any]:
    if not MANIFEST_PATH.exists():
        die(f"找不到配置中心：{to_display(MANIFEST_PATH)}")
    with MANIFEST_PATH.open(encoding="utf-8") as handle:
        manifest = json.load(handle)
    if "datasets" not in manifest:
        die("manifest.json 缺少 datasets 字段")
    return manifest


def dataset_path(entry: dict[str, Any]) -> Path:
    """数据集根目录。manifest 里的 path 相对 ir_code。"""
    rel = entry.get("path")
    if not rel:
        die(f"数据集 {entry.get('display_name')} 没有 path 字段")
    return (IR_CODE / rel).resolve()


def dataset_files(entry: dict[str, Any]) -> list[dict[str, Any]]:
    return list(entry.get("files") or [])


def iter_datasets(
    manifest: dict[str, Any], selected: Iterable[str] | None
) -> list[tuple[str, dict[str, Any]]]:
    datasets = manifest["datasets"]
    if not selected:
        return list(datasets.items())
    missing = [name for name in selected if name not in datasets]
    if missing:
        die(
            "manifest 里没有这些数据集：" + ", ".join(missing)
            + "\n可用名称见 python scripts/check_data.py --list"
        )
    return [(name, datasets[name]) for name in selected]


def iter_configs(entry: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for config in entry.get("configs") or []:
        yield config


def find_config(entry: dict[str, Any], config_id: str | None) -> dict[str, Any]:
    configs = list(iter_configs(entry))
    if not configs:
        die(f"{entry.get('display_name')} 没有 config")
    if config_id is None:
        if len(configs) > 1:
            die(
                f"{entry.get('display_name')} 有 {len(configs)} 条 config，请用 --config 指定："
                + "\n  " + "\n  ".join(c["config_id"] for c in configs)
            )
        return configs[0]
    for config in configs:
        if config["config_id"] == config_id:
            return config
    die(
        f"{entry.get('display_name')} 里没有 config {config_id}；可选："
        + ", ".join(c["config_id"] for c in configs)
    )


# --- 文件解析 ----------------------------------------------------------


def resolve_file(base: Path, rel: str) -> Path:
    """文件路径相对数据集目录解析；写 ../ 就能指到别的数据集。"""
    return (base / rel).resolve()


PLACEHOLDER_RE = re.compile(r"<[^>]*>")


def is_opaque_pattern(pattern: str) -> bool:
    """整条就是一个占位符，比如 <HF_DATASETS_CACHE>，没法核对。"""
    return bool(PLACEHOLDER_RE.fullmatch(pattern.strip()))


def expand_files(pattern: str, base: Path) -> list[Path]:
    """把 <占位符> 当成通配段，然后 glob：public/<name>.zip -> public/*.zip。"""
    if is_opaque_pattern(pattern):
        return []
    globbed = PLACEHOLDER_RE.sub("*", pattern)
    if "*" in globbed:
        if not base.is_dir():
            return []
        return sorted(path for path in base.glob(globbed) if path.is_file())
    path = resolve_file(base, globbed)
    return [path] if path.exists() else []


def real_files(directory: Path) -> list[Path]:
    """列出目录下真实文件，跳过 HF 缓存和 git 元数据。"""
    if not directory.exists():
        return []
    out = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIR_NAMES for part in path.relative_to(directory).parts):
            continue
        out.append(path)
    return out


def open_text(path: Path):
    """按后缀自动处理 gzip 的文本读取。"""
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open(encoding="utf-8", errors="replace")


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with open_text(path) as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def iter_tsv(path: Path, delimiter: str = "\t") -> Iterator[list[str]]:
    with open_text(path) as handle:
        for line in handle:
            line = line.rstrip("\r\n")
            if line:
                yield line.split(delimiter)


def iter_parquet(path: Path, columns: list[str] | None = None) -> Iterator[dict[str, Any]]:
    import pyarrow.parquet as pq

    parquet_file = pq.ParquetFile(path)
    for batch in parquet_file.iter_batches(batch_size=2048, columns=columns):
        for row in batch.to_pylist():
            yield row


def iter_json(path: Path) -> Iterator[Any]:
    with open_text(path) as handle:
        obj = json.load(handle)
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield {"key": key, "value": value}
    elif isinstance(obj, list):
        yield from obj


def get_field(row: dict[str, Any], field: str | None) -> Any:
    if field is None:
        return None
    current: Any = row
    for part in field.replace("[]", "").split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


# --- 样本长度 ----------------------------------------------------------


def percentile_nearest(sorted_values: list[int], percent: int) -> int | None:
    """最近秩法：排序后取第 ceil(p/100 * N) 个，不插值。"""
    total = len(sorted_values)
    if total == 0:
        return None
    rank = math.ceil(percent / 100 * total)
    index = min(max(rank, 1), total) - 1
    return sorted_values[index]


def summarize(sorted_values: list[int]) -> dict[str, Any]:
    total = len(sorted_values)
    if total == 0:
        return {"count": 0, "mean": None, "p25": None, "p50": None, "p75": None, "p90": None, "min": None, "max": None}
    return {
        "count": total,
        "mean": round(sum(sorted_values) / total, 2),
        "p25": percentile_nearest(sorted_values, 25),
        "p50": percentile_nearest(sorted_values, 50),
        "p75": percentile_nearest(sorted_values, 75),
        "p90": percentile_nearest(sorted_values, 90),
        "min": sorted_values[0],
        "max": sorted_values[-1],
    }


HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
WORD_RE = re.compile(r"[A-Za-z]+(?:['\u2019-][A-Za-z]+)*")
NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")
LATIN_RE = re.compile(r"[A-Za-z]")


def count_words(text: str) -> int:
    """中文每字计 1；英文按 Unicode 单词；连续数字计 1；标点不计。"""
    return len(HAN_RE.findall(text)) + len(WORD_RE.findall(text)) + len(NUM_RE.findall(text))


def detect_language(text: str, hint: str | None = None) -> str:
    """返回 zh / en / other。优先用数据自带的来源字段。"""
    if hint:
        lowered = hint.lower()
        if any(key in lowered for key in ("cmedqa", "chinese", "zh", "dureader", "t2ranking")):
            return "zh"
        if any(key in lowered for key in ("msmarco", "nq", "hotpot", "fever", "eli5", "squad", "trivia", "quora", "allnli", "fiqa", "scifact", "scidocs", "climate")):
            return "en"
    if not text:
        return "other"
    total = len(text)
    han = len(HAN_RE.findall(text))
    latin = len(LATIN_RE.findall(text))
    if han / total >= 0.05:
        return "zh"
    if latin / total >= 0.30:
        return "en"
    return "other"


# --- tokenizer ---------------------------------------------------------


_TOKENIZER = None


class TokenizerUnavailable(RuntimeError):
    """tokenizer 还没准备好，调用方自己决定是退出还是跳过。"""


def load_tokenizer(globals_block: dict[str, Any], override: str | None = None):
    """只加载 tokenizer，不加载模型权重。"""
    global _TOKENIZER
    if _TOKENIZER is not None:
        return _TOKENIZER
    try:
        from transformers import AutoTokenizer
    except ImportError as error:
        raise TokenizerUnavailable(f"缺少 transformers：{error}") from error

    raw = override or globals_block["tokenizer"]["path"]
    path = Path(raw)
    if not path.is_absolute():
        path = (IR_CODE / raw).resolve()
    if not path.exists():
        raise TokenizerUnavailable(
            f"找不到 tokenizer：{to_display(path)}（按 README A1.3 下载 Qwen3-1.7B，或用 --tokenizer 指定路径）"
        )
    _TOKENIZER = AutoTokenizer.from_pretrained(
        str(path),
        use_fast=globals_block["tokenizer"].get("use_fast", True),
        local_files_only=True,
    )
    return _TOKENIZER


def count_tokens(tokenizer, texts: list[str], add_special_tokens: bool = False) -> list[int]:
    result = tokenizer(
        texts,
        add_special_tokens=add_special_tokens,
        truncation=False,
        return_length=True,
    )
    return list(result["length"])


# --- 输出与进度 --------------------------------------------------------


def progress(iterable, total: int | None = None, desc: str = "", unit: str = "it"):
    try:
        from tqdm import tqdm

        return tqdm(iterable, total=total, desc=desc, unit=unit, dynamic_ncols=True)
    except ImportError:
        return iterable


def hsize(num_bytes: int | float) -> str:
    value = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TiB"


def banner(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def die(message: str) -> None:
    print(f"错误：{message}", file=sys.stderr)
    raise SystemExit(2)


def rule(char: str = "-", width: int = 78) -> None:
    print(char * width)


def show_sample(rows: list[dict[str, Any]], title: str, limit: int = 1) -> None:
    """打印一条样本，让人肉眼确认数据长什么样。"""
    print()
    print(f"[样本] {title}")
    if not rows:
        print("    （没有取到数据）")
        return
    for index, row in enumerate(rows[:limit], start=1):
        if limit > 1:
            print(f"  --- 第 {index} 条 ---")
        for key, value in row.items():
            if isinstance(value, list):
                preview = value[:1]
                print(f"    {key:16s} = list[{len(value)}] {short(preview)}")
            else:
                print(f"    {key:16s} = {short(value)}")


def short(value: Any, limit: int = 180) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = text.replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit] + " …"
