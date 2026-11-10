"""Platform-specific identities and adapter routing; no fallback across platforms."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlencode, urlsplit

from .errors import GatewayError

ARTICLE_ROUTES = {
    "juejin": ("juejin.cn", "post", "post"),
    "segmentfault": ("segmentfault.com", "a", "article"),
}

SOCIAL_ROUTES = {
    "xiaohongshu": ("www.xiaohongshu.com", "explore", "note", r"[a-f0-9]{24}"),
    "tieba": ("tieba.baidu.com", "p", "thread", r"\d{5,25}"),
}


def normalize_ref(platform, value):
    if platform == "bilibili":
        from .bilibili import normalize_content_ref

        return normalize_content_ref(value)
    value = value.strip()
    if platform == "zhihu":
        if re.fullmatch(r"zhihu:answer:\d{1,25}:\d{1,25}|zhihu:article:\d{1,25}", value):
            return value
        url = urlsplit(value)
        if url.scheme == "https" and url.netloc == "www.zhihu.com":
            match = re.fullmatch(r"/question/(\d{1,25})/answer/(\d{1,25})/?", url.path)
            if match:
                return f"zhihu:answer:{match[1]}:{match[2]}"
        if url.scheme == "https" and url.netloc == "zhuanlan.zhihu.com":
            match = re.fullmatch(r"/p/(\d{1,25})/?", url.path)
            if match:
                return f"zhihu:article:{match[1]}"
        raise GatewayError("INVALID_CONTENT_ID", "请提供知乎回答或专栏文章的标准HTTPS链接")
    if platform == "wechat":
        if re.fullmatch(r"wechat:(?:short:[\w-]{8,100}|index:[a-f0-9]{32}|article:[\w=+-]{5,100}:\d{1,25}:\d{1,3}(?::[a-f0-9]{32})?)", value):
            return value
        url = urlsplit(value)
        if url.scheme == "https" and url.netloc == "mp.weixin.qq.com":
            match = re.fullmatch(r"/s/([\w-]{8,100})/?", url.path)
            if match:
                return f"wechat:short:{match[1]}"
            query = parse_qs(url.query)
            if url.path == "/s" and all(query.get(k) for k in ("__biz", "mid", "idx")):
                identity = "wechat:article:" + ":".join(query[k][0] for k in ("__biz", "mid", "idx"))
                if re.fullmatch(r"wechat:article:[\w=+-]{5,100}:\d{1,25}:\d{1,3}", identity):
                    # sn identifies the public article URL; it is not an account cookie.
                    if query.get("sn"):
                        if not re.fullmatch(r"[a-f0-9]{32}", query["sn"][0]):
                            raise GatewayError("INVALID_CONTENT_ID", "公众号文章sn格式无效")
                        identity += ":" + query["sn"][0]
                    return identity
        raise GatewayError("INVALID_CONTENT_ID", "请提供公众号mp.weixin.qq.com公开文章链接")
    if platform in SOCIAL_ROUTES:
        host, path, kind, pattern = SOCIAL_ROUTES[platform]
        if re.fullmatch(rf"{platform}:{kind}:{pattern}", value):
            return value
        url = urlsplit(value)
        match = re.fullmatch(rf"/{path}/({pattern})/?", url.path)
        if platform == "xiaohongshu" and not match:
            match = re.fullmatch(rf"/discovery/item/({pattern})/?", url.path)
        if url.scheme == "https" and url.netloc == host and match:
            return f"{platform}:{kind}:{match[1]}"
        raise GatewayError("INVALID_CONTENT_ID", f"请提供{platform}标准HTTPS内容链接")
    if platform not in ARTICLE_ROUTES:
        raise GatewayError("UNSUPPORTED_PLATFORM", "平台未实现")
    host, path, kind = ARTICLE_ROUTES[platform]
    value = value.strip()
    match = re.fullmatch(rf"{platform}:{kind}:(\d{{10,25}})", value)
    if match:
        return value
    url = urlsplit(value)
    match = re.fullmatch(rf"/{path}/(\d{{10,25}})/?", url.path)
    if url.scheme != "https" or url.netloc != host or not match:
        raise GatewayError("INVALID_CONTENT_ID", f"请提供{platform}标准文章HTTPS链接或平台内容标识")
    return f"{platform}:{kind}:{match[1]}"


def content_key(platform, value):
    normalized = normalize_ref(platform, value)
    if platform == "bilibili":
        from .bilibili import parse_bvid

        return parse_bvid(normalized)
    return normalized


def content_url(platform, value):
    value = normalize_ref(platform, value)
    if platform == "bilibili":
        return f"https://www.bilibili.com/video/{value}"
    if platform in SOCIAL_ROUTES:
        host, path, _, _ = SOCIAL_ROUTES[platform]
        return f"https://{host}/{path}/{value.rsplit(':', 1)[1]}"
    if platform == "zhihu":
        pieces = value.split(":")
        return (f"https://www.zhihu.com/question/{pieces[2]}/answer/{pieces[3]}"
                if pieces[1] == "answer" else f"https://zhuanlan.zhihu.com/p/{pieces[2]}")
    if platform == "wechat":
        pieces = value.split(":")
        if pieces[1] == "short":
            return "https://mp.weixin.qq.com/s/" + pieces[2]
        if pieces[1] == "article":
            return "https://mp.weixin.qq.com/s?" + urlencode(dict(zip(("__biz", "mid", "idx", "sn"), pieces[2:])))
        return "https://weixin.sogou.com/"  # Index candidates carry their safe source URL separately.
    host, path, _ = ARTICLE_ROUTES[platform]
    return f"https://{host}/{path}/{value.rsplit(':', 1)[1]}"


def direct_candidate(platform, value):
    return {
        "id": content_key(platform, value),
        "platform": platform,
        "url": content_url(platform, value),
        "title": "",
        "description": "",
        "matched_keywords": [],
    }


def create_adapter(settings, store, task_id, request):
    platform = request.platforms[0]
    if platform == "bilibili":
        from .bilibili import BilibiliAdapter

        return BilibiliAdapter(settings, store, task_id, request)
    if platform in ("xiaohongshu", "zhihu", "tieba"):
        from .social import SocialAdapter

        return SocialAdapter(settings, store, task_id, request)
    if platform == "wechat":
        from .wechat import WeChatAdapter

        return WeChatAdapter(settings, store, task_id, request)
    from .community import JuejinAdapter, SegmentFaultAdapter

    cls = {"juejin": JuejinAdapter, "segmentfault": SegmentFaultAdapter}.get(platform)
    if cls is None:
        raise GatewayError("UNSUPPORTED_PLATFORM", "当前没有此平台适配器")
    return cls(settings, store, task_id, request)
