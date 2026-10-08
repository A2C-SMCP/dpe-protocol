"""协议错误（core §6）：错误码全集、retryable 与 dpe_hash 错误的转换。"""

from __future__ import annotations

from typing import Any

import dpe_hash
import pytest
from dpe_sdk import errors
from dpe_sdk.wire import Missing

#: core §6 错误码表：code → retryable
CORE_ERROR_TABLE = {
    "DPE_VALIDATION": False,
    "DPE_CONTRACT_UNSUPPORTED": False,
    "DPE_CATEGORY_UNKNOWN": False,
    "DPE_PRECONDITION_REQUIRED": False,
    "DPE_PRECONDITION_FAILED": False,
    "DPE_ALREADY_EXISTS": False,
    "DPE_NOT_FOUND": False,
    "DPE_SESSION_EXPIRED": False,
    "DPE_HASH_MISMATCH": False,
    "DPE_MISSING_CONTENT": False,
    "DPE_PAYLOAD_TOO_LARGE": False,
    "DPE_FORBIDDEN": False,
    "DPE_RATE_LIMITED": True,
    "DPE_UNAVAILABLE": True,
}


def test_every_core_code_has_a_class_with_spec_retryable() -> None:
    for code, retryable in CORE_ERROR_TABLE.items():
        cls = errors.error_class(code)
        assert cls is not None, code
        assert issubclass(cls, errors.DpeError)
        assert cls.code == code
        assert cls.retryable is retryable, code


def test_registry_has_no_codes_beyond_core() -> None:
    exported = {
        getattr(errors, name).code
        for name in errors.__all__
        if isinstance(getattr(errors, name), type)
        and issubclass(getattr(errors, name), errors.DpeError)
        and getattr(errors, name) not in (errors.DpeError, errors.UnknownCodeError)
    }
    assert exported == set(CORE_ERROR_TABLE)
    assert errors.error_class("DPE_SOMETHING_ELSE") is None


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (dpe_hash.ValidationError("bad", "/a"), errors.ValidationError),
        (dpe_hash.UndefinedFieldError("extra", "/x"), errors.ValidationError),
        (dpe_hash.CategoryUnknownError("cat", "/category"), errors.CategoryUnknownError),
        (dpe_hash.ContractUnsupportedError("c", "/pages/0"), errors.ContractUnsupportedError),
    ],
)
def test_from_hash_error_keeps_code_and_path(
    exc: dpe_hash.DpeHashError, expected: type[errors.DpeError]
) -> None:
    converted = errors.from_hash_error(exc)
    assert type(converted) is expected
    assert converted.code == exc.code
    assert converted.path == exc.path
    assert converted.message == exc.message


def test_missing_content_carries_list() -> None:
    missing = Missing(pages=["dpe1:" + "0" * 64])
    err = errors.MissingContentError(missing=missing, truncated=True)
    assert err.missing == missing
    assert err.truncated is True
    assert err.code == "DPE_MISSING_CONTENT"


def test_retry_after() -> None:
    err = errors.RateLimitedError("slow down", retry_after=1.5)
    assert err.path == ""
    assert err.retry_after == 1.5
    assert err.retryable is True
    assert errors.UnavailableError().retry_after is None


def test_str_follows_message_and_path() -> None:
    err = errors.from_hash_error(dpe_hash.CategoryUnknownError("未知", "/category"), "/objects/3")
    assert err.path == "/objects/3/category"
    assert str(err) == "未知（位置 /objects/3/category）"
    err.path = ""
    assert str(err) == "未知"
    assert str(errors.ForbiddenError()) == "DPE_FORBIDDEN"


def test_uniform_constructor_signature() -> None:
    """每个错误类都能以 (message, path) 构造，各类自有参数只走关键字。"""
    for code in CORE_ERROR_TABLE:
        cls = errors.error_class(code)
        assert cls is not None
        err = cls("m", "/p")
        assert (err.code, err.message, err.path) == (code, "m", "/p")
    assert errors.MissingContentError().missing == Missing()
    unknown = errors.UnknownCodeError("m", "/p", code="DPE_X")
    assert (unknown.code, unknown.message, unknown.path) == ("DPE_X", "m", "/p")
    assert str(errors.DpeError()) == "DpeError"  # 基类没有 code 时 str 也不失败


