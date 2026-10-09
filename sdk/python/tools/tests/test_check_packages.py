"""check_packages 的拦截能力：在 tmp_path 里拼最小发布物，逐条违反约束，确认检查器真会报错。"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from check_packages import check

WORKSPACE = Path(__file__).resolve().parents[2]


def _wheel(
    dist: Path,
    import_name: str,
    version: str,
    *,
    requires: tuple[str, ...] = (),
    source: str = '"""骨架。"""\n',
    tag: str = "py3-none-any",
    license_file: bool = True,
) -> None:
    dist_info = f"{import_name}-{version}.dist-info"
    metadata = f"Metadata-Version: 2.4\nName: {import_name}\nVersion: {version}\n"
    metadata += "".join(f"Requires-Dist: {r}\n" for r in requires)
    path = dist / f"{import_name}-{version}-{tag}.whl"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{dist_info}/METADATA", metadata)
        zf.writestr(f"{dist_info}/WHEEL", f"Wheel-Version: 1.0\nTag: {tag}\n")
        if license_file:
            zf.writestr(f"{dist_info}/licenses/LICENSE", "MIT License\n")
        zf.writestr(f"{import_name}/__init__.py", source)
    (dist / f"{import_name}-{version}.tar.gz").write_bytes(b"")


def _dist(
    tmp_path: Path,
    *,
    version: str = "0.1.4-dev",
    sdk_version: str | None = None,
    hash_requires: tuple[str, ...] = (),
    sdk_requires: tuple[str, ...] | None = None,
    hash_source: str = '"""骨架。"""\n',
    sdk_source: str = '"""骨架。"""\n',
    tag: str = "py3-none-any",
    license_file: bool = True,
) -> Path:
    sdk_version = sdk_version or version
    if sdk_requires is None:
        sdk_requires = (f"dpe-hash=={version}", "pydantic<3,>=2.7", "httpx<1,>=0.27")
    _wheel(
        tmp_path,
        "dpe_hash",
        version,
        requires=hash_requires,
        source=hash_source,
        tag=tag,
        license_file=license_file,
    )
    _wheel(tmp_path, "dpe_sdk", sdk_version, requires=sdk_requires, source=sdk_source)
    return tmp_path


def _assert_fails(errors: list[str], fragment: str) -> None:
    assert any(fragment in e for e in errors), errors


def test_valid_dev_build_passes(tmp_path: Path) -> None:
    assert check(_dist(tmp_path), None) == []


def test_valid_release_passes(tmp_path: Path) -> None:
    assert check(_dist(tmp_path, version="0.1.4"), "0.1.4") == []


def test_versions_compared_after_pep440_normalization(tmp_path: Path) -> None:
    dist = _dist(tmp_path, version="0.1.4.dev0", sdk_requires=("dpe-hash==0.1.4-dev",))
    assert check(dist, None) == []


def test_allows_stdlib_relative_and_declared_imports(tmp_path: Path) -> None:
    sdk_source = "import json\nfrom . import x\nimport pydantic\nfrom httpx import Client\n"
    assert check(_dist(tmp_path, sdk_source=sdk_source), None) == []


def test_rejects_runtime_dependency_of_dpe_hash(tmp_path: Path) -> None:
    errors = check(_dist(tmp_path, hash_requires=("requests",)), None)
    _assert_fails(errors, "dpe-hash：必须零运行时依赖")


@pytest.mark.parametrize(
    ("source", "module"),
    [
        ("import scripts.gen_vectors\n", "scripts"),
        ("from tfrobot.core import x\n", "tfrobot"),
        ("try:\n    import pydantic\nexcept ImportError:\n    pass\n", "pydantic"),
        ("if True:\n    from httpx import Client\n", "httpx"),
    ],
)
def test_rejects_undeclared_import_in_dpe_hash(tmp_path: Path, source: str, module: str) -> None:
    errors = check(_dist(tmp_path, hash_source=source), None)
    _assert_fails(errors, f"import 了未声明的模块 {module!r}")


def test_rejects_top_level_import_of_extra_only_dependency(tmp_path: Path) -> None:
    requires = ("dpe-hash==0.1.4-dev", 'rich; extra == "cli"')
    errors = check(_dist(tmp_path, sdk_requires=requires, sdk_source="import rich\n"), None)
    _assert_fails(errors, "在模块顶层 import 了可选依赖 'rich'")


def test_allows_lazy_import_of_extra_only_dependency(tmp_path: Path) -> None:
    requires = ("dpe-hash==0.1.4-dev", 'rich; extra == "cli"')
    source = "def main() -> None:\n    import rich\n    from rich import console\n"
    assert check(_dist(tmp_path, sdk_requires=requires, sdk_source=source), None) == []


def test_rejects_lazy_import_of_undeclared_module(tmp_path: Path) -> None:
    requires = ("dpe-hash==0.1.4-dev", 'rich; extra == "cli"')
    source = "def main() -> None:\n    import tfrobot\n"
    errors = check(_dist(tmp_path, sdk_requires=requires, sdk_source=source), None)
    _assert_fails(errors, "import 了未声明的模块 'tfrobot'")


def test_rejects_version_mismatch(tmp_path: Path) -> None:
    errors = check(_dist(tmp_path, sdk_version="0.1.5-dev"), None)
    _assert_fails(errors, "两包版本不一致")


@pytest.mark.parametrize(
    "requires",
    [
        ("pydantic<3,>=2.7",),
        ("dpe-hash>=0.1.4.dev0",),
        ("dpe-hash==0.1.3",),
        ("dpe-hash==0.1.4-dev", "dpe-hash<1"),
    ],
)
def test_rejects_missing_or_loose_pin(tmp_path: Path, requires: tuple[str, ...]) -> None:
    errors = check(_dist(tmp_path, sdk_requires=requires), None)
    _assert_fails(errors, "dpe-sdk：必须精确依赖 dpe-hash==")


def test_rejects_release_tag_mismatch(tmp_path: Path) -> None:
    errors = check(_dist(tmp_path, version="0.1.4"), "0.1.5")
    _assert_fails(errors, "与发布标签 0.1.5 不一致")


def test_rejects_publishing_dev_version(tmp_path: Path) -> None:
    errors = check(_dist(tmp_path), "0.1.4-dev")
    _assert_fails(errors, "开发版 0.1.4-dev 不得发布")


def test_rejects_non_pure_wheel(tmp_path: Path) -> None:
    errors = check(_dist(tmp_path, tag="cp311-cp311-linux_x86_64"), None)
    _assert_fails(errors, "wheel 必须是纯 Python")


def test_rejects_missing_license(tmp_path: Path) -> None:
    errors = check(_dist(tmp_path, license_file=False), None)
    _assert_fails(errors, "缺少许可证文件")


def test_rejects_missing_artifacts(tmp_path: Path) -> None:
    errors = check(tmp_path, None)
    _assert_fails(errors, "需要恰好一个 wheel 和一个 sdist")


@pytest.mark.parametrize("package", ["dpe-hash", "dpe-sdk"])
def test_package_license_matches_repository(package: str) -> None:
    repository_license = (WORKSPACE.parents[1] / "LICENSE").read_text(encoding="utf-8")
    package_license = (WORKSPACE / "packages" / package / "LICENSE").read_text(encoding="utf-8")
    assert package_license == repository_license
