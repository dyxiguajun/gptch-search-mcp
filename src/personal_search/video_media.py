"""Bounded DASH SegmentBase access. URLs stay in memory; FFmpeg only sees local files."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import struct
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .errors import GatewayError


def ffmpeg_path():
    found = shutil.which("ffmpeg")
    if found:
        return found
    return next(
        (
            p
            for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg")
            if os.path.isfile(p) and os.access(p, os.X_OK)
        ),
        None,
    )


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    offset: int
    size: int


def byte_range(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+-\d+", value):
        raise GatewayError("MEDIA_FORMAT_UNSUPPORTED", "缺少可信的DASH字节范围")
    start, end = map(int, value.split("-"))
    if start > end or end - start > 262144:
        raise GatewayError("MEDIA_FORMAT_UNSUPPORTED", "媒体初始化/索引范围过大或无效")
    return start, end


def parse_sidx(data: bytes, absolute_start: int) -> list[Segment]:
    """ISO BMFF sidx v0/v1; hierarchical indexes are explicitly unsupported."""
    pos = 0
    try:
        while pos + 8 <= len(data):
            size, kind = struct.unpack_from(">I4s", data, pos)
            if size < 8 or pos + size > len(data):
                break
            if kind != b"sidx":
                pos += size
                continue
            version = data[pos + 8]
            if version not in (0, 1):
                break
            scale = struct.unpack_from(">I", data, pos + 16)[0]
            if not scale:
                break
            fmt = ">II" if version == 0 else ">QQ"
            earliest, first = struct.unpack_from(fmt, data, pos + 20)
            cursor = pos + (28 if version == 0 else 36)
            count = struct.unpack_from(">H", data, cursor + 2)[0]
            cursor += 4
            if not 0 < count <= 20000 or cursor + 12 * count > pos + size:
                break
            byte = absolute_start + pos + size + first
            timestamp = earliest / scale
            segments = []
            for _ in range(count):
                ref, ticks, _sap = struct.unpack_from(">III", data, cursor)
                cursor += 12
                if ref >> 31 or not ref or not ticks:
                    raise ValueError
                duration = ticks / scale
                segments.append(Segment(timestamp, timestamp + duration, byte, ref))
                byte += ref
                timestamp += duration
            return segments
    except (struct.error, ValueError, IndexError):
        pass
    raise GatewayError("MEDIA_FORMAT_UNSUPPORTED", "未找到受支持的独立DASH分段索引；不回退完整下载")


def validate_media_url(url):
    p = urlsplit(url)
    host = p.hostname or ""
    if (
        p.scheme != "https"
        or p.username
        or p.password
        or p.port not in (None, 443)
        or not any(
            host.endswith("." + domain)
            for domain in ("bilivideo.com", "bilivideo.cn", "akamaized.net", "hdslb.com")
        )
    ):
        raise GatewayError("MEDIA_URL_NOT_ALLOWED", "媒体地址不在允许的平台CDN范围内")


class DASHReader:
    def __init__(self, adapter, cache: Path, binding: dict, profile: str, max_bytes: int):
        self.adapter, self.cache, self.binding = adapter, cache, binding
        self.profile, self.max_bytes = profile, max_bytes
        self.bytes_received = 0
        self.cache_hits = 0
        self.url = ""

    async def range(self, start, end):
        if end < start or end - start + 1 > min(
            8 * 1024 * 1024, self.max_bytes - self.bytes_received
        ):
            raise GatewayError("MEDIA_BYTE_BUDGET", "目标分段超过剩余媒体字节预算")
        validate_media_url(self.url)
        http = self.adapter.http
        await http.gate.enter()
        throttled, success = False, False
        try:
            async with http.pacing:
                if http.blocked:
                    raise http.blocked
                if http.requests >= http.max_requests:
                    raise GatewayError("REQUEST_BUDGET_EXCEEDED", "已达到媒体任务HTTP预算")
                await asyncio.sleep(max(0, http.interval - (time.monotonic() - http.last_request)))
                if http.blocked:
                    raise http.blocked
                http.last_request = time.monotonic()
                http.requests += 1
            async with http.client.stream(
                "GET",
                self.url,
                headers={"Range": f"bytes={start}-{end}", "Accept-Encoding": "identity"},
            ) as response:
                if response.status_code in (403, 412, 429):
                    throttled = True
                    http.blocked = GatewayError(
                        "RATE_LIMITED" if response.status_code == 429 else "RISK_CONTROL",
                        "媒体接口风控，停止取证",
                        http_status=response.status_code,
                    )
                    raise http.blocked
                match = re.fullmatch(
                    r"bytes (\d+)-(\d+)/(\d+|\*)", response.headers.get("content-range", "")
                )
                if (
                    response.status_code != 206
                    or not match
                    or (int(match[1]), int(match[2])) != (start, end)
                ):
                    raise GatewayError(
                        "MEDIA_RANGE_UNSUPPORTED", "CDN未准确响应字节范围，拒绝读取完整响应"
                    )
                chunks, size = [], 0
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    self.bytes_received += len(chunk)
                    size += len(chunk)
                    if self.bytes_received > self.max_bytes or size > end - start + 1:
                        raise GatewayError("MEDIA_BYTE_BUDGET", "媒体响应超过字节预算")
                    chunks.append(chunk)
                if size != end - start + 1:
                    raise GatewayError("MEDIA_TRUNCATED", "媒体字节范围不完整")
                success = True
                return b"".join(chunks)
        finally:
            await http.gate.leave(throttled=throttled, success=success)

    async def prepare(self):
        params = await self.adapter.signer.sign(
            {
                "bvid": self.binding["bvid"],
                "cid": self.binding["cid"],
                "qn": 16 if self.profile == "preview" else 64,
                "fnval": 16,
                "fourk": 0,
            }
        )
        data = await self.adapter.http.json(
            "/x/player/wbi/playurl", params, authenticated=self.adapter.authenticated
        )
        dash = data.get("dash") or {}
        if any(data.get(k) for k in ("drm_tech_type", "drm_type", "pssh")):
            raise GatewayError("MEDIA_PROTECTED", "不处理受保护媒体")
        duration = float(dash.get("duration") or 0)
        if abs(duration - self.binding["duration"]) > max(2, self.binding["duration"] * 0.01):
            raise GatewayError("CONTENT_IDENTITY_MISMATCH", "媒体时长与视频分P不一致")
        height = 480 if self.profile == "preview" else 720
        rows = [
            r
            for r in dash.get("video", [])
            if r.get("codecid") == 7
            and 0 < r.get("height", 0) <= height
            and not any(r.get(k) for k in ("drm_tech_type", "drm_type", "pssh"))
        ]
        if not rows:
            raise GatewayError("MEDIA_FORMAT_UNSUPPORTED", "没有可用的有限预览AVC DASH流")
        row = (
            min(rows, key=lambda r: (r["height"], r.get("bandwidth", 0)))
            if self.profile == "preview"
            else max(rows, key=lambda r: (r["height"], -r.get("bandwidth", 0)))
        )
        self.url = row.get("baseUrl") or row.get("base_url") or ""
        validate_media_url(self.url)
        ranges = row.get("SegmentBase") or row.get("segment_base") or {}
        init_range = byte_range(ranges.get("Initialization") or ranges.get("initialization"))
        index_range = byte_range(ranges.get("indexRange") or ranges.get("index_range"))
        self.init = await self.range(*init_range)
        index = await self.range(*index_range)
        self.segments = parse_sidx(index, index_range[0])
        self.quality = {k: row.get(k) for k in ("id", "width", "height", "codecs", "bandwidth")}
        # Content fingerprint, not expiring URLs or account identifiers.
        self.fingerprint = hashlib.sha256(self.init + index).hexdigest()

    async def fragment(self, timestamp):
        # Avoid floating point accumulation assigning an exact boundary to the
        # previous segment (whose last decoded frame precedes the requested time).
        segment = next(
            (s for s in self.segments if s.start - 1e-6 <= timestamp < s.end - 1e-6), None
        )
        if segment is None:
            raise GatewayError("MEDIA_TIME_UNAVAILABLE", "目标时间不在可用索引范围内")
        selected = [segment]
        i = self.segments.index(segment)
        # sidx durations can drift by milliseconds from actual tfdt/frame PTS.
        # A frame at a boundary may live in the next fragment; bounded lookahead
        # avoids silently returning the previous frame or downloading the full file.
        if segment.end - timestamp < 0.1 and i + 1 < len(self.segments):
            selected.append(self.segments[i + 1])
        bodies = []
        for piece in selected:
            key = hashlib.sha256(
                f"{self.binding['bvid']}:{self.binding['cid']}:{self.fingerprint}:{piece.offset}".encode()
            ).hexdigest()
            path = self.cache / f"{key}.bin"
            if (
                path.is_file()
                and time.time() - path.stat().st_mtime < 86400
                and path.stat().st_size == piece.size
            ):
                self.cache_hits += 1
                body = path.read_bytes()
                path.touch()
            else:
                body = await self.range(piece.offset, piece.offset + piece.size - 1)
                path.write_bytes(body)
            bodies.append(body)
        return self.init + b"".join(bodies), Segment(
            segment.start, selected[-1].end, segment.offset, sum(s.size for s in selected)
        )


async def decode_frame(media: bytes, timestamp: float, width: int):
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise GatewayError("MEDIA_RUNTIME_UNAVAILABLE", "需要本机FFmpeg，未自动安装")
    with tempfile.TemporaryDirectory(prefix="gptch-decode-") as folder:
        source = Path(folder) / "fragment.mp4"
        source.write_bytes(media)
        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "info",
            "-threads",
            "1",
            "-max_alloc",
            "67108864",
            "-protocol_whitelist",
            "file,pipe",
            "-copyts",
            "-i",
            str(source),
            "-an",
            "-sn",
            "-dn",
            "-filter_threads",
            "1",
            "-vf",
            f"select=gte(t\\,{timestamp:.6f}),scale=w='min({width},iw)':h=-2,showinfo",
            "-frames:v",
            "1",
            "-fps_mode",
            "passthrough",
            "-c:v",
            "png",
            "-threads",
            "1",
            "-f",
            "image2pipe",
            "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 25)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        pts = re.search(rb"\bn:\s*0\s+pts:.*?pts_time:([\d.e+-]+)", stderr)
        if (
            process.returncode
            or not stdout.startswith(b"\x89PNG\r\n\x1a\n")
            or not pts
            or len(stdout) > 3 * 1024 * 1024
        ):
            raise GatewayError(
                "MEDIA_DECODE_FAILED",
                "有限分段未能解码目标帧；不扩大为完整下载",
                output_bytes=len(stdout),
                timestamp_found=bool(pts),
                decoder_returncode=process.returncode,
            )
        actual = float(pts[1])
        if not timestamp - 0.001 <= actual <= timestamp + 1:
            raise GatewayError("MEDIA_TIMESTAMP_MISMATCH", "实际帧时间超出允许误差")
        return stdout, actual
