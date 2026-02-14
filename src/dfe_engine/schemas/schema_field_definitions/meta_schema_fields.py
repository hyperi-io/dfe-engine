META_SCHEMA_FIELDS = [
    {
        "name": "column",
        "is_key": True,
        "required": True,
        "type": "string"
    },
    {
        "name": "type",
        "required": True,
        "type": "string"
    },
    {
        "name": "default",
        "required": False,
        "type": "string"
    },
    {
        "name": "index_order",
        "required": False,
        "type": "int32"
    },
    {
        "name": "index_type",
        "required": False,
        "type": "string"
    },
    {
        "name": "ddl_comment",
        "required": False,
        "type": "string"
    }
]