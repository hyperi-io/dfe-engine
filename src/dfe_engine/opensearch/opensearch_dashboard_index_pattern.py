import os
import uuid
import json

import requests
import urllib.parse
from datetime import datetime
from typing import Dict, List, Optional, Any
from pydantic import BaseModel

from hs_lib.logger import logger


class APIEndpoints:
    SAVED_OBJECTS_FIND = "/api/saved_objects/_find"
    INDEX_PATTERN = "/api/saved_objects/index-pattern"
    IMPORT = "/api/saved_objects/_import"


class ContentTypes:
    JSON = "application/json"
    NDJSON = "application/ndjson"


class SecurityHeaders:
    KBN_XSRF = "kbn-xsrf"
    OSD_XSRF = "osd-xsrf"
    SECURITY_TENANT = "securitytenant"


DEFAULT_TENANT = "admin"  # If "admin" is consistently the default

OPENSEARCH_PORT = "9200"
OPENSEARCH_DASHBOARD_PORT = "5601"


class IndexPatternAttributes(BaseModel):
    title: str
    timeFieldName: str
    fields: List[Dict[str, Any]]


class IndexPattern(BaseModel):
    type: str
    id: str
    attributes: IndexPatternAttributes
    migrationVersion: Dict[str, str]
    references: List[Any]
    updated_at: str
    version: str


