"""Unit tests for ParameterValidator."""

from datetime import datetime
from uuid import UUID

import pytest

from dfe_engine.query.models import (
    AuthContext,
    ParameterDefinition,
    ParameterType,
    QueryDefinition,
    QueryDefaults,
    QueryLimits,
    QueryOptions,
)
from dfe_engine.query.validator import (
    AuthorizationError,
    ParameterValidationError,
    ParameterValidator,
    validate_params,
)


class TestParameterValidatorInit:
    """Test ParameterValidator initialization."""

    def test_default_values(self):
        """Test default validator values."""
        validator = ParameterValidator()

        assert validator.default_limit == 1000
        assert validator.max_limit == 100_000
        assert validator.default_timeout == 30
        assert validator.max_timeout == 300
        assert validator.max_time_range_days == 365

    def test_custom_values(self):
        """Test custom validator values."""
        validator = ParameterValidator(
            default_limit=500,
            max_limit=10_000,
            default_timeout=60,
            max_timeout=120,
        )

        assert validator.default_limit == 500
        assert validator.max_limit == 10_000


class TestAuthorizationCheck:
    """Test authorization checking."""

    @pytest.fixture
    def validator(self):
        return ParameterValidator()

    @pytest.fixture
    def basic_auth(self):
        return AuthContext(
            org_id="acme-corp",
            user_id="user-123",
            roles=["analyst"],
            permissions=["read:events"],
        )

    @pytest.fixture
    def admin_auth(self):
        return AuthContext(
            org_id="acme-corp",
            user_id="admin-123",
            roles=["admin"],
            permissions=["read:events", "write:events"],
        )

    def test_no_roles_required(self, validator, basic_auth):
        """Test query with no role requirements."""
        query_def = QueryDefinition(
            datasource="clickhouse:default",
            store="events",
            sql="SELECT 1 WHERE org_id = {{ _org_id }}",
            tenant_isolated=True,
        )

        # Should not raise
        validator._check_authorization(query_def, basic_auth)

    def test_role_check_pass(self, validator, basic_auth):
        """Test role check passes."""
        query_def = QueryDefinition(
            datasource="clickhouse:default",
            store="events",
            sql="SELECT 1 WHERE org_id = {{ _org_id }}",
            tenant_isolated=True,
            required_roles=["analyst", "admin"],
        )

        # Should not raise
        validator._check_authorization(query_def, basic_auth)

    def test_role_check_fail(self, validator, basic_auth):
        """Test role check fails."""
        query_def = QueryDefinition(
            datasource="clickhouse:default",
            store="events",
            sql="SELECT 1 WHERE org_id = {{ _org_id }}",
            tenant_isolated=True,
            required_roles=["admin"],
        )

        with pytest.raises(AuthorizationError, match="Requires one of roles"):
            validator._check_authorization(query_def, basic_auth)

    def test_permission_check_pass(self, validator, basic_auth):
        """Test permission check passes."""
        query_def = QueryDefinition(
            datasource="clickhouse:default",
            store="events",
            sql="SELECT 1 WHERE org_id = {{ _org_id }}",
            tenant_isolated=True,
            required_permissions=["read:events"],
        )

        # Should not raise
        validator._check_authorization(query_def, basic_auth)

    def test_permission_check_fail(self, validator, basic_auth):
        """Test permission check fails."""
        query_def = QueryDefinition(
            datasource="clickhouse:default",
            store="events",
            sql="SELECT 1 WHERE org_id = {{ _org_id }}",
            tenant_isolated=True,
            required_permissions=["write:events"],
        )

        with pytest.raises(AuthorizationError, match="Requires one of permissions"):
            validator._check_authorization(query_def, basic_auth)

    def test_non_isolated_requires_admin(self, validator, basic_auth, admin_auth):
        """Test non-tenant-isolated queries require admin."""
        query_def = QueryDefinition(
            datasource="clickhouse:default",
            store="system",
            sql="SELECT 1",
            tenant_isolated=False,
        )

        with pytest.raises(AuthorizationError, match="require admin role"):
            validator._check_authorization(query_def, basic_auth)

        # Admin should pass
        validator._check_authorization(query_def, admin_auth)


