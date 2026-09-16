"""统一读取层：把各种数据格式读成同一套记录。

统一记录（unit）字段：
    query_id, query_text, doc_id, text, language_hint, split, subset, source

对外两个能力：
    iter_units(...)   逐条产出候选样本（带 query 上下文）
    DocTexts(...)     按 doc id 取正文，支持流式遍历和按需查找

没能自动解析的格式会抛 NotSupported，并说明原因，绝不猜。
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path
from typing import Any, Iterable, Iterator

from _common import (
    SKIP_DIR_NAMES,
    iter_json,
    iter_jsonl,
    iter_parquet,
    iter_tsv,
    open_text,
    progress,
    real_files,
    resolve_file,
)

CORPUS_KINDS = {"tsv", "tsv_gz", "jsonl", "jsonl_gz", "parquet", "json"}


class NotSupported(RuntimeError):
    """该配置的样本来源还没法自动解析。"""


def _pattern_files(base: Path, pattern: str) -> list[Path]:
    from _common import PLACEHOLDER_RE

    if pattern.endswith((".parquet",)) or "<" in pattern or "*" in pattern:
        globbed = PLACEHOLDER_RE.sub("*", pattern)
        if "*" in globbed:
            return sorted(p for p in base.glob(globbed) if p.is_file())
    path = resolve_file(base, pattern)
    return [path] if path.exists() else []


def pick_text(row: dict[str, Any], fields: list[str]) -> str:
    parts = []
    for field in fields:
        value = row.get(field)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return "\n".join(parts)


# --- 候选样本 ----------------------------------------------------------


def iter_units(
    dataset_name: str,
    entry: dict[str, Any],
    config: dict[str, Any],
    base: Path,
    limit: int | None = None,
    subset: str | None = None,
) -> Iterator[dict[str, Any]]:
    spec = config.get("sample_source") or {}
    kind = spec.get("kind")
    split = config.get("split")
    text_spec = entry.get("text_source") or {}

    if kind in ("derived", "mteb_task"):
        raise NotSupported(
            f"{dataset_name}/{config['config_id']}：样本来源标为 {kind}，"
            "论文派生数据未发布或需逐 task 处理，无法自动解析。"
        )
    if kind in ("conversation", "tsv_triples", "tsv_tuples"):
        raise NotSupported(
            f"{dataset_name}/{config['config_id']}：{kind} 需要先确认候选切分方式，"
            "请先用本脚本看原始记录再决定解析规则。"
        )
    if kind == "external":
        text_dataset = spec.get("text_dataset") or config.get("text_dataset")
        raise NotSupported(
            f"{dataset_name}/{config['config_id']}：正文在 {text_dataset}，"
            f"请对该数据集单独运行；这里只放 query 和 qrels。"
        )

    if subset is not None:
        yield from _iter_one_file(
            dataset_name, config, base, spec, kind, split, subset, limit, text_spec
        )
        return
    if config.get("subsets"):
        for name in config["subsets"]:
            yield from _iter_one_file(
                dataset_name, config, base, spec, kind, split, name, limit, text_spec
            )
        return
    yield from _iter_one_file(
        dataset_name, config, base, spec, kind, split, None, limit, text_spec
    )


def _iter_one_file(
    dataset_name: str,
    config: dict[str, Any],
    base: Path,
    spec: dict[str, Any],
    kind: str,
    split: str | None,
    subset: str | None,
    limit: int | None,
    text_spec: dict[str, Any],
) -> Iterator[dict[str, Any]]:
    pattern = spec.get("file")
    if not pattern:
        raise NotSupported(f"{dataset_name}/{config['config_id']}：sample_source 没有 file 字段")
    if subset:
        pattern = pattern.replace("<subset>", subset).replace("<name>", subset)

    files = _pattern_files(base, pattern)
    if not files:
        raise NotSupported(
            f"{dataset_name}/{config['config_id']}：找不到 {pattern}（subset={subset}）"
        )

    produced = 0
    for path in files:
        domain = path.parent.name if "<domain>" in (spec.get("file") or "") else None
        for unit in _iter_file_units(kind, path, spec, split, subset, domain, text_spec):
            yield unit
            produced += 1
            if limit and produced >= limit:
                return


def _query_of(row: dict[str, Any]) -> tuple[str, str, bool]:
    """尽力取出 query 的 id 和文本。

    返回 (id, 文本, id 是否为合成)。
    有 qid 就用；没有 qid 但有 query 正文，就用正文当 id（这样去重才准）；
    两者都没有才用行号兜底，并标记为合成。
    """
    text = ""
    for field in ("query", "question", "query_text"):
        value = row.get(field)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            break
    if not text:
        for field in ("messages_w_content", "messages", "conversations"):
            messages = row.get(field)
            if isinstance(messages, list) and messages:
                first = messages[0]
                content = first.get("content") if isinstance(first, dict) else None
                if content:
                    text = "[提示词首段] " + content.strip()
                    break
    for field in ("qid", "query_id", "id"):
        value = row.get(field)
        if value not in (None, ""):
            return str(value), text, False
    if text and not text.startswith("[提示词首段]"):
        return text, text, False
    return "", text, True


def _iter_file_units(
    kind: str,
    path: Path,
    spec: dict[str, Any],
    split: str | None,
    subset: str | None,
    domain: str | None,
    text_spec: dict[str, Any],
) -> Iterator[dict[str, Any]]:
    unit_field = spec.get("unit_field")
    id_field = spec.get("id_field") or text_spec.get("id_field")
    source = str(path)

    if kind in ("jsonl_list", "jsonl_ids"):
        text_field = unit_field
        id_only = False
        if text_field is None and id_field:
            text_field = id_field
            id_only = True
        for line_no, row in enumerate(iter_jsonl(path), start=1):
            query_id, query_text, synthetic = _query_of(row)
            if not query_id:
                query_id = f"{path.stem}#{line_no}"
                synthetic = True
            hint = row.get("source") or row.get("dataset")
            values = row.get(text_field) or []
            ids = row.get(id_field) if id_field and id_field != text_field else None
            for index, value in enumerate(values):
                if isinstance(value, dict):
                    doc_id = str(value.get("docid") or value.get("id") or "") or None
                    text = str(value.get("content") or value.get("text") or "")
                elif id_only:
                    doc_id = str(value)
                    text = ""
                else:
                    doc_id = str(ids[index]) if isinstance(ids, list) and index < len(ids) else None
                    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                yield {
                    "query_id": query_id,
                    "query_id_synthetic": synthetic,
                    "query_text": query_text,
                    "doc_id": doc_id,
                    "text": text,
                    "language_hint": hint,
                    "split": split,
                    "subset": subset or domain,
                    "source": source,
                }
        return

    if kind in ("jsonl", "jsonl_gz"):
        for row in iter_jsonl(path):
            doc_id = row.get(id_field) if id_field else None
            fields = [unit_field] if unit_field else (spec.get("text_fields") or text_spec.get("text_fields") or [])
            yield {
                "query_id": None,
                "query_text": None,
                "doc_id": str(doc_id) if doc_id is not None else None,
                "text": pick_text(row, fields),
                "language_hint": row.get("source"),
                "split": split,
                "subset": subset or domain,
                "source": source,
            }
        return

    if kind in ("tsv", "tsv_gz"):
        id_index = spec.get("id_field", text_spec.get("id_field", 0))
        text_fields = spec.get("text_fields") or text_spec.get("text_fields") or [1]
        delimiter = spec.get("delimiter", "\t")
        for row in iter_tsv(path, delimiter):
            if len(row) <= max([id_index] + list(text_fields)):
                continue
            yield {
                "query_id": None,
                "query_text": None,
                "doc_id": row[id_index],
                "text": "\n".join(row[i] for i in text_fields),
                "language_hint": None,
                "split": split,
                "subset": subset or domain,
                "source": source,
            }
        return

    if kind == "parquet":
        fields = (
            spec.get("text_fields")
            or ([unit_field] if unit_field else [])
            or (text_spec.get("text_fields") or [])
        )
        for row in iter_parquet(path):
            doc_id = row.get(id_field) if id_field else None
            yield {
                "query_id": None,
                "query_text": None,
                "doc_id": str(doc_id) if doc_id is not None else None,
                "text": pick_text(row, fields),
                "language_hint": None,
                "split": split,
                "subset": subset or domain,
                "source": source,
            }
        return

    if kind == "parquet_nested":
        for row in iter_parquet(path):
            hits = row.get(unit_field or "hits") or []
            for hit in hits:
                yield {
                    "query_id": str(hit.get("qid") or ""),
                    "query_text": str(row.get("query") or ""),
                    "doc_id": str(hit.get("docid") or ""),
                    "text": hit.get("content") or "",
                    "language_hint": row.get("data_source"),
                    "split": split,
                    "subset": subset or domain,
                    "source": source,
                }
        return

    if kind == "json":
        for row in iter_json(path):
            yield {
                "query_id": str(row.get("key") or row.get("id") or ""),
                "query_text": None,
                "doc_id": None,
                "text": str(row.get("value") or ""),
                "language_hint": None,
                "split": split,
                "subset": subset or domain,
                "source": source,
            }
        return

    raise NotSupported(f"还没实现 kind={kind} 的读取器（{path.name}）")


# --- 正文索引 ----------------------------------------------------------


class DocTexts:
    """按 doc id 取正文。既支持全量流式遍历，也支持只找指定 id。"""

    def __init__(self, dataset_name: str, entry: dict[str, Any], base: Path):
        self.dataset_name = dataset_name
        self.entry = entry
        self.base = base
        self.spec = entry.get("text_source") or {}
        self.kind = self.spec.get("kind")

    def _files(self) -> list[Path]:
        file_pattern = self.spec.get("file")
        if not file_pattern:
            raise NotSupported(f"{self.dataset_name} 没有 text_source.file")
        files = _pattern_files(self.base, file_pattern)
        if not files:
            raise NotSupported(f"{self.dataset_name} 找不到正文文件 {file_pattern}")
        return files

    def _rows_for(self, path: Path) -> Iterator[tuple[str, str]]:
        """按 text_source 的格式，逐条产出 (doc_id, 正文)。"""
        kind = self.kind
        if kind in ("tsv", "tsv_gz"):
            id_index = self.spec.get("id_field", 0)
            text_fields = self.spec.get("text_fields") or [1]
            delimiter = self.spec.get("delimiter", "\t")
            for row in iter_tsv(path, delimiter):
                if len(row) <= max([id_index] + list(text_fields)):
                    continue
                yield row[id_index], "\n".join(row[i] for i in text_fields)
        elif kind in ("jsonl", "jsonl_gz"):
            id_field = self.spec.get("id_field")
            fields = self.spec.get("text_fields") or []
            for row in iter_jsonl(path):
                doc_id = row.get(id_field) if id_field else None
                if doc_id is not None:
                    yield str(doc_id), pick_text(row, fields)
        elif kind == "parquet":
            id_field = self.spec.get("id_field")
            fields = self.spec.get("text_fields") or []
            columns = [f for f in ([id_field] if id_field else []) + fields if f]
            for row in iter_parquet(path, columns=columns or None):
                doc_id = row.get(id_field) if id_field else None
                if doc_id is not None:
                    yield str(doc_id), pick_text(row, fields)
        elif kind == "parquet_nested":
            unit = self.spec.get("unit_field") or "hits"
            id_field = self.spec.get("id_field") or "docid"
            fields = self.spec.get("text_fields") or ["content"]
            for row in iter_parquet(path, columns=[unit]):
                for item in row.get(unit) or []:
                    if isinstance(item, dict) and item.get(id_field) is not None:
                        yield str(item[id_field]), pick_text(item, fields)
        elif kind == "json":
            for row in iter_json(path):
                yield str(row.get("key")), str(row.get("value") or "")
        elif kind == "tar_jsonl_gz":
            yield from _iter_tar_gz_jsonl(path, self.spec)
        elif kind == "jsonl_plus_idmap":
            with open_text(path) as handle:
                mapping = json.load(handle)
            for key, value in mapping.items():
                yield str(key), str(value)
        else:
            raise NotSupported(
                f"{self.dataset_name}：text_source kind={kind} 还没实现正文读取"
            )

    def iter_all(self, limit: int | None = None) -> Iterator[tuple[str, str, str]]:
        """产出 (doc_id, 正文, 来源文件)。"""
        produced = 0
        for path in self._files():
            for doc_id, text in self._rows_for(path):
                yield doc_id, text, str(path)
                produced += 1
                if limit and produced >= limit:
                    return

    def resolve(self, wanted: Iterable[str], verbose: bool = True) -> tuple[dict[str, str], int, list[str]]:
        """只找指定 id；找齐就提前停。返回 (命中表, 遍历行数, 没找到的 id)。"""
        wanted_set = {str(item) for item in wanted}
        found: dict[str, str] = {}
        scanned = 0
        for path in self._files():
            rows = self._rows_for(path)
            if verbose:
                rows = progress(rows, desc=f"找正文 {path.name}", unit="row")
            for doc_id, text in rows:
                scanned += 1
                if doc_id in wanted_set:
                    found[doc_id] = text
                    if len(found) == len(wanted_set):
                        return found, scanned, []
        return found, scanned, sorted(wanted_set - set(found))


def _iter_tar_gz_jsonl(path: Path, spec: dict[str, Any]) -> Iterator[tuple[str, str]]:
    """从 tar 内的 gzip jsonl 分片里抽 id 和正文。"""
    id_field = spec.get("id_field")
    fields = spec.get("text_fields") or []
    with tarfile.open(path, "r|*") as bundle:
        for member in bundle:
            if not member.isfile():
                continue
            stream = bundle.extractfile(member)
            if stream is None:
                continue
            import gzip

            with gzip.GzipFile(fileobj=stream) as unzipped:
                for raw in unzipped:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    doc_id = row.get(id_field) if id_field else None
                    if doc_id is not None:
                        yield str(doc_id), pick_text(row, fields)


def doc_texts_for_config(
    dataset_name: str,
    entry: dict[str, Any],
    config: dict[str, Any],
    base: Path,
    manifest: dict[str, Any],
) -> DocTexts:
    """样本的正文可能挂在别的数据集上，这里统一解析。"""
    spec = config.get("sample_source") or {}
    text_dataset = spec.get("text_dataset") or config.get("text_dataset") or entry.get("text_dataset")
    if text_dataset and text_dataset != dataset_name:
        other = manifest["datasets"][text_dataset]
        from _common import dataset_path

        return DocTexts(text_dataset, other, dataset_path(other))
    return DocTexts(dataset_name, entry, base)


def count_files(directory: Path) -> int:
    return len(real_files(directory))
