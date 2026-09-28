"""Hash 策略 URI 与版本分派（hash-contract-v1 §8）。

URI 形如 ``hash-strategy://{name}?algo_version={v}[&k=v...]``，是一个可扩展参数包。
比较时按「名称 + 参数集合」判等，不按原始字符串判等（参数顺序不影响语义）。
"""

from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit

from dpe_protocol.hashing import v1
from dpe_protocol.schema import Document

HASH_STRATEGY_SCHEME = "hash-strategy"


class UnsupportedHashStrategyError(ValueError):
    """SDK 未实现该 hash 策略 / 算法版本。"""


@dataclass(frozen=True)
class HashStrategyRef:
    name: str
    params: tuple[tuple[str, str], ...] = field(default=())

    @property
    def algo_version(self) -> str | None:
        return dict(self.params).get("algo_version")

    @classmethod
    def parse(cls, uri: str) -> "HashStrategyRef":
        parts = urlsplit(uri)
        if parts.scheme != HASH_STRATEGY_SCHEME or not parts.netloc:
            raise ValueError(f"not a hash-strategy URI: {uri!r}")
        return cls(name=parts.netloc, params=tuple(sorted(parse_qsl(parts.query, keep_blank_values=True))))

    def to_uri(self) -> str:
        query = urlencode(self.params)
        return f"{HASH_STRATEGY_SCHEME}://{self.name}" + (f"?{query}" if query else "")

    def __str__(self) -> str:
        return self.to_uri()


DEFAULT_HASH_STRATEGY = HashStrategyRef.parse("hash-strategy://default?algo_version=v1")

#: SDK 已实现的策略，用于与服务端能力文档 ``accepted_hash_strategies`` 求交集。
#: ``image-source`` 需要源文件字节，dpe-push/1 明确不支持，故不列入。
SUPPORTED_HASH_STRATEGIES: frozenset[HashStrategyRef] = frozenset({DEFAULT_HASH_STRATEGY})


@dataclass(frozen=True)
class PageHashes:
    number: int
    title: str | None
    page_hash: str
    #: 与 ``page.elements`` 数组顺序一一对应
    content_hashes: tuple[str, ...]


@dataclass(frozen=True)
class DocumentHashes:
    """一次计算得到的三层 hash 骨架，始终携带其 ``strategy``，防止跨策略误用。"""

    strategy: HashStrategyRef
    doc_hash: str
    #: 与 ``document.pages`` 数组顺序一一对应
    pages: tuple[PageHashes, ...]

    def all_content_hashes(self) -> set[str]:
        return {h for page in self.pages for h in page.content_hashes}


def compute_hashes(doc: Document, strategy: HashStrategyRef = DEFAULT_HASH_STRATEGY) -> DocumentHashes:
    """按 ``strategy`` 分派到对应算法版本，计算三层 hash。"""
    if strategy not in SUPPORTED_HASH_STRATEGIES:
        raise UnsupportedHashStrategyError(f"hash strategy not implemented by this SDK: {strategy}")
    pages = tuple(
        PageHashes(
            number=page.number,
            title=page.title,
            page_hash=v1.page_hash(page.title, hashes := tuple(v1.element_hash(e) for e in page.elements)),
            content_hashes=hashes,
        )
        for page in doc.pages
    )
    return DocumentHashes(
        strategy=strategy, doc_hash=v1.doc_hash(doc, {p.number: p.page_hash for p in pages}), pages=pages
    )
