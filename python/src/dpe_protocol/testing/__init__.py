"""测试工具：内存版 Robot 服务端与文档一致性检查。"""

from dpe_protocol.testing.conformance import ConformanceError, ConformanceIssue, ConformanceReport, check_documents
from dpe_protocol.testing.fake_server import FakeRobotServer, StoredDocument

__all__ = [
    "ConformanceError",
    "ConformanceIssue",
    "ConformanceReport",
    "FakeRobotServer",
    "StoredDocument",
    "check_documents",
]
