# Aggs on tenant.name -> field_A


def cte_template_1():
    return """{main_table_name} AS (
        SELECT
            {agg1_all_fields}
        FROM
            {org_id}.{source_table}
        WHERE
            {where_clause}
        GROUP BY
            {agg1_field}
    )
    """


# Aggs on tenant.name -> field_A -> field_B


def cte_template_2():
    return """{main_table_name} AS (
        WITH {agg1_name} AS (
            SELECT
                {agg1_all_fields}
            FROM
                {org_id}.{source_table}
            WHERE
                {where_clause}
            GROUP BY
                {agg1_field}
        )
        SELECT
            {agg2_all_fields}
        FROM
            {agg1_name}
        GROUP BY
            {agg2_field}
    )
    """


# Aggs on tenant.name -> field_A OR field_B


def cte_template_3():
    return """{main_table_name} AS (
        SELECT
            {agg1_all_fields}
        FROM
            {org_id}.{source_table}
        WHERE
            {where_clause}
        GROUP BY
            {agg1_fields}
    )
    """


# Aggs on tenant.name -> (single) field_A ->  (cardinality) field_B


def cte_template_4():
    return """{main_table_name} AS (
        SELECT
            {agg1_all_fields}
        FROM
            {org_id}.{source_table}
        WHERE
            {where_clause}
        GROUP BY
            {agg1_field}
        HAVING
            {condition}
    )
    """


# Aggs on tenant.name -> [(single) field_A -> [(cardinality) field_Y OR (cardinality) field_Z] OR (single) field_B -> [(cardinality) field_Y OR (cardinality) field_Z]]


def cte_template_5():
    return """{main_table_name} AS (
        WITH {agg1_name} AS (
            SELECT
                {agg1_all_fields}
            FROM
                {org_id}.{source_table}
            WHERE
                {where_clause}
            GROUP BY
                {agg1_field}
        )
        SELECT
            {agg2_all_fields}
        FROM
            {agg1_name}
        GROUP BY
            {agg2_fields}
        HAVING
            {having_condition}
    )
    """
