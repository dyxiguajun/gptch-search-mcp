from __future__ import annotations

import hashlib
import html
import json
import re
import time

from defusedxml import ElementTree

from .auth import AuthStore
from .cc_subtitles import CCSubtitleAdapter, VideoBinding, explicit_part
from .errors import GatewayError, safe_error
from .http import Transport
from .storage import cache_key
from .vendor.wbi import WbiSigner


def clean(text):
    return html.unescape(re.sub(r"<[^>]+>", "", str(text or ""))).strip()


def parse_bvid(value):
    # No arbitrary URL fetching, redirects, command execution or av/BV conversion.
    match = re.fullmatch(
        r"(?:https://(?:www\.)?bilibili\.com/video/)?(BV[0-9A-Za-z]{10})(?:/?(?:\?[^\s#]*)?(?:#[^\s]*)?)?",
        value.strip(),
    )
    if not match:
        raise GatewayError(
            "INVALID_CONTENT_ID", "请提供标准 BV 号或 https://www.bilibili.com/video/BV… 链接"
        )
    return match.group(1)


def comment_record(raw, root=0):
    return {
        "id": str(raw.get("rpid", "")),
        "root_id": str(raw.get("root") or root or ""),
        "parent_id": str(raw.get("parent") or ""),
        "text": raw.get("content", {}).get("message", ""),
        "likes": raw.get("like", 0),
        "published_at": raw.get("ctime", 0),
        "reply_count_reported": raw.get("rcount", 0),
    }


def comment_payload(data):
    """Project before both raw archival and caching; discard profiles and embedded previews."""

    def row(value):
        return {
            **{
                k: value[k]
                for k in ("rpid", "root", "parent", "oid", "type", "like", "ctime", "rcount")
                if k in value
            },
            "content": {"message": value.get("content", {}).get("message", "")},
        }

    return {
        "cursor": {
            k: v
            for k, v in (data.get("cursor") or {}).items()
            if k in ("is_end", "next", "mode", "all_count", "pagination_reply")
        },
        "page": {k: v for k, v in (data.get("page") or {}).items() if k == "count"},
        "top_replies": [row(r) for r in data.get("top_replies") or []],
        "replies": [row(r) for r in data.get("replies") or []],
    }


def normalize_content_ref(value):
    bvid = parse_bvid(value)
    part = explicit_part(value.strip())
    return bvid if part is None else f"{bvid}?p={part}"


