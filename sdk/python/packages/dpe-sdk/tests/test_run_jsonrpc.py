"""线协议编解码（connector 契约 §6.2–§6.3），不启动进程。"""

from __future__ import annotations

import json
from typing import Any

import pytest
from dpe_sdk.run import ProtocolFailure, RpcError
from dpe_sdk.run.errors import rpc_error_name
from dpe_sdk.run.jsonrpc import (
    LineDecoder,
    encode_notification,
    encode_request,
    parse_initialize_result,
    parse_response,
)


def test_encode_is_compact_single_line() -> None:
    data = encode_request(1, "scan", {"cursor": None, "text": "a\nb 中"}, 1024)
    assert data.endswith(b"\n") and data.count(b"\n") == 1
    assert json.loads(data) == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "scan",
        "params": {"cursor": None, "text": "a\nb 中"},
    }
    assert b" " not in data.replace("b 中".encode(), b"")
    assert json.loads(encode_notification("cancel", {"id": 3}, 1024)) == {
        "jsonrpc": "2.0",
        "method": "cancel",
        "params": {"id": 3},
    }


def test_encode_rejects_oversize() -> None:
    with pytest.raises(ValueError):
        encode_request(1, "scan", {"pad": "x" * 100}, 50)


def test_decoder_splits_across_chunks() -> None:
    decoder = LineDecoder(64)
    assert decoder.feed(b'{"a":1}\n{"b"') == [b'{"a":1}']
    assert decoder.pending == 4
    assert decoder.feed(b":2}\n") == [b'{"b":2}']
    assert decoder.pending == 0


def test_decoder_long_line_in_many_chunks() -> None:
    decoder = LineDecoder(1 << 20)
    body = b'{"pad":"' + b"x" * 200_000 + b'"}'
    for i in range(0, len(body), 4096):
        assert decoder.feed(body[i : i + 4096]) == []
    assert decoder.feed(b"\n{}\n") == [body, b"{}"]
    assert decoder.pending == 0


def test_decoder_limit_counts_trailing_lf() -> None:
    assert LineDecoder(8).feed(b"1234567\n") == [b"1234567"]
    with pytest.raises(ProtocolFailure):
        LineDecoder(8).feed(b"12345678\n")
    # 未见 LF 时也不无限缓冲
    with pytest.raises(ProtocolFailure):
        LineDecoder(8).feed(b"12345678")


def test_decoder_rejects_empty_line() -> None:
    with pytest.raises(ProtocolFailure):
        LineDecoder(64).feed(b'{"a":1}\n\n')


def response(**members: Any) -> bytes:
    return json.dumps({"jsonrpc": "2.0", **members}).encode()


def test_parse_result_and_error() -> None:
    ok = parse_response(response(id=3, result={"items": []}))
    assert (ok.id, ok.result, ok.error) == (3, {"items": []}, None)
    err = parse_response(
        response(id=4, error={"code": -32003, "message": "m", "data": {"retryable": True}})
    )
    assert err.error == (-32003, "m", {"retryable": True})


@pytest.mark.parametrize(
    "line",
    [
        b"not json",
        b"[]",
        b'[{"jsonrpc":"2.0","id":1,"result":{}}]',
        b"1",
        b'{"jsonrpc":"2.0","id":1,"result":{},"result":{}}',  # 重复键
        b'{"jsonrpc":"2.0","id":1,"result":"\\ud800"}',  # 孤立代理项
        b'{"jsonrpc":"2.0","id":1,"result":NaN}',
        b'\xef\xbb\xbf{"jsonrpc":"2.0","id":1,"result":{}}',  # BOM
        response(id=1),
        response(id=1, result={}, error={"code": 1, "message": ""}),
        response(id="1", result={}),
        response(id=True, result={}),
        response(id=None, error={"code": -32700, "message": "parse"}),
        response(id=1, error="boom"),
        response(id=1, error={"code": "x", "message": "m"}),
        response(id=1, error={"code": -32000}),
        response(method="log", params={}),  # 插件主动通知
        response(id=1, method="scan", params={}),  # 插件主动请求
        json.dumps({"jsonrpc": "1.0", "id": 1, "result": {}}).encode(),
    ],
)
def test_parse_rejects(line: bytes) -> None:
    with pytest.raises(ProtocolFailure) as info:
        parse_response(line)
    assert (info.value.level, info.value.code) == ("protocol", "protocol_error")


def test_initialize_result() -> None:
    init = parse_initialize_result(
        {"protocol_version": "dpe-connector/1", "plugin": {"name": "n", "version": "v"}}
    )
    assert init.capabilities.supports_cursor is False
    for bad in (
        {"plugin": {"name": "n", "version": "v"}},
        {"protocol_version": "p", "plugin": {"name": "", "version": "v"}},
        {"protocol_version": "p", "plugin": {"name": "n", "version": "v", "x": 1}},
        {"protocol_version": "p", "plugin": {"name": "n", "version": "v"}, "extra": 1},
        {"protocol_version": "p", "plugin": {"name": "n", "version": "v"}, "capabilities": None},
        {
            "protocol_version": "p",
            "plugin": {"name": "n", "version": "v"},
            "capabilities": {"supports_cursor": "yes"},
        },
        None,
    ):
        with pytest.raises(ProtocolFailure):
            parse_initialize_result(bad)


@pytest.mark.parametrize(
    ("code", "name"),
    [
        (-32700, "parse_error"),
        (-32600, "invalid_request"),
        (-32601, "method_not_found"),
        (-32602, "invalid_params"),
        (-32603, "internal_error"),
        (-32001, "cancelled"),
        (-32002, "unknown_handle"),
        (-32003, "source_failed"),
        (-32004, "version_unsupported"),
        (-32005, "invalid_config"),
        (-32099, "unknown_error"),
    ],
)
def test_error_names(code: int, name: str) -> None:
    assert rpc_error_name(code) == name


def test_rpc_error_retryable() -> None:
    assert RpcError(-32003, "m", {"retryable": True}).retryable is True
    assert RpcError(-32003, "m", {"retryable": "yes"}).retryable is False
    assert RpcError(-32003, "m").retryable is False
    # 未知错误码一律不可重试（§6.5）
    assert RpcError(-32099, "m", {"retryable": True}).retryable is False
