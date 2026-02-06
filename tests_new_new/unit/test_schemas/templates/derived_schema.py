import textwrap


LOGS_TEST_DERIVED_001_000_000 = {
    "derived_schema_name": "logs_test_derived",
    "derived_schema_version": "v001.000.000",
    "derived_schema_data": textwrap.dedent("""
        column,type,default,index_order,index_type,comment
        test_field_1,string,,,,
        test_field_2,string,,,,
        test_field_3,string,,,,
        test_field_4,string,,,,
        test_field_5,string,,,,
        test_field_6,string,,,,
        test_field_7,string,,,,
        test_field_8,string,,,,
        test_field_9,string,,,,
        test_field_10,string,,,,
    """),
    "meta_schema_name": "logs_test_meta"
}

LOGS_TEST_DERIVED_001_000_001 = {
    "derived_schema_name": "logs_test_derived",
    "derived_schema_version": "v001.000.001",
    "derived_schema_data": textwrap.dedent("""
        column,type,default,index_order,index_type,comment
        test_field_1,string,,,,
        test_field_2,string,,,,
        test_field_3,string,,,,
        test_field_4,string,,,,
        test_field_5,string,,,,
        test_field_6,string,,,,
        test_field_7,string,,,,
        test_field_8,string,,,,
        test_field_9,string,,,,
        test_field_10,string,,,,
    """),
    "meta_schema_name": "logs_test_meta"
}