class TestParameterTypeCoercion:
    """Test parameter type coercion."""

    @pytest.fixture
    def validator(self):
        return ParameterValidator()

    def test_coerce_string(self, validator):
        """Test string coercion."""
        assert validator._coerce_type(123, ParameterType.STRING) == "123"
        assert validator._coerce_type("hello", ParameterType.STRING) == "hello"

    def test_coerce_integer(self, validator):
        """Test integer coercion."""
        assert validator._coerce_type("42", ParameterType.INTEGER) == 42
        assert validator._coerce_type(42.9, ParameterType.INTEGER) == 42

    def test_coerce_float(self, validator):
        """Test float coercion."""
        assert validator._coerce_type("3.14", ParameterType.FLOAT) == 3.14
        assert validator._coerce_type(42, ParameterType.FLOAT) == 42.0

    def test_coerce_boolean(self, validator):
        """Test boolean coercion."""
        assert validator._coerce_type(True, ParameterType.BOOLEAN) is True
        assert validator._coerce_type("true", ParameterType.BOOLEAN) is True
        assert validator._coerce_type("false", ParameterType.BOOLEAN) is False
        assert validator._coerce_type("1", ParameterType.BOOLEAN) is True
        assert validator._coerce_type("0", ParameterType.BOOLEAN) is False

    def test_coerce_boolean_invalid(self, validator):
        """Test invalid boolean coercion."""
        with pytest.raises(ValueError, match="Cannot convert"):
            validator._coerce_type("maybe", ParameterType.BOOLEAN)

    def test_coerce_datetime(self, validator):
        """Test datetime coercion."""
        result = validator._coerce_type(
            "2024-01-15T10:30:00Z", ParameterType.DATETIME
        )
        assert isinstance(result, datetime)
        assert result.year == 2024
        assert result.month == 1
        assert result.day == 15

    def test_coerce_datetime_existing(self, validator):
        """Test datetime passthrough."""
        dt = datetime(2024, 1, 15, 10, 30)
        result = validator._coerce_type(dt, ParameterType.DATETIME)
        assert result == dt

    def test_coerce_uuid(self, validator):
        """Test UUID coercion."""
        uuid_str = "550e8400-e29b-41d4-a716-446655440000"
        result = validator._coerce_type(uuid_str, ParameterType.UUID)
        assert isinstance(result, UUID)
        assert str(result) == uuid_str

    def test_coerce_array(self, validator):
        """Test array coercion."""
        result = validator._coerce_type(
            ["1", "2", "3"],
            ParameterType.ARRAY,
            ParameterType.INTEGER,
        )
        assert result == [1, 2, 3]

    def test_coerce_array_invalid(self, validator):
        """Test invalid array coercion."""
        with pytest.raises(ValueError, match="Expected array"):
            validator._coerce_type("not-an-array", ParameterType.ARRAY)


class TestParameterConstraintValidation:
    """Test parameter constraint validation."""

    @pytest.fixture
    def validator(self):
        return ParameterValidator()

    def test_min_constraint(self, validator):
        """Test minimum value constraint."""
        param_def = ParameterDefinition(type=ParameterType.INTEGER, min=0)

        # Should not raise
        validator._validate_constraints("count", 5, param_def)

        with pytest.raises(ParameterValidationError, match="must be >= 0"):
            validator._validate_constraints("count", -1, param_def)

    def test_max_constraint(self, validator):
        """Test maximum value constraint."""
        param_def = ParameterDefinition(type=ParameterType.INTEGER, max=100)

        # Should not raise
        validator._validate_constraints("count", 50, param_def)

        with pytest.raises(ParameterValidationError, match="must be <= 100"):
            validator._validate_constraints("count", 150, param_def)

    def test_max_length_constraint(self, validator):
        """Test string max length constraint."""
        param_def = ParameterDefinition(type=ParameterType.STRING, max_length=10)

        # Should not raise
        validator._validate_constraints("name", "short", param_def)

        with pytest.raises(ParameterValidationError, match="exceeds max length"):
            validator._validate_constraints("name", "this is too long", param_def)

    def test_pattern_constraint(self, validator):
        """Test string pattern constraint."""
        param_def = ParameterDefinition(
            type=ParameterType.STRING,
            pattern=r"^[a-z]+$",
        )

        # Should not raise
        validator._validate_constraints("code", "abc", param_def)

        with pytest.raises(ParameterValidationError, match="does not match pattern"):
            validator._validate_constraints("code", "ABC123", param_def)

    def test_enum_constraint(self, validator):
        """Test enum value constraint."""
        param_def = ParameterDefinition(
            type=ParameterType.STRING,
            enum=["low", "medium", "high"],
        )

        # Should not raise
        validator._validate_constraints("severity", "high", param_def)

        with pytest.raises(ParameterValidationError, match="must be one of"):
            validator._validate_constraints("severity", "critical", param_def)

    def test_max_items_constraint(self, validator):
        """Test array max items constraint."""
        param_def = ParameterDefinition(
            type=ParameterType.ARRAY,
            max_items=3,
        )

        # Should not raise
        validator._validate_constraints("tags", ["a", "b"], param_def)

        with pytest.raises(ParameterValidationError, match="exceeds max items"):
            validator._validate_constraints("tags", ["a", "b", "c", "d"], param_def)


