"""工单 → DPE document 条目（#223 §4）。

- **文档对象**：``file_type: jira_issue``；``title`` = summary（标题是内容，改标题即内容变化）；
  ``doc_metadata = {issue_id, created_at}``，只放不可变的源事实；
- **页 1 正文**（``section: body``）：描述的 ADF 按 ``adf`` 的规则转换，描述为空即空页；
- **页 2 评论**（``section: comments``）：按 ``created``、再按评论 id 在本地排序（不依赖 API 的
  返回顺序），每条评论的每个元素都带 ``{comment_id, author, author_account_id, created_at,
  updated_at}``；
- **页 3 字段**（``section: fields``）：固定字段按固定顺序各一个 ``NarrativeText``，``text`` 是
  「字段名：值」（字段名用固定的英文名，不取 Jira 按账号语言翻译的名称），``metadata =
  {field, value}``，值为空不产出。状态、优先级、类型、解决结果的名称可能随凭证账号的语言设置
  而不同，所以 ``value`` 同时记 ``id`` 与 ``name``；人员 ``text`` 用显示名，``metadata`` 另带
  ``account_id``。
- **页 4 附件**（``section: attachments``，没有附件时不产生）：按附件 id 升序每个附件一个元素。
  图片（按文件扩展名查固定表）为 ``Image``，``blob`` 是句柄（附件 id）加附件元数据里的
  ``size``，字节由运行器经 ``read_blob`` 读取；其他附件为 ``NarrativeText``，只记文件名、类型与
  大小。**不在描述或评论的引用位置关联附件**：ADF 媒体节点的 id 是 Media Services 的 id，Jira
  Cloud 没有公开它与附件 id 的对应关系，猜测（按文件名）或依赖渲染结果都不确定；媒体节点照旧
  产出不带 blob 的 ``Image`` 占位（``adf``）。

时间戳一律规范化为 RFC 3339 UTC（毫秒精度，以 ``Z`` 结尾）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from dpe_jira.adf import to_elements

__all__ = ["IMAGE_TYPES", "ISSUE_FIELDS", "MappingError", "blob_handles", "issue_document", "utc"]

#: 字段页的字段：(Jira 字段 id, 固定显示名)，按此顺序产出
_FIELDS: tuple[tuple[str, str], ...] = (
    ("issuetype", "Issue Type"),
    ("status", "Status"),
    ("priority", "Priority"),
    ("assignee", "Assignee"),
    ("reporter", "Reporter"),
    ("labels", "Labels"),
    ("components", "Components"),
    ("fixVersions", "Fix Versions"),
    ("parent", "Parent"),
    ("resolution", "Resolution"),
    ("duedate", "Due Date"),
)
_NAMED = frozenset({"issuetype", "status", "priority", "resolution"})
_PEOPLE = frozenset({"assignee", "reporter"})
_NAME_LISTS = frozenset({"components", "fixVersions"})

#: 取工单全文时请求的字段
ISSUE_FIELDS: list[str] = [
    "summary",
    "description",
    "created",
    "attachment",
    *(field for field, _ in _FIELDS),
]

#: 图片附件：文件扩展名（小写）→ 固定的媒体类型。``mime_type`` 是字节的媒体类型（契约 1 §4.2），
#: 取固定表而不取 Jira 给的 mimeType（由上传端决定，同一种图片可能报成不同的值）；非图片附件的
#: mimeType 只作为源事实记在 metadata 里，附件上传后不再变化
IMAGE_TYPES: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
}
#: blob ``size`` 的上界（connector §6.6：≤ 2^53−1）
_MAX_SIZE = 2**53 - 1


class MappingError(Exception):
    """工单内容无法映射为合法的 DPE 文档（条目级 ``content_invalid``，不可重试）。"""


def utc(value: Any) -> str:
    """Jira 的时间戳（如 ``2026-10-14T10:00:00.000+0800``）→ RFC 3339 UTC，毫秒精度。"""
    if not isinstance(value, str):
        raise MappingError("时间戳缺失或不是字符串")
    for pattern in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            moment = datetime.strptime(value, pattern)
        except ValueError:
            continue
        moment = moment.astimezone(UTC)
        return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"
    raise MappingError("时间戳不是可识别的格式")


def issue_document(
    uri: str, issue: dict[str, Any], comments: list[dict[str, Any]]
) -> dict[str, Any]:
    """组装一篇工单的 document 条目（三页）。内容异常抛 ``MappingError``。"""
    issue_id = issue.get("id")
    fields = issue.get("fields")
    if not isinstance(issue_id, str) or not isinstance(fields, dict):
        raise MappingError("工单缺少 id 或 fields")
    summary = fields.get("summary")
    document: dict[str, Any] = {
        "file_type": "jira_issue",
        "doc_metadata": {"issue_id": issue_id, "created_at": utc(fields.get("created"))},
    }
    if isinstance(summary, str):
        document["title"] = summary
    pages: list[dict[str, Any]] = [
        {
            "page_metadata": {"section": "body"},
            "elements": to_elements(fields.get("description")),
        },
        {"page_metadata": {"section": "comments"}, "elements": _comment_elements(comments)},
        {"page_metadata": {"section": "fields"}, "elements": _field_elements(fields)},
    ]
    attachments = _attachment_elements(fields.get("attachment"))
    if attachments:
        pages.append({"page_metadata": {"section": "attachments"}, "elements": attachments})
    return {"kind": "document", "file_uri": uri, "document": document, "pages": pages}


def blob_handles(item: dict[str, Any]) -> dict[str, int]:
    """document 条目里出现的 blob 句柄 → 声明的 size（server 据此登记 ``read_blob`` 的句柄）。"""
    handles: dict[str, int] = {}
    for page in item.get("pages", []):
        for element in page["elements"]:
            blob = element.get("blob")
            if isinstance(blob, dict):
                handles[blob["handle"]] = blob["size"]
    return handles


def _attachment_elements(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise MappingError("附件列表不是数组")
    keyed: list[tuple[int, dict[str, Any]]] = []
    for attachment in raw:
        attachment_id = attachment.get("id") if isinstance(attachment, dict) else None
        if (
            not isinstance(attachment_id, str)
            or not attachment_id.isascii()
            or not attachment_id.isdigit()
        ):
            raise MappingError("附件缺少数字 id")
        keyed.append((int(attachment_id), attachment))
    keyed.sort(key=lambda entry: entry[0])
    elements: list[dict[str, Any]] = []
    for _, attachment in keyed:
        attachment_id = attachment["id"]
        name, size = attachment.get("filename"), attachment.get("size")
        if not isinstance(name, str) or not name:
            raise MappingError("附件缺少文件名")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 <= size <= _MAX_SIZE:
            raise MappingError("附件大小不合法")
        extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        image_type = IMAGE_TYPES.get(extension)
        if image_type is not None:
            elements.append(
                {
                    "category": "Image",
                    "text": name,
                    "mime_type": image_type,
                    "blob": {"handle": attachment_id, "size": size},
                    "metadata": {"attachment_id": attachment_id},
                }
            )
        else:
            elements.append(
                {
                    "category": "NarrativeText",
                    "text": name,
                    "metadata": {
                        "attachment_id": attachment_id,
                        "mime_type": _str(attachment.get("mimeType")),
                        "size": size,
                    },
                }
            )
    return elements


def _comment_elements(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keyed: list[tuple[str, int, dict[str, Any]]] = []
    for comment in comments:
        comment_id = comment.get("id")
        if not isinstance(comment_id, str) or not comment_id.isdigit():
            raise MappingError("评论缺少数字 id")
        keyed.append((utc(comment.get("created")), int(comment_id), comment))
    keyed.sort(key=lambda entry: (entry[0], entry[1]))
    elements: list[dict[str, Any]] = []
    for created, _, comment in keyed:
        author = comment.get("author") if isinstance(comment.get("author"), dict) else {}
        assert isinstance(author, dict)
        metadata = {
            "comment_id": comment["id"],
            "author": _str(author.get("displayName")),
            "author_account_id": _str(author.get("accountId")),
            "created_at": created,
            "updated_at": utc(comment.get("updated", comment.get("created"))),
        }
        elements.extend(to_elements(comment.get("body"), metadata))
    return elements


def _field_elements(fields: dict[str, Any]) -> list[dict[str, Any]]:
    elements: list[dict[str, Any]] = []
    for field, label in _FIELDS:
        raw = fields.get(field)
        rendered = _field_value(field, raw)
        if rendered is None:
            continue
        text, value, extra = rendered
        elements.append(
            {
                "category": "NarrativeText",
                "text": f"{label}: {text}",
                "metadata": {"field": field, "value": value, **extra},
            }
        )
    return elements


def _field_value(field: str, raw: Any) -> tuple[str, Any, dict[str, Any]] | None:
    """一个字段的（显示文本，规范化的值，额外 metadata）；值为空时为 ``None``。"""
    if raw is None:
        return None
    if field in _NAMED:
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
            return None
        return raw["name"], {"id": _str(raw.get("id")), "name": raw["name"]}, {}
    if field in _PEOPLE:
        if not isinstance(raw, dict) or not isinstance(raw.get("displayName"), str):
            return None
        name = raw["displayName"]
        return name, name, {"account_id": _str(raw.get("accountId"))}
    if field == "labels":
        labels = [label for label in raw if isinstance(label, str)] if isinstance(raw, list) else []
        return (", ".join(labels), labels, {}) if labels else None
    if field in _NAME_LISTS:
        names = (
            [
                item["name"]
                for item in raw
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            ]
            if isinstance(raw, list)
            else []
        )
        return (", ".join(names), names, {}) if names else None
    if field == "parent":
        key = raw.get("key") if isinstance(raw, dict) else None
        return (key, key, {}) if isinstance(key, str) and key else None
    if field == "duedate":
        return (raw, raw, {}) if isinstance(raw, str) and raw else None
    return None


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""
