__all__ = [
    'TargetsError',
    'TargetExistsError',
    'TargetsFileExistsError',
    'TargetsFileNotFoundError'
]

class TargetsError(Exception):
    pass

class TargetExistsError(TargetsError):
    def __init__(self, targets_file_path: str, target_name: str):
        super().__init__(f"The target '{target_name}' already exists in '{targets_file_path}'. Please try again with a different target name.")

class TargetsFileExistsError(TargetsError):
    def __init__(self, targets_file_path: str):
        super().__init__(f"The target file '{targets_file_path}' already exists. Please try again with a different file path or use `dfecli targets add` to create a new target.")

class TargetsFileNotFoundError(TargetsError):
    def __init__(self, targets_file_path: str):
        super().__init__(f"The targets file '{targets_file_path}' could not be found. Please initialise this using `dfecli targets init`.")