class TestValidateAndBuild:
    """Test the full validate_and_build workflow."""

    @pytest.fixture
    def validator(self):
        return ParameterValidator()

    @pytest.fixture
    def auth(self):
        return AuthContext(
            org_id="acme-corp",
            user_id="user-123",
            roles=["analyst"],
            permissions=["read:events"],
        )

    @pytest.fixture
    def query_def(self):
        return QueryDefinition(
            datasource="clickhouse:default",
            store="events",
            sql="SELECT 1 WHERE org_id = {{ _org_id }}",
            tenant_isolated=True,
            parameters={
                "event_type": ParameterDefinition(
                    type=ParameterType.STRING,
                    required=False,
                    default="all",
                ),
                "limit_override": ParameterDefinition(
                    type=ParameterType.INTEGER,
                    required=False,
                    min=1,
                    max=1000,
                ),
            },
        )

    def test_basic_validation(self, validator, query_def, auth):
        """Test basic parameter validation."""
        result = validator.validate_and_build(
            query_def,
            {"event_type": "login"},
            QueryOptions(limit=100),
            auth,
        )

        # Auth context injected
        assert result["_org_id"] == "acme-corp"
        assert result["_user_id"] == "user-123"
        assert result["_roles"] == ["analyst"]

        # Client param validated
        assert result["event_type"] == "login"

        # Standard params
        assert result["limit"] == 100
        assert result["offset"] == 0

    def test_default_values_applied(self, validator, query_def, auth):
        """Test default values are applied."""
        result = validator.validate_and_build(
            query_def,
            {},  # No params provided
            QueryOptions(),
            auth,
        )

        assert result["event_type"] == "all"

    def test_required_param_missing(self, validator, auth):
        """Test error on missing required parameter."""
        query_def = QueryDefinition(
            datasource="clickhouse:default",
            store="events",
            sql="SELECT 1 WHERE org_id = {{ _org_id }}",
            tenant_isolated=True,
            parameters={
                "severity": ParameterDefinition(
                    type=ParameterType.STRING,
                    required=True,
                ),
            },
        )

        with pytest.raises(ParameterValidationError, match="Missing required"):
            validator.validate_and_build(query_def, {}, QueryOptions(), auth)

    def test_unknown_param_rejected(self, validator, query_def, auth):
        """Test error on unknown parameter."""
        with pytest.raises(ParameterValidationError, match="Unknown parameter"):
            validator.validate_and_build(
                query_def,
                {"unknown_param": "value"},
                QueryOptions(),
                auth,
            )

    def test_limit_clamped_to_query_max(self, validator, auth):
        """Test limit is clamped to query-specific maximum."""
        # Query with lower max than global
        query_def = QueryDefinition(
            datasource="clickhouse:default",
            store="events",
            sql="SELECT 1 WHERE org_id = {{ _org_id }}",
            tenant_isolated=True,
            limits=QueryLimits(max_limit=500),
        )
        result = validator.validate_and_build(
            query_def,
            {},
            QueryOptions(limit=1000),  # Above query max but within global
            auth,
        )

        assert result["limit"] == 500  # Clamped to query max_limit

    def test_timeout_clamped_to_query_max(self, validator, auth):
        """Test timeout is clamped to query-specific maximum."""
        # Query with lower max than global
        query_def = QueryDefinition(
            datasource="clickhouse:default",
            store="events",
            sql="SELECT 1 WHERE org_id = {{ _org_id }}",
            tenant_isolated=True,
            limits=QueryLimits(max_timeout=60),
        )
        result = validator.validate_and_build(
            query_def,
            {},
            QueryOptions(timeout_seconds=120),  # Above query max but within global
            auth,
        )

        assert result["timeout_seconds"] == 60  # Clamped to query max_timeout

    def test_query_specific_defaults(self, validator, auth):
        """Test query-specific default overrides."""
        query_def = QueryDefinition(
            datasource="clickhouse:default",
            store="events",
            sql="SELECT 1 WHERE org_id = {{ _org_id }}",
            tenant_isolated=True,
            defaults=QueryDefaults(limit=50, timeout_seconds=10),
        )

        result = validator.validate_and_build(query_def, {}, QueryOptions(), auth)

        assert result["limit"] == 50
        assert result["timeout_seconds"] == 10

    def test_query_specific_limits(self, validator, auth):
        """Test query-specific limit overrides."""
        query_def = QueryDefinition(
            datasource="clickhouse:default",
            store="events",
            sql="SELECT 1 WHERE org_id = {{ _org_id }}",
            tenant_isolated=True,
            limits=QueryLimits(max_limit=500, max_timeout=60),
        )

        result = validator.validate_and_build(
            query_def,
            {},
            QueryOptions(limit=1000, timeout_seconds=120),
            auth,
        )

        # Clamped to query-specific limits
        assert result["limit"] == 500
        assert result["timeout_seconds"] == 60


