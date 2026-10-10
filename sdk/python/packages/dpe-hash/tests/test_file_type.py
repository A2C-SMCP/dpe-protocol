"""file_type 的语法校验与推荐登记表（core.md §2.5）：开放取值，语法是唯一约束。"""

from __future__ import annotations

import pytest
from dpe_hash import (
    RECOMMENDED_FILE_TYPES,
    FileTypeInvalidError,
    ValidationError,
    doc_hash,
    is_valid_file_type,
    validate_file_type,
)

#: 恰 32 字符（长度上界，界内合法）；33 字符的版本在拒绝用例里。
MAX_LENGTH = "max_length_value_0123456789abcde"


@pytest.mark.parametrize(
    "value",
    ["md", "a", "a1", "a_b_1", "3mf", "markdown", "custom_format_v2", MAX_LENGTH],
)
def test_syntax_accepts(value: str) -> None:
    assert is_valid_file_type(value)
    validate_file_type(value)  # 不抛异常


@pytest.mark.parametrize(
    "value",
    [
        "",  # 空串
        "Markdown",  # 大写
        "git-repo",  # 连字符
        "_internal",  # 首字符是下划线
        MAX_LENGTH + "f",  # 33 字符，超过长度上界
        "md ",  # 尾随空格
        "md\n",  # 尾随换行：全串匹配，不得按行尾锚点放过
        "märkdown",  # 非 ASCII
        "md.",  # 点不在字符集内
    ],
)
def test_syntax_rejects(value: str) -> None:
    assert not is_valid_file_type(value)
    with pytest.raises(FileTypeInvalidError) as info:
        validate_file_type(value)
    assert info.value.code == "DPE_VALIDATION"


@pytest.mark.parametrize("value", [None, 123, b"md", ["md"]])
def test_non_string_rejected(value: object) -> None:
    assert not is_valid_file_type(value)
    with pytest.raises(FileTypeInvalidError):
        validate_file_type(value)


def test_error_is_a_validation_error() -> None:
    """入口异常是 ``ValidationError`` 的子类：既有按基类捕获的调用方不受影响。"""
    with pytest.raises(ValidationError):
        validate_file_type("Markdown")


def test_recommended_table_is_syntactically_valid() -> None:
    """推荐表只是推荐：表内每个取值都必须语法合法，否则表本身自相矛盾。"""
    assert RECOMMENDED_FILE_TYPES
    for value in RECOMMENDED_FILE_TYPES:
        assert is_valid_file_type(value), value


def test_git_repo_registered_and_legacy_repos_kept() -> None:
    """#95：新增 `git_repo`；`*_repo` 历史取值保留（不影响已存数据），新文档改用 `git_repo`。"""
    assert "git_repo" in RECOMMENDED_FILE_TYPES
    assert "java_repo" in RECOMMENDED_FILE_TYPES
    assert "python_repo" in RECOMMENDED_FILE_TYPES
    assert "javascript_repo" in RECOMMENDED_FILE_TYPES
    assert "typescript_repo" in RECOMMENDED_FILE_TYPES


def test_hash_accepts_unregistered_value() -> None:
    """未登记但语法合法的取值照常进 doc_hash（接收方 MUST NOT 拒收）。"""
    registered = doc_hash({"file_type": "md"}, [])
    unregistered = doc_hash({"file_type": "custom_format_v2"}, [])
    assert registered != unregistered


def test_hash_rejects_invalid_syntax() -> None:
    with pytest.raises(FileTypeInvalidError) as info:
        doc_hash({"file_type": "Markdown"}, [])
    assert info.value.code == "DPE_VALIDATION"
    assert info.value.path == "/file_type"