class OpenSearchDashboardIndexPattern:
    def __init__(self, opensearch_apply, log_path="logs", verbose=False):
        """
        Initialize the OpenSearchDashboardIndexPattern.

        Args:
            opensearch_apply: OpenSearchApply instance
            log_path (str): Path for log files (default: "logs")
            verbose (bool): Enable verbose logging (default: False)
        """
        self.opensearch = opensearch_apply
        log_level = logging.DEBUG if verbose else logging.INFO
        self.logger = logger
        self.logger.info("Initialized OpenSearchDashboardIndexPattern.")
        self.output_dir = None
        self.session = requests.Session()
        self.session.auth = self.opensearch.auth
        self.session.verify = getattr(self.opensearch, "verify_certs", True)

    def import_saved_objects(
        self, ndjson_file: str, target_tenant: str, is_default: bool = False
    ) -> bool:
        """
        Import saved objects from an ndjson file to a target tenant.

        Args:
            ndjson_file (str): Path to the ndjson file containing saved objects
            target_tenant (str): Name of the target tenant
            is_default (bool): Whether these are default objects (default: False)

        Returns:
            bool: True if import was successful, False otherwise
        """
        try:
            dashboards_url = self._get_dashboard_url()
            headers = {
                **self.opensearch.headers,
                SecurityHeaders.OSD_XSRF: "true",
                SecurityHeaders.SECURITY_TENANT: target_tenant,
                SecurityHeaders.KBN_XSRF: "true",
                "Content-Type": ContentTypes.JSON,
            }

            with open(ndjson_file, "r") as f:
                ndjson_objects = []
                for line in f:
                    try:
                        json_obj = json.loads(line)
                        if "exportedCount" in json_obj:
                            continue
                        if "type" in json_obj and "attributes" in json_obj:
                            if (
                                json_obj["type"] == "index-pattern"
                                and "timeFieldName" not in json_obj["attributes"]
                            ):
                                json_obj["attributes"]["timeFieldName"] = "@timestamp"
                            ndjson_objects.append(json_obj)
                    except json.JSONDecodeError:
                        self.logger.warning(f"Skipping invalid JSON line: {line}")

            success = True
            for obj in ndjson_objects:
                try:
                    obj_type = obj["type"]
                    obj_title = obj["attributes"].get("title", "Unknown")
                    self.logger.info(
                        f"Importing {obj_type} '{obj_title}' to target tenant"
                    )

                    search_url = f"{dashboards_url}{APIEndpoints.SAVED_OBJECTS_FIND}"
                    search_response = self.session.get(
                        search_url,
                        headers=headers,
                        params={
                            "type": obj_type,
                            "search_fields": "title",
                            "search": obj_title,
                            "per_page": 1000,
                        },
                    )

                    json_payload = {
                        "attributes": obj["attributes"],
                        "references": obj.get("references", []),
                    }

                    import_url = f"{dashboards_url}/api/saved_objects/{obj_type}"
                    if search_response.status_code == 200:
                        saved_objects = search_response.json().get("saved_objects", [])
                        existing_obj = next(
                            (
                                o
                                for o in saved_objects
                                if o.get("attributes", {}).get("title") == obj_title
                            ),
                            None,
                        )
                        if existing_obj:
                            import_url = f"{import_url}/{existing_obj['id']}"
                            response = self.session.put(
                                import_url, headers=headers, json=json_payload
                            )
                        else:
                            response = self.session.post(
                                import_url, headers=headers, json=json_payload
                            )
                    else:
                        response = self.session.post(
                            import_url, headers=headers, json=json_payload
                        )

                    if response.status_code not in [200, 201]:
                        self.logger.error(
                            f"Failed to import {obj_type} '{obj_title}': {response.text}"
                        )
                        success = False
                        continue
                    else:
                        self.logger.info(
                            f"Successfully imported {obj_type} '{obj_title}' to tenant {target_tenant}"
                        )
                except Exception as e:
                    self.logger.error(f"Failed to import object: {str(e)}")
                    success = False
                    continue

            return success
        except Exception as e:
            self.logger.error(f"Failed to import saved objects: {str(e)}")
            return False

    def _get_dashboard_url(self) -> str:
        """
        Get the appropriate dashboard URL based on environment.

        Returns:
            str: Dashboard URL with correct protocol and port
        """
        # First check for explicit dashboard URL from environment
        dashboard_url = os.getenv("OPENSEARCH_DASHBOARDS_URL")
        if dashboard_url:
            return dashboard_url.rstrip("/")

        # Fall back to deriving from OpenSearch URL
        opensearch_url = self.opensearch.opensearch_url
        parsed_url = urllib.parse.urlparse(opensearch_url)

        # Replace the port while keeping the rest of the URL intact
        new_netloc = parsed_url.netloc.replace(
            OPENSEARCH_PORT, OPENSEARCH_DASHBOARD_PORT
        )
        new_url = parsed_url._replace(netloc=new_netloc).geturl()

        return new_url

    def import_dashboard_or_visualization(
        self, obj: Dict, tenant: str, is_default: bool = False
    ) -> bool:
        """
        Import a dashboard or visualization to a specific tenant.

        Args:
            obj (Dict): Dashboard or visualization object
            tenant (str): Name of the target tenant
            is_default (bool): Whether this is a default object (default: False)

        Returns:
            bool: True if import was successful, False otherwise
        """
        try:
            dashboards_url = self._get_dashboard_url()
            headers = {
                **self.opensearch.headers,
                SecurityHeaders.OSD_XSRF: "true",
                SecurityHeaders.SECURITY_TENANT: tenant,
                SecurityHeaders.KBN_XSRF: "true",
                "Content-Type": ContentTypes.JSON,
            }

            import_url = f"{dashboards_url}/api/saved_objects/{obj['type']}"
            response = self.session.post(
                import_url,
                headers=headers,
                json={"attributes": obj["attributes"], "overwrite": is_default},
                verify=getattr(self.opensearch, "verify_certs", True),
            )

            if response.status_code not in [200, 201]:
                self.logger.error(f"Failed to import {obj['type']}: {response.text}")
                return False
            return True
        except Exception as e:
            self.logger.error(f"Failed to import {obj['type']}: {str(e)}")
            return False

    def process_index_patterns(
        self,
        source_tenant: str,
        target_tenants: List[str],
        output_dir: str,
        template_names: str = None,
        default_ndjson_path: str = None,
    ) -> bool:
        """Process index patterns for source and target tenants.

        Args:
            source_tenant: Source tenant to export from
            target_tenants: List of target tenants to import to
            output_dir: Directory to save ndjson files
            template_names: Optional comma-separated list of template names to filter

        Returns:
            bool: True if successful, False otherwise
        """
        try:
            os.makedirs(output_dir, exist_ok=True)
            url = f"{self.opensearch.opensearch_url}/_index_template"
            response = self.session.get(
                url,
                headers=self.opensearch.headers,
                auth=self.opensearch.auth,
                verify=getattr(self.opensearch, "verify_certs", True),
            )
            if response.status_code != 200:
                self.logger.error("Failed to get index templates")
                return False

            templates = response.json().get("index_templates", [])
            if template_names:
                template_name_list = [
                    name.strip() for name in template_names.split(",")
                ]
                templates = [
                    t
                    for t in templates
                    if any(name in t.get("name", "") for name in template_name_list)
                ]

            if not templates:
                self.logger.error(
                    f"No templates found matching names: {template_names}"
                )
                return False

            success = True
            source_success = 0
            source_failure = 0
            skip_count = 0
            target_success = 0
            target_failure = 0

            # Step 1: Apply/create/update index patterns at source tenancy from index templates
            self.logger.info("\nStep 1: Creating/updating patterns on source tenant")
            patterns = {}

            for template in templates:
                template_name = template.get("name", "")
                if not template_name.startswith("logs-"):
                    self.logger.warning(f"Skipping non-logs pattern {template_name}")
                    skip_count += 1
                    continue

                pattern = self.generate_index_pattern(template_name, use_cm=True)
                if pattern:
                    tenant_success = self.apply_index_pattern(pattern, source_tenant)
                    if tenant_success:
                        patterns[template_name] = pattern
                        self.logger.info(
                            f"Successfully created/updated pattern {template_name}"
                        )
                        source_success += 1
                    else:
                        self.logger.warning(
                            f"Failed to create/update pattern {template_name}"
                        )
                        source_failure += 1
                        success = False

            if not success:
                self.logger.error(
                    "Failed to create/update some patterns on source tenant"
                )
                return False

            # Step 2: Export to ndjson from source tenancy
            self.logger.info("\nStep 2: Exporting patterns to ndjson files")
            pattern_files = {}

            for template_name, pattern in patterns.items():
                try:
                    pattern_file = os.path.join(
                        output_dir, f"{template_name}_index_pattern.ndjson"
                    )
                    self.save_index_pattern(pattern, output_dir, template_name)
                    pattern_files[template_name] = pattern_file
                except Exception as e:
                    self.logger.error(
                        f"Failed to export pattern {template_name}: {str(e)}"
                    )
                    success = False

            if not success:
                self.logger.error("Failed to export some patterns to ndjson files")
                return False

            # Step 3: Import ndjson files and apply/import to target tenants
            self.logger.info(
                "\nStep 3: Import ndjson files and apply/import to target tenants"
            )

            for tenant in target_tenants:
                self.logger.info(f"Processing target tenant: {tenant}")
                for template_name, pattern_file in pattern_files.items():
                    tenant_success = self.import_saved_objects(pattern_file, tenant)
                    if tenant_success:
                        target_success += 1
                    else:
                        self.logger.warning(
                            f"Failed to import pattern {template_name} to tenant {tenant}"
                        )
                        target_failure += 1
                        success = False

            # Step 4: Import default templates
            self.logger.info("\nStep 4: Import default templates")

            if default_ndjson_path:
                default_success, default_success_count, default_failure_count = (
                    self.import_default_templates(default_ndjson_path, target_tenants)
                )
                if not default_success:
                    self.logger.warning("Failed to import some default templates")
                    success = False

            # Update the final summary to include default template counts:
            self.logger.info("\nOperation Summary:")
            self.logger.info(
                f"Source tenant patterns - Created/Updated: {source_success}, Failed: {source_failure}"
            )
            self.logger.info(
                f"Target tenant patterns - Imported: {target_success}, Failed: {target_failure}"
            )
            self.logger.info(f"Test patterns skipped: {skip_count}")
            if default_ndjson_path:
                self.logger.info(
                    f"Default templates - Imported: {default_success_count}, Failed: {default_failure_count}"
                )

            if target_failure > 0:
                self.logger.warning("Failed to process some index patterns")
                success = False
            else:
                self.logger.info("Successfully processed all index patterns")

            return success
        except Exception as e:
            self.logger.error(f"Error processing index patterns: {str(e)}")
            return False

    def import_default_templates(
        self, default_ndjson_path: str, target_tenants: List[str]
    ) -> tuple[bool, int, int]:
        """
        Import default templates from an ndjson file to multiple target tenants.

        Args:
            default_ndjson_path (str): Path to the default ndjson file
            target_tenants (List[str]): List of target tenant names

        Returns:
            tuple[bool, int, int]: (success, imported_count, failed_count)
        """
        self.logger.info("\nImporting default templates")
        success = True
        imported_count = 0
        failed_count = 0

        for tenant in target_tenants:
            try:
                dashboards_url = self._get_dashboard_url()
                import_url = f"{dashboards_url}{APIEndpoints.IMPORT}?overwrite=true"

                headers = {
                    SecurityHeaders.KBN_XSRF: "true",
                    SecurityHeaders.OSD_XSRF: "true",
                    SecurityHeaders.SECURITY_TENANT: tenant,
                }

                with open(default_ndjson_path, "rb") as f:
                    files = {"file": (default_ndjson_path, f, ContentTypes.NDJSON)}

                    response = self.session.post(
                        import_url,
                        headers=headers,
                        files=files,
                        auth=self.opensearch.auth,
                        verify=getattr(self.opensearch, "verify_certs", True),
                    )

                if response.status_code in [200, 201]:
                    imported_count += 1
                    self.logger.info(
                        f"Successfully imported templates to tenant {tenant}"
                    )
                else:
                    failed_count += 1
                    success = False
                    self.logger.error(
                        f"Failed to import templates to tenant {tenant}: {response.text}"
                    )
            except Exception as e:
                self.logger.error(
                    f"Failed to import templates to tenant {tenant}: {str(e)}"
                )
                failed_count += 1
                success = False

        return success, imported_count, failed_count

    def _get_tenancies(self) -> List[str]:
        """Get list of available tenancies.

        Returns:
            List of tenancy names
        """
        try:
            dashboards_url = self._get_dashboard_url()
            url = f"{dashboards_url}/_dashboards/api/security/tenants"
            response = self.session.get(
                url,
                headers=self.opensearch.headers,
                auth=self.opensearch.auth,
                verify=getattr(self.opensearch, "verify_certs", True),
            )
            if response.status_code == 200:
                tenants = response.json()
                return [
                    t for t in tenants.keys() if t not in ["global_tenant", "admin"]
                ]

            self.logger.info(
                "Security plugin not accessible - using default tenant only"
            )
            return []
        except Exception as e:
            self.logger.error(f"Error getting tenancies: {str(e)}")
            return []

    def _get_index_fields(self, index_name: str) -> List[Dict]:
        """Get field definitions for an index from template or _mapping.

        Args:
            index_name: Name of the index

        Returns:
            List of field definitions
        """
        try:
            opensearch_url = self.opensearch.opensearch_url
            template_url = f"{opensearch_url}/_index_template/{index_name}"
            template_response = self.session.get(
                template_url,
                headers=self.opensearch.headers,
                auth=self.opensearch.auth,
                verify=getattr(self.opensearch, "verify_certs", True),
            )
            if template_response.status_code == 200:
                template_data = template_response.json()
                if "index_templates" in template_data:
                    for template in template_data["index_templates"]:
                        if template["name"] == index_name:
                            mappings = (
                                template.get("index_template", {})
                                .get("template", {})
                                .get("mappings", {})
                            )
                            if mappings:
                                return self._convert_mappings_to_fields(mappings)

            mapping_url = f"{opensearch_url}/{index_name}/_mapping"
            mapping_response = self.session.get(
                mapping_url,
                headers=self.opensearch.headers,
                auth=self.opensearch.auth,
                verify=getattr(self.opensearch, "verify_certs", True),
            )
            if mapping_response.status_code == 200:
                mapping_data = mapping_response.json()
                if mapping_data:
                    first_index = next(iter(mapping_data))
                    mappings = mapping_data[first_index].get("mappings", {})
                    return self._convert_mappings_to_fields(mappings)

            self.logger.error(f"Failed to get mappings for index {index_name}")
            return []
        except Exception as e:
            self.logger.error(f"Error getting index fields: {str(e)}")
            return []

    def _convert_mappings_to_fields(self, mappings: Dict) -> List[Dict]:
        """
        Convert OpenSearch mappings to index pattern fields.

        Args:
            mappings (Dict): OpenSearch mappings object

        Returns:
            List[Dict]: List of field definitions
        """
        fields = []
        properties = mappings.get("properties", {})
        fields.extend(
            [
                {
                    "count": 0,
                    "name": "@timestamp",
                    "type": "date",
                    "esTypes": ["date"],
                    "scripted": False,
                    "searchable": True,
                    "aggregatable": True,
                    "readFromDocValues": True,
                },
                {
                    "count": 0,
                    "name": "_id",
                    "type": "string",
                    "esTypes": ["_id"],
                    "scripted": False,
                    "searchable": True,
                    "aggregatable": True,
                    "readFromDocValues": False,
                },
                {
                    "count": 0,
                    "name": "_index",
                    "type": "string",
                    "esTypes": ["_index"],
                    "scripted": False,
                    "searchable": True,
                    "aggregatable": True,
                    "readFromDocValues": False,
                },
                {
                    "count": 0,
                    "name": "_score",
                    "type": "number",
                    "scripted": False,
                    "searchable": False,
                    "aggregatable": False,
                    "readFromDocValues": False,
                },
                {
                    "count": 0,
                    "name": "_source",
                    "type": "_source",
                    "esTypes": ["_source"],
                    "scripted": False,
                    "searchable": False,
                    "aggregatable": False,
                    "readFromDocValues": False,
                },
                {
                    "count": 0,
                    "name": "_type",
                    "type": "string",
                    "scripted": False,
                    "searchable": False,
                    "aggregatable": False,
                    "readFromDocValues": False,
                },
            ]
        )

        def process_field(name: str, field_def: Dict, parent: str = ""):
            full_name = f"{parent}{name}" if parent else name
            field_type = field_def.get("type", "keyword")
            if field_type == "text":
                fields.append(
                    {
                        "count": 0,
                        "name": full_name,
                        "type": "string",
                        "esTypes": ["text"],
                        "scripted": False,
                        "searchable": True,
                        "aggregatable": False,
                        "readFromDocValues": False,
                    }
                )
            else:
                field = {
                    "count": 0,
                    "name": full_name,
                    "type": self._map_type(field_type),
                    "esTypes": [field_type],
                    "scripted": False,
                    "searchable": True,
                    "aggregatable": True,
                    "readFromDocValues": True,
                }
                fields.append(field)
            if "properties" in field_def:
                for nested_name, nested_def in field_def["properties"].items():
                    process_field(nested_name, nested_def, f"{full_name}.")

        for field_name, field_def in properties.items():
            process_field(field_name, field_def)

        return fields

    def _map_type(self, es_type: str) -> str:
        """Map OpenSearch type to index pattern type.

        Args:
            es_type: OpenSearch field type

        Returns:
            Index pattern field type
        """
        type_map = {
            "keyword": "string",
            "text": "string",
            "long": "number",
            "integer": "number",
            "short": "number",
            "byte": "number",
            "double": "number",
            "float": "number",
            "half_float": "number",
            "scaled_float": "number",
            "date": "date",
            "boolean": "boolean",
            "binary": "binary",
            "ip": "ip",
        }
        return type_map.get(es_type, "string")

    def generate_index_pattern(self, index_name: str, use_cm: bool = True) -> Dict:
        """Generate index pattern object for an index.

        Args:
            index_name: Name of the index
            use_cm: Whether to use component templates (default: True)

        Returns:
            Index pattern object or None if pattern should be skipped
        """
        if not index_name.startswith("logs-"):
            self.logger.warning(f"Skipping non-logs pattern {index_name}")
            return None

        base_name = index_name[:-3] if index_name.endswith("-cm") else index_name
        cm_name = f"{base_name}-cm"
        cm_template_url = f"{self.opensearch.opensearch_url}/_index_template/{cm_name}"
        cm_response = self.session.get(
            cm_template_url,
            headers=self.opensearch.headers,
            auth=self.opensearch.auth,
            verify=getattr(self.opensearch, "verify_certs", True),
        )
        has_cm_template = cm_response.status_code == 200 and cm_response.json().get(
            "index_templates"
        )

        template_url = f"{self.opensearch.opensearch_url}/_index_template/{base_name}"
        template_response = self.session.get(
            template_url,
            headers=self.opensearch.headers,
            auth=self.opensearch.auth,
            verify=getattr(self.opensearch, "verify_certs", True),
        )
        has_base_template = (
            template_response.status_code == 200
            and template_response.json().get("index_templates")
        )

        if not has_cm_template and not has_base_template:
            self.logger.warning(
                f"No template found for {base_name}, skipping pattern creation"
            )
            return None

        if has_cm_template and not index_name.endswith("-cm"):
            self.logger.warning(f"Skipping {base_name} as CM template exists")
            return None

        fields = self._get_index_fields(index_name)
        if not fields:
            self.logger.warning(
                f"No valid mappings found for {index_name}, skipping pattern creation"
            )
            return None

        pattern_title = index_name
        pattern = {
            "attributes": {
                "fields": json.dumps(fields),
                "timeFieldName": "@timestamp",
                "title": f"{pattern_title}-*",
            },
            "id": str(uuid.uuid4())[:8]
            + "-"
            + str(uuid.uuid4())[9:13]
            + "-11ef-869d-bbd8322e0c5a",
            "migrationVersion": {"index-pattern": "7.6.0"},
            "references": [],
            "type": "index-pattern",
            "updated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "version": f"Wz{len(fields)},MV0=",
        }
        return pattern

    def save_index_pattern(self, pattern: Dict, output_dir: str, index_name: str):
        """Save index pattern as ndjson file.

        Args:
            pattern: Index pattern object
            output_dir: Output directory path
            index_name: Name of the index
        """
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)
        output_file = os.path.join(output_dir, f"{index_name}_index_pattern.ndjson")
        try:
            with open(output_file, "w") as f:
                pattern_entry = {
                    "type": "index-pattern",
                    "id": pattern["id"],
                    "attributes": {
                        "fields": pattern["attributes"]["fields"],
                        "timeFieldName": "@timestamp",
                        "title": pattern["attributes"]["title"],
                    },
                    "migrationVersion": {"index-pattern": "7.6.0"},
                    "references": [],
                    "updated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    "version": pattern["version"],
                }
                f.write(json.dumps(pattern_entry, separators=(",", ":")) + "\n")
                export_info = {
                    "type": "index-pattern",
                    "exportedCount": 1,
                    "missingRefCount": 0,
                    "missingReferences": [],
                }
                f.write(json.dumps(export_info, separators=(",", ":")) + "\n")
            self.logger.info(f"Generated index pattern file: {output_file}")
        except Exception as e:
            self.logger.error(f"Error saving index pattern: {str(e)}")

    def apply_index_pattern(self, pattern: Dict, tenant: Optional[str] = None) -> bool:
        """Apply index pattern to a tenant.

        Args:
            pattern: Index pattern object
            tenant: Optional tenant name (uses default if not specified)

        Returns:
            True if successful, False otherwise
        """
        try:
            headers = {
                **self.opensearch.headers,
                SecurityHeaders.OSD_XSRF: "true",
                "Content-Type": ContentTypes.JSON,
            }

            if tenant:
                headers[SecurityHeaders.SECURITY_TENANT] = tenant

            dashboards_url = self._get_dashboard_url()
            title = pattern["attributes"]["title"].rstrip("*")
            search_url = f"{dashboards_url}{APIEndpoints.SAVED_OBJECTS_FIND}"
            search_response = self.session.get(
                search_url,
                headers=headers,
                auth=self.opensearch.auth,
                params={
                    "type": "index-pattern",
                    "search_fields": "title",
                    "search": title,
                    "per_page": 1000,
                },
                verify=getattr(self.opensearch, "verify_certs", True),
            )

            pattern_data = {
                "attributes": {
                    "title": f"{title}*",
                    "timeFieldName": "@timestamp",
                    "fields": pattern["attributes"]["fields"],
                }
            }
            pattern_id = None
            if search_response.status_code == 200:
                saved_objects = search_response.json().get("saved_objects", [])
                for obj in saved_objects:
                    if obj.get("attributes", {}).get("title") == f"{title}*":
                        pattern_id = obj["id"]
                        break

            url = f"{dashboards_url}{APIEndpoints.INDEX_PATTERN}"
            if pattern_id:
                url = f"{url}/{pattern_id}"
                response = self.session.put(
                    url,
                    headers=headers,
                    auth=self.opensearch.auth,
                    json=pattern_data,
                    verify=getattr(self.opensearch, "verify_certs", True),
                )
            else:
                response = self.session.post(
                    url,
                    headers=headers,
                    auth=self.opensearch.auth,
                    json=pattern_data,
                    verify=getattr(self.opensearch, "verify_certs", True),
                )

            if response.status_code == 413:
                self.logger.warning(
                    f"Pattern too large, trying without fields for {title} {response.text}"
                )
                pattern_data["attributes"].pop("fields", None)
                if pattern_id:
                    response = self.session.put(
                        url,
                        headers=headers,
                        auth=self.opensearch.auth,
                        json=pattern_data,
                        verify=getattr(self.opensearch, "verify_certs", True),
                    )
                else:
                    response = self.session.post(
                        url,
                        headers=headers,
                        auth=self.opensearch.auth,
                        json=pattern_data,
                        verify=getattr(self.opensearch, "verify_certs", True),
                    )

            if response.status_code in [200, 201]:
                if (
                    hasattr(self.opensearch, "stats")
                    and "index_patterns" in self.opensearch.stats
                ):
                    self.opensearch.stats["index_patterns"]["created"] += 1
                return True
            else:
                action = "update" if pattern_id else "create"
                self.logger.error(
                    f"Failed to {action} index pattern: {response.status_code}"
                )
                return False
        except Exception as e:
            self.logger.error(
                "Failed to apply index pattern to Dashboards. Please verify:"
            )
            self.logger.error("1. OpenSearch Dashboards is running and accessible")
            self.logger.error("2. Security plugin is properly configured")
            self.logger.error("3. Authentication credentials are correct")
            self.logger.error("4. Network connectivity and firewall rules allow access")
            self.logger.error(f"Error details: {str(e)}")
            return False
