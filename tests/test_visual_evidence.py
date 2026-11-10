import io
import shutil
import struct
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from PIL import Image
from pydantic import ValidationError

from personal_search.errors import GatewayError
from personal_search.http import HEADERS, Transport
from personal_search.settings import Settings
from personal_search.storage import Store, atomic_json
from personal_search.video_media import (
    DASHReader,
    Segment,
    decode_frame,
    parse_sidx,
    validate_media_url,
)
from personal_search.visual_evidence import VideoInspectRequest, VisualEvidence, visual_clues


def sidx(version=0, hierarchical=False):
    payload = bytes([version, 0, 0, 0]) + struct.pack(">II", 1, 1000)
    payload += struct.pack(">II" if version == 0 else ">QQ", 0, 10)
    payload += struct.pack(">HH", 0, 2)
    payload += struct.pack(">III", 100 | (0x80000000 if hierarchical else 0), 5000, 0x90000000)
    payload += struct.pack(">III", 200, 5000, 0x90000000)
    return struct.pack(">I4s", len(payload) + 8, b"sidx") + payload


@pytest.mark.parametrize("version", [0, 1])
def test_sidx_index_absolute_offsets_and_times(version):
    data = sidx(version)
    rows = parse_sidx(data, 1000)
    assert rows == [
        Segment(0, 5, 1000 + len(data) + 10, 100),
        Segment(5, 10, 1110 + len(data), 200),
    ]


@pytest.mark.parametrize("data", [b"invalid", sidx(hierarchical=True), sidx()[:-1]])
def test_unsupported_indexes_fail_without_whole_media_fallback(data):
    with pytest.raises(GatewayError, match="索引"):
        parse_sidx(data, 0)


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.bilivideo.com/a",
        "https://evil.test/a",
        "https://bilivideo.com.evil.test/a",
        "https://user:secret@cdn.bilivideo.com/a",
        "https://127.0.0.1/a",
    ],
)
def test_media_url_cannot_expand_to_arbitrary_hosts(url):
    with pytest.raises(GatewayError):
        validate_media_url(url)


def test_request_bounds_and_local_change_independent_from_global_change():
    for value in ([-1], [float("nan")], [float("inf")]):
        with pytest.raises(ValidationError):
            VideoInspectRequest(content_id="BV1yvtv6VEuv", timestamps=value)
    with pytest.raises(ValidationError):
        VideoInspectRequest(content_id="BV1yvtv6VEuv", region=(0.9, 0, 0.1, 1))
    before = Image.new("L", (128, 72), 0)
    after = before.copy()
    after.paste(255, (0, 0, 8, 8))
    clues = visual_clues(before, after)
    assert (
        max(r["mean_absolute_change"] for r in clues["local_grid_changes"])
        > clues["mean_absolute_pixel_change_0_255"]
    )
    assert "frame_score" not in clues


def make_reader(tmp_path, handler, budget=1000):
    from types import SimpleNamespace

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=HEADERS)
    transport = Transport(
        {"SESSDATA": "fixture-only"}, interval=0, max_requests=5, concurrency=1, client=client
    )
    reader = DASHReader(
        SimpleNamespace(http=transport),
        tmp_path,
        {"bvid": "BV1yvtv6VEuv", "cid": 100},
        "preview",
        budget,
    )
    reader.url = "https://cdn.bilivideo.com/segment.m4s?transient=not-to-archive"
    return reader


async def test_media_ranges_reuse_budget_and_do_not_send_bilibili_cookies_to_cdn(tmp_path):
    seen = []

    def handler(r):
        seen.append(r)
        return httpx.Response(206, headers={"Content-Range": "bytes 10-13/100"}, content=b"data")

    r = make_reader(tmp_path, handler)
    assert await r.range(10, 13) == b"data"
    assert r.bytes_received == 4 and r.adapter.http.requests == 1
    assert seen[0].headers["range"] == "bytes=10-13" and "cookie" not in seen[0].headers
    await r.adapter.http.close()


