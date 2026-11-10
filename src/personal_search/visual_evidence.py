"""Optional visual evidence: bounded previews, explicit refinements, objective change clues."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import re
import shutil
import time
from contextlib import asynccontextmanager

from filelock import FileLock, Timeout
from PIL import Image, ImageChops, ImageFilter, ImageStat
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .bilibili import BilibiliAdapter, explicit_part, parse_bvid
from .errors import GatewayError
from .models import SearchRequest
from .storage import atomic_json
from .video_media import DASHReader, decode_frame


@asynccontextmanager
async def reserve_visual_slot(gateway):
    # Reuse the queue owner's existing spawn lock, so a worker cannot start
    # network collection midway through a bounded media operation.
    async with gateway.process_guard:
        if gateway.process is not None or gateway.current_id is not None:
            raise GatewayError("MEDIA_BUSY", "采集工作进程仍在运行，请待其结束后进行视觉取证")
        yield


class VideoInspectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    content_id: str = Field(min_length=1, max_length=300, description="BV或保留p参数的标准视频URL")
    timestamps: list[float] | None = Field(
        default=None,
        min_length=1,
        max_length=6,
        description="原视频秒数；不传时按preview_count做稀疏预览。观察后可定向加密。",
    )
    preview_count: int = Field(default=4, ge=1, le=6)
    profile: str = Field(
        default="preview",
        pattern="^(preview|detail)$",
        description="preview选最低分辨率；detail在当前可访问流中选不高于720p。",
    )
    region: tuple[float, float, float, float] | None = Field(
        default=None,
        description="可选归一化x0,y0,x1,y1局部裁图；由模型根据已观察画面选择，非自动参数识别。",
    )
    max_media_mb: int = Field(
        default=16,
        ge=1,
        le=32,
        description="本次网络媒体字节预算；复用已缓存内容不计下载量，非内存上限。",
    )
    max_requests: int = Field(
        default=20,
        ge=5,
        le=40,
        description="包括认证、详情、播放信息、索引及媒体分段的HTTP总预算。",
    )
    refresh: bool = False

    @model_validator(mode="after")
    def bounds(self):
        parse_bvid(self.content_id)
        if self.timestamps:
            if any(not 0 <= t <= 14400 for t in self.timestamps):
                raise ValueError("时间戳必须在0～14400秒")
            self.timestamps = sorted(set(self.timestamps))
        if self.region:
            x0, y0, x1, y1 = self.region
            if not 0 <= x0 < x1 <= 1 or not 0 <= y0 < y1 <= 1:
                raise ValueError("局部区域必须是有效归一化矩形")
        return self


def visual_clues(previous, current):
    gray = current.convert("L").resize((128, 72))
    edge = gray.filter(ImageFilter.FIND_EDGES)
    result = {
        "edge_energy_not_semantic_value": round(ImageStat.Stat(edge).mean[0], 3),
        "pixel_stddev": round(ImageStat.Stat(gray).stddev[0], 3),
    }
    if previous is not None:
        diff = ImageChops.difference(previous.convert("L").resize(gray.size), gray)
        result["mean_absolute_pixel_change_0_255"] = round(ImageStat.Stat(diff).mean[0], 3)
        result["local_grid_changes"] = [
            {
                "region": [x / 4, y / 4, (x + 1) / 4, (y + 1) / 4],
                "mean_absolute_change": round(
                    ImageStat.Stat(diff.crop((x * 32, y * 18, (x + 1) * 32, (y + 1) * 18))).mean[0],
                    3,
                ),
            }
            for y in range(4)
            for x in range(4)
        ]
        result["identical_comparison_pixels"] = diff.getbbox() is None
    return result


class VisualEvidence:
    MAX_DISK_BYTES = 128 * 1024 * 1024
    TTL = 7 * 86400

    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.root = settings.data_dir / "media"
        self.gate = asyncio.Lock()

    def cleanup(self):
        self.root.mkdir(parents=True, exist_ok=True)
        entries = []
        for p in self.root.iterdir():
            if p.name.startswith(".working-"):
                shutil.rmtree(p, ignore_errors=True)
                continue
            if p.is_dir() and re.fullmatch("[0-9a-f]{64}", p.name):
                files = [f for f in p.rglob("*") if f.is_file()]
                age = time.time() - p.stat().st_mtime
                if age > self.TTL:
                    shutil.rmtree(p)
                else:
                    entries.append((p.stat().st_mtime, p, sum(f.stat().st_size for f in files)))
            elif p.is_file() and p.suffix == ".bin":
                if time.time() - p.stat().st_mtime > 86400:
                    p.unlink()
                else:
                    entries.append((p.stat().st_mtime, p, p.stat().st_size))
        size = sum(v[2] for v in entries)
        for _, p, n in sorted(entries):
            if size <= self.MAX_DISK_BYTES:
                break
            shutil.rmtree(p) if p.is_dir() else p.unlink()
            size -= n
        return size

    def read(self, evidence_id):
        if not re.fullmatch("[0-9a-f]{64}", evidence_id):
            raise GatewayError("INVALID_EVIDENCE_ID", "无效的视觉证据标识")
        folder = self.root / evidence_id
        manifest = folder / "evidence.json"
        if not manifest.is_file() or time.time() - manifest.stat().st_mtime > self.TTL:
            raise GatewayError("EVIDENCE_EXPIRED", "视觉证据已过期或被容量清理，请按需重新取证")
        data = json.loads(manifest.read_text())
        images = []
        for frame in data["frames"]:
            # File names are generated locally, never accept a path supplied by callers/manifests.
            path = folder / f"frame-{frame['index']}.png"
            frame["local_image_path"] = str(path)
            if not path.is_file():
                raise GatewayError("EVIDENCE_EXPIRED", "图片已被清理，请按需重新取证")
            image = path.read_bytes()
            if hashlib.sha256(image).hexdigest() != frame["sha256"]:
                raise GatewayError("CONTENT_IDENTITY_MISMATCH", "视觉证据文件校验失败")
            images.append(image)
        folder.touch()
        return data, images

    async def inspect(self, request: VideoInspectRequest):
        # Concurrent decoders across tool invocations/processes are bounded to one.
        async with self.gate:
            self.root.mkdir(parents=True, exist_ok=True)
            lock = FileLock(self.root / ".decode.lock")
            try:
                lock.acquire(timeout=0)
            except Timeout:
                raise GatewayError("MEDIA_BUSY", "另一项视觉取证正在运行，请稍后查询") from None
            try:
                return await self._inspect(request)
            finally:
                lock.release()

    async def _inspect(self, request):
        logging.getLogger("httpx").setLevel(logging.WARNING)
        self.cleanup()
        started = time.perf_counter()
        bvid = parse_bvid(request.content_id)
        part = explicit_part(request.content_id) or 1
        req = SearchRequest(
            query="visual evidence", max_requests=request.max_requests, concurrency=1
        )
        adapter = BilibiliAdapter(self.settings, self.store, "media-evidence", req)
        canonical = {
            **request.model_dump(exclude={"refresh", "max_requests", "max_media_mb"}),
            "content_id": f"{bvid}?p={part}",
            "scope": adapter.cache_scope,
            "pipeline": 1,
        }
        evidence_id = hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()
        folder = self.root / evidence_id
        working = self.root / f".working-{evidence_id}"
        try:
            if not request.refresh:
                try:
                    meta, images = self.read(evidence_id)
                    return {
                        **meta,
                        "operation": {
                            "cached": True,
                            "http_requests": 0,
                            "media_bytes": 0,
                            "elapsed_seconds": round(time.perf_counter() - started, 3),
                        },
                    }, images
                except GatewayError as exc:
                    if exc.code != "EVIDENCE_EXPIRED":
                        raise
            async with asyncio.timeout(120):
                await adapter.preflight()
                detail, source = await adapter.video_details(bvid)
                page = next((p for p in detail["pages"] if int(p.get("page", 0)) == part), None)
                if page is None:
                    raise GatewayError("INVALID_VIDEO_PAGE", "请求分P不存在，不自动改取其他分P")
                duration = float(page.get("duration") or 0)
                if not 0 < duration <= 14400:
                    raise GatewayError(
                        "MEDIA_DURATION_UNSUPPORTED", "当前视觉预览支持最长4小时的单分P"
                    )
                timestamps = request.timestamps or [
                    round(duration * (i + 0.5) / request.preview_count, 3)
                    for i in range(request.preview_count)
                ]
                if any(t >= duration for t in timestamps):
                    raise GatewayError("INVALID_VIDEO_TIMESTAMP", "目标时间必须小于该分P时长")
                binding = {
                    "bvid": bvid,
                    "aid": int(detail["aid"]),
                    "cid": int(page["cid"]),
                    "page": part,
                    "duration": duration,
                }
                reader = DASHReader(
                    adapter, self.root, binding, request.profile, request.max_media_mb * 1024 * 1024
                )
                await reader.prepare()
                working.mkdir()
                frames, images, previous = [], [], None
                for i, t in enumerate(timestamps):
                    media, segment = await reader.fragment(t)
                    try:
                        image, actual = await decode_frame(
                            media, t, 640 if request.profile == "preview" else 1280
                        )
                    except GatewayError as exc:
                        exc.details.update(
                            indexed_segment=[segment.start, segment.end],
                            first_indexed_segment=[
                                reader.segments[0].start,
                                reader.segments[0].end,
                            ],
                        )
                        raise
                    with Image.open(io.BytesIO(image)) as decoded:
                        decoded.load()
                        clues = visual_clues(previous, decoded)
                        previous = decoded.copy()
                        if request.region:
                            x0, y0, x1, y1 = request.region
                            decoded = decoded.crop(
                                (
                                    int(x0 * decoded.width),
                                    int(y0 * decoded.height),
                                    int(x1 * decoded.width),
                                    int(y1 * decoded.height),
                                )
                            )
                            if decoded.width < 1 or decoded.height < 1:
                                raise GatewayError("INVALID_FRAME_REGION", "裁图区域小于一个像素")
                            output = io.BytesIO()
                            decoded.save(output, format="PNG")
                            image = output.getvalue()
                        dims = [decoded.width, decoded.height]
                    images.append(image)
                    if sum(len(v) for v in images) > 8 * 1024 * 1024:
                        raise GatewayError("MEDIA_OUTPUT_BUDGET", "本次图片输出超过8MB")
                    (working / f"frame-{i}.png").write_bytes(image)
                    frames.append(
                        {
                            "index": i,
                            "requested_seconds": t,
                            "actual_seconds": actual,
                            "timestamp_error_seconds": round(actual - t, 6),
                            "extracted_at": time.time(),
                            "dimensions": dims,
                            "region": request.region,
                            "features_scope": "full_frame",
                            "sha256": hashlib.sha256(image).hexdigest(),
                            "local_image_path": str(folder / f"frame-{i}.png"),
                            "clues": clues,
                            "source_segment_seconds": [segment.start, segment.end],
                        }
                    )
                meta = {
                    "status": "complete",
                    "evidence_id": evidence_id,
                    "binding": binding,
                    "source_url": f"https://www.bilibili.com/video/{bvid}/?p={part}",
                    "title": detail.get("title", ""),
                    "metadata_provenance": source,
                    "quality": reader.quality,
                    "frames": frames,
                    "method": "bounded_DASH_sidx_segments_local_FFmpeg_accurate_pts_no_semantic_scoring",
                    "scope": "requested_frames_only_not_full_video_understanding",
                    "source_content_is_untrusted": True,
                    "image_order": "MCP image blocks follow frames array order",
                    "created_at": time.time(),
                    "cache_policy": {
                        "ttl_seconds": self.TTL,
                        "shared_disk_cap_bytes": self.MAX_DISK_BYTES,
                    },
                    "operation": {
                        "cached": False,
                        "http_requests": adapter.http.requests,
                        "media_bytes": reader.bytes_received,
                        "segment_cache_hits": reader.cache_hits,
                        "elapsed_seconds": round(time.perf_counter() - started, 3),
                    },
                }
                atomic_json(working / "evidence.json", meta)
                if folder.exists():
                    shutil.rmtree(folder)
                working.rename(folder)
                self.cleanup()
                return meta, images
        except TimeoutError:
            raise GatewayError("MEDIA_TIMEOUT", "视觉取证超时，已停止解码并清理临时文件") from None
        finally:
            shutil.rmtree(working, ignore_errors=True)
            try:
                self.cleanup()
            finally:
                await adapter.close()
