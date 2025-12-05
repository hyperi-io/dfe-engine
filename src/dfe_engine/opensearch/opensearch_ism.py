from typing import Dict, Any
import os
from hs_lib.logger import logger


class OpenSearchISM:
    def __init__(self, logger: DFELog):
        self.logger = logger

    def validate_ism_policy(self, policy):
        """Validate ISM policy structure"""
        try:
            if not policy or not isinstance(policy, dict):
                self.logger.error("Invalid ISM policy: must be a non-empty dictionary")
                return False

            if "policy" not in policy:
                self.logger.error("Invalid ISM policy: missing 'policy' key")
                return False

            policy_content = policy["policy"]
            required_fields = ["policy_id", "description", "default_state", "states"]
            for field in required_fields:
                if field not in policy_content:
                    self.logger.error(f"Invalid ISM policy: missing '{field}'")
                    return False

            if not isinstance(policy_content["states"], list):
                self.logger.error("Invalid ISM policy: 'states' must be a list")
                return False

            return True
        except Exception as e:
            self.logger.error(f"ISM policy validation error: {e}")
            return False

    def get_ism_policy_path(
        self, base_path: str, cluster_type: str, is_aws: bool
    ) -> str:
        """Get appropriate ISM policy file path based on deployment type"""
        dir_path = os.path.dirname(base_path)
        filename = os.path.basename(base_path)
        if any(x in filename for x in ["test-", "modifiable", "version-control"]):
            return base_path
        if "hypersec-ism-hot-warm-delete" in filename:
            if cluster_type == "DEVTEST":
                return os.path.join(
                    dir_path, "os_hypersec-ism-hot-warm-delete-devtest.json"
                )
            elif is_aws:
                return os.path.join(
                    dir_path, "os_hypersec-ism-hot-warm-delete-aws.json"
                )
            return os.path.join(dir_path, "os_hypersec-ism-hot-warm-delete.json")

        base_name = os.path.splitext(filename)[0]
        if cluster_type == "DEVTEST":
            return os.path.join(dir_path, f"{base_name}-devtest.json")
        elif is_aws:
            return os.path.join(dir_path, f"{base_name}-aws.json")
        return base_path

    def _modify_policy_for_devtest(self, policy):
        """Modify ISM policy for DEVTEST environment"""
        if "policy" in policy and "policy_id" in policy["policy"]:
            policy_id = policy["policy"]["policy_id"]
            if any(x in policy_id for x in ["test-", "modifiable", "version-control"]):
                return policy

        if "states" in policy["policy"]:
            states = []
            for state in policy["policy"]["states"]:
                if state["name"] == "hot":
                    states.append(state.copy())
                elif state["name"] == "warm":
                    warm_state = {
                        "name": "warm",
                        "actions": [
                            {
                                "timeout": "1h",
                                "retry": {
                                    "count": 10,
                                    "backoff": "exponential",
                                    "delay": "5m",
                                },
                                "read_only": {},
                            }
                        ],
                        "transitions": [
                            {
                                "state_name": "delete",
                                "conditions": {"min_index_age": "1d"},
                            }
                        ],
                    }
                    states.append(warm_state)
                elif state["name"] == "delete":
                    states.append(state.copy())

            policy["policy"]["states"] = states

            if not any(
                x in policy_id for x in ["test-", "modifiable", "version-control"]
            ) and not policy["policy"]["description"].endswith(" (DEVTEST)"):
                policy["policy"]["description"] += " (DEVTEST)"

            if "ism_template" in policy["policy"]:
                if isinstance(policy["policy"]["ism_template"], list):
                    for template in policy["policy"]["ism_template"]:
                        template["priority"] = 50
                else:
                    policy["policy"]["ism_template"]["priority"] = 50

        return policy

    def _modify_policy_for_aws(self, policy: Dict[str, Any]) -> Dict[str, Any]:
        """Modify ISM policy for AWS environment"""
        if "states" in policy["policy"]:
            for state in policy["policy"]["states"]:
                if state["name"] == "warm":
                    state["actions"] = [
                        action
                        for action in state["actions"]
                        if not (isinstance(action, dict) and "allocation" in action)
                    ]
                    state["actions"].append(
                        {
                            "timeout": "1h",
                            "retry": {
                                "count": 10,
                                "backoff": "exponential",
                                "delay": "1h",
                            },
                            "warm_migration": {},
                        }
                    )

            if not policy["policy"]["description"].startswith("AWS"):
                policy["policy"]["description"] = (
                    "AWS " + policy["policy"]["description"]
                )

            if "ism_template" in policy["policy"]:
                if isinstance(policy["policy"]["ism_template"], list):
                    for template in policy["policy"]["ism_template"]:
                        template["priority"] = 200
                else:
                    policy["policy"]["ism_template"]["priority"] = 200

        return policy

    def _modify_policy_for_high_volume(
        self, policy: Dict[str, Any], source_name: str
    ) -> Dict[str, Any]:
        """Modify ISM policy for high volume sources"""
        policy["policy"]["description"] = (
            f"{source_name} HyperSec DFE streaming hot-warm-delete ISM streaming policy"
        )

        if "ism_template" in policy["policy"]:
            if isinstance(policy["policy"]["ism_template"], list):
                for template in policy["policy"]["ism_template"]:
                    template["index_patterns"] = [
                        f"{source_name}-*",
                        f".ds-{source_name}-*",
                    ]
                    template["priority"] = 300
            else:
                policy["policy"]["ism_template"]["index_patterns"] = [
                    f"{source_name}-*",
                    f".ds-{source_name}-*",
                ]
                policy["policy"]["ism_template"]["priority"] = 300

        return policy
