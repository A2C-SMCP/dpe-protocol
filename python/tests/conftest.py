import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from dpe_protocol import DocElement, DocMetadata, DocPage, Document, DPEPushClient, ElementCategory, ElementMetadata
from dpe_protocol.testing import FakeRobotServer

# 让 tests/examples 能 import 示例脚本
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "examples"))


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture
def server() -> FakeRobotServer:
    return FakeRobotServer()


@pytest.fixture
async def client(server: FakeRobotServer) -> AsyncIterator[DPEPushClient]:
    async with server.http_client() as http:
        c = DPEPushClient("http://robot.test", token="t0ken", robot_id="rid-1", http_client=http)
        c._sleep = _no_sleep
        yield c


def make_doc(*texts: str, file_uri: str = "dpe://acme/handbook") -> Document:
    return Document.model_validate(
        {
            "file_uri": file_uri,
            "file_type": "md",
            "doc_metadata": DocMetadata(filename="handbook.md"),
            "pages": [
                DocPage(
                    number=0,
                    title="第一章",
                    elements=[
                        DocElement(category=ElementCategory.TITLE, text="员工手册"),
                        *(DocElement(category=ElementCategory.NARRATIVE_TEXT, text=t) for t in texts),
                        DocElement(
                            category=ElementCategory.IMAGE,
                            ele_metadata=ElementMetadata(image_url="https://cdn/x.png", image_mime_type="image/png"),
                        ),
                    ],
                )
            ],
        }
    )
