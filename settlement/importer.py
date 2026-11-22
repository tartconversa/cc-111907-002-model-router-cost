"""来源资料导入与字段校验。

来源文件只读：导入时把字节副本保存到工作目录的 ``source/`` 下，之后所有分析
只依赖工作目录内的副本与事件日志，互不相关的工作目录不会互相污染。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .store import Store, StoreError
from .timeutil import parse_ts

# 来源契约的必填字段（与 inputs/schema.json 保持一致）。
EXPECTED_FIELDS = {
    "teams": {"team", "period", "budget_fen", "carry"},
    "prices": {"id", "provider", "model", "start", "currency",
               "per_1k_input", "per_1k_output"},
    "fx": {"period", "currency", "cny_rate"},
    "calls": {"id", "request", "attempt_id", "team", "provider", "model",
              "occurred", "received", "input_tokens", "output_tokens", "status"},
    "invoices": {"id", "revision", "provider", "period", "request", "attempt_id",
                 "currency", "amount", "kind", "confirmed"},
    "requests": {"id", "team", "batch", "units", "required_capability",
                 "deferrable", "period", "expires"},
    "capabilities": {"model", "skills", "input_per_unit", "output_per_unit"},
}


def _validate(entity: str, rows: list[dict[str, Any]]) -> list[str]:
    problems: list[str] = []
    if entity not in EXPECTED_FIELDS:
        return [f"未知实体 {entity}"]
    expected = EXPECTED_FIELDS[entity]
    for idx, row in enumerate(rows):
        if set(row) != expected:
            missing = expected - set(row)
            extra = set(row) - expected
            problems.append(f"{entity}[{idx}] 字段不符，缺 {sorted(missing)}，多 {sorted(extra)}")
    if entity == "calls":
        for row in rows:
            if row["status"] not in ("success", "failed"):
                problems.append(f"calls {row['id']} status 非法: {row['status']}")
            if parse_ts(row["received"]) < parse_ts(row["occurred"]):
                problems.append(f"calls {row['id']} received 早于 occurred")
    if entity == "invoices":
        for row in rows:
            if row["kind"] not in ("usage", "shared", "refund"):
                problems.append(f"invoices {row['id']} kind 非法: {row['kind']}")
    return problems


def import_sources(store: Store, input_dir: str | Path,
                   verify_manifest: bool = True) -> dict[str, Any]:
    """把一个契约目录下的全部 JSON 导入工作目录。

    返回每个文件的指纹、条数、校验问题与合并冲突。重复导入同一字节文件是
    幂等的：不产生新事实、不重复占用任何额度。
    """
    input_dir = Path(input_dir)
    summary: dict[str, Any] = {"imported": [], "skipped_duplicates": [],
                               "validation_problems": [], "conflicts": []}

    manifest_path = input_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None

    files = sorted(
        p for p in input_dir.glob("*.json")
        if p.name not in ("manifest.json", "schema.json")
    )
    if not files:
        raise StoreError(f"{input_dir} 下没有可导入的 JSON 来源文件")

    known_hashes = {s["sha256"] for s in store.snapshot["source_files"]}
    for path in files:
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        entity = path.name.removesuffix(".json")

        if verify_manifest and manifest is not None:
            expected = manifest.get("files", {}).get(path.name)
            if expected is not None and expected != digest:
                raise StoreError(f"{path.name} 与 manifest 哈希不一致，拒绝导入")

        rows = json.loads(raw.decode("utf-8"))
        # 拆分文件按文件名首段归并实体，如 invoices_extra.json -> invoices。
        stem = path.name.removesuffix(".json")
        entity = stem.split("_", 1)[0] if "_" in stem else stem
        if entity not in EXPECTED_FIELDS:
            summary["validation_problems"].append(f"{path.name}: 未知来源实体 {entity}")
            continue
        summary["validation_problems"].extend(_validate(entity, rows))

        if digest in known_hashes:
            summary["skipped_duplicates"].append(path.name)
            continue

        before_conflicts = len(store.snapshot["conflicts"])
        store.import_file(path)
        summary["imported"].append({"file": path.name, "sha256": digest, "rows": len(rows)})
        new_conflicts = store.snapshot["conflicts"][before_conflicts:]
        summary["conflicts"].extend(new_conflicts)

    return summary
