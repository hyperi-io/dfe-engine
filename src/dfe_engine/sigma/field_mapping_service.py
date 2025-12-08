import os
import csv
from pathlib import Path
from typing import Dict, Tuple, List, Any, Set
from hs_lib.logger import logger
from sqlalchemy import text
from ..yaml_utils import yaml_load

def resolve_schema_path(config: dict, schema_name: str, schema_version: str = None, is_meta: bool = True) -> str:
    """Resolve the path to a schema file."""
    meta_schema_base = Path(config['global_settings']['meta_schema_paths'])
    derived_schema_base = Path(config['global_settings']['derived_schema_paths'])

    if is_meta:
        base_path = meta_schema_base / schema_name.split(".")[0]
        version_path = schema_version.replace('.', '_') if schema_version else ''
        return str(base_path / version_path / schema_name) if version_path else str(base_path / schema_name)
    else:
        return str(derived_schema_base / schema_name)

def read_csv_mappings(schema_path: str) -> Dict[str, str]:
    """Read field mappings from a CSV file."""
    mappings = {}
    try:
        with open(schema_path, 'r') as file:
            reader = csv.DictReader(file)
            for row in reader:
                sigma_field_name = row.get('sigma_field_name', '')
                if sigma_field_name:
                    column_name = row.get('column', '')
                    if column_name:
                        mappings[sigma_field_name] = column_name
    except Exception as e:
        pass
    return mappings

