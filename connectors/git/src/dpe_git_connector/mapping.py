"""Git 文件 → DPE 产出条目（connector 契约 §6.5）。

一份文件产出一条 ``document``：``file_uri`` 由实例前缀与仓库内路径拼接；``document`` 与
``pages`` 由 ``extractors`` 按 file_type 构造。失败如实产出 ``error`` 条目（§6.5 的封闭枚举），
不静默跳过：

- ``source_unavailable``：git 读取失败（``retryable: true``——同样的扫描重跑可能成功）；
- ``content_invalid``：内容无法映射为合法 DPE 内容（不是 UTF-8、不是合法 JSON 等，不可重试），
  也包括「按固定预算切分后仍超过远端 ``max_payload_bytes``」——这是部署配置问题，如实暴露，
  不静默按远端值重新切分（那会让同一份源出现第二套映射）。

``file_uri`` 的构造必须调用 ``dpe_hash.normalize_file_uri``（唯一实现，core §1.1），不自行按
RFC 3986 手写规范化。路径字节按 RFC 3986 的百分号编码转成 ASCII——git 的路径是字节串，可能
不是 UTF-8；百分号编码对字节透明，非 UTF-8 路径的文件照常映射。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dpe_hash import ContractUnsupportedError, ValidationError, normalize_file_uri

from dpe_git_connector.extractors import ExtractError, element_bytes, extract
from dpe_git_connector.gitrepo import FileEntry, GitError, GitRepo

__all__ = [
    "DocumentMapper",
    "Item",
    "Limits",
    "build_file_uri",
]

#: RFC 3986 unreserved —— 这些字节照原样进 URI（其余一律百分号编码）
_UNRESERVED = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
#: 路径分隔符是 URI 的结构，保持字面
_KEEP = _UNRESERVED | {ord("/")}


def build_file_uri(prefix: str, path: bytes) -> str:
    """实例前缀 + 仓库内路径字节 → ``file_uri``（已按 core §1.1 规范化，作为身份）。

    ``prefix`` 是实例前缀，运行器已保证它是规范化不动点（契约 §6.3）且自带分隔符——码点前缀
    匹配不识别 path 段边界，前缀末尾不带 ``/`` 时相邻路径会拼接成越界 URI，因此这里显式要求。
    """
    if not prefix:
        raise ValueError("实例 URI 前缀不得为空")
    encoded = "".join(chr(byte) if byte in _KEEP else f"%{byte:02X}" for byte in path)
    return normalize_file_uri(prefix + encoded)


@dataclass(frozen=True)
class Item:
    """一条产出条目（§6.5）：``kind`` 与其余字段。"""

    kind: str
    fields: dict[str, Any]

    def payload(self) -> dict[str, Any]:
        return {"kind": self.kind, **self.fields}


@dataclass(frozen=True)
class Limits:
    """远端限额中与切分决策相关的项（``initialize`` 的 ``remote_limits``，可选）。"""

    max_payload_bytes: int | None = None

    def too_large(self, size: int) -> bool:
        return self.max_payload_bytes is not None and size > self.max_payload_bytes


class DocumentMapper:
    """把仓库里的一份文件映射成产出条目。"""

    def __init__(self, prefix: str, limits: Limits | None = None) -> None:
        self.prefix = prefix
        self.limits = limits or Limits()

    def document(self, repo: GitRepo, entry: FileEntry, file_type: str) -> Item:
        """读一份文件 → ``document`` 条目；失败给 ``error`` 条目。"""
        try:
            file_uri = build_file_uri(self.prefix, entry.path)
        except (ValueError, ValidationError, ContractUnsupportedError) as exc:
            return self._error(entry.path, "content_invalid", f"file_uri 不合法：{exc}", False)
        if not file_uri.startswith(self.prefix):
            # 路径由前缀拼接而来，正常不会越界；防御性上报（契约 §6.5 要求不静默忽略）
            return self._error(entry.path, "internal", "file_uri 越出实例前缀", retryable=False)
        try:
            data = repo.read_blob(entry.object_id, entry.size)
            extracted = extract(file_type, None, data)
        except GitError as exc:
            return self._error(entry.path, "source_unavailable", str(exc), retryable=True)
        except ExtractError as exc:
            return self._error(entry.path, "content_invalid", str(exc), retryable=False)
        except Exception as exc:
            # 条目级兜底：插件产出是不可信输入的对面（§6.5 要求「该条目失败，本轮继续」），
            # 任何未预期的解析异常（超大整数、超大字段、解析器内部错误）都不得带崩进程——
            # 崩了会让每轮都以 plugin_crashed 收场、实例永不收敛（§8.1-5）
            return self._error(
                entry.path, "internal", f"映射时发生未预期错误：{type(exc).__name__}: {exc}", False
            )
        oversize = self._oversize(extracted.pages)
        if oversize is not None:
            return self._error(
                entry.path,
                "content_invalid",
                "元素按固定预算切分后仍超过远端 max_payload_bytes"
                f"（{oversize} 字节 > {self.limits.max_payload_bytes}）",
                retryable=False,
            )
        return Item(
            kind="document",
            fields={
                "file_uri": file_uri,
                "document": extracted.document(),
                "pages": extracted.pages,
            },
        )

    def oversize_error(
        self, entry: FileEntry, size: int, budget: int, limit: int
    ) -> dict[str, Any]:
        """条目编码后装不进单条消息时的产出：条目级 ``content_invalid``（§6.5）。

        §6.5 明确「确实无法在限制内表达的内容 MUST 以条目级 error 如实上报，MUST NOT 发送超限
        消息」——运行器收到超限消息 MUST 终止实例（§6.2），照发会让每轮都以协议级失败收场。
        注：判据是**单条预算**（消息上限减去信封预留），比 ``max_message_bytes`` 本身小，消息
        按实测口径写，避免出现「N 字节超过上限 M」而 N ≤ M 的失真表述。
        """
        item = self._error(
            entry.path,
            "content_invalid",
            f"条目紧凑编码后 {size} 字节，超过单条预算 {budget}（消息上限 {limit}）",
            retryable=False,
        )
        return item.payload()

    def _oversize(self, pages: list[dict[str, Any]]) -> int | None:
        """仍超远端 ``max_payload_bytes`` 的元素字节数（守卫；不据此重新切分）。"""
        for page in pages:
            for element in page.get("elements", []):
                size = element_bytes(element)
                if self.limits.too_large(size):
                    return size
        return None

    def _error(self, path: bytes, code: str, message: str, retryable: bool) -> Item:
        return Item(
            kind="error",
            fields={
                "file_uri": self._safe_uri(path),
                "code": code,
                "message": message,
                "retryable": retryable,
            },
        )

    def _safe_uri(self, path: bytes) -> str | None:
        try:
            return build_file_uri(self.prefix, path)
        except (ValueError, ValidationError, ContractUnsupportedError):
            return None