def test_from_hash_error_unknown_code() -> None:
    class Future(dpe_hash.DpeHashError):
        code = "DPE_FUTURE"

    err = errors.from_hash_error(Future("x", "/a"), "/document")
    assert isinstance(err, errors.UnknownCodeError)
    assert (err.code, err.path, err.retryable) == ("DPE_FUTURE", "/document/a", False)


def test_from_problem_dispatches_on_code_not_status() -> None:
    err = errors.from_problem(
        {
            "type": "urn:dpe:error:not-found",
            "status": 412,
            "code": "DPE_NOT_FOUND",
            "detail": "gone",
            "retryable": False,
        }
    )
    assert type(err) is errors.NotFoundError
    assert err.message == "gone"


def test_from_problem_missing_content() -> None:
    h = "dpe1:" + "1" * 64
    err = errors.from_problem(
        {"code": "DPE_MISSING_CONTENT", "missing": {"pages": [h]}, "missing_truncated": True}
    )
    assert isinstance(err, errors.MissingContentError)
    assert err.missing.pages == [h] and err.missing.blobs == []
    assert err.truncated is True
    bare = errors.from_problem({"code": "DPE_MISSING_CONTENT"})
    assert isinstance(bare, errors.MissingContentError) and bare.missing == Missing()
    assert bare.truncated is True  # 缺省清单不可信，不等于「什么都不缺」
    empty = errors.from_problem({"code": "DPE_MISSING_CONTENT", "missing": {}})
    assert isinstance(empty, errors.MissingContentError) and empty.truncated is True


def test_from_problem_retry_after_and_unknown() -> None:
    err = errors.from_problem({"code": "DPE_UNAVAILABLE", "title": "busy"}, retry_after=3)
    assert isinstance(err, errors.UnavailableError)
    assert (err.retry_after, err.message, err.retryable) == (3, "busy", True)
    unknown = errors.from_problem({"code": "DPE_NEW", "retryable": True, "detail": "d"})
    assert isinstance(unknown, errors.UnknownCodeError)
    assert (unknown.code, unknown.retryable, unknown.message) == ("DPE_NEW", True, "d")
    not_dpe_bodies: list[Any] = [{"title": "not dpe"}, {"code": ""}, {"code": 1}, [], None, "x"]
    for not_dpe in not_dpe_bodies:
        with pytest.raises(ValueError):
            errors.from_problem(not_dpe)  # 运行时输入来自 json.loads，未必是对象


@pytest.mark.parametrize(
    "raw",
    [["dpe1:" + "1" * 64], "x", 1, {"pages": None}, {"pages": [1]}, {"blobs": "sha256:x"}],
)
def test_from_problem_malformed_missing_keeps_code(raw: object) -> None:
    err = errors.from_problem({"code": "DPE_MISSING_CONTENT", "missing": raw})
    assert isinstance(err, errors.MissingContentError)
    assert err.missing == Missing()
    assert err.truncated is True  # 清单不可信


@pytest.mark.parametrize(
    ("problem", "message"),
    [
        ({"code": "DPE_FORBIDDEN", "detail": 123, "title": "forbidden"}, "forbidden"),
        ({"code": "DPE_FORBIDDEN", "detail": {}, "title": None}, ""),
        ({"code": "DPE_FORBIDDEN", "detail": "", "title": "t"}, "t"),
    ],
)
def test_from_problem_message_falls_back_to_string_title(
    problem: dict[str, object], message: str
) -> None:
    assert errors.from_problem(problem).message == message


def test_from_problem_ignores_declared_retryable_for_known_codes() -> None:
    assert errors.from_problem({"code": "DPE_VALIDATION", "retryable": True}).retryable is False
    for flag in ("true", 1):
        err = errors.from_problem({"code": "DPE_NEW", "retryable": flag})
        assert err.retryable is False
    err = errors.from_problem({"code": "DPE_NEW", "retryable": True}, retry_after=2)
    assert (err.retryable, err.retry_after) == (True, 2)
    truncated = errors.from_problem(
        {
            "code": "DPE_MISSING_CONTENT",
            "missing": {"blobs": ["sha256:" + "2" * 64]},
            "missing_truncated": "yes",  # 不是布尔：不采信
        }
    )
    assert isinstance(truncated, errors.MissingContentError) and truncated.truncated is False