class FieldMappingService:
    """Service for handling field mappings between Sigma rules and schema fields."""

    def __init__(self, config: dict) -> None:
        """
        Initialize the field mapping service.

        Args:
            config: The DFE configuration dictionary
        """
        self.config = config
        self.rules_input_dir = config.get('global_settings', {}).get('sigma_rules_input_dir', '../.dfe_sigma_rules_output')
        
    def _load_included_sigma_rules(self, include_path: str) -> dict:
        """
        Loads sigma rules from an included file.
        
        Args:
            include_path: Path to the included sigma rules file
            
        Returns:
            Dictionary containing the sigma rules
        """
        try:
            if include_path.startswith('./'):
                include_path = include_path[2:]
            
            full_path = os.path.abspath(include_path)
            if not os.path.exists(full_path):
                base_dir = os.path.dirname(os.path.abspath(self.config.get('dfe_package_file_path', '')))
                full_path = os.path.abspath(os.path.join(base_dir, include_path))
                
            included_config = yaml_load(full_path)
            if not included_config or 'sigma_rules' not in included_config:
                return {}

            rules_config = included_config['sigma_rules']
            if 'rules' in rules_config and isinstance(rules_config['rules'], dict):
                device_types = list(rules_config['rules'].keys())
                if device_types:
                    device_type = device_types[0]
                    rules_config['rules'] = rules_config['rules'][device_type]
                    if isinstance(rules_config['rules'], list):
                        for i, rule in enumerate(rules_config['rules']):
                            if isinstance(rule, dict):
                                rule['path'] = f"{device_type}/{rule['path']}"
                            else:
                                rules_config['rules'][i] = f"{device_type}/{rule}"

            return rules_config

        except Exception as e:
            logger.error(f"Error loading included sigma rules from {include_path}: {e}")
            
        return {}

    def _get_schema_sigma_rules(self, schema_config: dict) -> dict:
        """
        Gets sigma rules configuration, either directly from the config or from an included file.
        
        Args:
            schema_config: Schema configuration dictionary
            
        Returns:
            Dictionary containing the sigma rules configuration
        """
        if 'include_sigma_rules' in schema_config:
            return self._load_included_sigma_rules(schema_config['include_sigma_rules'])
        return {}

    def _get_rule_dynamic_metadata(self, sigma_rules_config: dict, rule_path: str) -> dict:
        """
        Gets dynamic metadata for a specific rule.
        
        Args:
            sigma_rules_config: Sigma rules configuration dictionary
            rule_path: Path to the rule file
            
        Returns:
            Dictionary containing dynamic metadata for the rule
        """
        if not sigma_rules_config or 'rules' not in sigma_rules_config:
            return {}

        for rule in sigma_rules_config['rules']:
            if isinstance(rule, dict) and rule.get('path') == rule_path:
                return rule.get('alert_dynamic_metadata', {})

        return {}

    def _get_schema_path(self, schema_name: str, schema_version: str = None, is_meta: bool = True) -> str:
        """
        Constructs the path to a schema file.
        
        Args:
            schema_name: Name of the schema
            schema_version: Version of the schema
            is_meta: Whether this is a meta schema or derived schema
            
        Returns:
            Full path to the schema file
        """
        return resolve_schema_path(self.config, schema_name, schema_version, is_meta)

    def _read_csv_mappings(self, schema_path: str) -> Dict[str, str]:
        """
        Reads field mappings from a CSV file, using the sigma_field_name column.
        
        Args:
            schema_path: Path to the CSV schema file
            
        Returns:
            Dictionary mapping sigma field names to schema field names
        """
        return read_csv_mappings(schema_path)

    def _read_schema_metadata(self, schema_path: str) -> Dict[str, Dict[str, str]]:
        """
        Reads schema metadata from a CSV file.
        
        Args:
            schema_path: Path to the CSV schema file
            
        Returns:
            Dictionary mapping column names to their metadata
        """
        metadata = {}
        try:
            with open(schema_path, 'r') as file:
                reader = csv.DictReader(file)
                for row in reader:
                    column = row.get('column', '')
                    if column:
                        metadata[column] = {
                            'type': row.get('type', ''),
                            'index_type': row.get('index_type', '')
                        }
                        
            return metadata
            
        except Exception as e:
            self.logger.error(f"Error reading schema metadata from {schema_path}: {e}")
            return {}

    def get_rule_metadata(self, schema_config: dict, rule_name: str, rule: dict) -> Dict[str, Any]:
        """
        Get metadata for a specific rule, including alert metadata and dynamic metadata.
        
        Args:
            schema_config: Schema configuration dictionary
            rule_name: Name of the rule
            rule: Parsed rule dictionary
            
        Returns:
            Dictionary containing rule metadata
        """
        level_map = {
            'critical': {'severity': 'critical', 'score': 90},
            'high': {'severity': 'high', 'score': 70},
            'medium': {'severity': 'medium', 'score': 50},
            'low': {'severity': 'low', 'score': 30}
        }
        
        metadata = {
            'alert_schedule': 'smd',
            'alert_schedule_duration': '10mins',
            'alert_ratingtime_sla_applies': 'true',
            'alert_framework': 'MITRE ATT&CK'
        }
        
        level = rule.get('level', 'medium').lower()
        if level in level_map:
            metadata['alert_severity'] = level_map[level]['severity']
            metadata['alert_triage_score'] = level_map[level]['score']

        if 'title' in rule:
            metadata['alert_type'] = rule['title']
        elif 'tags' in rule and rule['tags']:
            metadata['alert_type'] = rule['tags'][0].replace('attack.', '').replace('_', ' ').title()

        if 'description' in rule:
            desc = rule['description']
            desc = desc.replace('\\', '\\\\') \
                      .replace("'", "''") \
                      .replace('%', '%%') \
                      .replace('_', '\\_') \
                      .replace('\0', '') \
                      .replace('\b', '') \
                      .replace('\n', ' ') \
                      .replace('\r', ' ') \
                      .replace('\t', ' ') \
                      .replace('\x1a', '')
            metadata['alert_description'] = desc
            
        if 'alert_field_defaults' in self.config.get('schemas', {}):
            metadata.update(self.config['schemas']['alert_field_defaults'])
            
        sigma_rule_config = schema_config.get('sigma_rules', {}).get(rule_name, {})
        if 'alert_metadata' in sigma_rule_config:
            metadata.update(sigma_rule_config['alert_metadata'])
            
        sigma_rules_config = self._get_schema_sigma_rules(schema_config)
        rule_path = sigma_rule_config.get('path', '')
        dynamic_metadata = self._get_rule_dynamic_metadata(sigma_rules_config, rule_path)
        
        return {
            'alert_metadata': metadata,
            'dynamic_metadata': dynamic_metadata
        }

    def validate_field_mappings(self, source_fields: List[str], mappings: Dict[str, str], schema_metadata: Dict[str, Dict[str, str]], schema_name: str = None) -> List[str]:
        """
        Validate field mappings to ensure all source fields are properly mapped to schema fields.
        
        Args:
            source_fields: List of source fields from the Sigma rule
            mappings: Dictionary of field mappings (source field -> schema field)
            schema_metadata: Dictionary of schema field metadata
            schema_name: Name of the schema being validated
            
        Returns:
            List of missing field mappings (warnings)
        """
        missing_mappings = []
        for field in source_fields:
            if field not in mappings:
                schema_info = f" in schema '{schema_name}'" if schema_name else ""
                logger.warning(f"Missing mapping for Sigma field: {field}{schema_info}")
                missing_mappings.append(field)
            else:
                schema_field = mappings[field]
                
                if isinstance(schema_field, str):
                    if ',' in schema_field:
                        for sub_field in schema_field.split(','):
                            sub_field = sub_field.strip()
                            if sub_field not in schema_metadata:
                                pass
                    else:
                        if schema_field not in schema_metadata:
                            pass
                elif isinstance(schema_field, list):
                    for sub_field in schema_field:
                        if sub_field not in schema_metadata:
                            pass
        
        mapped_schema_fields = set()
        duplicate_mappings = set()
        
        for field in source_fields:
            if field in mappings:
                schema_field = mappings[field]
                if isinstance(schema_field, str):
                    if ',' in schema_field:
                        for sub_field in schema_field.split(','):
                            sub_field = sub_field.strip()
                            if sub_field in mapped_schema_fields:
                                duplicate_mappings.add(sub_field)
                            mapped_schema_fields.add(sub_field)
                    else:
                        if schema_field in mapped_schema_fields:
                            duplicate_mappings.add(schema_field)
                        mapped_schema_fields.add(schema_field)
                elif isinstance(schema_field, list):
                    for sub_field in schema_field:
                        if sub_field in mapped_schema_fields:
                            duplicate_mappings.add(sub_field)
                        mapped_schema_fields.add(sub_field)
        
        for field in duplicate_mappings:
            schema_info = f" in schema '{schema_name}'" if schema_name else ""
            logger.warning(f"Duplicate mapping to schema field: {field}{schema_info}")
            
        return missing_mappings

    def get_db_schema_mappings(self, device: str, db_session=None) -> Dict[str, str]:
        """
        Gets field mappings from database for a specific device type.
        
        Args:
            device: Device type (windows, linux, etc.)
            db_session: Database session for accessing sigma_mappings table
            
        Returns:
            Dictionary mapping sigma field names to schema field names
        """
        if not db_session:
            return {}
        
        mappings = {}
        try:
            result = db_session.execute(
                text("SELECT sigma_field, schema_field FROM sigma_mappings WHERE device = :device"),
                {"device": device}
            )
            
            for row in result:
                mappings[row.sigma_field] = row.schema_field

            logger.info(f"Loaded {len(mappings)} field mappings from database for device '{device}'")

        except Exception as e:
            logger.error(f"Error fetching field mappings from database: {e}")
            
        return mappings

    def get_db_meta_schema(self, schema_name: str, db_session=None) -> Dict[str, Dict[str, str]]:
        """
        Gets meta schema information from database.
        
        Args:
            schema_name: Name of the meta schema
            db_session: Database session for accessing meta_schemas table
            
        Returns:
            Dictionary mapping column_names to their metadata
        """
        if not db_session:
            return {}
            
        schema_metadata = {}
        try:
            result = db_session.execute(
                text("""
                    SELECT column_name, column_type, index_order
                    FROM meta_schemas
                    WHERE name = :schema_name
                    ORDER BY index_order
                """),
                {"schema_name": schema_name}
            )

            for row in result:
                schema_metadata[row.column_name] = {
                    "type": row.column_type,
                    "index_type": "text_search" if row.column_type == "text" else "",
                    "index_order": row.index_order,
                }

            logger.info(f"Loaded meta schema '{schema_name}' from database with {len(schema_metadata)} columns")

        except Exception as e:
            logger.error(f"Error fetching meta schema from database: {e}")
            
        return schema_metadata

    def get_db_derived_schema_additions(self, schema_name: str, db_session=None) -> Dict[str, Dict[str, str]]:
        """
        Gets derived schema additional columns from database.
        
        Args:
            schema_name: Name of the derived schema
            db_session: Database session for accessing derived_schemas table
            
        Returns:
            Dictionary mapping column_names to their metadata
        """
        if not db_session:
            return {}
            
        schema_metadata = {}
        try:
            result = db_session.execute(
                text("""
                    SELECT column_name, column_type, index_order
                    FROM derived_schemas
                    WHERE name = :schema_name
                    ORDER BY index_order
                """),
                {"schema_name": schema_name}
            )

            for row in result:
                schema_metadata[row.column_name] = {
                    "type": row.column_type,
                    "index_type": "text_search" if row.column_type == "text" else "",
                    "index_order": row.index_order,
                }

            logger.info(f"Loaded derived schema additions for '{schema_name}' from database with {len(schema_metadata)} columns")

        except Exception as e:
            logger.error(f"Error fetching derived schema additions from database: {e}")
            
        return schema_metadata

    def get_db_derived_schema_overrides(self, schema_name: str, db_session=None) -> Set[str]:
        """
        Gets derived schema column overrides from database.
        
        Args:
            schema_name: Name of the derived schema
            db_session: Database session for accessing derived_schemas table
            
        Returns:
            Set of column names that should be excluded from the meta schema
        """
        if not db_session:
            return set()
            
        excluded_columns = set()
        try:
            logger.info(f"No column overrides available for derived schema '{schema_name}' in unified table structure")

        except Exception as e:
            logger.error(f"Error fetching derived schema overrides from database: {e}")
            
        return excluded_columns

    def get_combined_schema_mappings(self, schema_config: dict, rule_name: str, db_session=None) -> Dict[str, str]:
        """
        Gets field mappings from either database (if db_session provided) or files (CLI mode).
        
        This method acts as a facade to switch between database and file-based operations.
        
        Args:
            schema_config: Schema configuration dictionary
            rule_name: Name of the sigma rule
            db_session: Database session (if None, uses file-based approach for CLI)
            
        Returns:
            Dictionary mapping sigma fields to schema fields
        """
        device = schema_config.get('device', 'windows')
        
        if db_session:
            mappings = self.get_db_schema_mappings(device, db_session)
            logger.info(f"Using database-based mappings with {len(mappings)} entries for device '{device}'")
            return mappings

        file_mappings, _ = self.get_schema_mappings(schema_config, rule_name)
        logger.info(f"Using file-based mappings with {len(file_mappings)} entries")
        return file_mappings
        
    def get_schema_mappings(self, schema_config: dict, rule_name: str) -> Tuple[Dict[str, str], Dict[str, Dict[str, str]]]:
        """
        Gets field mappings from schema configuration and global mappings.
        
        Args:
            schema_config: Schema configuration dictionary
            rule_name: Name of the sigma rule
            
        Returns:
            Tuple of (field mappings dictionary, schema metadata dictionary)
        """
        mappings = {}
        schema_metadata = {}

        sigma_rules_config = self._get_schema_sigma_rules(schema_config)
        if sigma_rules_config and 'mapping' in sigma_rules_config:
            mappings.update(sigma_rules_config['mapping'])

        if 'meta_schema' in schema_config:
            meta_schema_path = self._get_schema_path(
                schema_config['meta_schema'],
                schema_version=schema_config.get('meta_schema_version'),
                is_meta=True
            )
            
            meta_mappings = self._read_csv_mappings(meta_schema_path)
            for field, value in meta_mappings.items():
                if field not in mappings:
                    mappings[field] = value
                    
            meta_metadata = self._read_schema_metadata(meta_schema_path)
            schema_metadata.update(meta_metadata)

        if 'additional_fields_config' in schema_config:
            add_schema_path = self._get_schema_path(
                schema_config['additional_fields_config'],
                is_meta=False
            )
            add_mappings = self._read_csv_mappings(add_schema_path)
            for field, value in add_mappings.items():
                if field not in mappings:
                    mappings[field] = value
                    
            add_metadata = self._read_schema_metadata(add_schema_path)
            schema_metadata.update(add_metadata)

        if 'derived_schema_file_path' in schema_config:
            derived_schema_path = self._get_schema_path(
                schema_config['derived_schema_file_path'],
                is_meta=False
            )
            derived_mappings = self._read_csv_mappings(derived_schema_path)
            for field, value in derived_mappings.items():
                if field not in mappings:
                    mappings[field] = value
                    
            derived_metadata = self._read_schema_metadata(derived_schema_path)
            schema_metadata.update(derived_metadata)
        
        if 'source_field_mappings' in self.config.get('schemas', {}):
            for field, value in self.config.get('schemas', {}).get('source_field_mappings', {}).items():
                if field not in mappings:
                    mappings[field] = value

        rule_mappings = schema_config.get('sigma_rules', {}).get(rule_name, {}).get('alert_fields', {})
        if rule_mappings:
            for field, value in rule_mappings.items():
                mappings[field] = value

        return mappings, schema_metadata

    def get_rule_source_fields(self, rule: dict) -> List[str]:
        """
        Extracts source fields used in a sigma rule.
        
        Args:
            rule: Parsed sigma rule dictionary
            
        Returns:
            List of source field names used in the rule
        """
        if not isinstance(rule, dict):
            try:
                import json
                rule = json.loads(rule)
            except (json.JSONDecodeError, TypeError):
                logger.error(f"Failed to parse rule content: {rule[:100] if isinstance(rule, str) else type(rule)}...")
                return []
                
        fields = set()
        detection = rule.get('detection', {})
        
        def extract_fields(condition):
            if isinstance(condition, dict):
                for key, value in condition.items():
                    if key != 'condition':
                        base_field = key.split('|')[0]
                        fields.add(base_field)
            elif isinstance(condition, list):
                for item in condition:
                    extract_fields(item)
        
        for condition_name, condition in detection.items():
            if condition_name != 'condition':
                extract_fields(condition)
        
        return sorted(list(fields))
