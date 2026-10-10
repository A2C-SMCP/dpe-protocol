"""connector 契约 §4.1.1 `config_schema` 的正则子集（实现侧）。

合法性只由文法与结构上界决定（``check_pattern``），与 Python 正则引擎无关；匹配只在
「解析 → 转译」之后进行（``translate_pattern`` 输出显式码点区间，不依赖引擎的字符类语义
或标志）。与规范参考实现 ``scripts/pattern_subset.py`` 同构但相互独立（CLAUDE.md：SDK 只
消费向量，不 import 生成器）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

__all__ = [
    "MAX_EXPANSION",
    "MAX_LENGTH",
    "MAX_NESTING",
    "PatternSubsetError",
    "check_pattern",
    "translate_pattern",
]

#: §4.1.1：pattern 长度上限（Unicode 标量值个数）
MAX_LENGTH = 1024
#: §4.1.1：展开规模上限
MAX_EXPANSION = 4096
#: §4.1.1：分组嵌套深度上限（括号嵌套层数；最外层分组计 1）
MAX_NESTING = 64

#: ``translate_pattern`` 结果缓存的条数上限。热路径只需当前清单的少量 pattern；单条转译输出
#: 有确定上界（实测最坏形状 ``"." * MAX_LENGTH`` 为 49152 字符 ≈ 48 KiB），故缓存驻留也有界
#: （最坏 ≈ 3 MiB）。
_TRANSLATE_CACHE_MAX = 64

#: 简写类 `\d` `\w` `\s` 的码点区间（§4.1.1 语义表；ASCII）
_SHORTHANDS: dict[str, tuple[tuple[int, int], ...]] = {
    "d": ((0x30, 0x39),),
    "w": ((0x30, 0x39), (0x41, 0x5A), (0x5F, 0x5F), (0x61, 0x7A)),
    "s": ((0x09, 0x0D), (0x20, 0x20)),
}

_ESCAPABLE = frozenset("()*+-.?[\\]^{|}$")
_CONTROL_ESCAPES = {"n": "\n", "r": "\r", "t": "\t"}
_METACHARS = frozenset("\\.^$|?*+()[]{}")
_HEX = frozenset("0123456789abcdefABCDEF")


class PatternSubsetError(ValueError):
    """pattern 不属于 §4.1.1 的子集。``reason`` 是机器可读的原因标记。"""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------


@dataclass
class _Alt:
    branches: list[list[_Piece]] = field(default_factory=list)


@dataclass
class _Piece:
    atom: _Atom
    quant: _Quant | None = None


@dataclass
class _Quant:
    text: str
    factor: int


@dataclass
class _Literal:
    cp: int


@dataclass
class _AnyChar:
    pass


@dataclass
class _Shorthand:
    kind: str


@dataclass
class _ClassItem:
    #: 单字符/区间：cp1 == cp2 时为单字符
    cp1: int = -1
    cp2: int = -1
    shorthand: str = ""  # "d"、"D"…；非空时忽略 cp1/cp2


@dataclass
class _CharClass:
    items: list[_ClassItem]
    negated: bool


@dataclass
class _Group:
    body: _Alt


@dataclass
class _Anchor:
    start: bool


_Atom = _Literal | _AnyChar | _Shorthand | _CharClass | _Group | _Anchor


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


class _Parser:
    def __init__(self, pattern: str) -> None:
        self.s = pattern
        self.i = 0
        self.n = len(pattern)
        self.depth = 0

    def fail(self, reason: str, message: str) -> PatternSubsetError:
        return PatternSubsetError(reason, message)

    def peek(self) -> str | None:
        return self.s[self.i] if self.i < self.n else None

    def take(self) -> str:
        c = self.s[self.i]
        self.i += 1
        return c

    def alt(self, top: bool) -> _Alt:
        alt = _Alt()
        alt.branches.append(self.branch(top))
        while self.peek() == "|":
            self.take()
            alt.branches.append(self.branch(top))
        return alt

    def branch(self, top: bool) -> list[_Piece]:
        pieces: list[_Piece] = []
        if self.peek() == "^":
            if not top:
                raise self.fail("anchor_position", "^ 只允许在顶层分支的开头，作字面须转义")
            self.take()
            pieces.append(_Piece(_Anchor(start=True)))
        while True:
            c = self.peek()
            if c is None or c == "|" or c == ")":
                break
            if c == "$":
                if not top:
                    raise self.fail("anchor_position", "$ 只允许在顶层分支的结尾，作字面须转义")
                self.take()
                pieces.append(_Piece(_Anchor(start=False)))
                if self.peek() not in (None, "|", ")"):
                    raise self.fail("anchor_position", "$ 只能出现在分支结尾")
                break
            if c == "^":
                raise self.fail("anchor_position", "^ 只能出现在分支开头，作字面须转义")
            atom = self.atom()
            pieces.append(_Piece(atom, self.quantifier()))
        return pieces

    def quantifier(self) -> _Quant | None:
        c = self.peek()
        if c in ("*", "+", "?"):
            self.take()
            self._reject_suffix()
            return _Quant(c, 1)
        if c == "{":
            start = self.i
            self.take()
            m = self._digits()
            if m is None:
                raise self.fail("invalid_repetition", "{ 后必须紧跟数字（作字面须转义）")
            if self.peek() == "}":
                self.take()
                quant = _Quant(self.s[start : self.i], m)
            elif self.peek() == ",":
                self.take()
                n = self._digits()
                if self.peek() != "}":
                    raise self.fail("invalid_repetition", "量词形式只允许 {m}、{m,}、{m,n}")
                self.take()
                if n is not None and m > n:
                    raise self.fail("invalid_repetition", "量词下界不得大于上界")
                quant = _Quant(self.s[start : self.i], m if n is None else n)
            else:
                raise self.fail("invalid_repetition", "量词形式只允许 {m}、{m,}、{m,n}")
            self._reject_suffix()
            return quant
        if c == "}":
            raise self.fail("invalid_repetition", "} 作字面须转义")
        return None

    def _reject_suffix(self) -> None:
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
            raise self.fail("invalid_repetition", f"{c} 作字面须转义，或作为量词紧跟在原子之后")
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
            self.take()
            c = self.peek()
            if c == ":":
                self.take()
                body = self.alt(top=False)
                self._expect_close()
                return _Group(body)
            if c == "=" or c == "!":
                raise self.fail("lookaround", "不支持 lookaround")
            if c == "<":
                nxt = self.s[self.i + 1] if self.i + 1 < self.n else ""
                if nxt in ("=", "!"):
                    raise self.fail("lookaround", "不支持 lookaround")
                raise self.fail("named_group", "不支持命名/捕获组语法")
            if c == "P":
                raise self.fail("named_group", "不支持命名/捕获组语法")
            if c == "#":
                raise self.fail("comment_group", "不支持 (?#…) 注释组")
            raise self.fail("inline_flag", "不支持内联标志或其他组扩展")
        body = self.alt(top=False)
        self._expect_close()
        return _Group(body)

    def _expect_close(self) -> None:
        if self.peek() != ")":
            raise self.fail("syntax", "括号不配对")
        self.take()

    def escape(self) -> _Atom:
        c = self.peek()
        if c is None:
            raise self.fail("syntax", "悬空反斜杠")
        self.take()
        if c in "dDwWsS":
            return _Shorthand(c)
        if c == "x":
            return _Literal(self._hex_escape())
        if c in _CONTROL_ESCAPES:
            return _Literal(ord(_CONTROL_ESCAPES[c]))
        if c in _ESCAPABLE:
            return _Literal(ord(c))
        if c in ("p", "P"):
            raise self.fail("unicode_property_escape", "不支持 \\p{…} / \\P{…}")
        if c in "0123456789":
            raise self.fail("backreference", "不支持反向引用")
        raise self.fail("unknown_escape", f"不允许的转义 \\{c}")

    def _hex_escape(self) -> int:
        digits = self.s[self.i : self.i + 2]
        if len(digits) != 2 or any(d not in _HEX for d in digits):
            raise self.fail("unknown_escape", "\\x 必须紧跟两位十六进制")
        self.i += 2
        return int(digits, 16)

    def char_class(self) -> _CharClass:
        negated = False
        if self.peek() == "^":
            self.take()
            negated = True
        items: list[_ClassItem] = []
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
                if items and self.s[self.i + 1 : self.i + 2] != "]":
                    if items[-1].shorthand:
                        raise self.fail(
                            "class_range_endpoint", "区间端点必须是单字符，简写类不可作端点"
                        )
                    raise self.fail(
                        "class_dash_position", "- 只能是类首/类尾的字面、转义或区间分隔符"
                    )
                self.take()
                items.append(_ClassItem(cp1=0x2D, cp2=0x2D))
                continue
            items.append(self.class_item())

    def class_item(self) -> _ClassItem:
        item = self.class_char_or_shorthand()
        if item.shorthand:
            return item
        if self.peek() == "-" and self.s[self.i + 1 : self.i + 2] != "]":
            self.take()
            end = self.class_char_or_shorthand()
            if end.shorthand or end.cp1 != end.cp2:
                raise self.fail("class_range_endpoint", "区间端点必须是单字符")
            if item.cp1 > end.cp1:
                raise self.fail("class_range_order", "区间的起点不得大于终点")
            return _ClassItem(cp1=item.cp1, cp2=end.cp1)
        return item

    def class_char_or_shorthand(self) -> _ClassItem:
        c = self.peek()
        if c in ("[", "]"):
            raise self.fail("class_unescaped_bracket", "类内 [ 与 ] 必须转义")
        if c == "-":
            raise self.fail("class_dash_position", "- 只能是类首/类尾的字面、转义或区间分隔符")
        if c == "\\":
            self.take()
            e = self.peek()
            if e is None:
                raise self.fail("syntax", "类内悬空反斜杠")
            self.take()
            if e in "dDwWsS":
                return _ClassItem(shorthand=e)
            if e == "x":
                cp = self._hex_escape()
                return _ClassItem(cp1=cp, cp2=cp)
            if e in _CONTROL_ESCAPES:
                cp = ord(_CONTROL_ESCAPES[e])
                return _ClassItem(cp1=cp, cp2=cp)
            if e in _ESCAPABLE:
                cp = ord(e)
                return _ClassItem(cp1=cp, cp2=cp)
            if e in ("p", "P"):
                raise self.fail("unicode_property_escape", "不支持 \\p{…} / \\P{…}")
            raise self.fail("unknown_escape", f"类内不允许的转义 \\{e}")
        if c is None:
            raise self.fail("syntax", "字符类未闭合")
        self.take()
        cp = self._literal_cp(c)
        return _ClassItem(cp1=cp, cp2=cp)


def _check_written_sequences(pattern: str) -> None:
    """类内 MUST NOT 出现相邻的 &&、--、~~（按字面文本判定：紧邻的两个同字符一律违例，无论前一个
    是否被转义——`[a\\--b]` 也违例；写出相邻字符请用 \\xHH）。"""
    for seq in ("&&", "--", "~~"):
        idx = pattern.find(seq)
        while idx >= 0:
            if _inside_class(pattern, idx):
                raise PatternSubsetError("class_set_operation", f"类内不得出现字面序列 {seq!r}")
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


# ---------------------------------------------------------------------------
# 展开规模（§4.1.1：按书写形式逐项计数，不去重）
# ---------------------------------------------------------------------------


def _size_alt(alt: _Alt) -> int:
    return sum(_size_piece(piece) for branch in alt.branches for piece in branch)


def _size_piece(piece: _Piece) -> int:
    size = _size_atom(piece.atom)
    return size * (piece.quant.factor if piece.quant else 1)


def _size_atom(atom: _Atom) -> int:
    if isinstance(atom, _CharClass):
        return max(1, len(atom.items))
    if isinstance(atom, _Group):
        # 分组至少计 1：空组计 0 会让「零尺寸原子 × 大计数」绕过展开规模上界
        return max(1, _size_alt(atom.body))
    if isinstance(atom, _Anchor):
        return 0
    return 1


def _parse_checked(pattern: str) -> _Alt:
    """完整校验（长度、类内序列、文法、展开规模）并返回 AST；越界抛 ``PatternSubsetError``。"""
    if len(pattern) > MAX_LENGTH:
        raise PatternSubsetError("length", f"长度超过 {MAX_LENGTH}")
    _check_written_sequences(pattern)
    parser = _Parser(pattern)
    alt = parser.alt(top=True)
    if parser.i != parser.n:
        raise PatternSubsetError("syntax", "解析未到结尾")
    if _size_alt(alt) > MAX_EXPANSION:
        raise PatternSubsetError("expansion", f"展开规模超过 {MAX_EXPANSION}")
    return alt


def check_pattern(pattern: str) -> None:
    """校验 pattern 属于 §4.1.1 子集；不属于时抛 ``PatternSubsetError``。"""
    _parse_checked(pattern)


# ---------------------------------------------------------------------------
# 转译（显式码点区间）
# ---------------------------------------------------------------------------


def _normalize(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for lo, hi in sorted(ranges):
        if merged and lo <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def _only_scalars(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
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


def _shorthand_ranges(kind: str) -> tuple[tuple[int, int], ...]:
    base = _SHORTHANDS[kind.lower()]
    return base if kind.islower() else _complement(base)


def _emit_cp(cp: int) -> str:
    if cp <= 0xFF:
        return rf"\x{cp:02x}"
    if cp <= 0xFFFF:
        return rf"\u{cp:04x}"
    return rf"\U{cp:08x}"


def _emit_class(ranges: list[tuple[int, int]]) -> str:
    if not ranges:
        # 语义上不匹配任何字符的类（如取反后为空）；合法配置中不出现
        return "(?!)"
    body = "".join(
        _emit_cp(lo) if lo == hi else f"{_emit_cp(lo)}-{_emit_cp(hi)}" for lo, hi in ranges
    )
    return f"[{body}]"


def _emit_atom(atom: _Atom) -> str:
    if isinstance(atom, _Literal):
        return _emit_cp(atom.cp)
    if isinstance(atom, _AnyChar):
        return _emit_class([(0x00, 0x09), (0x0B, 0x0C), (0x0E, 0xD7FF), (0xE000, 0x10FFFF)])
    if isinstance(atom, _Shorthand):
        return _emit_class(list(_shorthand_ranges(atom.kind)))
    if isinstance(atom, _CharClass):
        ranges: list[tuple[int, int]] = []
        for item in atom.items:
            if item.shorthand:
                ranges.extend(_shorthand_ranges(item.shorthand))
            else:
                ranges.append((item.cp1, item.cp2))
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


@lru_cache(maxsize=_TRANSLATE_CACHE_MAX)
def translate_pattern(pattern: str) -> str:
    """把子集内 pattern 转译为显式码点区间的 Python 正则（供 ``validate_config`` 使用）。

    本函数自身做完整校验（与 ``check_pattern`` 同一入口，不依赖调用序）。空 pattern 语义为
    「匹配任意串」，转译为 ``(?:)``
    （空组，匹配空串即匹配任意串）：它非空、不排除任何码点（含孤立代理项），也不会被
    ``"|".join(...)`` 之类的拼接路径吞掉（jsonschema 的 additionalProperties 判定即如此）。

    结果按 pattern 原文缓存（上限见 ``_TRANSLATE_CACHE_MAX``）：重复求值的 ``search`` 与
    ``validate_config`` 复用同一转译。缓存的是转译字符串而非 AST——最坏形状下 AST 的逐原子
    对象开销大于字符串；``re`` 模块对编译结果的缓存另由标准库管理。
    """
    if pattern == "":
        return "(?:)"
    return _emit_alt(_parse_checked(pattern))


def search(pattern: str, value: str) -> bool:
    """按 §4.1.1 语义在 ``value`` 中检索（未锚定）。"""
    return re.search(translate_pattern(pattern), value) is not None
