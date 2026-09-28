"""DPE 三层内容寻址 hash（hash-contract-v1 的符合性实现）。"""

from dpe_protocol.hashing.strategy import (
    DEFAULT_HASH_STRATEGY,
    SUPPORTED_HASH_STRATEGIES,
    DocumentHashes,
    HashStrategyRef,
    PageHashes,
    UnsupportedHashStrategyError,
    compute_hashes,
)

__all__ = [
    "DEFAULT_HASH_STRATEGY",
    "SUPPORTED_HASH_STRATEGIES",
    "DocumentHashes",
    "HashStrategyRef",
    "PageHashes",
    "UnsupportedHashStrategyError",
    "compute_hashes",
]
