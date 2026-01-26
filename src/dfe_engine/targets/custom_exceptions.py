__all__ = [
    'TargetsError',
    'TargetDeleteDefaultError',
    'TargetExistsError',
    'TargetKeyNotFoundError',
    'TargetKeyNotFoundError',
    'TargetNotFoundError',
    'TargetsFileExistsError',
    'TargetsFileNotFoundError',
    'TargetsWarning',
    'TargetAlreadyDefaultWarning',
    'TargetNoUpdateWarning',
]

class TargetsError(Exception):
    """
    Base class for targets related errors.
    """
    def __init__(self, message: str, custom_message: str = ""):
        super().__init__(f"{message} {custom_message}".strip() if custom_message else message)

class TargetDeleteDefaultError(TargetsError):
    """
    Raised when attempting to delete the default target.
    """
    def __init__(self, targets_file_path: str, target_name: str, custom_message: str = ""):
        super().__init__(f"Unable to delete target '{target_name}' from '{targets_file_path}' as it is the default target.", custom_message)

class TargetExistsError(TargetsError):
    """
    Raised when a target already exists.
    """
    def __init__(self, targets_file_path: str, target_name: str, custom_message: str = ""):
        super().__init__(f"The target '{target_name}' already exists in '{targets_file_path}'.", custom_message)

class TargetKeyNotFoundError(TargetsError):
    """
    Raised when a target does not contain a specified key.
    """
    def __init__(self, targets_file_path: str, target_name: str, target_key: str, custom_message: str = ""):
        super().__init__(f"The key '{target_key}' could not be found for target '{target_name}' in '{targets_file_path}'.", custom_message)

class TargetNotFoundError(TargetsError):
    """
    Raised when a target is not found.
    """
    def __init__(self, targets_file_path: str, target_name: str, custom_message: str = ""):
        super().__init__(f"The target '{target_name}' could not be found in '{targets_file_path}'.", custom_message)

class TargetsFileExistsError(TargetsError):
    """
    Raised when the targets file already exists.
    """
    def __init__(self, targets_file_path: str, custom_message: str = ""):
        super().__init__(f"The target file '{targets_file_path}' already exists.", custom_message)

class TargetsFileNotFoundError(TargetsError):
    """
    Raised when the targets file is not found.
    """
    def __init__(self, targets_file_path: str, custom_message: str = ""):
        super().__init__(f"The targets file '{targets_file_path}' could not be found.", custom_message)

class TargetsWarning(Exception):
    """
    Base class for targets related warnings.
    """
    def __init__(self, message: str, custom_message: str = ""):
        super().__init__(f"{message} {custom_message}".strip() if custom_message else message)

class TargetAlreadyDefaultWarning(TargetsWarning):
    """
    Raised when a target is already set as the default target.
    """
    def __init__(self, targets_file_path: str, target_name: str, custom_message: str = ""):
        super().__init__(f"The target '{target_name}' is already set as the default target in '{targets_file_path}'.", custom_message)

class TargetNoUpdateWarning(TargetsWarning):
    """
    Raised when a target already has the specified key value pair.
    """
    def __init__(self, targets_file_path: str, target_name: str, target_key: str = None, target_value: str = None, custom_message: str = ""):
        super().__init__(f"The target '{target_name}' in '{targets_file_path}' already {f"has '{target_key}' of '{target_value}'" if (target_key and target_value) else "matches the provided information"}.", custom_message)