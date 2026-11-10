import gzip
import json

import httpx
import psutil
import pytest

from personal_search.bilibili import BilibiliAdapter
from personal_search.http import HEADERS, Transport
from personal_search.models import SearchRequest
from personal_search.settings import Settings
from personal_search.storage import Store

BV = "BV1GJ411x7h7"


@pytest.fixture(autouse=True)
def cleanup_owned_login_selector(tmp_path):
    yield
    manifest = tmp_path / "auth/panel.json"
    if manifest.exists():
        value = json.loads(manifest.read_text())
        try:
            process = psutil.Process(value["pid"])
            if abs(process.create_time() - value["created"]) < .01:
                process.terminate()
                process.wait(5)
        except psutil.NoSuchProcess:
            pass


class PlatformFixture:
    """Deterministic protocol fixture, never described as real platform data."""

    def __init__(self):
        self.calls = []
        self.fail = None
        self.search_empty = False
        self.comments_end = True

    def __call__(self, request):
        self.calls.append(request)
        path = request.url.path
        q = request.url.params
        if self.fail:
            response = self.fail(request)
            if response is not None:
                return response
        if path == "/x/web-interface/nav":
            return httpx.Response(
                200,
                json={
                    "code": -101,
                    "data": {
                        "isLogin": False,
                        "wbi_img": {
                            "img_url": "https://i0.hdslb.com/" + "a" * 32 + ".png",
                            "sub_url": "https://i0.hdslb.com/" + "b" * 32 + ".png",
                        },
                    },
                },
            )
        if path.endswith("search/type"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "numPages": 2,
                        "numResults": 0 if self.search_empty else 2,
                        "result": []
                        if self.search_empty
                        else [
                            {
                                "bvid": BV,
                                "title": '<em class="keyword">FL Studio</em> 教程',
                                "author": "fixture",
                                "description": "Rawstyle Screech",
                            }
                        ],
                    },
                },
            )
        if path.endswith("/view"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "bvid": BV,
                        "aid": 99,
                        "title": "Fixture FL Studio 教程",
                        "desc": "Rawstyle Screech",
                        "pages": [{"page": 1, "cid": 100, "part": "P1", "duration": 5}],
                        "owner": {"name": "fixture"},
                        "stat": {"reply": 3},
                    },
                },
            )
        if path.endswith("/v2") and "/player/" in path:
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "bvid": q.get("bvid"),
                        "aid": int(q.get("aid", 99)),
                        "cid": int(q.get("cid", 100)),
                        "subtitle": {
                            "subtitles": [
                                {
                                    "lan": "zh-CN",
                                    "lan_doc": "中文",
                                    "subtitle_url": "https://aisubtitle.hdslb.com/fixture.json?auth_key=example-secret",
                                }
                            ]
                        },
                    },
                },
            )
        if path == "/fixture.json":
            return httpx.Response(
                200, json={"body": [{"from": 0.0, "to": 2.0, "content": "Fixture Rawstyle 教程"}]}
            )
        if path.endswith("reply/wbi/main"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "cursor": {"is_end": self.comments_end, "next": int(q.get("next", 0)) + 1},
                        "replies": [
                            {
                                "rpid": 1,
                                "content": {"message": "Fixture FLP resource"},
                                "member": {"uname": "test"},
                                "rcount": 1,
                            }
                        ],
                    },
                },
            )
        if path.endswith("reply/reply"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "page": {"count": 1},
                        "replies": [
                            {
                                "rpid": 2,
                                "root": 1,
                                "parent": 1,
                                "content": {"message": "Fixture reply"},
                            }
                        ],
                    },
                },
            )
        if path.endswith(".xml"):
            return httpx.Response(
                200,
                content=gzip.compress(
                    b'<i><d p="1.5,1,25,16777215,1000,0,abc,123">fixture danmaku</d></i>'
                ),
                headers={"content-encoding": "gzip"},
            )
        return httpx.Response(404)


@pytest.fixture
def env(tmp_path):
    settings = Settings(tmp_path, request_interval=0)
    settings.prepare()
    store = Store(tmp_path)
    platform = PlatformFixture()

    def factory(request=None, task_id=None):
        request = request or SearchRequest(query="FL Studio", max_items=1)
        task_id = task_id or store.create_task(request.model_dump())["id"]
        client = httpx.AsyncClient(transport=httpx.MockTransport(platform), headers=HEADERS)
        transport = Transport(
            interval=0,
            client=client,
            max_requests=request.max_requests,
            concurrency=request.concurrency,
        )
        return BilibiliAdapter(settings, store, task_id, request, transport=transport)

    return settings, store, platform, factory
