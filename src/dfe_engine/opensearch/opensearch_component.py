from typing import Dict, Any, List
import json
import os
from hs_lib.logger import logger


class ValidationError(Exception):
    """Custom exception for template validation errors"""

    def __init__(self, errors: List[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


class OpenSearchComponent:
    def __init__(self, logger: DFELog):
        self.logger = logger

    def validate_component_template(self, template):
        """Validate component template structure"""
        validation_errors = []

        try:
            if template is None:
                validation_errors.append(
                    "Invalid template: Template cannot be empty or None"
                )
            elif not isinstance(template, dict):
                validation_errors.append(
                    "Invalid template: Template must be a dictionary"
                )
            else:
                template_content = template.get("template", template)

                if not template_content or (
                    "mappings" not in template_content
                    and "settings" not in template_content
                ):
                    validation_errors.append(
                        "Invalid template: Template must contain either 'mappings' or 'settings' section"
                    )

                if "version" in template and not isinstance(template["version"], int):
                    validation_errors.append(
                        "Invalid template: 'version' must be an integer"
                    )

                if "mappings" in template_content:
                    mappings = template_content["mappings"]
                    if not isinstance(mappings, dict):
                        validation_errors.append(
                            "Invalid template: 'mappings' must be a dictionary containing field definitions"
                        )
                    else:
                        if "dynamic_templates" in mappings:
                            if not isinstance(mappings["dynamic_templates"], list):
                                validation_errors.append(
                                    "Invalid template: 'dynamic_templates' must be a list of mapping rules"
                                )
                            elif not mappings["dynamic_templates"]:
                                validation_errors.append(
                                    "Invalid template: 'dynamic_templates' must contain at least one mapping rule"
                                )

                        if "properties" in mappings and not isinstance(
                            mappings["properties"], dict
                        ):
                            validation_errors.append(
                                "Invalid template: 'properties' must be a dictionary defining field mappings"
                            )

                if "settings" in template_content and not isinstance(
                    template_content["settings"], dict
                ):
                    validation_errors.append(
                        "Invalid template: 'settings' must be a dictionary containing index settings"
                    )

            if validation_errors:
                for error in validation_errors:
                    self.logger.error(error)
                raise ValidationError(validation_errors)

            self.logger.info("Template validation successful")
            return True

        except ValidationError:
            raise
        except Exception as e:
            error_msg = f"Template validation error: {str(e)}"
            self.logger.error(error_msg)
            raise ValidationError([error_msg])

    def fix_component_template_structure(
        self, template: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Fix component template structure by moving _meta to root level and ensuring correct structure"""
        if not template or not isinstance(template, dict):
            return template

        new_template = {}

        if "version" in template:
            new_template["version"] = template["version"]

        if "template" in template and "_meta" in template["template"]:
            new_template["_meta"] = template["template"].pop("_meta")

        if "template" not in template:
            new_template["template"] = {}
        else:
            new_template["template"] = template["template"]

        if "settings" in template:
            new_template["template"]["settings"] = template["settings"]
        if "mappings" in template:
            new_template["template"]["mappings"] = template["mappings"]

        for key, value in template.items():
            if key not in ["template", "_meta", "settings", "mappings", "version"]:
                new_template[key] = value

        return new_template

    def get_volume_template_path(self, base_path: str, volume: str) -> str:
        """Get appropriate component template path based on volume setting"""
        if volume not in ["default", "medium", "high"]:
            self.logger.warning(f"Invalid volume setting '{volume}', using default")
            volume = "default"

        if volume == "default":
            return base_path

        dir_path = os.path.dirname(base_path)
        filename = os.path.basename(base_path)
        base_name = os.path.splitext(filename)[0]

        base_name = base_name.replace("-medium", "").replace("-large", "")

        volume_suffix = "-medium" if volume == "medium" else "-large"
        return os.path.join(dir_path, f"{base_name}{volume_suffix}.json")

    def modify_template_for_devtest(self, template: Dict[str, Any]) -> Dict[str, Any]:
        """Modify template settings for DEVTEST environment"""
        if "template" in template and "settings" in template["template"]:
            new_template = json.loads(json.dumps(template))
            new_template["template"]["settings"].update(
                {"index.number_of_shards": "1", "index.number_of_replicas": "0"}
            )
            return new_template
        return template