class BilibiliAdapter:
    def __init__(self, settings, store, task_id, request, *, transport=None):
        self.settings, self.store, self.task_id, self.request = settings, store, task_id, request
        cookies = AuthStore(settings.data_dir).load()
        self.authenticated = bool(cookies.get("SESSDATA"))
        # Cache scope is tied to the session without storing its raw credential.
        self.cache_scope = hashlib.sha256(
            cookies.get("SESSDATA", "anonymous").encode()
        ).hexdigest()[:16]
        self.http = transport or Transport(
            cookies,
            interval=settings.request_interval,
            max_requests=request.max_requests,
            concurrency=request.concurrency,
        )
        self.signer = WbiSigner(self.http)
        self.cache_hits = 0

    async def close(self):
        await self.http.close()

    async def preflight(self):
        if self.authenticated:
            nav = await self.http.json("/x/web-interface/nav", authenticated=True)
            if not nav.get("isLogin"):
                raise GatewayError("AUTH_EXPIRED", "B 站会话已过期，请运行 auth login")
            self.signer.use_nav(nav)

    async def api(self, path, params=None, *, signed=False, ttl=300, cache_variant=None):
        params = params or {}
        is_comments = path in ("/x/v2/reply/wbi/main", "/x/v2/reply/reply")
        # New namespace avoids reading old profile-bearing comment cache entries.
        cache_values = {"params": params, "scope": self.cache_scope}
        if cache_variant is not None:
            cache_values["page_occurrence"] = cache_variant
        key = cache_key(path + (":minimal-v2" if is_comments else ""), cache_values)
        cached = None if self.request.refresh else self.store.cache_get(key)
        if cached:
            self.cache_hits += 1
            return cached[0], {"cached": True, "fetched_at": cached[1]}
        query = await self.signer.sign(params) if signed else params
        data = await self.http.json(path, query, authenticated=self.authenticated)
        if is_comments:
            data = comment_payload(data)
        fetched = time.time()
        self.store.raw(self.task_id, path, data)
        # Player contains expiring signed URLs: use the original response in memory,
        # never cache a redacted player response that cannot subsequently be fetched.
        if path not in ("/x/player/v2", "/x/player/wbi/v2"):
            self.store.cache_put(key, data, ttl)
        return data, {"cached": False, "fetched_at": fetched}

    async def search_page(self, keyword, page):
        data, provenance = await self.api(
            "/x/web-interface/wbi/search/type",
            {
                "search_type": "video",
                "keyword": keyword,
                "page": page,
                "page_size": 20,
                "order": "totalrank",
            },
            signed=True,
            ttl=900,
        )
        rows = data.get("result")
        if rows is None and int(data.get("numResults", -1)) == 0:
            rows = []
        if not isinstance(rows, list):
            raise GatewayError("UPSTREAM_FORMAT_CHANGED", "搜索结果缺少 result 列表")
        results = []
        for item in rows:
            bvid = item.get("bvid", "")
            if not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid):
                continue
            results.append(
                {
                    "id": bvid,
                    "platform": "bilibili",
                    "url": f"https://www.bilibili.com/video/{bvid}",
                    "title": clean(item.get("title")),
                    "description": clean(item.get("description")),
                    "author": item.get("author", ""),
                    "published_at": item.get("pubdate", 0),
                    "duration": item.get("duration", ""),
                    "views": item.get("play", 0),
                    "matched_keywords": [keyword],
                    "search_page": page,
                    **provenance,
                }
            )
        return results, int(data.get("numPages") or 0)

    async def subtitles(self, bvid, aid, page):
        binding = VideoBinding(
            bvid=bvid,
            aid=int(aid),
            cid=int(page["cid"]),
            page=int(page.get("page", 1)),
            duration=float(page.get("duration") or 0),
        )
        return await CCSubtitleAdapter(self).extract(binding)

    async def comments(self, aid, mode):
        # Bilibili cursor mode: 3=hot, 2=latest. Keep server ranking, never sort by likes.
        sort = "hot" if mode == 3 else "latest"
        limit = self.request.max_comments if self.request.max_comments is not None else 100
        result = {
            "status": "partial",
            "sort": sort,
            "api_mode": mode,
            "scope": "visible_top_level",
            "all_platform_comments": False,
            "comments": [],
            "replies": [],
            "pages_fetched": 0,
            "reply_threads": [],
            "provenance": [],
            "coverage": {
                "requested_limit": limit,
                "visible_end": False,
                "rank_scope": "server_order_excluding_separate_pinned_list",
                "pinned_comments_omitted": 0,
            },
        }
        if not limit or sort not in self.request.comment_sorts:
            return {**result, "status": "not_requested", "replies_status": "not_requested"}
        seen, pinned_seen, cursor, offset, reached_end = set(), set(), 0, None, False
        requested_offsets = set()
        try:
            for page_index in range(self.request.comment_pages):
                params = {"type": 1, "oid": aid, "mode": mode, "ps": 20}
                if offset is not None:
                    params["pagination_str"] = json.dumps({"offset": offset}, separators=(",", ":"))
                else:
                    params["next"] = cursor
                # Some opaque offsets repeat across distinct server pages. Do not
                # replay our earlier cached page for the next occurrence.
                variant = page_index if offset in requested_offsets else None
                requested_offsets.add(offset)
                data, provenance = await self.api(
                    "/x/v2/reply/wbi/main",
                    params,
                    signed=True,
                    ttl=180 if mode == 2 else 1800,
                    cache_variant=variant,
                )
                result["pages_fetched"] += 1
                result["provenance"].append(provenance)
                cursor_data = data.get("cursor")
                if not isinstance(cursor_data, dict) or not isinstance(
                    cursor_data.get("is_end"), bool
                ):
                    raise GatewayError("UPSTREAM_FORMAT_CHANGED", "评论分页缺少可信结束标志")
                if cursor_data.get("mode", mode) != mode:
                    raise GatewayError("UPSTREAM_FORMAT_CHANGED", "平台评论排序与请求不符")
                result["coverage"]["platform_total_reported_includes_replies"] = cursor_data.get(
                    "all_count"
                )
                pinned_seen.update(str(r.get("rpid")) for r in data.get("top_replies") or [])
                result["coverage"]["pinned_comments_omitted"] = len(pinned_seen)
                rows = data.get("replies") or []
                if not isinstance(rows, list):
                    raise GatewayError("UPSTREAM_FORMAT_CHANGED", "评论响应缺少列表")
                before = len(seen)
                consumed = 0
                for row in rows:
                    consumed += 1
                    if row.get("root"):
                        continue  # Never count embedded replies as first-level comments.
                    if row.get("oid") is not None and str(row["oid"]) != str(aid):
                        raise GatewayError("CONTENT_IDENTITY_MISMATCH", "评论归属与请求视频不一致")
                    identity = str(row.get("rpid") or "")
                    if identity and identity not in seen:
                        seen.add(identity)
                        result["comments"].append(
                            {
                                **comment_record(row),
                                "sort": sort,
                                "rank": len(result["comments"]) + 1,
                            }
                        )
                        if len(result["comments"]) >= limit:
                            break
                reached_end = cursor_data["is_end"] and consumed == len(rows)
                result["coverage"]["visible_end"] = reached_end
                if reached_end or len(result["comments"]) >= limit:
                    break
                if len(seen) == before:
                    raise GatewayError("PAGINATION_STALLED", "评论页无新增标识；停止重复请求")
                next_offset = (cursor_data.get("pagination_reply") or {}).get("next_offset")
                if isinstance(next_offset, str) and next_offset:
                    # Opaque offsets need not be numeric or unique; detect repeated *rows* above.
                    offset = next_offset
                else:
                    next_cursor = cursor_data.get("next")
                    if not isinstance(next_cursor, int) or next_cursor == cursor:
                        raise GatewayError("PAGINATION_STALLED", "评论游标没有变化；停止重复请求")
                    cursor, offset = next_cursor, None
            met_limit = len(result["comments"]) >= limit
            result["coverage"].update(
                saved=len(result["comments"]), requested_limit_reached=met_limit
            )
            result["status"] = "complete" if reached_end or met_limit else "partial"
            result["reason"] = (
                "visible_end"
                if reached_end
                else "comment_limit"
                if met_limit
                else "comment_page_budget"
            )
            # Additional thread paging has its own budget and never implies all replies were fetched.
            threads = [r for r in result["comments"] if r["reply_count_reported"]][
                : self.request.max_reply_threads
            ]
            if self.request.reply_pages:
                for root in threads:
                    thread = {
                        "root_id": root["id"],
                        "status": "partial",
                        "fetched": 0,
                        "total_reported": root["reply_count_reported"],
                    }
                    result["reply_threads"].append(thread)
                    reply_seen = set()
                    for pn in range(1, self.request.reply_pages + 1):
                        reply_data, reply_prov = await self.api(
                            "/x/v2/reply/reply",
                            {"type": 1, "oid": aid, "root": root["id"], "pn": pn, "ps": 20},
                            ttl=180 if mode == 2 else 1800,
                        )
                        result["provenance"].append(reply_prov)
                        replies = reply_data.get("replies") or []
                        total = reply_data.get("page", {}).get("count")
                        if total is None:
                            raise GatewayError("UPSTREAM_FORMAT_CHANGED", "楼中楼分页缺少总量")
                        thread["total_reported"] = total
                        for reply in replies:
                            if (reply.get("oid") is not None and str(reply["oid"]) != str(aid)) or (
                                reply.get("root") and str(reply["root"]) != root["id"]
                            ):
                                raise GatewayError(
                                    "CONTENT_IDENTITY_MISMATCH", "楼中楼归属与请求不一致"
                                )
                            identity = str(reply.get("rpid", ""))
                            if identity and identity not in reply_seen:
                                reply_seen.add(identity)
                                result["replies"].append(comment_record(reply, root["id"]))
                        thread["fetched"] = len(reply_seen)
                        if len(reply_seen) >= int(total):
                            thread["status"] = "complete"
                            break
                        if not replies:
                            thread["reason"] = "empty_page_before_reported_end"
                            break
            incomplete_threads = any(t["status"] != "complete" for t in result["reply_threads"])
            unvisited_threads = sum(
                r["reply_count_reported"] > 0 for r in result["comments"]
            ) > len(result["reply_threads"])
            result["replies_complete_for_fetched_roots"] = (
                not incomplete_threads and not unvisited_threads
            )
            result["replies_status"] = (
                "not_requested"
                if not self.request.reply_pages or not self.request.max_reply_threads
                else "partial"
                if incomplete_threads or unvisited_threads
                else "complete"
            )
            if result["replies_status"] == "partial":
                result["status"] = "partial"
        except Exception as exc:
            result["status"] = "partial" if result["comments"] else "failed"
            result["error"] = safe_error(exc)
        result["coverage"]["saved"] = len(result["comments"])
        return result

    async def danmaku(self, cid):
        # One bulk XML fetch per CID; cache the pool, not each requested slice.
        cid = int(cid)
        key = cache_key("danmaku-pool-v2", {"cid": cid})
        cached = None if self.request.refresh else self.store.cache_get(key)
        if cached:
            self.cache_hits += 1
            pool, fetched_at = cached
        else:
            response = await self.http.get(f"https://comment.bilibili.com/{cid}.xml")
            try:
                tree = ElementTree.fromstring(response.content)
            except Exception:
                raise GatewayError("UPSTREAM_FORMAT_CHANGED", "弹幕接口未返回可解析 XML") from None
            if tree.tag != "i":
                raise GatewayError("UPSTREAM_FORMAT_CHANGED", "弹幕 XML 根节点不符合预期")
            if tree.findtext("chatid") and tree.findtext("chatid") != str(cid):
                raise GatewayError("CONTENT_IDENTITY_MISMATCH", "弹幕返回的CID不符")
            rows, seen, malformed, duplicates = [], set(), 0, 0
            for row in tree.findall("d"):
                attrs = row.get("p", "").split(",")
                try:
                    record = {
                        "playback_seconds": float(attrs[0]),
                        "mode": int(attrs[1]),
                        "published_at": int(attrs[4]),
                        "id": attrs[7],
                        "text": row.text or "",
                    }
                except (ValueError, IndexError):
                    malformed += 1
                    continue
                if record["id"] and record["id"] in seen:
                    duplicates += 1
                    continue
                seen.add(record["id"])
                rows.append(record)
            rows.sort(key=lambda r: r["playback_seconds"])
            fetched_at = time.time()
            pool = {
                "cid": cid,
                "danmaku": rows,
                "malformed_rows": malformed,
                "duplicate_rows": duplicates,
            }
            self.store.raw(self.task_id, f"danmaku_{cid}", pool)
            self.store.cache_put(key, pool, 1800)
        if pool.get("cid") != cid:
            raise GatewayError("CONTENT_IDENTITY_MISMATCH", "弹幕缓存CID不符")
        rows = pool["danmaku"]
        return {
            "status": "partial"
            if pool["malformed_rows"] or len(rows) > self.request.max_danmaku
            else "complete",
            "cid": cid,
            "scope": "current_visible_xml_pool",
            "all_historical_danmaku": False,
            "available_in_response": len(rows),
            "malformed_rows": pool["malformed_rows"],
            "duplicate_rows": pool["duplicate_rows"],
            "requested_limit": self.request.max_danmaku,
            "danmaku": rows[: self.request.max_danmaku],
            "fetched_at": fetched_at,
            "cached": bool(cached),
        }

    async def guarded(self, function, *args):
        try:
            return await function(*args)
        except Exception as exc:
            return {"status": "failed", "error": safe_error(exc)}

    async def video_details(self, bvid):
        # The legacy view path can return a 412 HTML page while its WBI counterpart
        # serves normal metadata. Use the existing signer; retain fail-fast risk control.
        detail_path = "/x/web-interface/wbi/view"
        data, provenance = await self.api(detail_path, {"bvid": bvid}, signed=True, ttl=86400)
        pages = data.get("pages") or []
        if not pages or not data.get("aid"):
            raise GatewayError("UPSTREAM_FORMAT_CHANGED", "视频详情缺少 aid/pages")
        if data.get("bvid") != bvid:
            raise GatewayError("CONTENT_IDENTITY_MISMATCH", "视频详情与请求BV不一致，拒绝采集")
        return data, {**provenance, "api_path": detail_path, "wbi_signed": True}

    async def extract(self, bvid):
        direct_part = explicit_part(bvid)
        bvid = parse_bvid(bvid)
        data, provenance = await self.video_details(bvid)
        pages = data["pages"]
        requested_parts = {
            explicit_part(value)
            for value in self.request.content_ids
            if parse_bvid(value) == bvid and explicit_part(value) is not None
        }
        if direct_part is not None:
            requested_parts.add(direct_part)
        selected_pages = pages
        if requested_parts:
            by_number = {int(page.get("page", index + 1)): page for index, page in enumerate(pages)}
            if not requested_parts.issubset(by_number):
                raise GatewayError(
                    "INVALID_VIDEO_PAGE",
                    "请求的分P不存在，拒绝改取其他分P",
                    requested_pages=sorted(requested_parts),
                    available_pages=sorted(by_number),
                )
            selected_pages = [by_number[number] for number in sorted(requested_parts)]
        item = {
            "id": bvid,
            "platform": "bilibili",
            "url": f"https://www.bilibili.com/video/{bvid}",
            "title": data.get("title", ""),
            "description": data.get("desc", ""),
            "author": data.get("owner", {}).get("name", ""),
            "published_at": data.get("pubdate", 0),
            "stats": data.get("stat", {}),
            "metadata": provenance,
            "pages": [],
            "available_video_pages": len(pages),
            "auth_status": "authenticated" if self.authenticated else "anonymous",
            "extracted_at": time.time(),
        }
        for page in selected_pages[: self.request.video_pages]:
            part = {"cid": page["cid"], "page": page.get("page", 1), "title": page.get("part", "")}
            part["subtitles"] = (
                await self.guarded(self.subtitles, bvid, data["aid"], page)
                if self.request.subtitle_tracks
                else {"status": "not_requested"}
            )
            part["danmaku"] = (
                await self.guarded(self.danmaku, page["cid"])
                if self.request.max_danmaku
                else {"status": "not_requested"}
            )
            item["pages"].append(part)
        item["comments_hot"] = await self.comments(data["aid"], 3)
        item["comments_latest"] = await self.comments(data["aid"], 2)
        components = [
            item["comments_hot"],
            item["comments_latest"],
            *[p[k] for p in item["pages"] for k in ("subtitles", "danmaku")],
        ]
        item["status"] = (
            "partial"
            if len(selected_pages) > len(item["pages"])
            or any(
                c["status"] in ("failed", "partial") or c.get("code") == "AUTH_REQUIRED"
                for c in components
            )
            else "complete"
        )
        item["coverage"] = {
            "all_platform_content": False,
            "video_page_limit": self.request.video_pages,
            "requested_video_pages": sorted(requested_parts) if requested_parts else None,
            "comment_page_limit_per_sort": self.request.comment_pages,
            "comment_sorts": self.request.comment_sorts,
            "comment_limit_per_sort": self.request.max_comments,
            "reply_pages_per_thread": self.request.reply_pages,
            "note": "complete 仅指所声明接口和范围；评论、弹幕均受平台可见范围限制。",
        }
        return item
