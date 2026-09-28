"""DPE 元数据模型 —— 镜像内核 ``TFDocMetadata`` / ``TFElementMetadata`` 的声明字段集。

``DocMetadata`` 进入 ``doc_hash``（经 ``json_stable``），因此它的**声明字段集与序列化方式**
必须与内核逐字节一致：

- 内核对 ``doc_metadata`` 的规范化输入是 ``model_dump(mode="json")``，即「全部声明字段 +
  未赋值字段为 null」，本模型声明同一组字段，缺一个或多一个都会导致 ``doc_hash`` 分叉。
- 内核配置了 ``json_encoders={datetime: isoformat}``，本模型用 ``field_serializer`` 复刻，
  带时区的时间序列化为 ``+00:00`` 而非 pydantic 默认的 ``Z``。

唯一有意的偏离：内核 ``created_at`` 默认 ``datetime.now()``，本模型默认 ``None``。
默认取当前时间会让同一份内容每次构造得到不同 ``doc_hash``，破坏增量协商；
数据源应显式填写源系统中的创建时间。
"""

from datetime import datetime
from typing import Any

from pydantic import AnyUrl, BaseModel, ConfigDict, field_serializer


class DocMetadata(BaseModel):
    """Document 级元数据（进入 ``doc_hash``）。允许扩展字段，扩展字段同样进入 hash。"""

    model_config = ConfigDict(extra="allow")

    created_at: datetime | None = None
    last_modified: datetime | None = None
    forward_citation_uris: list[AnyUrl] | None = None
    backward_citation_uris: list[AnyUrl] | None = None
    languages: list[str] | None = None
    filename: str | None = None
    summary: str | None = None
    global_dict: dict[str, str] | None = None

    @field_serializer("created_at", "last_modified", when_used="json")
    def _serialize_datetime(self, value: datetime | None) -> str | None:
        return value.isoformat() if value else None


class ElementMetadata(BaseModel):
    """Element 级元数据。

    只有 ``text_as_html``（Table / Formula）与 ``image_url`` / ``image_base64`` /
    ``image_path`` / ``image_mime_type``（Image）进入 ``content_hash``，其余字段仅随内容投递。
    结构化子对象（坐标、数据源）在源端不做强类型约束，原样透传给内核校验。
    """

    model_config = ConfigDict(extra="allow")

    filename: str | None = None
    filetype: str | None = None
    last_modified: datetime | None = None
    coordinates: dict[str, Any] | None = None
    data_source: dict[str, Any] | None = None
    parent_id: str | None = None
    related_ids: list[str] | None = None
    is_active: bool | None = None
    is_deprecated: bool | None = None
    is_narrative: bool | None = None
    category_depth: int | None = None
    text_as_html: str | None = None
    languages: list[str] | None = None
    emphasized_text_contents: list[str] | None = None
    emphasized_text_tags: list[str] | None = None
    header_info: str | None = None
    is_continuation: bool | None = None
    detection_class_prob: float | None = None
    image_path: str | None = None
    image_base64: str | None = None
    image_mime_type: str | None = None
    image_url: str | None = None
    page_number: int | None = None
    page_name: str | None = None
    encrypted: bool = False
    sent_from: list[str] | None = None
    sent_to: list[str] | None = None
    subject: str | None = None
    link_texts: list[str] | None = None
    link_urls: list[str] | None = None
    links: list[dict[str, Any]] | None = None
    section: str | None = None
    editor: str | None = None
    editor_email: str | None = None
    editor_id: str | None = None
    status: str | None = None
    priority: str | None = None

    @field_serializer("last_modified", when_used="json")
    def _serialize_datetime(self, value: datetime | None) -> str | None:
        return value.isoformat() if value else None
