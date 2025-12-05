import requests

from typing import List
from hs_lib.logger import logger


class OpenSearchTenancy:
    def __init__(
        self,
        opensearch_apply,
        log_path="logs",
        verbose=False,
        ca_cert_path=None,
        excluded_tenants=None,
    ):
        """
        Initialize OpenSearchTenancy.

        Args:
            opensearch_apply: OpenSearchApply instance
            log_path (str): Path for log files (default: "logs")
            verbose (bool): Enable verbose logging (default: False)
            ca_cert_path (str): Optional CA certificate path
        """
        self.opensearch = opensearch_apply
        self.ca_cert_path = ca_cert_path
        self.excluded_tenants = excluded_tenants or ["global_tenant", "admin"]
        log_level = logging.DEBUG if verbose else logging.INFO
        self.logger = logger
        self.logger.info("Initialized OpenSearchTenancy.")

    def _make_request(self, method: str, url: str, **kwargs) -> requests.Response:
        """
        Make HTTP request with proper SSL verification.

        Args:
            method: HTTP method (get, post, etc)
            url: Request URL
            **kwargs: Additional request arguments

        Returns:
            Response from request
        """
        verify = self.ca_cert_path if self.ca_cert_path else True
        return requests.request(method, url, verify=verify, **kwargs)

    def _get_dashboard_url(self) -> str:
        """
        Get the dashboard URL.

        Returns:
            str: Dashboard URL with correct protocol and port
        """
        opensearch_url = self.opensearch.opensearch_url
        if "://" in opensearch_url:
            protocol, rest = opensearch_url.split("://")
            host = rest.split("/")[0].split(":")[0]
            return f"{protocol}://{host}:5601"
        return f"http://{opensearch_url.split('/')[0].split(':')[0]}:5601"

    def list_tenancies(self) -> List[str]:
        """
        Get list of available tenancies.

        Returns:
            List of tenancy names
        """
        try:
            dashboards_url = self._get_dashboard_url()
            opensearch_url = self.opensearch.opensearch_url.rstrip("/")

            base_headers = {
                **self.opensearch.headers,
                "Content-Type": "application/json",
                "Accept": "application/json",
            }

            endpoints = [
                (opensearch_url, "/_plugins/_security/api/tenants", base_headers),
                (dashboards_url, "/_dashboards/api/security/tenants", base_headers),
            ]

            # Check if security plugin is installed and configured
            try:
                security_check_url = f"{opensearch_url}/_cat/plugins"
                response = self._make_request(
                    "get",
                    security_check_url,
                    headers=base_headers,
                    auth=self.opensearch.auth,
                )

                if response.status_code == 200:
                    plugins = response.text
                    has_security = (
                        "opensearch-security" in plugins
                        or "opendistro_security" in plugins
                    )
                    if not has_security:
                        self.logger.info(
                            "Security plugin not installed - using default tenant"
                        )
                        return []

                    security_config_url = f"{opensearch_url}/_plugins/_security/health"
                    health_response = self._make_request(
                        "get",
                        security_config_url,
                        headers=base_headers,
                        auth=self.opensearch.auth,
                    )

                    if health_response.status_code != 200:
                        self.logger.info(
                            "Security plugin installed but not configured - using default tenant"
                        )
                        return []
            except Exception:
                pass

            # Try different endpoints to get tenant list
            for base_url, endpoint, headers in endpoints:
                try:
                    url = f"{base_url}{endpoint}"
                    response = self._make_request(
                        "get", url, headers=headers, auth=self.opensearch.auth
                    )

                    if response.status_code == 200:
                        tenants = response.json()
                        tenant_list = [
                            t for t in tenants.keys() if t not in self.excluded_tenants
                        ]

                        if tenant_list:
                            self.logger.info(
                                f"Found user tenants: {', '.join(tenant_list)}"
                            )
                            return tenant_list

                        self.logger.info("Only system tenants found")
                        return []
                except Exception:
                    continue

            self.logger.info("Security plugin not available - using default tenant")
            return []

        except Exception as e:
            self.logger.error(f"Error listing tenancies: {str(e)}")
            return []
