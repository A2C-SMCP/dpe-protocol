import pytest

from dpe_protocol import Document, ElementMetadata, check_document, make_dpe_uri
from tests.conftest import make_doc


def test_make_dpe_uri() -> None:
    assert make_dpe_uri("acme-crm", "/docs/a b.md") == "dpe://acme-crm/docs/a%20b.md"
    with pytest.raises(ValueError):
        make_dpe_uri("Acme", "x")
    with pytest.raises(ValueError):
        make_dpe_uri("acme", "/")


def test_check_document() -> None:
    assert check_document(make_doc("a")) == []
    assert check_document(make_doc("a", file_uri="https://acme.com/x"))[0].startswith("file_uri must use")
    assert "differs from expected" in check_document(make_doc("a"), expected_file_uri="dpe://acme/other")[0]
    doc = make_doc("a")
    doc.pages[0].elements[-1].ele_metadata = ElementMetadata(image_path="r1/x.png")
    assert "image_path" in check_document(doc)[0]


def test_from_kernel_dump_strips_kernel_fields() -> None:
    doc = Document.from_kernel_dump(
        {
            "doc_id": 7, "doc_hash": "x", "hash_strategy_uri": "hash-strategy://default?algo_version=v1",
            "file_uri": "dpe://acme/a", "file_type": "txt", "doc_metadata": {"created_at": None},
            "pages": [{"page_id": 1, "page_hash": "y", "number": 0, "title": None,
                       "elements": [{"ele_id": 3, "seq_in_page": 0, "content_hash": "z", "text": "hello"}]}],
        }
    )  # fmt: skip
    assert doc.pages[0].elements[0].text == "hello"
