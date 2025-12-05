import datetime


class DataGenerator:
    def __init__(self):
        self.template_record = {
            "timestamp": "2024-11-27T12:34:56.789Z",
            "timestamp_load": "2024-11-27T12:35:10.123Z",
            "event_hash": "sample_hash_12345",
            "logoriginal": "Sample log message for event description",
            "org_id": "org_12345",
            "tags": "tag1,tag2",
            "access_list": "read,write",
            "access_mask": "0x1F",
            "access_reason": "user_requested",
            "account_name": "user_account",
            "account_type": "admin",
            "activity_id": "activity_67890",
            "acting_username": "admin_user",
            "actor_username": "actor_user",
            "authentication_package_name": "Kerberos",
            "category": "authentication",
            "channel": "security",
            "description": "Successful logon event",
            "domain": "domain.local",
            "elevated_token": "true",
            "event_id": 4624,
            "event_creation_time": "2024-11-27T12:34:56.789Z",
            "event_received_time": "2024-11-27T12:35:00.789Z",
            "event_time": "2024-11-27T12:34:56.789Z",
            "file_version": "1.0.0",
            "hashes": "SHA256:abc123...",
            "hostname": "host123",
            "impersonation_level": "Delegation",
            "integrity_level": "High",
            "ip_address": "192.168.1.100",
            "destination_ip": "192.168.1.101",
            "destination_ip_ip4": "192.168.1.101",
            "destination_ip_ip6": "fe80::1",
            "destination_is_ipv6": "false",
            "src_ip_addr": "192.168.1.102",
            "src_ip_addr_v4": "192.168.1.102",
            "src_ip_addr_v6": None,
            "source_ip_addr": "192.168.1.103",
            "source_ip_addr_v4": "192.168.1.103",
            "source_ip_addr_v6": None,
            "ip_port": 443,
            "destination_port": 443,
            "source_port": 80,
            "src_port": 8080,
            "src_port_number": 8080,
            "source_port_number": 80,
            "logon_type": 2,
            "level_value": 4,
            "message_id": 123,
            "new_process_id": 2345,
            "logon_key_length": 128,
            "restricted_sid_count": 1,
            "key_length": "128",
            "keywords": None,
            "logon_process_name": "User32",
            "mandatory_level": "System",
            "message": "User successfully logged in",
            "object_server": "Security",
            "object_type": "File",
            "opcode": "Start",
            "opcode_value": None,
            "previous_time": "2024-11-26T22:00:00.000Z",
            "privilege_list": "SeTcbPrivilege",
            "provider_uuid": "uuid_12345",
            "product": "Windows",
            "query_name": "SELECT * FROM logs",
            "query_results": "Success",
            "query_status": "Complete",
            "record_number": None,
            "resource_attributes": "None",
            "registry_event_type": "Write",
            "registry_key_path": "HKEY_LOCAL_MACHINE\\SOFTWARE",
            "registry_value_data": "DataValue",
            "service": "Windows Service",
            "service_state": "Running",
            "severity": "Information",
            "share_local_path": "\\\\server\\share",
            "share_name": "ShareName",
            "source_module_name": "Sysmon",
            "source_module_type": "Monitor",
            "source_name": "WindowsEventLog",
            "source_user": "SourceUser",
            "source_type": "Log",
            "status": "Success",
            "subject_domain_name": "DomainName",
            "subject_logon_id": "logon_1234",
            "subject_user_name": "UserName",
            "subject_user_sid": "SID12345",
            "sysmon_schema_version": "4.0",
            "sysmon_command_line": "cmd.exe",
            "sysmon_company": "Microsoft",
            "sysmon_current_directory": "C:\\Windows\\System32",
            "sysmon_description": "Command Prompt",
            "sysmon_file_version": "10.0.0",
            "sysmon_hash_imphash": "abc12345",
            "sysmon_hash_md5": "md512345",
            "sysmon_hash_sha256": "sha256_12345",
            "sysmon_hashes": "SHA256:sha256_12345",
            "sysmon_image": "C:\\Windows\\explorer.exe",
            "sysmon_integrity_level": "Medium",
            "sysmon_logon_guid": "guid_56789",
            "sysmon_logon_id": "logon_id_67890",
            "sysmon_original_file_name": "explorer.exe",
            "sysmon_parent_command_line": "parent_cmd.exe",
            "sysmon_parent_image": "C:\\Windows\\cmd.exe",
            "sysmon_parent_process_guid": "parent_guid_12345",
            "sysmon_parent_process_id": 1000,
            "sysmon_user": "SysmonUser",
            "sysmon_utc_time": "2024-11-27T12:36:56.789Z",
            "target_domain_name": "TargetDomain",
            "target_user_name": "TargetUser",
            "target_user_sid": "SID56789",
            "target_process_file_path": "C:\\Windows\\target.exe",
            "target_process_uuid": "uuid_67890",
            "target_process_id": "5678",
            "target_user": "Target",
            "target_user_domain": "TargetDomain",
            "thread_id": 1234,
            "thread_new_id": 5678,
            "token_elevation_type": "Full",
            "username": "user123",
            "virtual_account": "false",
        }

    def generate_timestamp(self):
        current_time_utc = datetime.datetime.now(datetime.timezone.utc)
        one_second_before_utc = current_time_utc - datetime.timedelta(seconds=1)
        return one_second_before_utc.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    def generate_records(self, num_records=1):
        timestamp_str = self.generate_timestamp()
        record = self.template_record.copy()

        for key in record:
            if key in [
                "timestamp",
                "timestamp_load",
                "event_creation_time",
                "event_received_time",
                "event_time",
                "previous_time",
                "sysmon_utc_time",
            ]:
                record[key] = timestamp_str
            elif key.endswith("ip") or key.endswith("port"):
                record[key] = "192.168.1.1" if key.endswith("ip") else "80"
        return record
        # records.append(record)

        # return records

    # def generate_records_with_timestamps(self, num_records, timestamp_str):
    #     yield

    # def write_records_to_file(self, records, output_file):
    #     with open(output_file, "w") as json_file:
    #         for record in records:
    #             json.dump(record, json_file, separators=(',', ':'))
    #             json_file.write('\n')


# @click.group()
# @click.version_option(version='1.0.0', prog_name='data_generator')
# def cli():
#     click.echo("Data Generator CLI")

# @click.command()
# @click.option("--num-records", default=5, help="Number of records to generate")
# @click.option("--output-file", default="generated_data.json", help="Output file to write generated records")
# def generate_data(num_records: int, output_file: str):
#     data_generator = DataGenerator()
#     for i in range(num_records):
#         record = data_generator.generate_records(5)
#         with open(output_file, "a") as json_file:
#             json_file.write(json.dumps(record, default=str) + "\n")
#             print("Generated record count: ", i+1)

# cli.add_command(generate_data)

# if __name__ == "__main__":
#     cli()
