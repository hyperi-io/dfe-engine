import os
from hs_lib.logger import logger


class OpenSearchIndexTemplates:
    def __init__(self, logger: DFELog):
        self.logger = logger

    def validate_index_template(self, template):
        """Validate index template structure"""
        try:
            if not template:
                self.logger.error(
                    "Invalid index template: Template cannot be empty or None"
                )
                return False
            if not isinstance(template, dict):
                self.logger.error(
                    "Invalid index template: Template must be a dictionary"
                )
                return False

            if "index_patterns" not in template:
                self.logger.error(
                    "Invalid index template: Missing required 'index_patterns' field"
                )
                return False
            if not isinstance(template["index_patterns"], list):
                self.logger.error(
                    "Invalid index template: 'index_patterns' must be a list of index pattern strings"
                )
                return False
            if not template["index_patterns"]:
                self.logger.error(
                    "Invalid index template: 'index_patterns' must contain at least one pattern"
                )
                return False

            if "composed_of" in template:
                if not isinstance(template["composed_of"], list):
                    self.logger.error(
                        "Invalid index template: 'composed_of' must be a list of component template names"
                    )
                    return False

            if "template" in template:
                if not isinstance(template["template"], dict):
                    self.logger.error(
                        "Invalid index template: 'template' must be a dictionary containing index settings and mappings"
                    )
                    return False

                if "settings" in template["template"]:
                    if not isinstance(template["template"]["settings"], dict):
                        self.logger.error(
                            "Invalid index template: 'settings' must be a dictionary containing index settings"
                        )
                        return False

                if "mappings" in template["template"]:
                    if not isinstance(template["template"]["mappings"], dict):
                        self.logger.error(
                            "Invalid index template: 'mappings' must be a dictionary containing field mappings"
                        )
                        return False

            return True
        except Exception as e:
            self.logger.error(f"Index template validation error: {str(e)}")
            return False

    def get_template_path(self, base_path: str, cluster_type: str, is_aws: bool) -> str:
        """Get appropriate template file path based on deployment type"""
        dir_path = os.path.dirname(base_path)
        filename = os.path.basename(base_path)
        base_name = os.path.splitext(filename)[0]

        base_templates = ["logs_beats_skeleton", "os_common_template_unmatched"]

        if (
            any(base_name == template for template in base_templates)
            or "test-" in base_name
        ):
            return base_path

        base_name = base_name.replace("-devtest", "").replace("-aws", "")

        if cluster_type == "DEVTEST":
            return os.path.join(dir_path, f"{base_name}-devtest.json")
        elif is_aws:
            return os.path.join(dir_path, f"{base_name}-aws.json")
        return base_path

    def standardize_name(self, name: str, for_api: bool = True) -> str:
        """
        Standardize object names
        - Source files use underscore (_)
        - OpenSearch API uses hyphen (-)
        """
        if for_api:
            return name.replace("_", "-")
        return name.replace("-", "_")
