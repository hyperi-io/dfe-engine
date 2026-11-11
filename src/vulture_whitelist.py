#  Project:      dfe-engine
#  File:         src/vulture_whitelist.py
#  Purpose:      The uses vulture cannot see, so its dead-code pass names only leftovers
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Names reached by something vulture cannot follow, each group under the reason it is reached.

hyperi-ci runs ``vulture src/``, so this file is scanned with the package and never imported.
Vulture matches the bare name, so an entry hides that name everywhere.
"""


class _Names:
    def __getattr__(self, name: str) -> None: ...


_ = _Names()

# Rust app config models: rendered to the YAML each app reads.
_.accepted_headers, _.access_key_id, _.account_key, _.account_name, _.acknowledgements, _.acks
_.array_to_json, _.auto_offset_reset, _.aws_access_key_id, _.aws_profile, _.aws_secret_access_key
_.aws_session_token, _.batch_messages, _.batch_size, _.batch_timeout_ms, _.bearer_token
_.ca_cert_file, _.ca_file, _.ca_secret, _.cache_ttl_secs, _.category_to_topic, _.client_auth
_.coercion, _.collapse_underscores, _.collector_timestamp_path, _.collision_strategy
_.commit_interval_ms, _.compression, _.correct_known_bad, _.debounce_ms, _.default_db
_.default_org_id, _.default_segment, _.default_table, _.default_timezone, _.default_topic
_.disable_json_tables, _.disable_raw_tables, _.dlq, _.dlq_on_invalid, _.drop_tags
_.external_topics, _.extra_env, _.extra_yaml_files, _.extract_timestamp_collector
_.field_sanitization, _.file_extension, _.flush_age_secs, _.flush_records, _.from_field
_.grpc_endpoint, _.handle_numeric_prefix, _.header_name, _.header_values, _.ingest_bindings
_.inject_timestamp_load, _.input_topic, _.invalid_action, _.json_output, _.key_field
_.librdkafka_options, _.limit_bytes, _.linger_ms, _.max_body_size, _.max_buffer_bytes
_.max_future_seconds, _.max_hold_ms, _.max_message_size, _.max_past_seconds
_.max_poll_interval_ms, _.memory, _.message_timeout_ms, _.null_handling, _.null_strings
_.numeric_prefix, _.oauth_client_secret, _.oauth_extensions, _.oauth_scope, _.org_id_field
_.org_routes, _.otlp, _.output_topic, _.path_template, _.PATH_TEMPLATE_PLACEHOLDERS
_.poll_interval_secs, _.pressure_threshold, _.project_id, _.prometheus_rw, _.raw_output
_.raw_source_fields, _.recv_buffer_size, _.recv_timeout_ms, _.refresh_interval_secs
_.refresh_on_error, _.reload_interval_secs, _.remove_routing_fields, _.request_timeout_ms
_.require_json, _.required_fields, _.retries, _.roll_interval_secs, _.roll_size_bytes
_.schema_cache
_.secret_access_key, _.service_account_key, _.session_timeout_ms, _.skip_verify, _.splunk_hec
_.strip_at_prefix, _.table_capture_modes, _.table_fields, _.tags_fields, _.tags_output
_.timeout_ms, _.timestamp_dq, _.timezone_fields, _.topic_fields, _.topic_regex, _.topic_suffixes
_.tracking_enabled, _.trim_underscores, _.type_mappings, _.unknown_type_fallback, _.use_ssl
_.validation, _.writer_parallelism

# API request and response bodies: pydantic fills or emits these fields for clients.
_.action_writable, _.api_cors_origins, _.api_host, _.api_port, _.api_token, _.applies_routing
_.apps_sync_error, _.apps_synced, _.as_of, _.auth_enabled, _.auth_local_enabled
_.authorization_url, _.availability, _.availability_reason, _.average, _.awaiting_review
_.bucket_epoch, _.clickhouse_data_database, _.clickhouse_database, _.clickhouse_default_ttl_days
_.clickhouse_host, _.config_schema, _.consumer_group, _.copy_directives, _.core_tables_altered
_.create_table, _.deploy_target, _.desired_partitions, _.desired_replication_factor
_.directory_setting, _.editable, _.expires_in, _.explain_queries, _.field_count, _.groups_skipped
_.has_schema, _.header_fields, _.highest, _.highest_7d, _.highest_7d_label, _.hunt_count
_.json_column, _.json_derived_count, _.landed_in_default, _.mapping_count, _.member_count
_.min_interval_seconds, _.orphaned_groups, _.peak_memory, _.pending_reconcile, _.per_source_apps
_.preferences, _.principal, _.principals, _.processes, _.provider_idle_scaling
_.provider_max_replica_memory_gb, _.provider_min_replica_memory_gb, _.provider_replicas
_.python_version, _.queued, _.read_rows, _.relinked, _.requested_fire, _.rule_errors, _.runnable
_.runnable_intakes, _.runnable_reason, _.sampled, _.service_account_json, _.services_config_dir
_.source_build, _.source_deployment, _.source_tables_altered, _.source_url, _.storage_model
_.target_table_name, _.upstream_modified, _.url_scheme, _.validation, _.wasm_bytes
_.argo_namespace_prefix, _.notes, _.wasm_memory_bytes, _.wildcard

# Stored documents, results and dataclass fields: pydantic or asdict reads them.
_.actual_rows, _.actual_time_ms, _.admin_username, _.apps_sync_error, _.apps_synced, _.average
_.break_glass, _.bucket_epoch, _.ch_password_env, _.completed_steps, _.confidence, _.connection_id
_.create_table, _.current_step, _.current_table_topic_type, _.desired_partitions
_.desired_replication_factor, _.estimated_cost, _.estimated_improvement, _.estimated_rows
_.existing_schema, _.explain_duration_ms, _.field_mappings, _.has_transform, _.hinted_columns
_.hyperdx_source_error, _.hyperdx_source_teams, _.hyperdx_team_api_key_env, _.hyperdx_team_id
_.inherits, _.initial_setup, _.last_completed_at, _.last_login_at, _.match_ratio
_.matched_searchable, _.module_name, _.next_cursor, _.number, _.oidc_providers, _.parser_config
_.pending_steps, _.placeholder, _.poll_interval_seconds, _.previous_deployed_versions, _.principal
_.projected_per_day, _.proposed_columns, _.proposed_indices, _.proposed_query, _.proposed_schema
_.rationale, _.requested_limit, _.rows_in_window, _.secret_ref, _.step_details, _.topics_ensured
_.total_count, _.total_estimated_cost, _.total_estimated_rows, _.trace_id

# Resilience settings reach scalo's ResilienceConfig through model_dump.
_.waking_budget_seconds

# Enum members reached by a value lookup.
_.DEPRECATED, _.GIT_TOKEN, _.HYPERSEC, _.PAUSED, _.RANDOM, _.SOURCE, _.STOPPED, _.USAGE

# pySigma's TextQueryBackend reads these class attributes and calls these methods by name.
_.backend_processing_pipeline, _.cidr_expression, _.convert_condition_field_eq_val_str
_.correlation_methods, _.escape_and_quote_field, _.finalize_query_default

# Reached by Python or a library through a protocol or hook.
_.__getattr__, _._rendering_index, _._shape_only, _.context_settings, _.default_flow_style
_.external_topics, _.preserve_quotes, _.show

# Declared by a base class for its subclasses to implement or fill.
_.healthcheck, _.Output

# The dfe-schemas validation script (scripts/validate_schemas.py) calls these.
_.from_file, _.load_table_config, _.load_table_spec

# Tests reset these process-wide singletons and caches between cases.
_.reload_contracts, _.reset_instance, _.reset_settings

# Alerting code with no caller outside its tests (hunts/alert_grouping.py, alerting/dispatcher.py).
_.AlertGroupingConfig, _.AlertStateManager, _.build_group_key, _.build_grouping_query
_.check_cooldown, _.cooldown_td, _.dispatch, _.ensure_table_exists, _.get_ddl, _.has_group_by
_.max_alerts_per_run, _.record_fire

# Documented library API (docs/data-plane/schema-classes.md, query-api-python.md).
_.clone_version, _.get_order_by_columns, _.reference_root, _.to_csv, _.to_pandas
_.to_pydict, _.to_pylist

# Helpers and constants currently used only by tests.
_._resolve_roles_from_groups, _.add_verify_key_pem, _.candidate_fields
_.execute_ddl_statements, _.extract_metadata, _.FORK_ACCEPTS
_.get_draft, _.get_published, _.get_version, _.list_adapters, _.load_clickhouse_settings
_.load_plan, _.protocol_paths, _.query_names, _.read_payload, _.save_draft, _.save_plan
_.set_deployed

# Registry entries the schema vocabulary route reads through pydantic from_attributes.
_.retired_names, _.retired_attributes, _.ch_overrides
