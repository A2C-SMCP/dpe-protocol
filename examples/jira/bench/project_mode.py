"""基准测试变体「项目 = 文档」（#237 §1）：**只用于测试**，不进插件的正式映射，也不是配置开关。

与正式插件（「工单 = 文档」，``dpe_jira.server.Serve``）除粒度外完全一致：同一套枚举、游标编码、
单条消息预算与分段、ADF 转换、字段与附件规则（``dpe_jira.mapping``）。差别只在文档的组织：

- **每个项目一篇** ``jira_project`` 文档（``file_uri`` = 前缀 + ``project/<key>``）：先是项目自己的
  三页（``description`` / ``components`` / ``versions``，同 ``project_document``），之后每个工单依次
  占正文、评论、字段、附件页（同 ``issue_document``），工单按 key 的数字部分升序；
- 工单页的 ``page_metadata`` 在原有 ``section`` 之外带 ``issue_id``、``issue_key``；正文页另带
  ``title``（summary）与 ``created_at``——工单文档对象上的这些字段在这里没有别处可放；
- **判变化**：游标编码与正式插件相同（工单 id → [key, updated]，项目 id → [key, 摘要]），只是项目
  的摘要是「项目三页的内容摘要 : 成员指纹」，成员指纹覆盖项目内全部工单的（id、key、updated）。
  一个项目里任一工单新增、消失、移入移出、updated 变化，或项目三页变化，这条记录就变了，项目
  即重组；判断只看这一条记录，所以整篇失败回退之后（含本轮已消失或移出的工单）下一轮一定重组；
- **变了就整篇重组**：插件跨轮只有游标，无法缓存全部工单的内容，所以重新拉取该项目的**全部**
  工单（全文与评论）来组装整篇文档。这是这种粒度的真实成本，不做绕过（例如把内容塞进游标）；
- 工单少了即少了那几页；工单换项目即两篇各自重组（没有跨文档的 move）；项目改 key 仍表达为
  ``move``；项目消失（``jql`` 范围内不再有它的工单）即删除整篇；
- **失败影响面**：任一工单读到 404（枚举后被删）或无法映射，整篇项目文档都不产出（条目级
  ``source_unavailable`` / ``content_invalid``），项目与其全部工单的游标记录沿用上一轮；
  暂时性失败与正式插件一样中止本轮；key 里找不到所属项目的工单（不含 ``-``，或项目改 key 后枚举
  里仍带旧前缀）不进任何文档，也不报错——正式插件里它们各有一篇文档。这是边角情形，基准数据不涉及；
- **未覆盖**：项目改 key（表达为 ``move``）只经代码推演，没有端到端用例——模拟 Jira 只能改工单的
  key，不能改项目的 key。

**与正式插件的耦合**：组轮开头（枚举、取项目、解释游标、矛盾判定，``Serve._prepare_round``）、
删除推断（``_deletions``）、取评论、单条预算与分段（``_emit``）都直接调用 ``Serve`` 的实现，
不复制；依赖的私有成员即上面导入的那些。正式插件改了这些部分，变体随之改变——两种模式「除粒度
外完全一致」靠的就是共用同一份代码。
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from dpe_hash import jcs

from dpe_jira import cli
from dpe_jira.client import JiraError, NotFound
from dpe_jira.mapping import ISSUE_FIELDS, MappingError, issue_document, project_document
from dpe_jira.server import (
    Serve,
    _content_digest,
    _error_item,
    _Issue,
    _Out,
    _Project,
    _Ref,
    _Round,
    _RoundAborted,
    _Step,
    issue_uri,
    project_uri,
)

__all__ = ["ProjectServe", "main"]


def _project_key(issue_key: str) -> str:
    return issue_key.rsplit("-", 1)[0]


def _issue_order(issue: _Issue) -> tuple[int, str]:
    """工单在项目文档里的顺序：key 的数字部分升序（ABC-9 在 ABC-10 之前），再按 key 兜底。"""
    number = issue.key.rsplit("-", 1)[-1]
    return (int(number) if number.isdigit() else -1, issue.key)


@dataclass
class _ProjectRound(_Round):
    """项目模式的一轮：一篇项目文档对应多条游标记录（项目自己 + 其中每个工单）。"""

    #: 项目 ref → 本轮属于它的工单（按文档顺序）
    members: dict[_Ref, list[_Issue]] = field(default_factory=dict)
    #: 项目 ref → 文档完整发出后要写入的工单记录
    pending_marks: dict[_Ref, dict[_Ref, tuple[str, str]]] = field(default_factory=dict)

    def commit(self, out: _Out) -> None:
        super().commit(out)
        if out.ref is not None:
            self.marks.update(self.pending_marks.pop(out.ref, {}))

    def revert(self, ref: _Ref | None) -> None:
        """整篇没产出：项目与它本轮全部工单的记录都沿用上一轮（新的不记）。

        项目的记录里带着成员指纹（``_members_fingerprint``）：回退到上一轮的值之后，下一轮的指纹
        必然与之不同，这个项目一定重组——不依赖逐个工单记录（含本轮已消失或移出的）能否回退。
        """
        super().revert(ref)
        if ref is None:
            return
        self.pending_marks.pop(ref, None)
        for issue in self.members.get(ref, []):
            super().revert(("issue", issue.id))


def _members_fingerprint(members: list[_Issue]) -> str:
    """项目内全部工单的（id、key、updated）指纹：新增、消失、移入移出、更新都会改变它。"""
    rows = sorted([issue.id, issue.key, issue.updated] for issue in members)
    return hashlib.sha256(jcs(rows).encode("utf-8")).hexdigest()[:32]


class ProjectServe(Serve):
    """「项目 = 文档」变体：只改组轮与项目文档的产出，其余沿用 ``Serve``。"""

    def _configure(self, prefix: Any, config: Any) -> str | None:
        """与正式插件相同的握手校验，另要求产出项目文档（项目模式的文档就是项目文档）。"""
        problem = super()._configure(prefix, config)
        if problem is None and not self._include_projects:
            return "项目模式要求 include_project_docs 为 true"
        return problem

    def _open_round(self, cursor: str | None) -> _Round:
        prepared = self._prepare_round(cursor)
        by_key: dict[str, list[_Issue]] = defaultdict(list)
        for issue in prepared.current:
            by_key[_project_key(issue.key)].append(issue)
        members = {
            ("project", p.id): sorted(by_key.get(p.key, []), key=_issue_order)
            for p in prepared.projects
        }
        steps: list[_Step] = []
        previous = prepared.previous
        if previous is not None:  # 增量轮：消失的项目整篇删除（工单没有自己的文档）
            steps = self._deletions(prepared, kinds=("project",))
        for project in prepared.projects:
            old = None if previous is None else previous.get(("project", project.id))
            moved = old[0] if old is not None and old[0] != project.key else None
            steps.append(_Step(project=project, moved_from=moved))
        return _ProjectRound(
            self._rounds,
            steps,
            incremental=previous is not None,
            previous=previous or {},
            marks={},
            members=members,
        )

    def _project_step(self, round_: _Round, step: _Step, budget: int) -> list[_Out]:
        project = step.project
        assert project is not None and self._client is not None
        assert isinstance(round_, _ProjectRound)
        ref: _Ref = ("project", project.id)
        uri = project_uri(self.prefix, project.key)
        try:
            components = self._client.components(project.key)
            versions = self._client.versions(project.key)
        except NotFound:
            round_.revert(ref)
            return [_Out(_error_item(uri, "source_unavailable", "项目在枚举之后不可读", True))]
        except JiraError as exc:
            raise _RoundAborted(f"读取项目失败：{exc}", exc.retryable) from None
        try:
            own = project_document(uri, project.raw, components, versions)
        except MappingError as exc:
            round_.revert(ref)
            return [_Out(_error_item(uri, "content_invalid", f"项目内容无法映射：{exc}", False))]
        members = round_.members.get(ref, [])
        mark = (project.key, f"{_content_digest(own)}:{_members_fingerprint(members)}")
        if round_.incremental and round_.previous.get(ref) == mark:
            # 项目三页与全部工单都没变（key 也没变，所以不是移动）：跳过，记录照旧
            round_.marks[ref] = mark
            round_.marks.update({("issue", i.id): (i.key, i.updated) for i in members})
            return []
        item = self._assemble(round_, ref, project, own)
        if item["kind"] != "document":
            round_.revert(ref)
            return [_Out(item)]
        return self._emit(round_, ref, mark, item, step.moved_from, budget)

    def _assemble(
        self, round_: _ProjectRound, ref: _Ref, project: _Project, own: dict[str, Any]
    ) -> dict[str, Any]:
        """重新拉取项目内全部工单，组装整篇文档；任一工单失败即整篇失败（返回 error 条目）。"""
        assert self._client is not None
        uri = own["file_uri"]
        pages = list(own["pages"])
        issue_marks: dict[_Ref, tuple[str, str]] = {}
        for entry in round_.members.get(ref, []):
            try:
                issue = self._client.issue(entry.id, ISSUE_FIELDS)
                comments = self._all_comments(entry.id)
            except NotFound:
                return _error_item(
                    uri, "source_unavailable", f"工单 {entry.key} 在枚举之后被删除或移走", True
                )
            except JiraError as exc:
                raise _RoundAborted(f"读取工单失败：{exc}", exc.retryable) from None
            if not isinstance(issue, dict):
                return _error_item(uri, "content_invalid", "工单响应不是对象", False)
            try:
                mapped = issue_document(issue_uri(self.prefix, entry.key), issue, comments)
            except MappingError as exc:
                return _error_item(
                    uri, "content_invalid", f"工单 {entry.key} 内容无法映射：{exc}", False
                )
            pages.extend(_issue_pages(entry, mapped))
            issue_marks[("issue", entry.id)] = (entry.key, entry.updated)
        round_.pending_marks[ref] = issue_marks
        return {**own, "pages": pages}


def _issue_pages(entry: _Issue, mapped: dict[str, Any]) -> list[dict[str, Any]]:
    """工单文档的各页 → 项目文档里的工单页（``page_metadata`` 补上工单标识）。"""
    document = mapped["document"]
    out: list[dict[str, Any]] = []
    for page in mapped["pages"]:
        metadata = {**page["page_metadata"], "issue_id": entry.id, "issue_key": entry.key}
        if metadata["section"] == "body":
            if "title" in document:
                metadata["title"] = document["title"]
            metadata["created_at"] = document["doc_metadata"]["created_at"]
        out.append({"page_metadata": metadata, "elements": page["elements"]})
    return out


def main() -> int:
    """插件入口：与正式插件同一个 stdio 循环，只换成 ``ProjectServe``。"""
    return cli.main(ProjectServe())


if __name__ == "__main__":
    raise SystemExit(main())
