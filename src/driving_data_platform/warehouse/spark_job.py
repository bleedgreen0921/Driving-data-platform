"""正式 Spark SQL local 加工；仅写暂存，提交由宿主对账后执行。"""

import json
import sys
from pathlib import Path

from pyspark.sql import SparkSession

SCHEMAS = {
    "scene": "token STRING, log_token STRING, name STRING, description STRING",
    "log": "token STRING, location STRING",
    "sample": "token STRING, scene_token STRING, timestamp BIGINT",
    "sample_data": "token STRING, sample_token STRING, timestamp BIGINT, calibrated_sensor_token STRING, ego_pose_token STRING, filename STRING, is_key_frame BOOLEAN, width BIGINT, height BIGINT",
    "calibrated_sensor": "token STRING, sensor_token STRING",
    "sensor": "token STRING, channel STRING, modality STRING",
    "category": "token STRING, name STRING, description STRING",
    "instance": "token STRING, category_token STRING",
    "sample_annotation": "token STRING, sample_token STRING, instance_token STRING, translation ARRAY<DOUBLE>, size ARRAY<DOUBLE>, rotation ARRAY<DOUBLE>, num_lidar_pts BIGINT, num_radar_pts BIGINT, visibility_token STRING, attribute_tokens ARRAY<STRING>",
}
OUTPUTS = [
    ("scene", "dim_scene"),
    ("category", "dim_category"),
    ("sensor", "dim_sensor"),
    ("location", "dim_location"),
    ("frame", "fact_frame"),
    ("media", "fact_media"),
    ("annotation", "fact_annotation"),
    ("frame_category", "frame_category"),
    ("frame_channel", "frame_channel"),
    ("statistics", "statistics"),
    ("metrics", "metrics"),
]


def main():
    source, destination = map(Path, sys.argv[1:3])
    spark = (
        SparkSession.builder.master("local[2]")
        .appName("ddp-warehouse")
        .config("spark.sql.shuffle.partitions", "2")
        .config("spark.ui.enabled", "false")
        .getOrCreate()
    )
    try:
        for name, raw in SCHEMAS.items():
            schema = f"entity_id STRING, source_token STRING, timestamp_us BIGINT, raw_record STRUCT<{raw}>"
            spark.read.schema(schema).option("mode", "FAILFAST").json(
                str(source / "records" / f"{name}.jsonl")
            ).createOrReplaceTempView(name)
        manifest = {}
        for query, name in OUTPUTS:
            frame = spark.sql((Path("/app/sql") / f"{query}.sql").read_text()).cache()
            frame.createOrReplaceTempView(name)
            frame.write.mode("error").parquet(str(destination / name / "parquet"))
            frame.write.mode("error").json(str(destination / name / "json"))
            persisted = spark.read.parquet(str(destination / name / "parquet"))
            if (
                persisted.exceptAll(frame).limit(1).count()
                or frame.exceptAll(persisted).limit(1).count()
            ):
                raise ValueError("Parquet 回读对账失败: " + name)
            manifest[name] = {
                "count": frame.count(),
                "schema": frame.schema.jsonValue(),
            }
        (destination / "schema-counts.json").write_text(
            json.dumps(manifest, sort_keys=True)
        )
    finally:
        spark.stop()


if __name__ == "__main__":
    main()
