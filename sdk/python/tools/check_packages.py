"""打包约束检查：CI 与发布流水线共用，只检查实际构建出的发布物，只用标准库。

用法::

    python tools/check_packages.py dist [--expect-version X.Y.Z]

断言：
1. 每个包恰好有一个 wheel 和一个 sdist，wheel 标签为 ``py3-none-any``（纯 Python），
   并随附许可证文件（``.dist-info/licenses/LICENSE``）；
2. dpe-hash 没有任何运行时依赖（内核直接依赖它）；
3. dpe-sdk 精确依赖同版本的 dpe-hash，两包版本相同；
4. 给出 ``--expect-version`` 时（发布），版本等于标签版本且不是开发版；
   版本一律按 PEP 440 规范化后比较（``0.1.4-dev`` 与 ``0.1.4.dev0`` 相同），
   不依赖构建后端是否规范化 METADATA；
5. 静态 import 白名单：wheel 中的 ``import`` / ``from … import`` 只允许标准库、自身以及
   已声明的运行时依赖。用白名单而不是黑名单，内核、向量生成器（``scripts.gen_vectors``）
   等未声明的模块不必列名即可拦下。可选依赖（``extra == …``）只允许在函数体内惰性 import：
   模块顶层 import 会让未装该 extra 的用户在导入时失败。``importlib.import_module`` 等
   动态 import 不在检查范围内，
   由评审把关；依赖名按 ``-``/``.`` → ``_`` 映射到 import 名，
   发布名与 import 名不同的依赖需另行处理。
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
import zipfile
from dataclasses import dataclass, field
from email.parser import Parser
from pathlib import Path

# 发布名 → import 名
PACKAGES = {"dpe-hash": "dpe_hash", "dpe-sdk": "dpe_sdk"}

_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_PIN = re.compile(r"^\s*[A-Za-z0-9._-]+\s*==\s*([^\s;,]+)\s*$")
# 本仓库 bump-my-version 只产出 X.Y.Z 与 X.Y.Z-dev 两态，对应的 PEP 440 写法见 _canon_version
_VERSION = re.compile(r"^(\d+\.\d+\.\d+)(?:[-_.]?dev(\d*))?$", re.IGNORECASE)


@dataclass
class Wheel:
    path: Path
    version: str
    tags: list[str]
    requires: list[str]
    has_license: bool
    sources: dict[str, str] = field(default_factory=dict)


def _canon_version(version: str) -> str:
    """把 ``X.Y.Z`` / ``X.Y.Z-dev`` / ``X.Y.Z.dev0`` 规范化为 PEP 440 形式；其他写法原样返回。"""
    match = _VERSION.match(version.strip())
    if match is None:
        return version.strip()
    base, dev = match.groups()
    return base if dev is None else f"{base}.dev{int(dev or 0)}"


def _import_name(requirement: str) -> str:
    match = _REQ_NAME.match(requirement)
    if match is None:
        raise ValueError(f"无法解析依赖声明：{requirement!r}")
    return re.sub(r"[-.]+", "_", match.group(1)).lower()


def _read_wheel(path: Path) -> Wheel:
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        dist_info = next(n.split("/")[0] for n in names if n.split("/")[0].endswith(".dist-info"))
        meta = Parser().parsestr(zf.read(f"{dist_info}/METADATA").decode("utf-8"))
        wheel_meta = Parser().parsestr(zf.read(f"{dist_info}/WHEEL").decode("utf-8"))
        sources = {n: zf.read(n).decode("utf-8") for n in names if n.endswith(".py")}
    return Wheel(
        path=path,
        version=meta["Version"],
        tags=wheel_meta.get_all("Tag") or [],
        requires=meta.get_all("Requires-Dist") or [],
        has_license=f"{dist_info}/licenses/LICENSE" in names,
        sources=sources,
    )


def _import_roots(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Import):
        return {alias.name.split(".")[0] for alias in node.names}
    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        return {node.module.split(".")[0]}
    return set()


def _is_type_checking(test: ast.expr) -> bool:
    """``if TYPE_CHECKING:`` 或 ``if typing.TYPE_CHECKING:``。"""
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _imported_roots(source: str, filename: str) -> tuple[set[str], set[str]]:
    """返回 (运行期模块级 import 的根, 惰性 import 的根：函数体内或 ``if TYPE_CHECKING:`` 下)。

    ``if TYPE_CHECKING:`` 的体只在类型检查器里存在，运行期不会执行，因此不算模块顶层 import
    （可选依赖常这样声明）；它的 ``else`` 分支照常按运行期处理。
    """
    eager: set[str] = set()
    lazy: set[str] = set()

    def visit(node: ast.AST, in_lazy_scope: bool) -> None:
        (lazy if in_lazy_scope else eager).update(_import_roots(node))
        nested = in_lazy_scope or isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            for branch in node.body:
                visit(branch, True)
            for branch in node.orelse:
                visit(branch, nested)
            return
        for child in ast.iter_child_nodes(node):
            visit(child, nested)

    visit(ast.parse(source, filename=filename), False)
    return eager, lazy


def check(dist: Path, expect_version: str | None) -> list[str]:
    errors: list[str] = []
    wheels: dict[str, Wheel] = {}

    for dist_name, import_name in PACKAGES.items():
        found = sorted(dist.glob(f"{import_name}-*.whl"))
        sdists = sorted(dist.glob(f"{import_name}-*.tar.gz"))
        if len(found) != 1 or len(sdists) != 1:
            errors.append(
                f"{dist_name}：需要恰好一个 wheel 和一个 sdist，"
                f"实际 wheel {len(found)} 个、sdist {len(sdists)} 个"
            )
            continue
        wheel = _read_wheel(found[0])
        wheels[dist_name] = wheel

        if wheel.tags != ["py3-none-any"]:
            errors.append(f"{dist_name}：wheel 必须是纯 Python（py3-none-any），实际 {wheel.tags}")
        if not wheel.has_license:
            errors.append(f"{dist_name}：wheel 缺少许可证文件 licenses/LICENSE")

        runtime = [r for r in wheel.requires if "extra ==" not in r]
        allowed = set(sys.stdlib_module_names) | {import_name}
        allowed |= {_import_name(r) for r in runtime}
        optional = {_import_name(r) for r in wheel.requires if "extra ==" in r}
        for filename, source in wheel.sources.items():
            eager, lazy = _imported_roots(source, filename)
            for root in sorted((eager - allowed) | (lazy - allowed - optional)):
                if root in optional:
                    errors.append(
                        f"{dist_name}：{filename} 在模块顶层 import 了可选依赖 {root!r}"
                        "（只能在函数体内惰性 import）"
                    )
                else:
                    errors.append(f"{dist_name}：{filename} import 了未声明的模块 {root!r}")

    hash_wheel = wheels.get("dpe-hash")
    sdk_wheel = wheels.get("dpe-sdk")
    if hash_wheel is not None and hash_wheel.requires:
        errors.append(f"dpe-hash：必须零运行时依赖，实际 {hash_wheel.requires}")
    if hash_wheel is not None and sdk_wheel is not None:
        if _canon_version(hash_wheel.version) != _canon_version(sdk_wheel.version):
            errors.append(
                f"两包版本不一致：dpe-hash {hash_wheel.version}，dpe-sdk {sdk_wheel.version}"
            )
        hash_reqs = [r for r in sdk_wheel.requires if _import_name(r) == "dpe_hash"]
        pins = [m.group(1) for r in hash_reqs if (m := _PIN.match(r))]
        if (
            len(hash_reqs) != 1
            or len(pins) != 1
            or (_canon_version(pins[0]) != _canon_version(hash_wheel.version))
        ):
            errors.append(f"dpe-sdk：必须精确依赖 dpe-hash=={hash_wheel.version}，实际 {hash_reqs}")

    if expect_version is not None:
        for dist_name, wheel in wheels.items():
            if _canon_version(wheel.version) != _canon_version(expect_version):
                errors.append(
                    f"{dist_name}：版本 {wheel.version} 与发布标签 {expect_version} 不一致"
                )
            if ".dev" in _canon_version(wheel.version):
                errors.append(f"{dist_name}：开发版 {wheel.version} 不得发布")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("dist", type=Path, help="uv build 的输出目录")
    parser.add_argument("--expect-version", help="发布标签对应的版本（py-v 之后的部分）")
    args = parser.parse_args()

    errors = check(args.dist, args.expect_version)
    if errors:
        for err in errors:
            print(f"FAIL {err}", file=sys.stderr)
        return 1
    print(f"OK   {', '.join(PACKAGES)} 打包约束全部满足")
    return 0


if __name__ == "__main__":
    sys.exit(main())
