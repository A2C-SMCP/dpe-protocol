"""契约常量与 vectors/manifest.json 互校：常量由规范产出，SDK 只能与之一致。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from dpe_hash import (
    CATEGORY_CONTENT_FIELDS,
    CONTRACT,
    RECOMMENDED_FILE_TYPES,
    SUPPORTED_CONTRACTS,
)


@pytest.fixture(scope="module")
def manifest(vectors_dir: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((vectors_dir / "manifest.json").read_text(encoding="utf-8"))
    return data


def test_contract(manifest: dict[str, Any]) -> None:
    assert manifest["contract"] == CONTRACT
    assert CONTRACT in SUPPORTED_CONTRACTS


def test_file_types(manifest: dict[str, Any]) -> None:
    assert list(RECOMMENDED_FILE_TYPES) == manifest["file_types"]  # 含规范表中顺序


def test_category_content_fields(manifest: dict[str, Any]) -> None:
    assert {c: list(f) for c, f in CATEGORY_CONTENT_FIELDS.items()} == manifest[
        "category_content_fields"
    ]


def test_constants_are_immutable() -> None:
    with pytest.raises(TypeError):
        CATEGORY_CONTENT_FIELDS["Video"] = ("text",)  # type: ignore[index]
