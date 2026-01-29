DFE_PACKAGE = {
    "schemas": {
        "build": {
            "no_cluster_declarations_needed": True,
            "use_replicated_merge_tree": False,
            "use_shared_merge_tree": False,
            "schemas": [
                {
                    "name": "logs_test_1",
                    "meta_schema": "logs_test",
                    "meta_schema_version": "v001.000.000",
                    "derived_schema_directory": "logs_test/logs_test_derived",
                    "derived_schema_version": "v001.000.000"
                },
                {
                    "name": "logs_test_2",
                    "meta_schema": "logs_test",
                    "meta_schema_version": "v001.000.000",
                    "derived_schema_directory": "logs_test/logs_test_derived",
                    "derived_schema_version": "v001.000.000"
                },
                {
                    "name": "logs_test_3",
                    "meta_schema": "logs_test",
                    "meta_schema_version": "v001.000.000",
                    "derived_schema_directory": "logs_test/logs_test_derived",
                    "derived_schema_version": "v001.000.000"
                },
                {
                    "name": "logs_test_4",
                    "meta_schema": "logs_test",
                    "meta_schema_version": "v001.000.000",
                    "derived_schema_directory": "logs_test/logs_test_derived",
                    "derived_schema_version": "v001.000.000"
                },
                {
                    "name": "logs_test_5",
                    "meta_schema": "logs_test",
                    "meta_schema_version": "v001.000.000",
                    "derived_schema_directory": "logs_test/logs_test_derived",
                    "derived_schema_version": "v001.000.000"
                },
                {
                    "name": "logs_test_6",
                    "meta_schema": "logs_test",
                    "meta_schema_version": "v001.000.000",
                    "derived_schema_directory": "logs_test/logs_test_derived",
                    "derived_schema_version": "v001.000.000"
                },
                {
                    "name": "logs_test_7",
                    "meta_schema": "logs_test",
                    "meta_schema_version": "v001.000.000",
                    "derived_schema_directory": "logs_test/logs_test_derived",
                    "derived_schema_version": "v001.000.000"
                },
                {
                    "name": "logs_test_8",
                    "meta_schema": "logs_test",
                    "meta_schema_version": "v001.000.000",
                    "derived_schema_directory": "logs_test/logs_test_derived",
                    "derived_schema_version": "v001.000.000"
                },
                {
                    "name": "logs_test_9",
                    "meta_schema": "logs_test",
                    "meta_schema_version": "v001.000.000",
                    "derived_schema_directory": "logs_test/logs_test_derived",
                    "derived_schema_version": "v001.000.000"
                },
                {
                    "name": "logs_test_10",
                    "meta_schema": "logs_test",
                    "meta_schema_version": "v001.000.000",
                    "derived_schema_directory": "logs_test/logs_test_derived",
                    "derived_schema_version": "v001.000.000"
                },
                {
                    "name": "logs_exclude_test_1",
                    "meta_schema": "logs_exclude_test",
                    "meta_schema_version": "v001.000.000",
                    "derived_schema_directory": "logs_exclude_test/logs_exclude_test_derived",
                    "derived_schema_version": "v001.000.000"
                }
            ]
        },
        "apply": {
            "do_add_roles": False,
            "do_add_columns": True
        }
    }
}