import os
import json
import requests

from typing import Dict, List, Optional
from pathlib import Path
from hs_lib.logger import logger
from .opensearch_apply import OpenSearchApply


class CMTemplateApply:
    """Class for applying CM templates to OpenSearch."""

    def __init__(
        self,
        opensearch_url: str,
        aws_profile: Optional[str] = None,
        iam_role: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        log_path: str = "logs",
        verbose: bool = False,
    ):
        """Initialize CMTemplateApply.

        Args:
            opensearch_url: OpenSearch endpoint URL
            aws_profile: AWS profile for credentials
            iam_role: AWS IAM role ARN
            username: OpenSearch username for basic auth
            password: OpenSearch password for basic auth
            log_path: Path to log directory
            verbose: Enable verbose logging
        """
        log_level = logging.DEBUG if verbose else logging.INFO
        self.logger = logger
        self.opensearch = OpenSearchApply(
            opensearch_url=opensearch_url,
            aws_profile=aws_profile,
            iam_role=iam_role,
            username=username,
            password=password,
            log_path=log_path,
            verbose=verbose,
        )

    def find_cm_templates(self, templates_path: str) -> List[str]:
        """Find CM template files from a path.

        Args:
            templates_path: Path to template file or directory

        Returns:
            List of paths to CM template files
        """
        if os.path.isfile(templates_path):
            if templates_path.endswith("_cm.json"):
                return [templates_path]
            return []

        cm_templates = []
        for root, _, files in os.walk(templates_path):
            for file in files:
                if file.endswith("_cm.json"):
                    cm_templates.append(os.path.join(root, file))
        return cm_templates

    def load_cm_template(self, template_path: str) -> Dict:
        """Load a CM template from file.

        Args:
            template_path: Path to CM template file

        Returns:
            Template as dictionary

        Raises:
            Exception if template cannot be loaded
        """
        try:
            with open(template_path, "r") as f:
                return json.load(f)
        except Exception as e:
            self.logger.error(f"Failed to load CM template {template_path}: {str(e)}")
            raise

    def apply_cm_template(self, template_path: str) -> bool:
        """Apply a single CM template.

        Args:
            template_path: Path to CM template file

        Returns:
            True if successful, False otherwise
        """
        try:
            template = self.load_cm_template(template_path)
            template_name = Path(template_path).stem.replace("_", "-")
            template_content = template.get("template", {})
            settings = {}

            if "settings" in template_content:
                settings = template_content["settings"].copy()
                if "index" in settings:
                    index_settings = settings["index"].copy()
                    if "lifecycle" in index_settings:
                        del index_settings["lifecycle"]
                    if index_settings:
                        settings["index"] = index_settings

            mappings = template_content.get("mappings", {})
            if "properties" in mappings:
                if "tags" in mappings["properties"]:
                    mappings["properties"]["tags"] = {"type": "object"}

                total_fields = self._count_fields(mappings["properties"])
                self.logger.info(
                    f"Template has {total_fields} fields, setting appropriate limit"
                )
                if "index" not in settings:
                    settings["index"] = {}
                settings["index"]["mapping"] = {
                    "total_fields": {"limit": max(10000, total_fields + 1000)}
                }

            index_patterns = template.get("index_patterns", ["*"])
            converted_patterns = [
                pattern.replace("_", "-") for pattern in index_patterns
            ]
            cluster_type = self.opensearch.detect_cluster_type()
            composed_of = template.get("composed_of", [])
            if cluster_type == "DEVTEST":
                composed_of = [
                    "os-hypersec-log-component-template-devtest"
                    if component == "hypersec-log-component-template"
                    else component
                    for component in composed_of
                ]

            index_template = {
                "_meta": template.get("_meta", {}),
                "composed_of": composed_of,
                "priority": template.get("priority", 0),
                "data_stream": template.get("data_stream", {}),
                "index_patterns": converted_patterns,
                "template": {"settings": settings, "mappings": mappings},
            }

            self.logger.debug(
                f"Settings after cleanup: {json.dumps(settings, indent=2)}"
            )

            url = f"{self.opensearch.opensearch_url}/_index_template/{template_name}"
            get_response = requests.get(
                url, headers=self.opensearch.headers, auth=self.opensearch.auth
            )
            self.logger.info("\n" + "=" * 80)
            self.logger.info(f"Template: {template_name}")
            self.logger.info(f"Path: {template_path}")

            version_num = 1
            if get_response.status_code == 200:
                existing = get_response.json()
                self.logger.debug(
                    f"Get template response for {template_name}: {json.dumps(existing, indent=2)}"
                )
                if "index_templates" in existing:
                    found = False
                    for template_data in existing["index_templates"]:
                        if template_data["name"] == template_name:
                            found = True
                            template_def = template_data.get("index_template", {})
                            current_version = template_def.get("version", 0)
                            version_num = current_version + 1
                            self.logger.info(
                                f"Current version in OpenSearch: {current_version}"
                            )
                            self.logger.info(f"Incrementing to version: {version_num}")
                            break
                    if not found:
                        self.logger.info("First version of template")
                        self.logger.debug(
                            f"Template {template_name} not found in response"
                        )
                else:
                    self.logger.info("First version of template")
                    self.logger.debug("No index_templates found in response")
            else:
                self.logger.info("First version of template")
                self.logger.debug(
                    f"Get template request failed with status {get_response.status_code}"
                )

            index_template["version"] = version_num
            self.logger.debug(f"Setting template version to: {version_num}")
            self.logger.debug(
                f"Final template structure for {template_name}: {json.dumps(index_template, indent=2)}"
            )
            self.logger.debug(f"Template index patterns: {converted_patterns}")
            self.logger.info(f"Applying template {template_name} version {version_num}")
            put_response = requests.put(
                url,
                headers=self.opensearch.headers,
                auth=self.opensearch.auth,
                json=index_template,
            )

            if put_response.status_code in [200, 201]:
                self.logger.info(
                    f"Successfully applied index template {template_name} version {version_num}"
                )
                return True
            else:
                self.logger.error(
                    f"Failed to apply index template {template_name}: {put_response.text}"
                )
                return False

        except Exception as e:
            self.logger.error(f"Error applying CM template {template_path}: {str(e)}")
            return False

    def _count_fields(self, properties: Dict) -> int:
        """Count total number of fields in mappings recursively."""
        count = len(properties)
        for value in properties.values():
            if isinstance(value, dict):
                if "properties" in value:
                    count += self._count_fields(value["properties"])
                elif "fields" in value:
                    count += len(value["fields"])
        return count

    def apply_cm_templates(self, templates_path: str) -> bool:
        """Apply CM templates from a path.

        Args:
            templates_path: Path to template file or directory

        Returns:
            True if all templates applied successfully, False otherwise

        Note:
            If templates_path is a file, only that template will be applied.
            If templates_path is a directory, all CM templates in that directory will be applied.
        """
        test_url = f"{self.opensearch.opensearch_url}/_cluster/health"
        try:
            response = requests.get(
                test_url, headers=self.opensearch.headers, auth=self.opensearch.auth
            )
            if response.status_code == 200:
                health_data = response.json()
                self.logger.info("\n=== OpenSearch CM Template Application Started ===")
                cluster_type = self.opensearch.detect_cluster_type()
                is_aws = self.opensearch.is_aws_cluster()
                self.logger.info(f"Cluster Type: {cluster_type}")
                self.logger.info(f"AWS Cluster: {'Yes' if is_aws else 'No'}")
                self.logger.info(
                    f"Full cluster health response: {json.dumps(health_data, indent=2)}"
                )
            else:
                self.logger.error(f"OpenSearch is not accessible: {response.text}")
                return False
        except Exception as e:
            self.logger.error(f"Failed to connect to OpenSearch: {str(e)}")
            return False

        success = True
        templates = self.find_cm_templates(templates_path)

        if not templates:
            self.logger.warning(f"No CM templates found in {templates_path}")
            return True

        self.logger.info(f"Found {len(templates)} CM templates to apply")
        self.logger.info("=" * 80)
        templates.sort()

        for template_path in templates:
            if not self.apply_cm_template(template_path):
                success = False
            self.logger.info("-" * 80)

        self.logger.info("=" * 80)
        return success
