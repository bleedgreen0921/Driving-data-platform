#!/bin/bash
set -euo pipefail
umask 077
DDP_HMS_DATABASE=${DDP_HMS_DATABASE:-ddp_metastore_v410}
if [[ ! "$DDP_HMS_DATABASE" =~ ^[a-z0-9_]+$ ]]; then echo '非法元数据库名称' >&2; exit 2; fi
DDP_HMS_HEAP_MIB=${DDP_HMS_HEAP_MIB:-768}
if [[ ! "$DDP_HMS_HEAP_MIB" =~ ^[0-9]+$ ]]; then echo '非法内存配置' >&2; exit 2; fi
export HIVE_CONF_DIR=/tmp/ddp-hms-conf
export HADOOP_CONF_DIR=$HIVE_CONF_DIR
mkdir -p "$HIVE_CONF_DIR"
cat > "$HIVE_CONF_DIR/hive-site.xml" <<CONFIG
<configuration>
<property><name>javax.jdo.option.ConnectionURL</name><value>jdbc:postgresql://postgres:5432/${DDP_HMS_DATABASE}</value></property>
<property><name>javax.jdo.option.ConnectionDriverName</name><value>org.postgresql.Driver</value></property>
<property><name>javax.jdo.option.ConnectionUserName</name><value>ddp_metastore</value></property>
<property><name>javax.jdo.option.ConnectionPassword</name><value>${DDP_HMS_PASSWORD:?required}</value></property>
<property><name>hive.metastore.schema.verification</name><value>true</value></property>
<property><name>datanucleus.schema.autoCreateAll</name><value>false</value></property>
<property><name>hive.metastore.warehouse.dir</name><value>file:/catalog</value></property>
<property><name>hive.metastore.event.db.notification.api.auth</name><value>false</value></property>
</configuration>
CONFIG
export HADOOP_HEAPSIZE=$DDP_HMS_HEAP_MIB
export HADOOP_CLIENT_OPTS="-Xmx${DDP_HMS_HEAP_MIB}m"
case "${1:-serve}" in
  init) exec /opt/hive/bin/schematool -dbType postgres -initSchema ;;
  validate) exec /opt/hive/bin/schematool -dbType postgres -validate ;;
  serve) exec /opt/hive/bin/hive --skiphadoopversion --skiphbasecp --service metastore ;;
  *) echo '不支持的元数据服务操作' >&2; exit 2 ;;
esac
