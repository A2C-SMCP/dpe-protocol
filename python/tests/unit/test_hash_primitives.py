from dpe_protocol.hashing import HashStrategyRef
from dpe_protocol.hashing.primitives import concat_parts, json_stable


def test_concat_parts_is_unambiguous() -> None:
    assert concat_parts([b"ab", b"c"]) != concat_parts([b"a", b"bc"])
    assert concat_parts([b"ab"]) == b"\x00\x00\x00\x02ab"


def test_json_stable_matches_kernel_format() -> None:
    assert json_stable({"b": 1, "a": "中文", "c": [1, 2]}) == '{"a": "中文", "b": 1, "c": [1, 2]}'
    # 码点序：U+FFFF 排在 U+2000B（UTF-16 码元序下顺序相反）之前
    assert json_stable({"𠀋": 1, "￿": 0}) == '{"￿": 0, "𠀋": 1}'


def test_strategy_ref_equality_ignores_param_order() -> None:
    a = HashStrategyRef.parse("hash-strategy://tabular?algo_version=v3&include_coords=true")
    b = HashStrategyRef.parse("hash-strategy://tabular?include_coords=true&algo_version=v3")
    assert a == b and a.algo_version == "v3"
