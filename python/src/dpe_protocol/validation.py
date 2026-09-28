"""文档级校验：一份 DPE 文档是否满足投递约束。

只检查「看一份文档就能判断」的规则；需要对比两次产出才能判断的约束（确定性）
见 :func:`dpe_protocol.testing.check_documents`。
"""

from dpe_protocol.push.client import check_pushable
from dpe_protocol.schema import Document
from dpe_protocol.uri import DPE_SCHEME


def check_document(doc: Document, *, expected_file_uri: str | None = None) -> list[str]:
    """返回违规描述列表，空列表表示通过。

    Args:
        doc: 待检查的文档。
        expected_file_uri: 调用方预期的 ``file_uri``（例如上层在拉取前就已确定的值），不一致即违规。
    """
    violations = check_pushable(doc)
    if doc.file_uri.scheme != DPE_SCHEME:
        violations.append(f"file_uri must use the reserved dpe:// namespace, got {doc.file_uri}")
    if expected_file_uri is not None and str(doc.file_uri) != expected_file_uri:
        violations.append(f"document file_uri {doc.file_uri} differs from expected {expected_file_uri}")
    return violations
