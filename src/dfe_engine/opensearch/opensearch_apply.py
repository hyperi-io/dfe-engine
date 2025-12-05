import json
import os
import re
from typing import Any, Dict, Optional, Tuple

import boto3
import requests
from hs_lib.logger import logger
from requests.auth import HTTPBasicAuth

from .opensearch_component import OpenSearchComponent
from .opensearch_index_templates import OpenSearchIndexTemplates
from .opensearch_ism import OpenSearchISM


class OpenSearchApply:
    def __init__(
        self,
        opensearch_url,
        aws_profile=None,
        iam_role=None,
        username=None,
        password=None,
        log_path="logs",
        verbose=False,
        bypass_auth=False,
        force_devtest=False,
    ):
        self.logger = logger
        self.logger.info("Initialized OpenSearchApply.")
        self.opensearch_url = opensearch_url
        self.auth = self.get_auth(
            aws_profile, iam_role, username, password, bypass_auth
        )
        self.headers = {"Content-Type": "application/json"}
        self._cluster_type = "DEVTEST" if force_devtest else None
        self._is_aws = None

        self.component = OpenSearchComponent(self.logger)
        self.index_templates = OpenSearchIndexTemplates(self.logger)
        self.ism = OpenSearchISM(self.logger)

        self.stats = {
            "component_templates": {"created": 0, "updated": 0},
            "index_templates": {"created": 0, "updated": 0},
            "ism_policies": {"created": 0, "updated": 0},
        }

    def get_auth(
        self,
        aws_profile=None,
        iam_role=None,
        username=None,
        password=None,
        bypass_auth=False,
    ):
        if bypass_auth:
            self.logger.info("Bypassing authentication for testing.")
            return None

        try:
            if aws_profile:
                boto3.Session(profile_name=aws_profile)
                auth = None
                self.logger.info(
                    f"Using AWS profile '{aws_profile}' for authentication."
                )
            elif iam_role:
                boto3_session = boto3.Session()
                sts_client = boto3_session.client("sts")
                assumed_role = sts_client.assume_role(
                    RoleArn=iam_role, RoleSessionName="OpenSearchSession"
                )
                credentials = assumed_role["Credentials"]
                auth = HTTPBasicAuth(
                    credentials["AccessKeyId"], credentials["SecretAccessKey"]
                )
                self.logger.info(f"Using IAM role '{iam_role}' for authentication.")
            elif username and password:
                auth = HTTPBasicAuth(username, password)
                self.logger.info(
                    "Using OpenSearch username/password for authentication."
                )
            else:
                raise ValueError(
                    "Either AWS IAM Role, AWS profile, or OpenSearch username and password must be provided for authentication."
                )
            return auth
        except Exception as e:
            self.logger.error(f"Authentication error: {e}")
            raise

    def detect_cluster_type(self) -> str:
        """
        Detect if cluster is PRODUCTION or DEVTEST based on number of nodes.

        Uses GET /_cluster/health endpoint which returns:
        {
          "cluster_name": "cluster",
          "status": "green",
          "timed_out": false,
          "number_of_nodes": 3,
          "number_of_data_nodes": 2,
          "active_primary_shards": 10,
          "active_shards": 20,
          "relocating_shards": 0,
          "initializing_shards": 0,
          "unassigned_shards": 0
        }

        If number_of_nodes >= 9 then this is a PRODUCTION cluster.
        Otherwise this is a TEST/DEV cluster and we will call it a DEVTEST cluster.

        Returns:
            str: 'PRODUCTION' if nodes >= 9, otherwise 'DEVTEST'
        """
        if self._cluster_type is None:
            try:
                url = f"{self.opensearch_url}/_cluster/health"
                response = requests.get(url, headers=self.headers, auth=self.auth)
                if response.status_code == 200:
                    health_data = response.json()
                    num_nodes = health_data.get("number_of_nodes", 0)
                    self._cluster_type = "PRODUCTION" if num_nodes >= 9 else "DEVTEST"
                    self.logger.info(
                        f"Detected cluster type: {self._cluster_type} (nodes: {num_nodes})"
                    )
                else:
                    self.logger.error(
                        f"Failed to get cluster health. Status: {response.status_code}, Response: {response.text}"
                    )
                    raise Exception("Failed to detect cluster type")
            except Exception as e:
                self.logger.error(f"Error detecting cluster type: {e}")
                raise
        return self._cluster_type

    def is_aws_cluster(self) -> bool:
        """
        Detect if cluster is AWS managed by checking cluster name pattern.

        Uses GET / endpoint which returns cluster information including:
        {
            "cluster_name": "my-domain-123456789012",
            "cluster_uuid": "...",
            "version": {
                "distribution": "opensearch",
                "number": "2.x.x",
                ...
            }
        }

        For AWS OpenSearch domains, the cluster_name follows the pattern:
        domainname-accountid (e.g., "my-domain-123456789012")

        This helps with:
        - Local testing with a Standalone OpenSearch Cluster
        - On prem deployments
        - DEVTEST deployments should work fine whether its an AWS cluster or not
        as there's no ultrawarm dependency

        Returns: True if AWS managed (cluster_name matches pattern domainname-accountid), False otherwise
        """
        if self._is_aws is None:
            try:
                url = f"{self.opensearch_url}/"
                response = requests.get(url, headers=self.headers, auth=self.auth)
                if response.status_code == 200:
                    cluster_info = response.json()
                    cluster_name = cluster_info.get("cluster_name", "")
                    # AWS OpenSearch domains have cluster names in format: domainname-accountid
                    # Example: my-domain-123456789012
                    self._is_aws = bool(re.match(r"^[\w-]+-\d{12}$", cluster_name))
                    self.logger.info(
                        f"Detected {'AWS' if self._is_aws else 'non-AWS'} cluster"
                    )
                    self.logger.debug(
                        f"Cluster info: {json.dumps(cluster_info, indent=2)}"
                    )
                else:
                    self.logger.error(
                        f"Failed to get cluster info. Status: {response.status_code}, Response: {response.text}"
                    )
                    raise Exception("Failed to detect if AWS cluster")
            except Exception as e:
                self.logger.error(f"Error detecting if AWS cluster: {e}")
                raise
        return self._is_aws

    def get_deployment_type(self) -> Tuple[str, bool]:
        """
        Get both cluster type and AWS status.
        Returns: Tuple of (cluster_type, is_aws)
        """
        return self.detect_cluster_type(), self.is_aws_cluster()

    def get_object_metadata(
        self, object_type: str, object_id: str
    ) -> Tuple[bool, Optional[int], Optional[int], Optional[int]]:
        """
        Get object metadata including existence, version, seq_no and primary_term.
        Returns: (exists, version, seq_no, primary_term)
        """
        endpoints = {
            "ism_policy": "_plugins/_ism/policies",
            "index_template": "_index_template",
            "component_template": "_component_template",
        }

        if object_type not in endpoints:
            self.logger.error(f"Invalid object type: {object_type}")
            return False, None, None, None

        endpoint = endpoints[object_type]
        base_url = f"{self.opensearch_url}/{endpoint}/{object_id}"

        response = requests.get(base_url, headers=self.headers, auth=self.auth)
        exists = response.status_code == 200

        version = 0
        seq_no = None
        primary_term = None

        if exists:
            if object_type == "component_template":
                templates = response.json().get("component_templates", [])
                for template in templates:
                    if template.get("name") == object_id:
                        version = template.get("component_template", {}).get("version")
                        if version is None:
                            version = template.get("version")
                        if version is None:
                            seq_no = template.get("seq_no")
                            if seq_no is not None:
                                version = 999999
                            else:
                                version = 0
                        seq_no = template.get("seq_no")
                        primary_term = template.get("primary_term")
                        break
            elif object_type == "index_template":
                templates = response.json().get("index_templates", [])
                for template in templates:
                    if template.get("name") == object_id:
                        version = template.get("index_template", {}).get("version", 0)
                        seq_no = template.get("seq_no")
                        primary_term = template.get("primary_term")
                        break
            elif object_type == "ism_policy":
                policy = response.json().get("policy", {})
                version = response.json().get("_version", 0)
                seq_no = policy.get("_seq_no")
                primary_term = policy.get("_primary_term")

        return exists, version, seq_no, primary_term

    def update_opensearch_object(
        self, object_type: str, object_id: str, payload: Dict[str, Any]
    ) -> bool:
        """
        Generic wrapper function to handle create/update operations for OpenSearch objects.
        Uses PUT for creation and updates with version control.
        """
        try:
            endpoints = {
                "ism_policy": "_plugins/_ism/policies",
                "index_template": "_index_template",
                "component_template": "_component_template",
            }

            if object_type not in endpoints:
                self.logger.error(f"Invalid object type: {object_type}")
                return False

            endpoint = endpoints[object_type]
            base_url = f"{self.opensearch_url}/{endpoint}/{object_id}"

            exists, current_version, seq_no, primary_term = self.get_object_metadata(
                object_type, object_id
            )
            new_version = (current_version or 0) + 1
            if object_type == "component_template":
                if isinstance(payload, dict):
                    if "template" not in payload:
                        payload["template"] = {}
                    payload["version"] = new_version
            elif object_type == "index_template":
                payload["version"] = new_version
            elif object_type == "ism_policy":
                if "policy" in payload:
                    payload["policy"]["last_updated_time"] = new_version

            if not exists:
                self.logger.info(
                    f"Creating {object_type} '{object_id}' with version {new_version}"
                )
                response = requests.put(
                    base_url, headers=self.headers, json=payload, auth=self.auth
                )
                if response.status_code in [200, 201]:
                    self.logger.info(
                        f"Successfully created {object_type} '{object_id}'"
                    )
                    if object_type == "component_template":
                        self.stats["component_templates"]["created"] += 1
                    elif object_type == "index_template":
                        self.stats["index_templates"]["created"] += 1
                    elif object_type == "ism_policy":
                        self.stats["ism_policies"]["created"] += 1
                    return True
                else:
                    self.logger.error(
                        f"Failed to create {object_type} '{object_id}'. Status Code: {response.status_code}, Response: {response.text}"
                    )
                    return False

            self.logger.info(
                f"Updating {object_type} '{object_id}' to version {new_version}"
            )

            if object_type == "ism_policy":
                update_response = requests.put(
                    base_url, headers=self.headers, json=payload, auth=self.auth
                )
                if update_response.status_code == 200:
                    self.logger.info(
                        f"Successfully updated {object_type} '{object_id}'"
                    )
                    if object_type == "component_template":
                        self.stats["component_templates"]["updated"] += 1
                    elif object_type == "index_template":
                        self.stats["index_templates"]["updated"] += 1
                    elif object_type == "ism_policy":
                        self.stats["ism_policies"]["updated"] += 1
                    return True
                elif update_response.status_code == 409:
                    exists, _, new_seq_no, new_primary_term = self.get_object_metadata(
                        object_type, object_id
                    )
                    if (
                        exists
                        and new_seq_no is not None
                        and new_primary_term is not None
                    ):
                        retry_url = f"{base_url}?if_seq_no={new_seq_no}&if_primary_term={new_primary_term}"
                        retry_response = requests.put(
                            retry_url,
                            headers=self.headers,
                            json=payload,
                            auth=self.auth,
                        )
                        if retry_response.status_code == 200:
                            self.logger.info(
                                f"Successfully updated {object_type} '{object_id}' after retry"
                            )
                            if object_type == "component_template":
                                self.stats["component_templates"]["updated"] += 1
                            elif object_type == "index_template":
                                self.stats["index_templates"]["updated"] += 1
                            elif object_type == "ism_policy":
                                self.stats["ism_policies"]["updated"] += 1
                            return True

                    delete_response = requests.delete(
                        base_url, headers=self.headers, auth=self.auth
                    )
                    if delete_response.status_code not in [200, 404]:
                        self.logger.error(
                            f"Failed to delete old ISM policy '{object_id}'. Status Code: {delete_response.status_code}, Response: {delete_response.text}"
                        )
                        return False

                    create_response = requests.put(
                        base_url, headers=self.headers, json=payload, auth=self.auth
                    )
                    if create_response.status_code in [200, 201]:
                        self.logger.info(
                            f"Successfully recreated {object_type} '{object_id}'"
                        )
                        if object_type == "component_template":
                            self.stats["component_templates"]["updated"] += 1
                        elif object_type == "index_template":
                            self.stats["index_templates"]["updated"] += 1
                        elif object_type == "ism_policy":
                            self.stats["ism_policies"]["updated"] += 1
                        return True

                    self.logger.error(
                        f"Failed to recreate {object_type} '{object_id}'. Status Code: {create_response.status_code}, Response: {create_response.text}"
                    )
                    return False
                else:
                    self.logger.error(
                        f"Failed to update {object_type} '{object_id}'. Status Code: {update_response.status_code}, Response: {update_response.text}"
                    )
                    return False
            else:
                if seq_no is not None and primary_term is not None:
                    update_url = (
                        f"{base_url}?if_seq_no={seq_no}&if_primary_term={primary_term}"
                    )
                else:
                    update_url = base_url

                update_response = requests.put(
                    update_url, headers=self.headers, json=payload, auth=self.auth
                )

                if update_response.status_code == 200:
                    self.logger.info(
                        f"Successfully updated {object_type} '{object_id}'"
                    )
                    if object_type == "component_template":
                        self.stats["component_templates"]["updated"] += 1
                    elif object_type == "index_template":
                        self.stats["index_templates"]["updated"] += 1
                    elif object_type == "ism_policy":
                        self.stats["ism_policies"]["updated"] += 1
                    return True
                elif update_response.status_code == 409:
                    exists, _, new_seq_no, new_primary_term = self.get_object_metadata(
                        object_type, object_id
                    )
                    if (
                        exists
                        and new_seq_no is not None
                        and new_primary_term is not None
                    ):
                        retry_url = f"{base_url}?if_seq_no={new_seq_no}&if_primary_term={new_primary_term}"
                        retry_response = requests.put(
                            retry_url,
                            headers=self.headers,
                            json=payload,
                            auth=self.auth,
                        )
                        if retry_response.status_code == 200:
                            self.logger.info(
                                f"Successfully updated {object_type} '{object_id}' after retry"
                            )
                            if object_type == "component_template":
                                self.stats["component_templates"]["updated"] += 1
                            elif object_type == "index_template":
                                self.stats["index_templates"]["updated"] += 1
                            elif object_type == "ism_policy":
                                self.stats["ism_policies"]["updated"] += 1
                            return True

                    self.logger.error(
                        f"Failed to update {object_type} '{object_id}' after version conflict"
                    )
                    return False

                self.logger.error(
                    f"Failed to update {object_type} '{object_id}'. Status Code: {update_response.status_code}, Response: {update_response.text}"
                )
                return False

        except Exception as e:
            if str(e):
                self.logger.error(f"Error applying {object_type}: {e}")
            return False

    def apply_component_template(
        self, template_path, volume="default", template_name=None
    ):
        """Apply component template with volume consideration"""
        try:
            cluster_type, is_aws = self.get_deployment_type()

            with open(template_path, "r") as f:
                template = json.load(f)

            template = self.component.fix_component_template_structure(template)

            if not self.component.validate_component_template(template):
                return False

            if cluster_type == "DEVTEST":
                template = self.component.modify_template_for_devtest(template)

            if template_name is None:
                template_name = self.index_templates.standardize_name(
                    os.path.splitext(os.path.basename(template_path))[0], for_api=True
                )

            return self.update_opensearch_object(
                object_type="component_template",
                object_id=template_name,
                payload=template,
            )

        except Exception as e:
            self.logger.error(f"Error applying component template: {e}")
            return False

    def standardize_name(self, name: str, for_api: bool = True) -> str:
        """
        Standardize object names
        - Source files use underscore (_)
        - OpenSearch API uses hyphen (-)
        """
        if for_api:
            return name.replace("_", "-")
        return name.replace("-", "_")

    def apply_index_template(self, template_path):
        """Apply index template with proper version control"""
        try:
            cluster_type, is_aws = self.get_deployment_type()
            with open(template_path, "r") as f:
                template = json.load(f)

            if not self.index_templates.validate_index_template(template):
                return False

            if cluster_type == "DEVTEST" and "composed_of" in template:
                composed_of = template["composed_of"]
                for i, component in enumerate(composed_of):
                    if component == "hypersec-log-component-template":
                        composed_of[i] = "os-hypersec-log-component-template-devtest"
                template["composed_of"] = composed_of

            # Ensure all patterns end with -* but preserve both regular and data stream patterns
            index_patterns = template.get("index_patterns", ["default"])
            normalized_patterns = []
            for pattern in index_patterns:
                if not pattern.endswith("-*"):
                    if pattern.endswith("*"):
                        pattern = pattern[:-1]
                    if pattern.endswith("-"):
                        pattern = pattern[:-1]
                    pattern = pattern + "-*"
                normalized_patterns.append(pattern)
            template["index_patterns"] = normalized_patterns
            # Use first pattern for template name
            template_name = normalized_patterns[0].rstrip("*")
            if template_name.endswith("-"):
                template_name = template_name[:-1]

            return self.update_opensearch_object(
                object_type="index_template", object_id=template_name, payload=template
            )

        except Exception as e:
            self.logger.error(f"Error applying index template: {e}")
            return False

    def modify_policy_for_devtest(self, policy: Dict[str, Any]) -> Dict[str, Any]:
        """Delegate to ISM class"""
        return self.ism._modify_policy_for_devtest(policy)

    def modify_policy_for_aws(self, policy: Dict[str, Any]) -> Dict[str, Any]:
        """Delegate to ISM class"""
        return self.ism._modify_policy_for_aws(policy)

    def modify_policy_for_high_volume(
        self, policy: Dict[str, Any], source_name: str
    ) -> Dict[str, Any]:
        """Delegate to ISM class"""
        return self.ism._modify_policy_for_high_volume(policy, source_name)

    def apply_ism_policy(self, policy_file, source_name=None, volume=None):
        """Apply ISM policy with proper version control and environment-specific handling."""
        try:
            cluster_type = self.detect_cluster_type()
            is_aws = self.is_aws_cluster()
            policy_dir = os.path.abspath(os.path.dirname(policy_file))
            base_name = os.path.splitext(os.path.basename(policy_file))[0]

            if not os.path.exists(policy_file):
                if base_name.startswith("hot_warm_delete"):
                    if cluster_type == "DEVTEST":
                        new_file = os.path.join(
                            policy_dir, "hot_warm_delete-devtest.json"
                        )
                    elif is_aws:
                        new_file = os.path.join(policy_dir, "hot_warm_delete-aws.json")
                    else:
                        new_file = os.path.join(policy_dir, "hot_warm_delete.json")
                    if os.path.exists(new_file):
                        policy_file = new_file
                elif base_name.startswith("os_hypersec-ism-hot-warm-delete"):
                    if cluster_type == "DEVTEST":
                        new_file = os.path.join(
                            policy_dir, "os_hypersec-ism-hot-warm-delete-devtest.json"
                        )
                    elif is_aws:
                        new_file = os.path.join(
                            policy_dir, "os_hypersec-ism-hot-warm-delete-aws.json"
                        )
                    else:
                        new_file = os.path.join(
                            policy_dir, "os_hypersec-ism-hot-warm-delete.json"
                        )
                    if os.path.exists(new_file):
                        policy_file = new_file

            with open(policy_file, "r") as f:
                policy_data = json.load(f)

            if not self.ism.validate_ism_policy(policy_data):
                return False

            modified_policy = json.loads(json.dumps(policy_data))

            policy_id = policy_data["policy"]["policy_id"]

            try:
                base_name = os.path.splitext(os.path.basename(policy_file))[0]
                is_test_policy = any(
                    x in base_name for x in ["test-", "modifiable", "version-control"]
                )

                if not is_test_policy:
                    if cluster_type == "DEVTEST":
                        modified_policy = self.ism._modify_policy_for_devtest(
                            modified_policy
                        )
                    elif is_aws:
                        modified_policy = self.ism._modify_policy_for_aws(
                            modified_policy
                        )

                if volume == "high" and source_name and cluster_type != "DEVTEST":
                    modified_policy = self.ism._modify_policy_for_high_volume(
                        modified_policy, source_name
                    )
            except Exception as e:
                self.logger.error(f"Error modifying policy: {str(e)}")
                return False

            return self.update_opensearch_object(
                object_type="ism_policy",
                object_id=policy_id,
                payload={"policy": modified_policy["policy"]},
            )

        except Exception as e:
            self.logger.error(f"Error applying ISM policy: {str(e)}")
            return False

    def apply_all(self, templates_dir):
        """
        Apply all templates and policies in a directory in the correct order:
        1. Component templates first (from HyperSec_Framework/)
        2. Index templates second (from main directory)
        3. ISM policies last (from HyperSec_Framework/)
        """
        try:
            # Get cluster health info once at the start
            url = f"{self.opensearch_url}/_cluster/health"
            response = requests.get(url, headers=self.headers, auth=self.auth)
            if response.status_code == 200:
                health_data = response.json()
                self.logger.info("\n=== OpenSearch Template Application Started ===")
                cluster_type = self.detect_cluster_type()
                is_aws = self.is_aws_cluster()
                self.logger.info(f"Cluster Type: {cluster_type}")
                self.logger.info(f"AWS Cluster: {'Yes' if is_aws else 'No'}")
                self.logger.info(
                    f"Full cluster health response: {json.dumps(health_data, indent=2)}"
                )
            else:
                self.logger.error(
                    f"Failed to get cluster health. Status: {response.status_code}"
                )
                return False

            if os.path.isfile(templates_dir):
                try:
                    with open(templates_dir, "r") as f:
                        template = json.load(f)

                    if "policy" in template:
                        self.logger.info(f"Applying ISM policy: {templates_dir}")
                        return self.apply_ism_policy(templates_dir)
                    elif "composed_of" in template:
                        self.logger.info(f"Applying index template: {templates_dir}")
                        return self.apply_index_template(templates_dir)
                    elif "template" in template:
                        self.logger.info(
                            f"Applying component template: {templates_dir}"
                        )
                        return self.apply_component_template(templates_dir)
                    else:
                        self.logger.error(
                            f"Unknown template type in file: {templates_dir}"
                        )
                        return False
                except Exception as e:
                    self.logger.error(f"Error reading template file: {e}")
                    return False

            if not os.path.exists(templates_dir):
                self.logger.error(f"Path not found: {templates_dir}")
                return False

            if not os.path.isdir(templates_dir):
                self.logger.error(f"Not a directory: {templates_dir}")
                return False

            framework_dir = os.path.join(templates_dir, "HyperSec_Framework")
            if not os.path.exists(framework_dir):
                parent_dir = os.path.dirname(templates_dir)
                framework_dir = os.path.join(parent_dir, "HyperSec_Framework")
                if not os.path.exists(framework_dir):
                    self.logger.error(
                        f"HyperSec_Framework directory not found in {templates_dir} or {parent_dir}"
                    )
                    return False

            framework_files = []
            for root, _, files in os.walk(framework_dir):
                files.sort()
                for file in files:
                    if not file.endswith(".json"):
                        continue

                    if cluster_type == "DEVTEST":
                        if any(
                            x in file
                            for x in ["-medium.", "-large.", "-aws.", "-dev-test."]
                        ):
                            continue
                        base_name = os.path.splitext(file)[0]
                        if not base_name.endswith("-devtest"):
                            devtest_file = f"{base_name}-devtest.json"
                            if devtest_file in files:
                                continue
                    elif is_aws:
                        if "-devtest." in file:
                            continue
                        base_name = os.path.splitext(file)[0]
                        if not base_name.endswith("-aws"):
                            aws_file = f"{base_name}-aws.json"
                            if aws_file in files:
                                continue
                    else:
                        if any(x in file for x in ["-aws.", "-devtest."]):
                            continue

                    framework_files.append(os.path.join(root, file))

            self.logger.info("\n=== Applying Component Templates ===")
            component_count = 0
            for template_path in framework_files:
                with open(template_path, "r") as f:
                    content = json.load(f)

                if "policy" not in content and "composed_of" not in content:
                    component_count += 1
                    template_name = os.path.splitext(os.path.basename(template_path))[0]
                    self.logger.info(
                        f"\nComponent Template [{component_count}]: {template_name}"
                    )
                    if not self.apply_component_template(template_path):
                        self.logger.error(
                            f"Failed to apply component template: {template_name}"
                        )
                        return False
            self.logger.info(
                f"\nSuccessfully applied {component_count} component templates"
            )

            self.logger.info("\n=== Applying Index Templates ===")
            index_count = 0
            for root, dirs, files in os.walk(templates_dir):
                if "HyperSec_Framework" in root:
                    continue

                for file in files:
                    if file.endswith("_opensearch_template.json"):
                        index_count += 1
                        template_path = os.path.join(root, file)
                        template_name = os.path.splitext(
                            os.path.basename(template_path)
                        )[0]
                        self.logger.info(
                            f"\nIndex Template [{index_count}]: {template_name}"
                        )
                        if not self.apply_index_template(template_path):
                            self.logger.error(
                                f"Failed to apply index template: {template_name}"
                            )
                            return False
            self.logger.info(f"\nSuccessfully applied {index_count} index templates")

            self.logger.info("\n=== Applying ISM Policies ===")
            policy_count = 0
            for template_path in framework_files:
                with open(template_path, "r") as f:
                    content = json.load(f)

                if "policy" in content:
                    policy_count += 1
                    policy_name = os.path.splitext(os.path.basename(template_path))[0]

                    if policy_name.startswith(
                        "os_hypersec-ism-hot-warm-delete"
                    ) and not any(
                        x in policy_name
                        for x in ["test-", "modifiable", "version-control"]
                    ):
                        if cluster_type == "DEVTEST" and not policy_name.endswith(
                            "-devtest"
                        ):
                            policy_file = os.path.join(
                                os.path.dirname(template_path),
                                "os_hypersec-ism-hot-warm-delete-devtest.json",
                            )
                        elif is_aws and not policy_name.endswith("-aws"):
                            policy_file = os.path.join(
                                os.path.dirname(template_path),
                                "os_hypersec-ism-hot-warm-delete-aws.json",
                            )
                        else:
                            policy_file = template_path
                    else:
                        policy_file = template_path

                    self.logger.info(f"Using policy file: {policy_file}")

                    self.logger.info(f"\nISM Policy [{policy_count}]: {policy_name}")
                    if not self.apply_ism_policy(policy_file):
                        self.logger.error(f"Failed to apply ISM policy: {policy_name}")
                        return False
            self.logger.info(f"\nSuccessfully applied {policy_count} ISM policies")

            self.logger.info("\n=== Final Summary ===")
            self.logger.info("Component Templates:")
            self.logger.info(
                f"  Created: {self.stats['component_templates']['created']}"
            )
            self.logger.info(
                f"  Updated: {self.stats['component_templates']['updated']}"
            )
            self.logger.info(
                f"  Total: {self.stats['component_templates']['created'] + self.stats['component_templates']['updated']}"
            )

            self.logger.info("\nIndex Templates:")
            self.logger.info(f"  Created: {self.stats['index_templates']['created']}")
            self.logger.info(f"  Updated: {self.stats['index_templates']['updated']}")
            self.logger.info(
                f"  Total: {self.stats['index_templates']['created'] + self.stats['index_templates']['updated']}"
            )

            self.logger.info("\nISM Policies:")
            self.logger.info(f"  Created: {self.stats['ism_policies']['created']}")
            self.logger.info(f"  Updated: {self.stats['ism_policies']['updated']}")
            self.logger.info(
                f"  Total: {self.stats['ism_policies']['created'] + self.stats['ism_policies']['updated']}"
            )

            self.logger.info("\nSuccessfully applied all templates and policies")
            return True

        except Exception as e:
            self.logger.error(f"Error applying templates: {e}")
            return False
