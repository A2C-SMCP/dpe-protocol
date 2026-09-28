from datetime import datetime

from dpe_protocol import ElementMetadata
from dpe_protocol.testing import check_documents
from tests.conftest import make_doc


async def test_deterministic_documents_pass() -> None:
    report = await check_documents(make_doc("a"), make_doc("a"), expected_file_uri="dpe://acme/handbook")
    assert report.ok
    report.raise_for_issues()


async def test_nondeterministic_metadata_is_caught() -> None:
    first, second = make_doc("a"), make_doc("a")
    first.doc_metadata.created_at = datetime(2026, 1, 1, 0, 0, 0)
    second.doc_metadata.created_at = datetime(2026, 1, 1, 0, 0, 1)  # 典型错误：写入当前时间
    report = await check_documents(first, second)
    assert report.rules == {"deterministic", "push_roundtrip"}


async def test_file_uri_and_static_violations_are_caught() -> None:
    first = make_doc("a", file_uri="dpe://acme/other")
    first.pages[0].elements[-1].ele_metadata = ElementMetadata(image_path="r1/x.png")
    second = make_doc("a", file_uri="dpe://acme/third")
    report = await check_documents(first, second, expected_file_uri="dpe://acme/a")
    assert {"static", "file_uri"} <= report.rules
    assert len([i for i in report.issues if i.rule == "file_uri"]) == 2
