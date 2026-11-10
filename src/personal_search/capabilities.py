"""Local capability discovery; availability never implies a live network check.

Register only implemented adapters here. The public catalogue is intersected with
the platforms accepted by SearchRequest, so a roadmap entry cannot become usable
merely by being mentioned in a registry or skill.
"""

from __future__ import annotations

from copy import deepcopy
from typing import get_args

from . import __version__
from .models import FetchSection, SearchDepth, SearchRequest
from .video_media import ffmpeg_path

PLATFORM_CAPABILITIES = {
    "bilibili": {
        "display_name": "哔哩哔哩",
        "implementation": "available",
        "search": True,
        "video_details": True,
        "subtitles": True,
        "comments": True,
        "replies": True,
        "danmaku": True,
        "collection_budgets": {
            "comment_sorts": ["hot", "latest"],
            "hot_sort": "platform_mode_3_server_order",
            "max_comments_per_sort": 100,
            "comment_profiles_saved": False,
            "disable_comments": {"comment_sorts": []},
            "hot20": {"comment_sorts": ["hot"], "max_comments": 20, "reply_pages": 0},
            "hot100": {"comment_sorts": ["hot"], "max_comments": 100, "reply_pages": 0},
            "subtitles_only": {"comment_sorts": [], "max_danmaku": 0},
            "danmaku": "One bulk current-pool XML request per CID; full pool cached across slice budgets.",
            "paging": "Explicit count budgets allow a bounded page margin; stop at count, visible end, budget or failure.",
        },
        "content_kinds": ["video"],
        "content_id_formats": ["BV号", "https://www.bilibili.com/video/BV…/?p=N"],
        "fetch_sections": [s for s in get_args(FetchSection) if s != "body"],
        "auth_status_tool": "auth_status",
        "authentication": "公开搜索可尝试匿名；字幕和评论可能需要本机有效登录。",
        "verification": {"level": "live_api_verified", "date": "2026-10-15"},
        "coverage_notes": [
            "字幕仅提取已存在且通过归属检查的CC轨道；无字幕时不执行音频下载或ASR。",
            "评论和弹幕受接口当前可见范围限制；不是全平台或全部历史内容。",
        ],
    },
}

for _name, _label, _example in (
    ("juejin", "掘金", "https://juejin.cn/post/数字文章ID"),
    ("segmentfault", "SegmentFault 思否", "https://segmentfault.com/a/数字文章ID"),
):
    PLATFORM_CAPABILITIES[_name] = {
        "display_name": _label,
        "implementation": "available",
        "search": True,
        "article_body": True,
        "video_details": False,
        "subtitles": False,
        "comments": False,
        "replies": False,
        "danmaku": False,
        "content_kinds": ["article"],
        "content_id_formats": [
            _example,
            f"{_name}:post:数字ID" if _name == "juejin" else "segmentfault:article:数字ID",
        ],
        "fetch_sections": ["metadata", "body"],
        "authentication": "已验证公开匿名入口；不读取B站会话，未实现本平台登录或受限内容读取。",
        "verification": {"level": "live_native_search_and_article_verified", "date": "2026-11-03"},
        "parser": "Scrapling 0.5 Selector (adaptive=False)",
        "coverage_notes": [
            "使用平台原生搜索，仅保留文章；不以公开搜索引擎的site查询冒充。",
            "只读可见正文和代码块；不抓评论、账号资料或图片内容，不支持音视频。",
            "遇到登录、验证码、403/412/429或结构变化停止，不启用验证码求解/代理轮换。",
            "来源原创/转载字段仅为平台声明，不等于独立验证。",
        ],
    }

for _name, _label, _kinds in (
    ("xiaohongshu", "小红书", ["note"]),
    ("zhihu", "知乎", ["answer", "article"]),
    ("tieba", "百度贴吧", ["thread_first_floor"]),
):
    PLATFORM_CAPABILITIES[_name] = {
        "display_name": _label, "implementation": "experimental",
        "search": True, "platform_native_search": True, "article_body": True,
        "available_without_login": False, "content_kinds": _kinds,
        "fetch_sections": ["metadata", "body"], "subtitles": False, "comments": False,
        "content_id_formats": {
            "xiaohongshu": ["https://www.xiaohongshu.com/explore/24位笔记ID", "xiaohongshu:note:24位笔记ID"],
            "zhihu": ["https://www.zhihu.com/question/问题ID/answer/回答ID", "https://zhuanlan.zhihu.com/p/文章ID"],
            "tieba": ["https://tieba.baidu.com/p/帖子ID", "tieba:thread:帖子ID"],
        }[_name],
        "danmaku": False, "video_details": False,
        "auth_status_tool": "auth_status", "login_tool": "open_login_panel",
        "authentication": "本人在项目独立Chrome窗口登录；Cookie仅存本机。需真人验收，不保证通过平台验证。",
        "verification": {"level": "implemented_pending_human_login_and_live_collection", "live_verified": False},
        "coverage_notes": [
            "正常网页原生搜索与可见文字，不逆向签名、不绕过验证码、不采账号资料。",
            "小红书只取笔记说明文字，不分析图像/视频；知乎仅所选回答/专栏；贴吧仅主帖第一楼。",
            "浏览器任务内部串行，按请求/时间预算和风控停止；不会因配置并发而启动多个浏览器。",
        ],
    }

for _name in ("xiaohongshu", "tieba"):
    PLATFORM_CAPABILITIES[_name]["implementation"] = "available"
    PLATFORM_CAPABILITIES[_name]["verification"] = {
        "level": "human_login_native_search_selected_body_SQLite_and_Codex_MCP_readback_verified",
        "date": "2026-11-03", "live_verified": True,
        "tested_scope": "one search page and one selected text body; not whole-platform coverage",
    }