async def test_media_full_response_is_rejected_without_consuming_body(tmp_path):
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError("must not read full response")
            yield b""

    r = make_reader(tmp_path, lambda _: httpx.Response(200, stream=Body()))
    with pytest.raises(GatewayError) as error:
        await r.range(0, 3)
    assert error.value.code == "MEDIA_RANGE_UNSUPPORTED" and r.bytes_received == 0
    await r.adapter.http.close()


async def test_media_risk_control_stops_without_retry_or_another_request(tmp_path):
    r = make_reader(tmp_path, lambda _: httpx.Response(412))
    for _ in range(2):
        with pytest.raises(GatewayError) as error:
            await r.range(0, 3)
        assert error.value.code == "RISK_CONTROL"
    assert r.adapter.http.requests == 1
    await r.adapter.http.close()


async def test_media_byte_budget_and_wrong_ranges_are_rejected(tmp_path):
    r = make_reader(
        tmp_path,
        lambda _: httpx.Response(
            206, headers={"Content-Range": "bytes 0-3/100"}, content=b"too-long"
        ),
    )
    with pytest.raises(GatewayError) as error:
        await r.range(0, 3)
    assert error.value.code == "MEDIA_BYTE_BUDGET"
    with pytest.raises(GatewayError):
        await r.range(0, 1000)
    await r.adapter.http.close()


async def test_sidx_drift_uses_bounded_next_segment_and_reuses_cached_fragments(tmp_path):
    r = make_reader(tmp_path, lambda _: httpx.Response(500))
    r.segments = [Segment(85.0006, 90.0006, 100, 3), Segment(90.0006, 95.0006, 103, 3)]
    r.init = b"init"
    r.fingerprint = "fixture"
    calls = []

    async def fetch(a, b):
        calls.append((a, b))
        return b"xyz"

    r.range = fetch
    result, coverage = await r.fragment(90)
    assert result == b"initxyzxyz" and coverage.end == 95.0006
    assert calls == [(100, 102), (103, 105)]
    await r.fragment(90)
    assert len(calls) == 2 and r.cache_hits == 2
    await r.adapter.http.close()


async def test_real_ffmpeg_decodes_actual_timestamp_and_cleans_temporary_input(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg unavailable")
    path = tmp_path / "fixture.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=10",
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            str(path),
        ],
        check=True,
    )
    data, timestamp = await decode_frame(path.read_bytes(), 1.2, 160)
    assert timestamp == pytest.approx(1.2, abs=0.001)
    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (160, 90)


def test_evidence_read_validates_hash_expiry_and_caller_id(tmp_path):
    service = VisualEvidence(Settings(tmp_path), Store(tmp_path))
    folder = service.root / ("a" * 64)
    folder.mkdir(parents=True)
    (folder / "frame-0.png").write_bytes(b"changed")
    atomic_json(folder / "evidence.json", {"frames": [{"index": 0, "sha256": "wrong"}]})
    with pytest.raises(GatewayError) as error:
        service.read("a" * 64)
    assert error.value.code == "CONTENT_IDENTITY_MISMATCH"
    with pytest.raises(GatewayError):
        service.read("../../auth/bilibili.json")
    import os

    past = time.time() - service.TTL - 1
    os.utime(folder / "evidence.json", (past, past))
    with pytest.raises(GatewayError) as error:
        service.read("a" * 64)
    assert error.value.code == "EVIDENCE_EXPIRED"


def test_cache_cleanup_bounds_disk_and_removes_old_workspaces(tmp_path):
    service = VisualEvidence(Settings(tmp_path), Store(tmp_path))
    service.root.mkdir()
    service.MAX_DISK_BYTES = 10
    (service.root / "x.bin").write_bytes(b"x" * 20)
    (service.root / ".working-old").mkdir()
    assert service.cleanup() == 0
    assert not (service.root / "x.bin").exists() and not (service.root / ".working-old").exists()


