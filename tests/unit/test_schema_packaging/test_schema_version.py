import os
import pytest
from pathlib import Path


def test_derived_schema_version():
    """Test that derived schema version is properly set in metadata.txt"""
    repo_root = Path(__file__).parent.parent.parent.parent
    metadata_path = repo_root / "tests/resources/post_build_artefacts/dfe_derived_schemas/metadata.txt"

    assert metadata_path.exists(), f"metadata.txt not found at {metadata_path}"
    with open(metadata_path, "r") as f:
        content = f.read().strip()

    assert "DERIVED_SCHEMA_RELEASE_VERSION=" in content, (
        "Version variable not found in metadata.txt"
    )
    version = content.split("=")[1].strip()

    version_parts = version.split(".")
    assert len(version_parts) == 3, (
        "Version should have major, minor, and patch numbers"
    )
    for part in version_parts:
        assert part.isdigit(), "Version numbers should be digits"


def test_schema_directory_structure():
    """Test that schema directory structure follows the versioning pattern"""
    repo_root = Path(__file__).parent.parent.parent.parent
    base_dir = repo_root / "tests/resources/post_build_artefacts/dfe_derived_schemas/logs_beats_filebeat"

    if not base_dir.exists():
        pytest.skip(f"Schema directory not found: {base_dir}")
    
    schema_dirs = [
        d.name
        for d in base_dir.iterdir() 
        if d.name.startswith("logs_beats_filebeat_")
        and d.is_dir()
    ]

    assert len(schema_dirs) > 0, "No schema directories found"

    for schema_dir_name in schema_dirs:
        schema_dir = os.path.join(base_dir, schema_dir_name)

        version_dirs = [
            d
            for d in os.listdir(schema_dir)
            if os.path.isdir(os.path.join(schema_dir, d))
        ]

        assert len(version_dirs) > 0, (
            f"No version directories found in {schema_dir_name}"
        )

        for version_dir_name in version_dirs:
            assert version_dir_name.startswith("v"), (
                f"Version directory should start with 'v': {version_dir_name}"
            )

            version_numbers = version_dir_name[1:].split("_")
            assert len(version_numbers) == 3, (
                f"Version should have three parts: {version_dir_name}"
            )

            for part in version_numbers:
                assert len(part) == 3, (
                    f"Each version part should be 3 digits: {version_dir_name}"
                )
                assert part.isdigit(), (
                    f"Version parts should be numbers: {version_dir_name}"
                )

            schema_file = os.path.join(
                schema_dir, version_dir_name, f"{schema_dir_name}_sub.csv"
            )
            assert os.path.exists(schema_file), f"Schema file not found: {schema_file}"


def test_package_info_version_matches():
    """Test that PKG-INFO version matches metadata.txt version"""
    metadata_path = os.path.join(
        "tests/resources/post_build_artefacts/dfe_derived_schemas/metadata.txt",
    )
    pkginfo_path = os.path.join(
        "tests/resources/post_build_artefacts/dfe_derived_schemas/PKG-INFO",
    )

    with open(metadata_path, "r") as f:
        metadata_content = f.read().strip()
    metadata_version = metadata_content.split("=")[1].strip()

    if os.path.exists(pkginfo_path):
        with open(pkginfo_path, "r") as f:
            pkginfo_content = f.read()
        for line in pkginfo_content.split("\n"):
            if line.startswith("Version:"):
                pkginfo_version = line.split(":")[1].strip()
                assert pkginfo_version == metadata_version, (
                    "Version mismatch between metadata.txt and PKG-INFO"
                )