PLATFORM_CAPABILITIES["xiaohongshu"]["note_caption_text"] = True
PLATFORM_CAPABILITIES["xiaohongshu"]["browser_budgets"] = {
    "cold_search_and_one_caption_tested": {"max_requests": 240, "actual_http": 184},
    "warm_static_cache_different_keyword_candidates_only_tested": {"max_requests": 120, "actual_http": 72},
    "notes": "Browser HTTP budget includes required site JS/CSS and normal site security/config requests. Public CDN static cache is capped at32MiB/24h; no account API cache.",
}
PLATFORM_CAPABILITIES["zhihu"]["verification"] = {
    "level": "human_login_me_API_verified_native_search_HTTP403_blocked",
    "date": "2026-11-03", "live_verified": False,
    "human_report": "user changed network environment and successfully logged in",
    "search_retry_after_block": False,
}

PLATFORM_CAPABILITIES["wechat"] = {
    "display_name": "微信公众文章", "implementation": "experimental",
    "search": True, "platform_native_search": False, "search_provider": "sogou_wechat_index",
    "wechat_in_app_search": False, "article_body": True, "comments": False,
    "video_details": False, "subtitles": False, "danmaku": False,
    "content_kinds": ["public_article", "index_discovery"], "fetch_sections": ["metadata", "body"],
    "content_id_formats": ["https://mp.weixin.qq.com/s/公开文章ID", "https://mp.weixin.qq.com/s?__biz=公众号标识&mid=文章ID&idx=序号", "索引返回的wechat:index:前缀ID"],
    "authentication": "公开链接与搜狗索引无需微信登录；不索取微信聊天会话。",
    "verification": {"level": "live_index_10_results_article_redirect_captcha_blocked", "live_article_verified": False},
    "coverage_notes": ["搜狗微信索引不是客户端搜一搜；索引候选未经跳转前不是已读取原文。",
                       "读取正文时验证最终mp.weixin.qq.com文章归属；受平台可见性/验证限制。"],
}


def capabilities():
    schema = SearchRequest.model_json_schema()["properties"]
    platform_type = get_args(SearchRequest.model_fields["platforms"].annotation)[0]
    accepted = get_args(platform_type)
    platforms = {
        name: deepcopy(value)
        for name, value in PLATFORM_CAPABILITIES.items()
        if name in accepted and value.get("implementation") in ("available", "experimental")
    }
    if "bilibili" in platforms:
        platforms["bilibili"]["visual_evidence"] = {
            "implemented": True,
            "available_in_runtime": ffmpeg_path() is not None,
            "tool": "inspect_video",
            "read_saved_tool": "read_video_evidence",
            "max_frames_per_call": 6,
            "media_formats": ["unprotected_AVC_DASH_SegmentBase_sidx_with_exact_HTTP_Range"],
            "preview_and_explicit_timestamps": True,
            "local_grid_change_clues": True,
            "semantic_value_decision": "model; no Python semantic score, OCR or parameter recognition",
            "audio_extraction": False,
            "asr": False,
            "verification": {
                "level": "live_video_stdio_image_payload_and_Codex_local_image_bridge_observed",
                "date": "2026-10-17",
                "native_Work_image_rendering_verified": False,
                "automatic_Work_selection_verified": False,
            },
            "limits": {
                "decode_concurrency": 1,
                "max_media_mb_per_call": 32,
                "frame_retention_days": 7,
                "shared_cache_mb": 128,
            },
        }
    limits = {}
    for name in (
        "keywords",
        "platforms",
        "pages_per_keyword",
        "max_candidates",
        "max_items",
        "max_comments",
        "comment_pages",
        "reply_pages",
        "max_reply_threads",
        "max_danmaku",
        "subtitle_tracks",
        "video_pages",
        "max_requests",
        "timeout_seconds",
        "content_ids",
    ):
        field = schema[name]
        field = next((v for v in field.get("anyOf", []) if v.get("type") == "integer"), field)
        limits[name] = {k: field[k] for k in ("minimum", "maximum", "maxItems") if k in field}
    modes = {}
    for depth in get_args(SearchDepth):
        request = SearchRequest(query="capability discovery", depth=depth)
        modes[depth] = {
            "pages_per_keyword": request.pages_per_keyword,
            "max_items": request.max_items,
        }
    return {
        "schema_version": 1,
        "service": "GPTCH Search / Personal AI Search Gateway",
        "version": __version__,
        "execution": "local",
        "platforms": platforms,
        "unlisted_platforms_supported": False,
        "availability_scope": "Implemented adapters and accepted request schemas; no live auth/network probe.",
        "search_modes": modes,
        "request_limits": limits,
        "keywords_total_limit": limits["keywords"]["maxItems"],
        "cache": {"default": "reuse", "refresh": "Only set true when fresh evidence is needed."},
        "workflow": {
            "discover": "get_capabilities",
            "search": "deep_search",
            "poll_and_candidates": "get_task",
            "read_saved_content": "fetch_content",
            "read_local_archive": "search_archive",
            "task_control": "manage_task",
            "login_selection": "open_login_panel",
            "login_status": "auth_status",
            "optional_video_visual_evidence": "inspect_video",
            "read_saved_video_images": "read_video_evidence",
        },
        "result_notes": [
            "已知可靠能力可复用，未知或变化时再核实；未列出的平台不可通过此网关搜索。",
            "完成状态不等于完整覆盖；检查progress.search_truncated和各组件coverage。",
            "workflow仅为工具索引，不是必经步骤；模式是预算预设，模型按需选择最短有效路径。",
            "保留来源平台、原始链接及采集时间；站外网页发现不能冒充平台原生搜索。",
        ],
    }
