"""文档一致性检查：验证上层产出的 DPE 文档满足增量投递所依赖的约束。

本项目不定义 connector 的形态。上层用自己的方式对**同一份未修改的源数据产出两次文档**，交给
:func:`check_documents` 即可::

    first, second = await build_doc(item), await build_doc(item)
    (await check_documents(first, second, expected_file_uri=uri)).raise_for_issues()

检查项：

- ``static``：静态规则（``dpe://`` 命名空间、禁止 ``image_path`` 等），见 :func:`dpe_protocol.check_document`
- ``file_uri``：文档的 ``file_uri`` 与预期一致，且两次产出相同
- ``deterministic``：两次产出的 ``doc_hash`` 相同（抓「metadata 写了当前时间」之类的问题）
- ``push_roundtrip``：经 :class:`FakeRobotServer` 投递第一份，再投递第二份必须为 ``unchanged``

``file_uri`` 全局唯一、``page.number`` 在源数据变更时保持稳定，需要跨文档或构造变更场景才能检验，
由上层在自己的测试中覆盖。
"""

from dataclasses import dataclass, field

from dpe_protocol.hashing import compute_hashes
from dpe_protocol.push import DPEPushClient
from dpe_protocol.schema import Document
from dpe_protocol.testing.fake_server import FakeRobotServer
from dpe_protocol.validation import check_document


@dataclass(frozen=True)
class ConformanceIssue:
    rule: str
    message: str
    file_uri: str | None = None


class ConformanceError(AssertionError):
    def __init__(self, issues: list[ConformanceIssue]) -> None:
        super().__init__("\n".join(f"[{i.rule}] {i.file_uri or '-'}: {i.message}" for i in issues))
        self.issues = issues


@dataclass
class ConformanceReport:
    issues: list[ConformanceIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    @property
    def rules(self) -> set[str]:
        return {i.rule for i in self.issues}

    def raise_for_issues(self) -> None:
        if self.issues:
            raise ConformanceError(self.issues)


async def check_documents(
    first: Document, second: Document, *, expected_file_uri: str | None = None, push: bool = True
) -> ConformanceReport:
    """对同一份未修改源数据的两次产出运行全部检查。"""
    report = ConformanceReport()
    uri = str(first.file_uri)

    def issue(rule: str, message: str) -> None:
        report.issues.append(ConformanceIssue(rule, message, uri))

    static = check_document(first)
    for violation in static:
        issue("static", violation)
    if expected_file_uri is not None and uri != expected_file_uri:
        issue("file_uri", f"document file_uri {uri} differs from expected {expected_file_uri}")
    if str(second.file_uri) != uri:
        issue("file_uri", f"two builds produced different file_uri ({uri} != {second.file_uri})")

    hash_a, hash_b = compute_hashes(first).doc_hash, compute_hashes(second).doc_hash
    if hash_a != hash_b:
        issue("deterministic", f"two builds produced different doc_hash ({hash_a} != {hash_b})")

    if push and not static:
        server = FakeRobotServer()
        async with server.http_client() as http:
            client = DPEPushClient("http://conformance.test", http_client=http)
            try:
                pushed = await client.push(first)
                again = await client.push(second, base_doc_hash=pushed.doc_hash)
            except Exception as exc:  # noqa: BLE001 - 任何异常都说明文档无法按协议投递
                issue("push_roundtrip", f"push raised {exc!r}")
            else:
                if again.status != "unchanged":
                    issue(
                        "push_roundtrip", f"re-pushing the second build returned {again.status!r}, expected 'unchanged'"
                    )
    return report
