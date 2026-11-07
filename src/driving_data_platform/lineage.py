"""清单关联血缘；不扫描媒体/Parquet，不触发加工或写入。"""

import hashlib

from driving_data_platform.common.run_query import (
    EvidenceError,
    identity,
    raw_run,
    read_json,
    scan_runs,
)
from driving_data_platform.publishing.store import active, connect


def dependencies(repo, manifest):
    declaration, _, _ = read_json(repo, "configs/table-lineage-v1.json")
    entries = declaration["dependencies"]
    for item in entries:
        _, checksum, _ = _sql_digest(repo, item["sql"])
        if (
            checksum != item["sha256"]
            or manifest.get("code", {}).get(item["sql"]) != item["sha256"]
            or item["target"] not in manifest.get("tables", {})
        ):
            return {
                "status": "unknown",
                "reason": "SQL 摘要缺失或不匹配，不能推断历史表依赖",
                "items": [],
            }
    return {"status": "matched", "declaration": "table-lineage-v1", "items": entries}


def _sql_digest(repo, relative):
    data = (repo / relative).read_bytes()
    return None, hashlib.sha256(data).hexdigest(), None


def lineage(root, repo, *, run_id=None, generation=None):
    if (run_id is None) == (generation is None):
        raise ValueError("run_id 与 generation 必须且只能指定一个")
    report = None
    if run_id is not None:
        report, _, _ = raw_run(root, run_id)
        generation = report["steps"].get("load", {}).get("generation")
    if generation is not None:
        identity(generation, r"g_[a-f0-9]{64}")
    result = {
        "schema_version": 1,
        "run_id": run_id,
        "generation": generation,
        "scope": "仅核对清单关联，未执行全量数据完整性检查",
        "nodes": [],
        "edges": [],
        "warnings": [],
        "related_runs": [],
        "table_dependencies": {"status": "unknown", "items": []},
    }
    nodes, edges = result["nodes"], result["edges"]

    def node(key, kind, value, status="recorded_only", evidence=None):
        nodes.append(
            {
                "id": key,
                "kind": kind,
                "identity": value,
                "status": status,
                "evidence": evidence,
            }
        )
        return key

    def edge(source, target, status="matched"):
        edges.append({"source": source, "target": target, "status": status})

    def completion(relative, expected=None, expected_type=dict):
        if expected is not None:
            identity(expected, r"[a-f0-9]{64}")
        try:
            data, checksum, _ = read_json(root, relative, expected_type=expected_type)
            state = "matched" if expected and checksum == expected else "unrecorded"
            if expected and checksum != expected:
                state = "inconsistent"
                result["warnings"].append(
                    {"reference": relative, "message": "清单摘要冲突"}
                )
            return (
                data if state == "matched" else {},
                state,
                {
                    "reference": relative,
                    "sha256": checksum,
                    "expected_sha256": expected,
                },
            )
        except (FileNotFoundError, EvidenceError):
            result["warnings"].append(
                {"reference": relative, "message": "本地清单缺失或不可安全读取"}
            )
            return (
                {},
                "recorded_only",
                {"reference": relative, "expected_sha256": expected},
            )

    if report:
        node(
            "run",
            "pipeline_report",
            {"run_id": run_id, "status": report["status"]},
            "matched",
            {"reference": f"reports/pipeline-{run_id}.json"},
        )
    manifest, db_checksum = {}, None
    with connect(root) as conn:
        pointer = active(conn)["generation"]
        if generation:
            row = conn.execute(
                "SELECT manifest,manifest_sha256 FROM control.generation WHERE generation=%s",
                (generation,),
            ).fetchone()
            if row is None:
                if report is None:
                    raise FileNotFoundError("服务版本不存在")
                result["warnings"].append(
                    {"message": "报告中的服务版本在数据库中不存在"}
                )
            else:
                manifest, db_checksum = row
                node(
                    "generation",
                    "postgres_generation",
                    generation,
                    "matched",
                    {"reference": "control.generation", "sha256": db_checksum},
                )
                result["is_active"] = generation == pointer
                events = conn.execute(
                    "SELECT event_id,previous_generation,generation,revision,action,created_at FROM control.publication WHERE generation=%s OR previous_generation=%s ORDER BY revision",
                    (generation, generation),
                ).fetchall()
                result["publication_events"] = [
                    dict(
                        zip(
                            (
                                "event_id",
                                "previous_generation",
                                "generation",
                                "revision",
                                "action",
                                "created_at",
                            ),
                            [*r[:-1], r[-1].isoformat()],
                        )
                    )
                    for r in events
                ]
                for event in result["publication_events"]:
                    node(
                        "event:" + event["event_id"],
                        "publication_event",
                        event,
                        "matched",
                        {"reference": "control.publication"},
                    )
                    edge("generation", "event:" + event["event_id"])
    runs, warnings = scan_runs(root)
    result["related_runs"] = [
        i["run_id"]
        for i in runs
        if generation and i["candidate_generation"] == generation
    ]
    result["warnings"].extend(warnings)
    steps = report["steps"] if report else {}
    wh = steps.get("warehouse", {})
    ods_step = steps.get("ods", {})
    snapshot = (
        (ods_step.get("result", ods_step)).get("snapshot_id")
        if report
        else manifest.get("snapshot_id")
    )
    version = (
        report.get("processing_version")
        if report
        else manifest.get("processing_version")
    )
    # 所有定位由已登记身份和固定目录构成，不使用报告提供的任意路径。
    warehouse = {}
    catalog = manifest.get("catalog") or {}
    if report:
        catalog_id = steps.get("load", {}).get("catalog_id")
    else:
        database = catalog.get("database", "")
        catalog_id = (
            database.removeprefix("ddp_hms_")
            if database.startswith("ddp_hms_")
            else None
        )
    if report and manifest:
        conflicts = []
        for key, reported in (
            ("snapshot_id", snapshot),
            ("processing_version", version),
        ):
            if reported is not None and reported != manifest.get(key):
                conflicts.append(key)
        if (
            catalog_id
            and catalog.get("database")
            and catalog["database"] != "ddp_hms_" + catalog_id
        ):
            conflicts.append("catalog_id")
        if conflicts:
            node(
                "report_identity_conflict",
                "identity_conflict",
                conflicts,
                "inconsistent",
            )
            result["warnings"].append({"message": "报告与数据库保存身份冲突"})
    if catalog_id:
        identity(catalog_id)
        consumed, state, ref = completion(
            f"metastore/generations/{catalog_id}/consumed/complete.json", db_checksum
        )
        node("consumed", "table_name_consumption", catalog_id, state, ref)
        if generation and manifest:
            edge("consumed", "generation", state)
        receipt = consumed.get("catalog") or catalog
        consumption_state = state
        if receipt:
            if receipt.get("database") != "ddp_hms_" + catalog_id or not isinstance(
                receipt.get("tables"), list
            ):
                raise EvidenceError("HMS 清单身份不支持或冲突")
            for table in receipt["tables"]:
                identity(table, r"[a-z_]{1,64}")
            node(
                "hms",
                "hms_catalog",
                {"database": receipt.get("database"), "tables": receipt.get("tables")},
                state,
                ref,
            )
            edge("hms", "consumed", state)
        source_hash = receipt.get("source_sha256")
        if (
            report
            and wh.get("completion_sha256")
            and source_hash != wh["completion_sha256"]
        ):
            result["warnings"].append({"message": "报告与消费清单的数仓摘要冲突"})
            node(
                "report_conflict",
                "report_warehouse_reference",
                wh["completion_sha256"],
                "inconsistent",
            )
    else:
        source_hash = db_checksum if manifest else wh.get("completion_sha256")
    if (
        snapshot
        and version
        and (not report or wh.get("directory") or wh.get("completion_sha256"))
    ):
        identity(snapshot, r"[a-zA-Z0-9_-]{1,128}")
        identity(version, r"[a-zA-Z0-9_-]{1,128}")
        warehouse, state, ref = completion(
            f"warehouse/{snapshot}-{version}/complete.json",
            source_hash or wh.get("completion_sha256"),
        )
        if warehouse and (
            warehouse.get("snapshot_id") != snapshot
            or warehouse.get("processing_version") != version
        ):
            state = "inconsistent"
            result["warnings"].append({"message": "数仓身份冲突"})
        node(
            "warehouse",
            "warehouse_parquet",
            {"snapshot_id": snapshot, "processing_version": version},
            state,
            ref,
        )
        if catalog_id:
            edge(
                "warehouse",
                "hms" if any(n["id"] == "hms" for n in nodes) else "consumed",
                state,
            )
        elif manifest:
            edge("warehouse", "generation", state)
        if state == "matched":
            result["table_dependencies"] = dependencies(repo, warehouse)
        contract = warehouse.get("data_contract") or {}
        for key in ("contract_id", "version", "sha256"):
            if contract.get(key) is not None:
                identity(contract[key], r"[a-zA-Z0-9_-]{1,128}")
        result["data_contract"] = {
            k: contract.get(k) for k in ("contract_id", "version", "sha256")
        }
    if snapshot:
        identity(snapshot, r"[a-zA-Z0-9_-]{1,128}")
        ods, state, ref = completion(
            f"ingestion/snapshots/{snapshot}-nuscenes-ods-v1/complete.json",
            warehouse.get("input_sha256")
            or manifest.get("input_sha256")
            or ods_step.get("completion_sha256"),
        )
        if ods and ods.get("snapshot_id") != snapshot:
            state = "inconsistent"
            result["warnings"].append({"message": "ODS 身份冲突"})
        node("ods", "ods_snapshot", snapshot, state, ref)
        if any(n["id"] == "warehouse" for n in nodes):
            edge("ods", "warehouse", state)
        checksum = ods.get("artifacts", {}).get("file-manifest.json")
        if checksum:
            files, file_state, file_ref = completion(
                f"ingestion/snapshots/{snapshot}-nuscenes-ods-v1/file-manifest.json",
                checksum,
                list,
            )
            node(
                "source_files",
                "source_file_manifest",
                {
                    "sha256": checksum,
                    "file_count": len(files) if isinstance(files, list) else None,
                },
                file_state,
                file_ref,
            )
            edge("source_files", "ods", file_state)
        from driving_data_platform.common.preflight import TABLES

        result["source_tables"] = sorted(
            p[8:-6]
            for p in ods.get("artifacts", {})
            if p.startswith("records/") and p.endswith(".jsonl") and p[8:-6] in TABLES
        )
    if result["table_dependencies"]["status"] == "matched":
        evidence = next(n["evidence"] for n in nodes if n["id"] == "warehouse")
        entries = result["table_dependencies"]["items"]
        targets = {e["target"] for e in entries}
        sources = {
            name for e in entries for name in e["upstream"] if name not in targets
        }
        for source in sorted(sources):
            ods_state = next(n["status"] for n in nodes if n["id"] == "ods")
            node(
                "ods:" + source,
                "source_table",
                source,
                ods_state,
                next(n["evidence"] for n in nodes if n["id"] == "ods"),
            )
            edge("ods", "ods:" + source, ods_state)
        for entry in entries:
            target = entry["target"]
            node("warehouse:" + target, "warehouse_table", target, "matched", evidence)
            edge("warehouse", "warehouse:" + target)
            for upstream in entry["upstream"]:
                edge(
                    ("warehouse:" if upstream in targets else "ods:") + upstream,
                    "warehouse:" + target,
                )
            parent = "warehouse:" + target
            if catalog_id and any(n["id"] == "hms" for n in nodes):
                table = catalog.get("database") or next(
                    n["identity"]["database"] for n in nodes if n["id"] == "hms"
                )
                node(
                    "hms:" + target,
                    "hms_table",
                    table + "." + target,
                    consumption_state,
                    evidence,
                )
                edge(parent, "hms:" + target, consumption_state)
                parent = "hms:" + target
            if manifest:
                key = "serving:" + target
                node(
                    key,
                    "serving_table",
                    {
                        "table": entry["serving_table"],
                        "kind": entry["kind"],
                        "generation": generation,
                    },
                    consumption_state if catalog_id else "matched",
                    {"reference": "control.generation"},
                )
                edge(parent, key, consumption_state if catalog_id else "matched")
                edge(key, "generation", consumption_state if catalog_id else "matched")
    result["completeness"] = (
        "inconsistent"
        if any(n["status"] == "inconsistent" for n in nodes)
        else (
            "partial"
            if result["warnings"]
            or not generation
            or result["table_dependencies"]["status"] != "matched"
            or not nodes
            or any(n["status"] != "matched" for n in nodes)
            else "matched"
        )
    )
    return result
