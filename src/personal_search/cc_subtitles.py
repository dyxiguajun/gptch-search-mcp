"""Async transport adapter for the user's MIT-licensed CC Extractor checks.

The original UI, requests client, cookie files and UI retry loop stay outside
the gateway. All requests use its shared budget, pacing and risk-control circuit.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import asdict, dataclass
from urllib.parse import parse_qs, urlsplit

from .errors import GatewayError
from .storage import cache_key
from .vendor import cc_ext

CC_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


def explicit_part(value):
    values = parse_qs(urlsplit(value).query, keep_blank_values=True).get("p")
    if values is None:
        return None
    if len(values) != 1 or not values[0].isascii() or not values[0].isdigit():
        raise GatewayError("INVALID_VIDEO_PAGE", "分P参数必须是单个正整数")
    part = int(values[0])
    if part < 1:
        raise GatewayError("INVALID_VIDEO_PAGE", "分P参数必须大于零")
    return cc_ext.parse_page_number(f"?p={part}")


def subtitle_segments(body, duration=None):
    segments = []
    for row in body:
        try:
            start, end = float(row["from"]), float(row["to"])
        except (KeyError, TypeError, ValueError):
            raise GatewayError("UPSTREAM_FORMAT_CHANGED", "字幕时间字段格式异常") from None
        if not math.isfinite(start) or not math.isfinite(end) or not 0 <= start <= end:
            raise GatewayError("UPSTREAM_FORMAT_CHANGED", "字幕时间范围异常")
        if duration and end > duration + min(5, max(2, duration * 0.01)):
            raise GatewayError(
                "SUBTITLE_CONTENT_MISMATCH",
                "字幕超出视频分P时长，拒绝归档。",
                video_duration_seconds=duration,
                subtitle_end_seconds=end,
            )
        segments.append({"start": start, "end": end, "text": row.get("content", "")})
    return segments


@dataclass(frozen=True)
class VideoBinding:
    bvid: str
    aid: int
    cid: int
    page: int
    duration: float

    def verify_player(self, player):
        if any(str(player.get(k)) != str(getattr(self, k)) for k in ("bvid", "aid", "cid")):
            raise GatewayError(
                "SUBTITLE_CONTENT_MISMATCH",
                "播放器响应与目标视频或分P不一致，拒绝读取字幕。",
                expected_bvid=self.bvid,
                expected_aid=self.aid,
                expected_cid=self.cid,
            )


class CCSubtitleAdapter:
    def __init__(self, gateway):
        self.gateway = gateway

    async def extract(self, binding):
        gateway = self.gateway
        if not math.isfinite(binding.duration) or binding.duration <= 0:
            raise GatewayError("SUBTITLE_VALIDATION_UNAVAILABLE", "分P缺少有效时长，无法验证字幕")
        key = cache_key(
            "cc_subtitles_v1",
            {
                "binding": asdict(binding),
                "tracks": gateway.request.subtitle_tracks,
                "session_scope": gateway.cache_scope,
            },
        )
        cached = None if gateway.request.refresh else gateway.store.cache_get(key)
        if cached and cached[0].get("binding") == asdict(binding):
            gateway.cache_hits += 1
            return {**cached[0], "cached": True, "fetched_at": cached[1]}
        headers = {
            **CC_HEADERS,
            "Referer": f"https://www.bilibili.com/video/{binding.bvid}?p={binding.page}",
        }
        attempts, failure, needs_login = [], None, not gateway.authenticated
        # Real validation on all three supplied videos found correct bodies in
        # WBI, while the original standard endpoint returned mismatched/empty URLs.
        # Try each normal provider once; risk control always stops the transport.
        for path in ("/x/player/wbi/v2", "/x/player/v2"):
            params = {
                "bvid": binding.bvid,
                "aid": binding.aid,
                "cid": binding.cid,
                "_ts": int(time.time() * 1000),
            }
            if "/wbi/" in path:
                params = await gateway.signer.sign(params)
            player = await gateway.http.json(
                path,
                params,
                authenticated=gateway.authenticated,
                headers=headers,
            )
            binding.verify_player(player)
            needs_login = needs_login or bool(player.get("need_login_subtitle"))
            tracks = (player.get("subtitle") or {}).get("subtitles") or []
            if not isinstance(tracks, list):
                raise GatewayError("UPSTREAM_FORMAT_CHANGED", "播放器字幕轨道格式异常")
            attempt = {"api_path": path, "available_tracks": len(tracks)}
            attempts.append(attempt)
            if not tracks:
                continue
            selected = []
            for track in tracks[: gateway.request.subtitle_tracks]:
                try:
                    selected.append(await self._track(binding, track, headers))
                except GatewayError as exc:
                    failure = exc.as_dict()
                    attempt["error"] = failure
                    # Retain already validated tracks if a later request fails.
                    # With no valid result, circuit/budget failures must propagate
                    # immediately instead of trying another player provider.
                    if not selected and (
                        gateway.http.blocked
                        or exc.code
                        in (
                            "AUTH_EXPIRED",
                            "AUTH_REQUIRED",
                            "REQUEST_BUDGET_EXCEEDED",
                        )
                    ):
                        raise
                    break
            if selected:
                result = {
                    "status": "partial" if len(selected) < len(tracks) else "complete",
                    "scope": "cc_ext_validated_tracks",
                    "binding": asdict(binding),
                    "tracks": selected,
                    "available_tracks": len(tracks),
                    "error": attempt.get("error"),
                    "api_path": path,
                    "listing_attempts": attempts,
                    "cached": False,
                    "fetched_at": time.time(),
                }
                if not result["error"]:
                    gateway.store.cache_put(key, result, 86400)
                return result
        if failure:
            return {
                "status": "failed",
                "tracks": [],
                "error": failure,
                "binding": asdict(binding),
                "listing_attempts": attempts,
                "cached": False,
            }
        return {
            "status": "unavailable",
            "code": "AUTH_REQUIRED" if needs_login else "SUBTITLE_NOT_AVAILABLE",
            "tracks": [],
            "binding": asdict(binding),
            "listing_attempts": attempts,
            "scope": "verified_player_listings",
            "cached": False,
            "fetched_at": time.time(),
            "note": "当前会话的两个播放器接口未提供轨道；不代表所有客户端均无字幕。",
        }

    async def _track(self, binding, track, headers):
        if not isinstance(track, dict):
            raise GatewayError("UPSTREAM_FORMAT_CHANGED", "字幕轨道格式异常")
        url = track.get("subtitle_url") or track.get("subtitle_url_v2")
        if not isinstance(url, str) or not url.strip() or url.strip() == "/":
            raise GatewayError("SUBTITLE_URL_UNAVAILABLE", "字幕轨道没有可用下载地址")
        bodies = []
        nonce = int(time.time() * 1000)
        for index in range(2):
            response = await self.gateway.http.get(
                cc_ext.fresh_subtitle_url(url, max(nonce + index, int(time.time() * 1000))),
                headers=headers,
            )
            try:
                payload = response.json()
                body = payload.get("body")
            except (ValueError, AttributeError):
                raise GatewayError("UPSTREAM_FORMAT_CHANGED", "字幕文件没有返回 JSON") from None
            if not isinstance(body, list) or not body:
                raise GatewayError("SUBTITLE_CONTENT_UNAVAILABLE", "字幕文件没有可用正文")
            # Validate fields before calling the original CC helpers, which assume
            # well-formed rows and otherwise raise untyped Python exceptions.
            subtitle_segments(body, binding.duration)
            if any(not isinstance(row.get("content"), str) for row in body):
                raise GatewayError("UPSTREAM_FORMAT_CHANGED", "字幕正文格式异常")
            bodies.append(payload)
        fingerprints = [cc_ext.subtitle_fingerprint(p) for p in bodies]
        digests = [
            hashlib.sha256(
                json.dumps(p["body"], sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            for p in bodies
        ]
        if fingerprints[0] != fingerprints[1] or digests[0] != digests[1]:
            raise GatewayError("SUBTITLE_UNSTABLE", "同一轨道两次内容不一致，拒绝归档")
        payload = bodies[-1]
        if not cc_ext.ok_by_duration(payload, track, binding.duration):
            raise GatewayError(
                "SUBTITLE_CONTENT_MISMATCH",
                "字幕覆盖时长不符合原CC工具的分P校验，拒绝归档。",
                video_duration_seconds=binding.duration,
                subtitle_end_seconds=cc_ext.subtitle_max_to_seconds(payload),
            )
        text = cc_ext.subtitle_json_to_text(payload)
        language = str(track.get("lan") or "").lower().removeprefix("ai-").split("-")[0]
        threshold = {"zh": 0.02, "ja": 0.002, "en": 0.01}.get(language)
        if threshold is not None and cc_ext.detect_lang_score(text, language) < threshold:
            raise GatewayError("SUBTITLE_LANGUAGE_MISMATCH", "字幕内容与轨道语言不符，拒绝归档")
        segments = subtitle_segments(payload["body"], binding.duration)
        self.gateway.store.raw(self.gateway.task_id, f"subtitle_{binding.cid}", payload)
        return {
            "id": str(track.get("id_str") or track.get("id") or ""),
            "language": track.get("lan", ""),
            "label": track.get("lan_doc", ""),
            "segments": segments,
            "binding": asdict(binding),
            "verification": {
                "player_identity": True,
                "double_fetch_equal": True,
                "cc_duration_check": True,
                "language_check": threshold is not None,
                "body_sha256": digests[-1],
            },
        }