class TestTimeBoundsValidation:
    """Test time bounds validation."""

    @pytest.fixture
    def validator(self):
        return ParameterValidator()

    @pytest.fixture
    def auth(self):
        return AuthContext(
            org_id="acme-corp",
            user_id="user-123",
            roles=["analyst"],
        )

    @pytest.fixture
    def time_bounded_query(self):
        return QueryDefinition(
            datasource="clickhouse:default",
            store="events",
            sql="SELECT 1 WHERE org_id = {{ _org_id }}",
            tenant_isolated=True,
            time_column="created_at",
        )

    def test_time_bounds_parsed(self, validator, time_bounded_query, auth):
        """Test time bounds are parsed correctly."""
        result = validator.validate_and_build(
            time_bounded_query,
            {},
            QueryOptions(
                time_from="2024-01-01T00:00:00Z",
                time_to="2024-01-15T00:00:00Z",
            ),
            auth,
        )

        assert isinstance(result["time_from"], datetime)
        assert isinstance(result["time_to"], datetime)
        assert result["time_from"].year == 2024
        assert result["time_from"].month == 1

    def test_time_to_defaults_to_now(self, validator, time_bounded_query, auth):
        """Test time_to defaults to current time."""
        from datetime import timedelta, UTC

        # Use a time_from within the allowed range (now - 30 days)
        recent_time = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        result = validator.validate_and_build(
            time_bounded_query,
            {},
            QueryOptions(time_from=recent_time),
            auth,
        )

        assert "time_to" in result
        assert isinstance(result["time_to"], datetime)

    def test_time_from_required(self, validator, auth):
        """Test time_from required when specified."""
        query_def = QueryDefinition(
            datasource="clickhouse:default",
            store="events",
            sql="SELECT 1 WHERE org_id = {{ _org_id }}",
            tenant_isolated=True,
            time_column="created_at",
            time_required=True,
        )

        with pytest.raises(ParameterValidationError, match="requires time_from"):
            validator.validate_and_build(query_def, {}, QueryOptions(), auth)

    def test_time_range_exceeded(self, validator, time_bounded_query, auth):
        """Test error on exceeded time range."""
        with pytest.raises(ParameterValidationError, match="exceeds maximum"):
            validator.validate_and_build(
                time_bounded_query,
                {},
                QueryOptions(
                    time_from="2020-01-01T00:00:00Z",
                    time_to="2024-01-01T00:00:00Z",  # 4 years
                ),
                auth,
            )

    def test_time_from_after_time_to(self, validator, time_bounded_query, auth):
        """Test error when time_from is after time_to."""
        with pytest.raises(ParameterValidationError, match="must be before"):
            validator.validate_and_build(
                time_bounded_query,
                {},
                QueryOptions(
                    time_from="2024-01-15T00:00:00Z",
                    time_to="2024-01-01T00:00:00Z",
                ),
                auth,
            )


class TestModuleLevelFunction:
    """Test module-level convenience function."""

    def test_validate_params_function(self):
        """Test validate_params convenience function."""
        query_def = QueryDefinition(
            datasource="clickhouse:default",
            store="events",
            sql="SELECT 1 WHERE org_id = {{ _org_id }}",
            tenant_isolated=True,
        )
        auth = AuthContext(
            org_id="acme-corp",
            user_id="user-123",
            roles=["analyst"],
        )

        result = validate_params(query_def, {}, QueryOptions(), auth)

        assert result["_org_id"] == "acme-corp"
        assert "limit" in result
