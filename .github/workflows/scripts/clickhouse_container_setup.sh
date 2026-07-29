#!/usr/bin/env bash

# Start ClickHouse service. Same version the integration conftest and dfe-infra
# use -- CI testing a different release from the deployed one is how a query
# passes here and meets a changed default in production.
# renovate: datasource=docker depName=clickhouse/clickhouse-server
docker run --rm -d --name clickhouse -e CLICKHOUSE_SKIP_USER_SETUP=1 -p 9000:9000/tcp clickhouse/clickhouse-server:26.3

# Wait for ClickHouse to be ready
echo "Waiting for ClickHouse to be ready..."
timeout 60 bash -c 'until echo "SELECT 1" | docker exec -i clickhouse clickhouse-client; do sleep 2; done'
echo "ClickHouse is ready!"

# Setup localhost configuration
mkdir -p ~/.dfe
cat <<EOF > ~/.dfe/dfe_targets.yaml
default_target: integration
targets:
    integration:
        ch_host: localhost
        ch_port: 9000
        ch_username: default
        ch_password: 
        ip_config_bucket_name: xdr-test-config-bucket
        ip_config_bucket_region: ap-southeast-2
        ip_config_standard_enrichment_path: standard_enrichment_files
        ip_config_receiver_path: standard_enrichment_files
        ip_config_geo_ip_path: geoip
        ip_templates_path: vector_templates
        hunt_config_path: tests/resources/workshop_setup/test_hunts
        hunt_rules_path: tests/resources/workshop_setup/test_rules/executables
        helm_template: resources/pipeline_template/config_only.yaml
        vector_config_mount_path: /etc/vector
EOF
echo "Created ~/.dfe/dfe_targets.yaml with localhost ClickHouse"