ARG HIVE_IMAGE=apache/hive@sha256:b957bb0e698e560d24e162b7549d79f191d58703e92899a886618c317f6e8f30
ARG SPARK_IMAGE=spark@sha256:d283f4305f6b307a7b1effce4fce9f6866f4b536b168809ddbaadb6aea793694
FROM ${HIVE_IMAGE} AS hive
FROM ${SPARK_IMAGE}
USER root
COPY --from=hive /opt/hive/lib /opt/ddp/hive-client
