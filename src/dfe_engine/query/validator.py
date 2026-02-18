"""
Parameter Validator - Validates and coerces query parameters.

Validates client-provided parameters against query definition schemas,
applies defaults, enforces limits, and merges with auth context.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from typing import Any

from hyperi_pylib.logger import logger

from dfe_engine.query.models import (
    AuthContext,
    ParameterDefinition,
    ParameterType,
    QueryDefinition,
    QueryOptions,
)

# Global defaults (can be overridden by settings)
DEFAULT_LIMIT = 1000
MAX_LIMIT = 100_000
DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 300
MAX_TIME_RANGE_DAYS = 365


class ParameterValidationError(Exception):
    """Parameter validation failed."""

    def __init__(self, message: str, param: str | None = None):
        super().__init__(message)
        self.param = param


class AuthorizationError(Exception):
    """User not authorized for this query."""

    pass


class ParameterValidator:
    """
    Validates and transforms query parameters.

    Flow:
    1. Check authorization (roles/permissions)
    2. Validate client parameters against schema
    3. Apply defaults for missing optional params
    4. Enforce limits (max values)
    5. Inject auth context (_org_id, _user_id, etc.)
    6. Return merged parameter dict for SQL rendering
    """

    def __init__(
        self,
        default_limit: int = DEFAULT_LIMIT,
        max_limit: int = MAX_LIMIT,
        default_timeout: int = DEFAULT_TIMEOUT,
        max_timeout: int = MAX_TIMEOUT,
        max_time_range_days: int = MAX_TIME_RANGE_DAYS,
    ):
        self.default_limit = default_limit
        self.max_limit = max_limit
        self.default_timeout = default_timeout
        self.max_timeout = max_timeout
        self.max_time_range_days = max_time_range_days

    def validate_and_build(
        self,
        query_def: QueryDefinition,
        client_params: dict[str, Any] | None,
        options: QueryOptions | None,
        auth: AuthContext,
    ) -> dict[str, Any]:
        """
        Validate parameters and build final parameter dict for SQL rendering.

        Args:
            query_def: Query definition from registry
            client_params: Parameters provided by client
            options: Standard query options (limit, timeout, etc.)
            auth: Authentication context from JWT

        Returns:
            Merged parameter dict including validated params and auth context

        Raises:
            AuthorizationError: User not authorized
            ParameterValidationError: Invalid parameter value
        """
        # Step 1: Authorization check
        self._check_authorization(query_def, auth)

        # Step 2: Initialize result with auth context (reserved params)
        result: dict[str, Any] = {
            "_org_id": auth.org_id,
            "_user_id": auth.user_id,
            "_roles": auth.roles,
            "_request_id": auth.request_id or str(uuid.uuid4()),
        }

        # Step 3: Validate and add client parameters
        client_params = client_params or {}
        for name, param_def in query_def.parameters.items():
            if name in client_params:
                result[name] = self._validate_param(
                    name, client_params[name], param_def
                )
            elif param_def.default is not None:
                result[name] = param_def.default
            elif param_def.required:
                raise ParameterValidationError(
                    f"Missing required parameter: {name}", param=name
                )
            # Else: optional param not provided, not in result

        # Check for unknown parameters
        known_params = set(query_def.parameters.keys())
        for name in client_params:
            if name not in known_params:
                raise ParameterValidationError(
                    f"Unknown parameter: {name}", param=name
                )

        # Step 4: Standard parameters (limit, timeout, time bounds)
        options = options or QueryOptions()
        result.update(self._resolve_standard_params(query_def, options))

        return result

    def _check_authorization(
        self, query_def: QueryDefinition, auth: AuthContext
    ) -> None:
        """Check if user is authorized to execute this query."""
        # Check required roles
        if query_def.required_roles:
            if not any(role in auth.roles for role in query_def.required_roles):
                raise AuthorizationError(
                    f"Requires one of roles: {query_def.required_roles}"
                )

        # Check required permissions
        if query_def.required_permissions:
            if not any(
                perm in auth.permissions for perm in query_def.required_permissions
            ):
                raise AuthorizationError(
                    f"Requires one of permissions: {query_def.required_permissions}"
                )

        # Check tenant isolation - non-isolated queries require admin
        if not query_def.tenant_isolated:
            if "admin" not in auth.roles:
                raise AuthorizationError(
                    "Non-tenant-isolated queries require admin role"
                )

    def _validate_param(
        self,
        name: str,
        value: Any,
        param_def: ParameterDefinition,
    ) -> Any:
        """Validate and coerce a single parameter value."""
        if value is None:
            if param_def.required:
                raise ParameterValidationError(
                    f"Parameter '{name}' cannot be null", param=name
                )
            return param_def.default

        # Type validation and coercion
        try:
            value = self._coerce_type(value, param_def.type, param_def.items)
        except (ValueError, TypeError) as e:
            raise ParameterValidationError(
                f"Parameter '{name}' type error: {e}", param=name
            ) from e

        # Constraint validation
        self._validate_constraints(name, value, param_def)

        return value

    def _coerce_type(
        self,
        value: Any,
        param_type: ParameterType,
        array_item_type: ParameterType | None = None,
    ) -> Any:
        """Coerce value to expected type."""
        if param_type == ParameterType.STRING:
            return str(value)

        elif param_type == ParameterType.INTEGER:
            return int(value)

        elif param_type == ParameterType.FLOAT:
            return float(value)

        elif param_type == ParameterType.BOOLEAN:
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                if value.lower() in ("true", "1", "yes"):
                    return True
                if value.lower() in ("false", "0", "no"):
                    return False
            raise ValueError(f"Cannot convert '{value}' to boolean")

        elif param_type == ParameterType.DATETIME:
            if isinstance(value, datetime):
                return value
            if isinstance(value, str):
                # Parse ISO8601
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            raise ValueError(f"Cannot convert '{value}' to datetime")

        elif param_type == ParameterType.DATE:
            if isinstance(value, datetime):
                return value.date()
            if isinstance(value, str):
                return datetime.fromisoformat(value).date()
            raise ValueError(f"Cannot convert '{value}' to date")

        elif param_type == ParameterType.UUID:
            if isinstance(value, uuid.UUID):
                return value
            return uuid.UUID(str(value))

        elif param_type == ParameterType.ARRAY:
            if not isinstance(value, list):
                raise ValueError(f"Expected array, got {type(value).__name__}")
            if array_item_type:
                return [self._coerce_type(v, array_item_type) for v in value]
            return value

        return value

    def _validate_constraints(
        self,
        name: str,
        value: Any,
        param_def: ParameterDefinition,
    ) -> None:
        """Validate value against parameter constraints."""
        # Numeric min/max
        if param_def.min is not None:
            if isinstance(value, (int, float)) and value < param_def.min:
                raise ParameterValidationError(
                    f"Parameter '{name}' must be >= {param_def.min}", param=name
                )

        if param_def.max is not None:
            if isinstance(value, (int, float)) and value > param_def.max:
                raise ParameterValidationError(
                    f"Parameter '{name}' must be <= {param_def.max}", param=name
                )

        # String max_length
        if param_def.max_length is not None:
            if isinstance(value, str) and len(value) > param_def.max_length:
                raise ParameterValidationError(
                    f"Parameter '{name}' exceeds max length {param_def.max_length}",
                    param=name,
                )

        # String pattern
        if param_def.pattern is not None:
            if isinstance(value, str) and not re.match(param_def.pattern, value):
                raise ParameterValidationError(
                    f"Parameter '{name}' does not match pattern {param_def.pattern}",
                    param=name,
                )

        # Enum constraint
        if param_def.enum is not None:
            if value not in param_def.enum:
                raise ParameterValidationError(
                    f"Parameter '{name}' must be one of: {param_def.enum}",
                    param=name,
                )

        # Array max_items
        if param_def.max_items is not None:
            if isinstance(value, list) and len(value) > param_def.max_items:
                raise ParameterValidationError(
                    f"Parameter '{name}' exceeds max items {param_def.max_items}",
                    param=name,
                )

    def _resolve_standard_params(
        self,
        query_def: QueryDefinition,
        options: QueryOptions,
    ) -> dict[str, Any]:
        """Resolve standard parameters with defaults and limits."""
        result: dict[str, Any] = {}

        # Get query-specific overrides
        defaults = query_def.defaults or {}
        limits = query_def.limits or {}

        # Limit
        query_default_limit = getattr(defaults, "limit", None) or self.default_limit
        query_max_limit = getattr(limits, "max_limit", None) or self.max_limit

        if options.limit is not None:
            result["limit"] = min(options.limit, query_max_limit, self.max_limit)
        else:
            result["limit"] = query_default_limit

        # Offset-based pagination
        result["offset"] = options.offset or 0

        # Cursor-based pagination (mutually exclusive with offset)
        if options.cursor:
            result["_cursor"] = options.cursor
            # When using cursor, offset is ignored
            result["offset"] = 0

        # Keyset pagination
        if options.after_key is not None:
            result["_after_key"] = options.after_key
        if options.order_by:
            result["_order_by"] = options.order_by
            result["_order_dir"] = options.order_dir

        # Timeout
        query_default_timeout = (
            getattr(defaults, "timeout_seconds", None) or self.default_timeout
        )
        query_max_timeout = getattr(limits, "max_timeout", None) or self.max_timeout

        if options.timeout_seconds is not None:
            result["timeout_seconds"] = min(
                options.timeout_seconds, query_max_timeout, self.max_timeout
            )
        else:
            result["timeout_seconds"] = query_default_timeout

        # Time bounds (if query has time_column)
        if query_def.time_column:
            result["_time_column"] = query_def.time_column
            max_range = (
                getattr(limits, "max_time_range_days", None)
                or query_def.max_time_range_days
                or self.max_time_range_days
            )

            # time_to defaults to now
            if options.time_to:
                result["time_to"] = datetime.fromisoformat(
                    options.time_to.replace("Z", "+00:00")
                )
            else:
                from datetime import UTC

                result["time_to"] = datetime.now(UTC)

            # time_from
            if options.time_from:
                result["time_from"] = datetime.fromisoformat(
                    options.time_from.replace("Z", "+00:00")
                )
            elif query_def.time_required:
                raise ParameterValidationError(
                    "This query requires time_from parameter", param="time_from"
                )

            # Validate time range if both provided
            if "time_from" in result:
                range_days = (result["time_to"] - result["time_from"]).days
                if range_days > max_range:
                    raise ParameterValidationError(
                        f"Time range exceeds maximum of {max_range} days",
                        param="time_from",
                    )
                if range_days < 0:
                    raise ParameterValidationError(
                        "time_from must be before time_to", param="time_from"
                    )

        return result


# Module-level convenience
_validator: ParameterValidator | None = None


def get_validator() -> ParameterValidator:
    """Get singleton validator instance."""
    global _validator
    if _validator is None:
        _validator = ParameterValidator()
    return _validator


def validate_params(
    query_def: QueryDefinition,
    client_params: dict[str, Any] | None,
    options: QueryOptions | None,
    auth: AuthContext,
) -> dict[str, Any]:
    """Validate and build parameters for query execution."""
    return get_validator().validate_and_build(query_def, client_params, options, auth)
