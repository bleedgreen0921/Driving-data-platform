ARG PG_BASE
FROM ${PG_BASE} AS builder
ARG SOURCE_SHA256
COPY pgvector.tar.gz /tmp/pgvector.tar.gz
RUN echo "${SOURCE_SHA256}  /tmp/pgvector.tar.gz" | sha256sum -c - \
 && apt-get update \
 && apt-get install -y --no-install-recommends build-essential postgresql-server-dev-16 \
 && mkdir /tmp/extension \
 && tar -xzf /tmp/pgvector.tar.gz --strip-components=1 -C /tmp/extension \
 && cd /tmp/extension \
 && make PG_CONFIG=/usr/lib/postgresql/16/bin/pg_config OPTFLAGS='' \
 && make PG_CONFIG=/usr/lib/postgresql/16/bin/pg_config DESTDIR=/opt/extension install
FROM ${PG_BASE}
COPY --from=builder /opt/extension/ /
