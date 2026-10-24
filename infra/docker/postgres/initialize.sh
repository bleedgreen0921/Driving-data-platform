#!/usr/bin/env bash
set -euo pipefail
# psql variables quote injected values as SQL literals, never shell interpolation.
psql --username "$POSTGRES_USER" --dbname postgres --set=ON_ERROR_STOP=1 \
  --set=control_password="$DDP_CONTROL_PASSWORD" \
  --set=hive_password="$DDP_HIVE_PASSWORD" \
  --set=scheduler_password="$DDP_SCHEDULER_PASSWORD" <<'SQL'
CREATE ROLE ddp_control LOGIN PASSWORD :'control_password';
CREATE ROLE ddp_hive LOGIN PASSWORD :'hive_password';
CREATE ROLE ddp_scheduler LOGIN PASSWORD :'scheduler_password';
CREATE DATABASE ddp_control OWNER ddp_control;
CREATE DATABASE ddp_hive OWNER ddp_hive;
CREATE DATABASE ddp_scheduler OWNER ddp_scheduler;
REVOKE CONNECT, TEMPORARY ON DATABASE ddp_control FROM PUBLIC;
REVOKE CONNECT, TEMPORARY ON DATABASE ddp_hive FROM PUBLIC;
REVOKE CONNECT, TEMPORARY ON DATABASE ddp_scheduler FROM PUBLIC;
SQL
