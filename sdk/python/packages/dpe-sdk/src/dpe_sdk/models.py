"""三层对象的数据模型（core §2）：SDK 公共 API 的输入输出类型。

- 线上对象：``ElementObject``、``PageObject``（``elements`` 为 content_hash 列表）、
  ``DocumentObject``（``pages`` 为 page_hash 列表）；
- 展开视图：``Document`` / ``Page``，页与元素内联给出，对应 dpe_hash 的 ``ExpandedDocument``。

**校验只有一份实现，就是 dpe_hash。** 每个模型在原始输入上调用 dpe_hash，按 core §2.8 的顺序
校验完整个对象（含子对象），pydantic 只承载类型；category 允许的字段与 file_type 枚举也都取自
dpe_hash，SDK 不维护副本。pydantic 自身的字段校验自底向上、收集全部错误，满足不了 §2.8 的
「第一处违例、固定顺序」，所以不用它判定合法性。

**错误**：构造（关键字参数）、``model_validate``、``model_validate_json`` 失败时抛 dpe_hash 的
``DpeHashError``，``code`` 与 ``path`` 同一致性向量，``path`` 相对于被构造的对象。本模型作为别的
pydantic 模型的字段时，错误按 pydantic 惯例包装成 ``pydantic.ValidationError``，其
``errors()[i]["ctx"]["error"]`` 即 ``DpeHashError``。

**表示与等价**：模型保留输入的原样表示（core §2.7：读回 SHOULD 返回原样），序列化只输出显式给出
的字段；null 与缺省、metadata 缺省与 ``{}`` 等内容等价由 hash 判定，模型的 ``==`` 只比较结构。
hash 不缓存，每次调用都经 dpe_hash 重新校验并计算。模型是 frozen 的，构造时深拷贝输入；
metadata 仍是普通 dict，原地修改它不受保护，但改动后的 hash 照样按新内容计算，不会失真。

线上对象的子 hash 列表按 validation context 的 ``contract`` 校验（缺省 ``dpe_hash.CONTRACT``），
如 ``PageObject.model_validate(data, context={"contract": "dpe2"})``。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self, cast

import dpe_hash
from dpe_hash import CONTRACT, DocumentHashes, DpeHashError
from pydantic import (
    BaseModel,
    ConfigDict,
    SerializerFunctionWrapHandler,
    ValidationInfo,
    model_serializer,
    model_validator,
)
from pydantic import ValidationError as PydanticValidationError

from dpe_sdk import _ijson

__all__ = ["Document", "DocumentObject", "ElementObject", "Page", "PageObject"]


def _plain(value: Any) -> Any:
    """深拷贝成纯 JSON 值：模型实例取其序列化结果，Mapping → dict，tuple → list。

    校验与模型都基于这份拷贝，调用方之后修改自己的 dict 不会影响模型。
    """
    if isinstance(value, BaseModel):
        value = value.model_dump()
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _contract(info: ValidationInfo) -> str:
    context = info.context
    if isinstance(context, Mapping) and "contract" in context:
        return cast(str, context["contract"])
    return CONTRACT


def _dpe_error(exc: PydanticValidationError) -> DpeHashError | None:
    """顶层校验错误中的 DpeHashError（由本模块的 validator 抛出、被 pydantic 包装）。"""
    errors = exc.errors()
    if len(errors) == 1 and errors[0]["loc"] == ():
        error = errors[0].get("ctx", {}).get("error")
        if isinstance(error, DpeHashError):
            return error
    return None


class _DpeModel(BaseModel):
    """公共基类：封闭 schema、不可变，顶层入口抛 DpeHashError，序列化只输出显式给出的字段。"""

    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="never")

    def __init__(self, **data: Any) -> None:
        try:
            super().__init__(**data)
        except PydanticValidationError as exc:
            raise _dpe_error(exc) or exc from None

    # 覆写的 __init__ 只拆包错误，不改变校验。不加此标记时 pydantic 会把 model_validate 改走
    # 「自定义 __init__」路径，validation context（线上对象的 contract）随之丢失；
    # pydantic 自己的 RootModel 用同一标记。
    __init__.__pydantic_base_init__ = True  # type: ignore[attr-defined]

    @classmethod
    def model_validate(cls, obj: Any, *args: Any, **kwargs: Any) -> Self:
        try:
            return super().model_validate(obj, *args, **kwargs)
        except PydanticValidationError as exc:
            raise _dpe_error(exc) or exc from None

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        """带 ``update`` 时按新内容重新校验（pydantic 原生实现直接写入、不校验）。"""
        if not update:
            return super().model_copy(deep=deep)
        return type(self).model_validate({**self.model_dump(), **update})

    @classmethod
    def model_validate_json(
        cls, json_data: str | bytes | bytearray, *args: Any, **kwargs: Any
    ) -> Self:
        """按 I-JSON 严格解析（拒绝重复键，core §2.8 第 0 步）后校验，不用 pydantic 的 JSON 解析器。

        pydantic 的解析器对重复键后写覆盖，满足不了 I-JSON。
        """
        return cls.model_validate(_ijson.loads(json_data), *args, **kwargs)

    @model_serializer(mode="wrap")
    def _serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # 未给出的字段不输出：category 未允许的内容字段即使为 null 也不得出现（core §2.8 第 3 步）
        data: dict[str, Any] = handler(self)
        return {k: v for k, v in data.items() if k in self.model_fields_set}


class ElementObject(_DpeModel):
    """元素对象（core §2.3）。允许哪些内容字段由 ``category`` 决定（契约 1 §4.1）。"""

    category: str
    text: str | None = None
    text_as_html: str | None = None
    blob: str | None = None
    mime_type: str | None = None
    metadata: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def _validate(cls, data: Any, info: ValidationInfo) -> Any:
        plain = _plain(data)
        dpe_hash.content_hash(plain, _contract(info))
        return plain

    def content_hash(self, contract: str = CONTRACT) -> str:
        return dpe_hash.content_hash(cast(dpe_hash.ElementObject, self.model_dump()), contract)


class PageObject(_DpeModel):
    """页对象（core §2.2）：``elements`` 为 content_hash 列表，数组顺序即页内阅读顺序。"""

    title: str | None = None
    page_metadata: dict[str, Any] | None = None
    elements: list[str]

    @model_validator(mode="before")
    @classmethod
    def _validate(cls, data: Any, info: ValidationInfo) -> Any:
        plain = _plain(data)
        dpe_hash.object_hash(plain, "page", _contract(info))
        return plain

    def page_hash(self, contract: str = CONTRACT) -> str:
        return dpe_hash.object_hash(self.model_dump(), "page", contract)


class DocumentObject(_DpeModel):
    """文档对象（core §2.1）：``pages`` 为 page_hash 列表，数组顺序即页的阅读顺序。"""

    file_type: str
    title: str | None = None
    doc_metadata: dict[str, Any] | None = None
    pages: list[str]

    @model_validator(mode="before")
    @classmethod
    def _validate(cls, data: Any, info: ValidationInfo) -> Any:
        plain = _plain(data)
        dpe_hash.object_hash(plain, "document", _contract(info))
        return plain

    def doc_hash(self, contract: str = CONTRACT) -> str:
        return dpe_hash.object_hash(self.model_dump(), "document", contract)


def _build_page(page: dict[str, Any]) -> Page:
    """由已校验的纯 JSON 值直接建实例，不再经 pydantic 校验（整树已由 dpe_hash 校验过）。"""
    elements = [ElementObject.model_construct(**el) for el in page["elements"]]
    return Page.model_construct(**{**page, "elements": elements})


#: ``Page`` 单独校验时借用的文档外壳；外壳本身合法，错误只可能出在页上
_PAGE_SHELL = "/pages/0"


class Page(_DpeModel):
    """展开视图中的页：``elements`` 为元素对象本身。"""

    title: str | None = None
    page_metadata: dict[str, Any] | None = None
    elements: list[ElementObject]

    @model_validator(mode="before")
    @classmethod
    def _validate(cls, data: Any, info: ValidationInfo) -> Any:
        plain = _plain(data)
        # 借 document_hashes 的展开视图校验（页字段 → 各元素，同 core §2.8），把位置还原为相对页
        try:
            dpe_hash.document_hashes({"file_type": "unk", "pages": [plain]}, _contract(info))
        except DpeHashError as exc:
            path = exc.path[len(_PAGE_SHELL) :] if exc.path.startswith(_PAGE_SHELL) else exc.path
            raise type(exc)(exc.message, path) from None
        return {**plain, "elements": _build_page(plain).elements}


class Document(_DpeModel):
    """展开视图中的文档（vectors/README.md）：页与元素内联给出，数组顺序即阅读顺序。"""

    file_type: str
    title: str | None = None
    doc_metadata: dict[str, Any] | None = None
    pages: list[Page]

    @model_validator(mode="before")
    @classmethod
    def _validate(cls, data: Any, info: ValidationInfo) -> Any:
        plain = _plain(data)
        dpe_hash.document_hashes(plain, _contract(info))
        return {**plain, "pages": [_build_page(p) for p in plain["pages"]]}

    def hashes(self, contract: str = CONTRACT) -> DocumentHashes:
        """三层 hash，形如 ``{"doc_hash", "pages": [{"page_hash", "elements"}…]}``。"""
        return dpe_hash.document_hashes(
            cast(dpe_hash.ExpandedDocument, self.model_dump()), contract
        )

    def doc_hash(self, contract: str = CONTRACT) -> str:
        return self.hashes(contract)["doc_hash"]
