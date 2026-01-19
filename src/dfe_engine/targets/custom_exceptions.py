__all__ = [
    'TargetsError',
    'TargetExistsError',
    'TargetsFileNotFoundError'
]

class TargetsError(Exception):
    pass

class TargetExistsError(TargetsError):
    def __init__(self, targets_file_path: str, target_name: str):
        super().__init__(f"The target '{target_name}' already exists in '{targets_file_path}'. Please try again with a different target name.")

class TargetsFileNotFoundError(TargetsError):
    def __init__(self, targets_file_path: str):
        super().__init__(f"The targets file '{targets_file_path}' could not be found. Please initialise this using `dfecli targets init`.")