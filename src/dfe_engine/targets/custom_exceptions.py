__all__ = [
    'TargetsError',
    'TargetAlreadyDefaultError',
    'TargetDeleteDefaultError',
    'TargetExistsError',
    'TargetNotFoundError',
    'TargetsFileExistsError',
    'TargetsFileNotFoundError'
]

class TargetsError(Exception):
    def __init__(self, message: str, custom_message: str = ""):
        super().__init__(f"{message} {custom_message}".strip() if custom_message else message)

class TargetAlreadyDefaultError(TargetsError):
    def __init__(self, targets_file_path: str, target_name: str, custom_message: str = ""):
        super().__init__(f"The target '{target_name}' is already set as the default target in '{targets_file_path}'.", custom_message)

class TargetDeleteDefaultError(TargetsError):
    def __init__(self, targets_file_path: str, target_name: str, custom_message: str = ""):
        super().__init__(f"Unable to delete target '{target_name}' from '{targets_file_path}' as it is the default target.", custom_message)

class TargetExistsError(TargetsError):
    def __init__(self, targets_file_path: str, target_name: str, custom_message: str = ""):
        super().__init__(f"The target '{target_name}' already exists in '{targets_file_path}'.", custom_message)

class TargetNotFoundError(TargetsError):
    def __init__(self, targets_file_path: str, target_name: str, custom_message: str = ""):
        super().__init__(f"The target '{target_name}' could not be found in '{targets_file_path}'.", custom_message)

class TargetsFileExistsError(TargetsError):
    def __init__(self, targets_file_path: str, custom_message: str = ""):
        super().__init__(f"The target file '{targets_file_path}' already exists.", custom_message)

class TargetsFileNotFoundError(TargetsError):
    def __init__(self, targets_file_path: str, custom_message: str = ""):
        super().__init__(f"The targets file '{targets_file_path}' could not be found.", custom_message)