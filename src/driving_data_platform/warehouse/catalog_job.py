"""独立 Spark 进程通过 Hive 表名消费正式 Parquet，不读取业务 JSON 副本。"""

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from pyspark.sql import SparkSession
from pyspark.sql.types import StructType

from driving_data_platform.warehouse.catalog_contract import check_schema


def check_table(spark, request, name):
    qualified = request["database"] + "." + name
    details = {
        r.col_name: r.data_type
        for r in spark.sql("DESCRIBE TABLE EXTENDED " + qualified).collect()
    }
    expected = request["source"] + "/" + name + "/parquet"
    actual = urlsplit(details.get("Location", ""))
    properties = {
        r.key: r.value for r in spark.sql("SHOW TBLPROPERTIES " + qualified).collect()
    }
    if (
        actual.scheme != "file"
        or actual.path.rstrip("/") != expected
        or details.get("Provider", "").lower() != "parquet"
        or details.get("Type") != "EXTERNAL"
        or properties.get("ddp.source_sha256") != request["source_sha256"]
    ):
        raise ValueError("表位置、类型或版本标记不一致: " + name)
    frame = spark.table(qualified)
    required = check_schema(frame.schema.jsonValue(), request["tables"][name]["schema"])
    for field in required:
        if frame.where(frame[field].isNull()).limit(1).count():
            raise ValueError("非空字段出现 NULL: " + name + "." + field)
    return frame


def main():
    request_file = Path(sys.argv[1])
    request = json.loads(request_file.read_text())
    mode = sys.argv[2]
    if not re.fullmatch(
        r"ddp_hms_[0-9a-f]{32}", request["database"]
    ) or not re.fullmatch(r"/warehouse/[a-zA-Z0-9_-]+", request["source"]):
        raise ValueError("拒绝非试验数据库或非法数仓目录")
    if not all(re.fullmatch(r"[a-z_]+", name) for name in request["tables"]):
        raise ValueError("非法表名")
    spark = (
        SparkSession.builder.master("local[2]")
        .appName("ddp-parquet-catalog-" + mode)
        .config("spark.sql.shuffle.partitions", "2")
        .config("spark.sql.warehouse.dir", "file:/catalog")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.hive.metastore.version", "4.1.0")
        .config("spark.sql.hive.metastore.jars", "path")
        .config(
            "spark.sql.hive.metastore.jars.path",
            "/opt/ddp/hive-client/*.jar,/opt/spark/jars/hadoop-client-api-*.jar,/opt/spark/jars/hadoop-client-runtime-*.jar",
        )
        .config("spark.hadoop.hive.metastore.uris", request["uri"])
        .config("spark.hadoop.hive.metastore.client.connect.retry.delay", "1")
        .config("spark.hadoop.hive.metastore.connect.retries", "1")
        .config("spark.hadoop.hive.metastore.client.socket.timeout", "5")
        .config("spark.sql.files.ignoreMissingFiles", "false")
        .config("spark.sql.files.ignoreCorruptFiles", "false")
        .enableHiveSupport()
        .getOrCreate()
    )
    try:
        if (
            spark.sparkContext._jsc.hadoopConfiguration().get("hive.metastore.uris")
            != request["uri"]
        ):
            raise ValueError("远端元数据配置未生效")
        database = request["database"]
        if mode == "register":
            spark.sql(
                f"CREATE DATABASE IF NOT EXISTS {database} LOCATION 'file:/catalog/{database}'"
            )
            for index, (name, table) in enumerate(request["tables"].items()):
                ddl = StructType.fromJson(table["schema"]).toDDL()
                location = "file:" + request["source"] + "/" + name + "/parquet"
                spark.sql(
                    f"CREATE TABLE IF NOT EXISTS {database}.{name} ({ddl}) USING PARQUET LOCATION '{location}' TBLPROPERTIES ('ddp.source_sha256'='{request['source_sha256']}')"
                )
                check_table(spark, request, name)
                if "--fail-register" in sys.argv and index == 4:
                    raise ValueError("fault: partial-registration")
            receipt = {
                "database": database,
                "source_sha256": request["source_sha256"],
                "tables": sorted(request["tables"]),
            }
            temporary = request_file.parent / "registered.tmp"
            temporary.write_text(json.dumps(receipt, sort_keys=True))
            temporary.replace(request_file.parent / "registered.json")
        elif mode in {"consume", "check"}:
            actual = {t.name for t in spark.catalog.listTables(database)}
            if actual != set(request["tables"]):
                raise ValueError("目录表集合不完整或有额外表")
            counts = {}
            for name, table in request["tables"].items():
                frame = check_table(spark, request, name).cache()
                if mode == "consume":
                    frame.write.mode("error").json(
                        request["output"] + "/" + name + "/json"
                    )
                else:
                    # 强制读取所有列，不仅依赖可能由文件元信息回答的 count。
                    frame.toJSON().mapPartitions(
                        lambda records: [sum(1 for _ in records)]
                    ).collect()
                count = frame.count()
                if count != table["count"]:
                    raise ValueError("表行数不一致: " + name)
                counts[name] = {"count": count, "schema": frame.schema.jsonValue()}
                frame.unpersist()
            if mode == "consume":
                (Path(request["output"]) / "schema-counts.json").write_text(
                    json.dumps(counts, sort_keys=True)
                )
        elif mode == "drop":
            spark.sql("DROP TABLE " + database + ".dim_location")
        elif mode == "redirect":
            spark.sql(
                f"ALTER TABLE {database}.dim_location SET LOCATION 'file:/catalog/invalid' "
            )
        else:
            raise ValueError("不支持的元数据操作")
    finally:
        spark.stop()


if __name__ == "__main__":
    main()
