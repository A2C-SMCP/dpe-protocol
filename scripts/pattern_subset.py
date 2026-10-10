#!/usr/bin/env python3
"""connector 契约 §4.1.1 `config_schema` 正则子集的规范参考实现（仅标准库）。

- 合法性只由文法与结构上界决定（`check_pattern`），与本地正则引擎无关；
- 供生成器与 SDK 对照消费：`search` 先把 pattern 转译为**显式码点区间**再匹配
  （`translate_pattern` 输出 Python 语法形式），不依赖引擎的字符类语义或标志；
- 该模块是规范参考实现，只被 `scripts/gen_vectors.py` 与测试使用；
  SDK 与各运行器 MUST 自行实现，不 import 本模块（CLAUDE.md：SDK 只消费向量）。

用法：

    from pattern_subset import check_pattern, translate_pattern, search, PatternOutsideSubset
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Union

__all__ = [
    "MAX_EXPANSION",
    "MAX_LENGTH",
    "MAX_NESTING",
    "PatternOutsideSubset",
    "check_pattern",
    "expand_size",
    "parse_pattern",
    "search",
    "translate_pattern",
]

#: §4.1.1：pattern 长度上限（Unicode 标量值个数）
MAX_LENGTH = 1024
#: §4.1.1：展开规模上限
MAX_EXPANSION = 4096
#: §4.1.1：分组嵌套深度上限（括号嵌套层数；最外层分组计 1）
MAX_NESTING = 64

#: 简写类 `\d` `\w` `\s` 的码点区间（§4.1.1 语义表；ASCII）
_SHORTHAND_RANGES: dict[str, tuple[tuple[int, int], ...]] = {
    "d": ((0x30, 0x39),),
    "w": ((0x30, 0x39), (0x41, 0x5A), (0x5F, 0x5F), (0x61, 0x7A)),
    "s": ((0x09, 0x0D), (0x20, 0x20)),
}

#: 可转义的元字符（§4.1.1 escape 产生式，另加 `\$` 与 `\n` `\r` `\t` 与 `\xHH`）
_ESCAPABLE = frozenset("()*+-.?[\\]^{|}$")
_CONTROL_ESCAPES = {"n": "\n", "r": "\r", "t": "\t"}

#: 元字符（字面使用 MUST 转义）
_METACHARS = frozenset("\\.^$|?*+()[]{}")

_HEX = frozenset("0123456789abcdefABCDEF")


class PatternOutsideSubset(ValueError):
    """pattern 不属于 §4.1.1 的子集。``reason`` 是机器可读的原因标记，``position`` 是违例位置。"""

    def __init__(self, reason: str, message: str, position: int = -1) -> None:
        super().__init__(
            f"{message}（{reason}，位置 {position}）"
            if position >= 0
            else f"{message}（{reason}）"
        )
        self.reason = reason
        self.message = message
        self.position = position


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------


@dataclass
class _Alt:
    branches: list[list["_Piece"]] = field(default_factory=list)


@dataclass
class _Piece:
    atom: "_Atom"
    quant: "_Quant | None" = None


@dataclass
class _Quant:
    text: str  # 转译时原样输出（*、+、?、{m}、{m,}、{m,n}）
    factor: int  # 展开规模因子（{m}→m、{m,n}→n、{m,}→m、*+?→1）


@dataclass
class _Literal:
    cp: int


@dataclass
class _AnyChar:
    pass


@dataclass
class _Shorthand:
    kind: str  # d D w W s S


@dataclass
class _CharClass:
    #: 逐项书写的成员：(cp, cp) 单字符或 (cp1, cp2) 区间；("short", kind) 简写
    items: list[Union[tuple[int, int], tuple[str, str]]]
    negated: bool


@dataclass
class _Group:
    body: _Alt


@dataclass
class _Anchor:
    start: bool  # True → ^（文本开头），False → $（文本结尾）


_Atom = Union[_Literal, _AnyChar, _Shorthand, _CharClass, _Group, _Anchor]


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


class _Parser:
    def __init__(self, pattern: str) -> None:
        self.s = pattern
        self.i = 0
        self.n = len(pattern)
        self.depth = 0

    def fail(
        self, reason: str, message: str, position: int | None = None
    ) -> PatternOutsideSubset:
        return PatternOutsideSubset(
            reason, message, self.i if position is None else position
        )

    def peek(self) -> str | None:
        return self.s[self.i] if self.i < self.n else None

    def take(self) -> str:
        c = self.s[self.i]
        self.i += 1
        return c

    # pattern = branch *("|" branch)
    def alt(self, top: bool) -> _Alt:
        alt = _Alt()
        alt.branches.append(self.branch(top))
        while self.peek() == "|":
            self.take()
            alt.branches.append(self.branch(top))
        return alt

    # branch = ["^"] *piece ["$"]
    def branch(self, top: bool) -> list[_Piece]:
        pieces: list[_Piece] = []
        if self.peek() == "^":
            if not top:
                raise self.fail(
                    "anchor_position", "^ 只允许在顶层分支的开头，作字面须转义"
                )
            self.take()
            pieces.append(_Piece(_Anchor(start=True)))
        while True:
            c = self.peek()
            if c is None or c == "|" or c == ")":
                break
            if c == "$":
                if not top:
                    raise self.fail(
                        "anchor_position", "$ 只允许在顶层分支的结尾，作字面须转义"
                    )
                self.take()
                pieces.append(_Piece(_Anchor(start=False)))
                # $ 之后只能接 |、) 或结尾，否则 $ 不在分支尾
                if self.peek() not in (None, "|", ")"):
                    raise self.fail("anchor_position", "$ 只能出现在分支结尾")
                break
            if c == "^":
                raise self.fail("anchor_position", "^ 只能出现在分支开头，作字面须转义")
            pieces.append(self.piece())
        return pieces

    def piece(self) -> _Piece:
        atom = self.atom()
        return _Piece(atom, self.quantifier())

    def quantifier(self) -> _Quant | None:
        c = self.peek()
        if c in ("*", "+", "?"):
            self.take()
            self._reject_quantifier_suffix()
            return _Quant(c, 1)
        if c == "{":
            start = self.i
            self.take()
            m = self._digits()
            if m is None:
                raise self.fail(
                    "invalid_repetition", "{ 后必须紧跟数字（作字面须转义）", start
                )
            if self.peek() == "}":
                self.take()
                quant = _Quant(self.s[start : self.i], m)
            elif self.peek() == ",":
                self.take()
                n = self._digits()
                if self.peek() != "}":
                    raise self.fail(
                        "invalid_repetition", "量词形式只允许 {m}、{m,}、{m,n}", start
                    )
                self.take()
                if n is not None and m > n:
                    raise self.fail("invalid_repetition", "量词下界不得大于上界", start)
                quant = _Quant(self.s[start : self.i], m if n is None else n)
            else:
                raise self.fail(
                    "invalid_repetition", "量词形式只允许 {m}、{m,}、{m,n}", start
                )
            self._reject_quantifier_suffix()
            return quant
        if c == "}":
            raise self.fail("invalid_repetition", "} 作字面须转义")
        return None

    def _reject_quantifier_suffix(self) -> None:
        nxt = self.peek()
        if nxt == "?":
            raise self.fail("lazy_quantifier", "不支持惰性量词")
        if nxt == "+":
            raise self.fail("possessive_quantifier", "不支持占有量词")
        if nxt in ("*", "{", "}"):
            raise self.fail("stacked_quantifier", "同一原子至多一个量词")

    def _digits(self) -> int | None:
        start = self.i
        while True:
            c = self.peek()
            if c is None or c not in "0123456789":
                break
            self.i += 1
        if self.i == start:
            return None
        return int(self.s[start : self.i])

    def atom(self) -> _Atom:
        c = self.take()
        if c == "(":
            return self.group()
        if c == "[":
            return self.char_class()
        if c == ".":
            return _AnyChar()
        if c == "\\":
            return self.escape()
        if c in "{}":
            raise self.fail(
                "invalid_repetition", f"{c} 作字面须转义，或作为量词紧跟在原子之后"
            )
        if c in _METACHARS:
            raise self.fail("syntax", f"元字符 {c!r} 使用位置不合法")
        return _Literal(self._literal_cp(c))

    def _literal_cp(self, c: str) -> int:
        cp = ord(c)
        if 0xD800 <= cp <= 0xDFFF:
            raise self.fail("not_scalar_value", "字面不是 Unicode 标量值（孤立代理项）")
        return cp

    def group(self) -> _Group:
        if self.depth >= MAX_NESTING:
            raise self.fail("nesting_depth", f"分组嵌套深度超过 {MAX_NESTING}")
        self.depth += 1
        try:
            return self._group_body()
        finally:
            self.depth -= 1

    def _group_body(self) -> _Group:
        if self.peek() == "?":
            mark = self.i
            self.take()
            c = self.peek()
            if c == ":":
                self.take()
                body = self.alt(top=False)
                self._expect_close()
                return _Group(body)
            if c == "=" or c == "!":
                raise self.fail("lookaround", "不支持 lookaround", mark)
            if c == "<":
                nxt = self.s[self.i + 1] if self.i + 1 < self.n else ""
                if nxt in ("=", "!"):
                    raise self.fail("lookaround", "不支持 lookaround", mark)
                raise self.fail("named_group", "不支持命名/捕获组语法", mark)
            if c == "P":
                raise self.fail("named_group", "不支持命名/捕获组语法", mark)
            if c == "#":
                raise self.fail("comment_group", "不支持 (?#…) 注释组", mark)
            raise self.fail("inline_flag", "不支持内联标志或其他组扩展", mark)
        body = self.alt(top=False)
        self._expect_close()
        return _Group(body)

    def _expect_close(self) -> None:
        if self.peek() != ")":
            raise self.fail("syntax", "括号不配对")
        self.take()

    def escape(self) -> _Atom:
        mark = self.i - 1
        c = self.peek()
        if c is None:
            raise self.fail("syntax", "悬空反斜杠", mark)
        self.take()
        if c in "dDwWsS":
            return _Shorthand(c)
        if c == "x":
            return _Literal(self._hex_escape(mark))
        if c in _CONTROL_ESCAPES:
            return _Literal(ord(_CONTROL_ESCAPES[c]))
        if c in _ESCAPABLE:
            return _Literal(ord(c))
        if c == "p" or c == "P":
            raise self.fail("unicode_property_escape", "不支持 \\p{…} / \\P{…}", mark)
        if c in "0123456789":
            raise self.fail("backreference", "不支持反向引用", mark)
        if c == "b" or c == "B":
            raise self.fail("unknown_escape", "不支持 \\b / \\B", mark)
        raise self.fail("unknown_escape", f"不允许的转义 \\{c}", mark)

    def _hex_escape(self, mark: int) -> int:
        digits = self.s[self.i : self.i + 2]
        if len(digits) != 2 or any(d not in _HEX for d in digits):
            raise self.fail("unknown_escape", "\\x 必须紧跟两位十六进制", mark)
        self.i += 2
        return int(digits, 16)

    # class = "[" ["^"] ("-" / class-item) *class-item ["-"] "]"
    def char_class(self) -> _CharClass:
        negated = False
        if self.peek() == "^":
            self.take()
            negated = True
        items: list[Union[tuple[int, int], tuple[str, str]]] = []
        while True:
            c = self.peek()
            if c is None:
                raise self.fail("syntax", "字符类未闭合")
            if c == "]":
                self.take()
                if not items:
                    raise self.fail("empty_class", "字符类不得为空")
                return _CharClass(items, negated)
            if c == "-":
                # 类首、类尾的 - 是字面字符；中间位置不合法
                if items and self.s[self.i + 1 : self.i + 2] != "]":
                    if items[-1][0] == "short":
                        raise self.fail(
                            "class_range_endpoint",
                            "区间端点必须是单字符，简写类不可作端点",
                        )
                    raise self.fail(
                        "class_dash_position",
                        "- 只能是类首/类尾的字面、转义或区间分隔符",
                    )
                self.take()
                items.append((0x2D, 0x2D))
                continue
            items.append(self.class_item())

    def class_item(self) -> Union[tuple[int, int], tuple[str, str]]:
        item = self.class_char_or_shorthand()
        if item[0] == "short":
            return item
        assert isinstance(item[0], int) and item[0] == item[1]
        if self.peek() == "-" and self.s[self.i + 1 : self.i + 2] != "]":
            self.take()
            end = self.class_char_or_shorthand()
            if end[0] == "short" or end[0] != end[1]:
                raise self.fail("class_range_endpoint", "区间端点必须是单字符")
            assert isinstance(end[0], int)
            if item[0] > end[0]:
                raise self.fail("class_range_order", "区间的起点不得大于终点")
            return (item[0], end[0])
        return item

    def class_char_or_shorthand(self) -> Union[tuple[int, int], tuple[str, str]]:
        c = self.peek()
        if c in ("[", "]"):
            raise self.fail("class_unescaped_bracket", "类内 [ 与 ] 必须转义")
        if c == "-":
            raise self.fail(
                "class_dash_position", "- 只能是类首/类尾的字面、转义或区间分隔符"
            )
        if c == "\\":
            mark = self.i
            self.take()
            e = self.peek()
            if e is None:
                raise self.fail("syntax", "类内悬空反斜杠")
            self.take()
            if e in "dDwWsS":
                return ("short", e)
            if e == "x":
                cp = self._hex_escape(mark)
                return (cp, cp)
            if e in _CONTROL_ESCAPES:
                return (ord(_CONTROL_ESCAPES[e]), ord(_CONTROL_ESCAPES[e]))
            if e in _ESCAPABLE:
                return (ord(e), ord(e))
            if e in ("p", "P"):
                raise self.fail("unicode_property_escape", "不支持 \\p{…} / \\P{…}")
            raise self.fail("unknown_escape", f"类内不允许的转义 \\{e}")
        if c is None:
            raise self.fail("syntax", "字符类未闭合")
        self.take()
        cp = self._literal_cp(c)
        return (cp, cp)


def _check_written_sequences(pattern: str) -> None:
    """类内 MUST NOT 出现相邻的 &&、--、~~（按字面文本判定：紧邻的两个同字符一律违例，无论前一个
    是否被转义——`[a\\--b]` 也违例；写出相邻字符请用 \\xHH）。"""
    for seq in ("&&", "--", "~~"):
        idx = pattern.find(seq)
        while idx >= 0:
            if _inside_class(pattern, idx):
                raise PatternOutsideSubset(
                    "class_set_operation", f"类内不得出现字面序列 {seq!r}", idx
                )
            idx = pattern.find(seq, idx + 1)


def _is_escaped(text: str, index: int) -> bool:
    backslashes = 0
    j = index - 1
    while j >= 0 and text[j] == "\\":
        backslashes += 1
        j -= 1
    return backslashes % 2 == 1


def _inside_class(text: str, index: int) -> bool:
    inside = False
    for j, c in enumerate(text[: index + 1]):
        if _is_escaped(text, j):
            continue
        if c == "[" and not inside:
            inside = True
        elif c == "]" and inside:
            inside = False
    return inside


def parse_pattern(pattern: str) -> _Alt:
    """解析为 AST；不属于子集时抛 ``PatternOutsideSubset``。"""
    if len(pattern) > MAX_LENGTH:
        raise PatternOutsideSubset("length", f"长度超过 {MAX_LENGTH}", MAX_LENGTH)
    _check_written_sequences(pattern)
    parser = _Parser(pattern)
    alt = parser.alt(top=True)
    if parser.i != parser.n:
        raise PatternOutsideSubset("syntax", "解析未到结尾", parser.i)
    if expand_size(alt) > MAX_EXPANSION:
        raise PatternOutsideSubset("expansion", f"展开规模超过 {MAX_EXPANSION}", -1)
    return alt


def check_pattern(pattern: str) -> None:
    """校验 pattern 属于 §4.1.1 子集；不属于时抛 ``PatternOutsideSubset``。"""
    parse_pattern(pattern)


# ---------------------------------------------------------------------------
# 展开规模（§4.1.1：按书写形式逐项计数，不去重）
# ---------------------------------------------------------------------------


def expand_size(node: _Alt | _Piece | _Atom) -> int:
    """计算展开规模：字面/转义/`.`/简写各计 1；字符类按写出的项计（取反不计）；锚点计 0；
    分组至少计 1（空组计 1）；量词为因子 × 被量化原子的规模（因子见 §4.1.1）。"""
    if isinstance(node, _Alt):
        return sum(expand_size(piece) for branch in node.branches for piece in branch)
    if isinstance(node, _Piece):
        atom_size = expand_size(node.atom)
        return atom_size * (node.quant.factor if node.quant else 1)
    if isinstance(node, _CharClass):
        return max(1, len(node.items))
    if isinstance(node, _Group):
        # 分组至少计 1：空组计 0 会让「零尺寸原子 × 大计数」绕过展开规模上界
        return max(1, expand_size(node.body))
    if isinstance(node, _Anchor):
        return 0
    return 1  # _Literal / _AnyChar / _Shorthand


# ---------------------------------------------------------------------------
# 转译（显式码点区间 → Python 语法）
# ---------------------------------------------------------------------------


def _shorthand_ranges(kind: str) -> tuple[tuple[int, int], ...]:
    if kind in ("d", "w", "s"):
        return _SHORTHAND_RANGES[kind]
    return _complement(_SHORTHAND_RANGES[kind.lower()])


def _normalize(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for lo, hi in sorted(ranges):
        if merged and lo <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def _only_scalars(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """去掉落在代理码位（U+D800–U+DFFF）上的片段——子集按 Unicode 标量值定义。"""
    out: list[tuple[int, int]] = []
    for lo, hi in ranges:
        if hi < 0xD800 or lo > 0xDFFF:
            out.append((lo, hi))
        else:
            if lo < 0xD800:
                out.append((lo, 0xD7FF))
            if hi > 0xDFFF:
                out.append((0xE000, hi))
    return out


def _complement(ranges: tuple[tuple[int, int], ...]) -> tuple[tuple[int, int], ...]:
    out: list[tuple[int, int]] = []
    cursor = 0
    for lo, hi in _normalize(list(ranges)):
        if lo > cursor:
            out.append((cursor, lo - 1))
        cursor = hi + 1
    if cursor <= 0x10FFFF:
        out.append((cursor, 0x10FFFF))
    return tuple(_normalize(_only_scalars(out)))


def _emit_cp(cp: int) -> str:
    if cp <= 0xFF:
        return rf"\x{cp:02x}"
    if cp <= 0xFFFF:
        return rf"\u{cp:04x}"
    return rf"\U{cp:08x}"


def _emit_class(ranges: list[tuple[int, int]]) -> str:
    if not ranges:
        # 语义上不匹配任何字符的类（如取反后为空）；工程上不出现于合法配置
        return "(?!)"
    body = "".join(
        _emit_cp(lo) if lo == hi else f"{_emit_cp(lo)}-{_emit_cp(hi)}"
        for lo, hi in ranges
    )
    return f"[{body}]"


def _emit_atom(atom: _Atom) -> str:
    if isinstance(atom, _Literal):
        return _emit_cp(atom.cp)
    if isinstance(atom, _AnyChar):
        return _emit_class(
            [(0x00, 0x09), (0x0B, 0x0C), (0x0E, 0xD7FF), (0xE000, 0x10FFFF)]
        )
    if isinstance(atom, _Shorthand):
        return _emit_class(list(_shorthand_ranges(atom.kind)))
    if isinstance(atom, _CharClass):
        ranges: list[tuple[int, int]] = []
        for item in atom.items:
            if item[0] == "short":
                kind = item[1]
                assert isinstance(kind, str)
                ranges.extend(_shorthand_ranges(kind))
            else:
                assert isinstance(item[0], int) and isinstance(item[1], int)
                ranges.append((item[0], item[1]))
        # 匹配宇宙是 Unicode 标量值：正类区间同样剔除代理区，与取反/`.`/补集一致
        merged = _normalize(_only_scalars(ranges))
        if atom.negated:
            merged = list(_complement(tuple(merged)))
        return _emit_class(merged)
    if isinstance(atom, _Group):
        return f"(?:{_emit_alt(atom.body)})"
    assert isinstance(atom, _Anchor)
    return r"\A" if atom.start else r"\Z"


def _emit_alt(alt: _Alt) -> str:
    branches = []
    for branch in alt.branches:
        parts = []
        for piece in branch:
            part = _emit_atom(piece.atom)
            if piece.quant is not None:
                part += piece.quant.text
            parts.append(part)
        branches.append("".join(parts))
    return "|".join(branches)


def translate_pattern(pattern: str) -> str:
    """转译为显式码点区间的 Python 正则（供参考实现与测试交叉核对；语义即 §4.1.1）。

    空 pattern 语义为「匹配任意串」，转译为 ``(?:)``（空组，匹配空串即匹配任意串）：它非空、
    不排除任何码点（含孤立代理项），也不会被 ``"|".join(...)`` 之类的拼接路径吞掉
    （jsonschema 的 additionalProperties 判定即如此）。
    """
    if pattern == "":
        return "(?:)"
    return _emit_alt(parse_pattern(pattern))


def search(pattern: str, value: str) -> bool:
    """按 §4.1.1 语义在 ``value`` 中检索（未锚定）；pattern 不合法时抛 ``PatternOutsideSubset``。"""
    return re.search(translate_pattern(pattern), value) is not None