async def test_visual_service_preserves_part_and_refuses_nonexistent_part(env, monkeypatch):
    import personal_search.visual_evidence as module

    settings, store, platform, factory = env
    monkeypatch.setattr(module, "BilibiliAdapter", lambda *args: factory())
    service = VisualEvidence(settings, store)
    with pytest.raises(GatewayError) as error:
        await service.inspect(VideoInspectRequest(content_id="BV1GJ411x7h7?p=2", timestamps=[1]))
    assert error.value.code == "INVALID_VIDEO_PAGE"
    assert not any("playurl" in r.url.path or r.url.path.endswith(".xml") for r in platform.calls)


async def test_visual_service_without_subtitles_crops_archives_and_reuses_frames(env, monkeypatch):
    import personal_search.visual_evidence as module

    settings, store, platform, factory = env
    monkeypatch.setattr(module, "BilibiliAdapter", lambda *args: factory())
    picture = io.BytesIO()
    Image.new("RGB", (64, 32), "red").save(picture, format="PNG")

    class FakeReader:
        def __init__(self, *args):
            self.quality = {"id": 16, "width": 64, "height": 32}
            self.bytes_received = 123
            self.cache_hits = 0

        async def prepare(self):
            pass

        async def fragment(self, t):
            return b"fixture", Segment(0, 5, 0, 123)

    async def decode(*args):
        return picture.getvalue(), 1.0

    monkeypatch.setattr(module, "DASHReader", FakeReader)
    monkeypatch.setattr(module, "decode_frame", decode)
    service = VisualEvidence(settings, store)
    request = VideoInspectRequest(
        content_id="BV1GJ411x7h7", timestamps=[1], region=(0, 0, 0.5, 0.5)
    )
    meta, images = await service.inspect(request)
    assert meta["binding"]["cid"] == 100 and meta["binding"]["page"] == 1
    assert meta["frames"][0]["actual_seconds"] == 1 and meta["frames"][0]["dimensions"] == [32, 16]
    assert Path(meta["frames"][0]["local_image_path"]).read_bytes() == images[0]
    before = len(platform.calls)
    warm, again = await service.inspect(request)
    assert (
        again == images
        and warm["operation"]["http_requests"] == 0
        and len(platform.calls) == before
    )
    assert not any("/player/" in r.url.path or "/reply/" in r.url.path for r in platform.calls)


async def test_missing_ffmpeg_is_explicit_without_changing_existing_content_capabilities(
    monkeypatch,
):
    from personal_search import capabilities as discovery
    from personal_search import video_media

    monkeypatch.setattr(discovery, "ffmpeg_path", lambda: None)
    report = discovery.capabilities()["platforms"]["bilibili"]
    assert report["visual_evidence"]["available_in_runtime"] is False
    assert all(report[k] for k in ("search", "subtitles", "comments", "danmaku"))
    monkeypatch.setattr(video_media, "ffmpeg_path", lambda: None)
    with pytest.raises(GatewayError) as error:
        await decode_frame(b"fixture", 0, 640)
    assert error.value.code == "MEDIA_RUNTIME_UNAVAILABLE"


async def test_visual_slot_excludes_worker_spawn_and_reports_busy():
    import asyncio
    from types import SimpleNamespace

    from personal_search.visual_evidence import reserve_visual_slot

    gateway = SimpleNamespace(process_guard=asyncio.Lock(), process=None, current_id=None)
    spawned = asyncio.Event()

    async def worker_spawn():
        async with gateway.process_guard:
            spawned.set()

    async with reserve_visual_slot(gateway):
        task = asyncio.create_task(worker_spawn())
        await asyncio.sleep(0)
        assert not spawned.is_set()
    await task
    assert spawned.is_set()
    gateway.process = object()
    with pytest.raises(GatewayError) as error:
        async with reserve_visual_slot(gateway):
            raise AssertionError("busy visual slot must not enter")
    assert error.value.code == "MEDIA_BUSY"
