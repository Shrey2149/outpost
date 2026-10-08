#!/bin/sh
# Local stack: Postgres (engine schema) + Operaton with authorization and REST auth on.
set -e
docker network inspect matrixnet >/dev/null 2>&1 || docker network create matrixnet
docker start matrix-db 2>/dev/null || docker run -d --name matrix-db --network matrixnet \
  -e POSTGRES_DB=operaton -e POSTGRES_USER=operaton -e POSTGRES_PASSWORD=operaton \
  -v matrix-db-data:/var/lib/postgresql/data -p 5433:5432 postgres:17
docker start matrix-operaton 2>/dev/null || docker run -d --name matrix-operaton --network matrixnet -p 8080:8080 \
  -e DB_DRIVER=org.postgresql.Driver -e DB_URL=jdbc:postgresql://matrix-db:5432/operaton \
  -e DB_USERNAME=operaton -e DB_PASSWORD=operaton -e WAIT_FOR=matrix-db:5432 \
  -e OPERATON_BPM_AUTHORIZATION_ENABLED=true -e OPERATON_BPM_RUN_AUTH_ENABLED=true \
  -e OPERATON_BPM_RUN_EXAMPLE_ENABLED=false \
  operaton/operaton:latest
until curl -sf -u demo:demo http://localhost:8080/engine-rest/engine >/dev/null; do sleep 3; done
echo "Operaton up: http://localhost:8080"
