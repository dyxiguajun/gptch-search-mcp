"""Public native technical-community adapters; Scrapling handles parsing only.

No browser stealth, CAPTCHA solving, account extraction, or public-web fallback.
"""

from __future__ import annotations

import json
import logging
import re
import time
from urllib.parse import urljoin, urlsplit

from lxml import html as dom_html
from scrapling import Selector

from .errors import GatewayError
from .http import Transport
from .platforms import content_url, normalize_ref
from .storage import cache_key


def html(text, url=""):
    return Selector(text or "<html/>", url=url, adaptive=False, huge_tree=False)


def clean(text):
    return str(html(str(text or "")).get_all_text(separator=" ", strip=True))


def next_state(page):
    node = page.css("script#__NEXT_DATA__").first
    if node is None:
        raise GatewayError("UPSTREAM_FORMAT_CHANGED", "页面未提供预期的公开数据，不猜测结果")
    try:
        return json.loads(node.text)["props"]["pageProps"]["initialState"]
    except (ValueError, KeyError, TypeError):
        raise GatewayError("UPSTREAM_FORMAT_CHANGED", "平台页面数据结构已改变") from None


def article_blocks(root, limit=120000):
    """Walk block order without dropping highlight-node whitespace or nested code.

    Scrapling's valid_values=True drops whitespace-only nodes, even strip=False.
    Use the mature underlying DOM here, preserving raw text and code boundaries.
    """
    tree = dom_html.fromstring(
        str(root.html_content), parser=dom_html.HTMLParser(no_network=True, huge_tree=False)
    )
    for ignored in tree.xpath(".//script|.//style|.//button|.//svg|.//noscript|.//iframe"):
        ignored.drop_tree()
    for br in tree.xpath(".//br"):
        br.tail = "\n" + (br.tail or "")
        br.drop_tree()
    blocks = []
    tags = {"p", "pre", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table", "blockquote"}

    def text_of(node):
        return "".join(node.xpath(".//text()"))

    def emit(kind, text, language=""):
        if text.strip():
            blocks.append((kind, language, text if kind == "code" else text.strip()))

    def walk(node):
        if node.tag == "pre":
            code = node.find(".//code")
            code = code if code is not None else node
            classes = code.get("class", "") + " " + node.get("class", "")
            match = re.search(r"(?:language-|lang-)([\w+-]+)", classes)
            language = match[1] if match else code.get("lang", "")
            emit("code", text_of(code), language)
            return
        if node.tag in tags and not node.xpath(".//pre"):
            if node.tag == "table":
                value = "\n".join(
                    "\t".join(text_of(cell).strip() for cell in row.xpath("./th|./td"))
                    for row in node.xpath(".//tr")
                )
            elif node.tag in ("ul", "ol"):
                value = "\n".join(text_of(li).strip() for li in node.xpath("./li"))
            else:
                value = text_of(node)
            emit("heading" if re.fullmatch(r"h[1-6]", node.tag or "") else node.tag, value)
            return
        inline = [node.text or ""]
        for child in node:
            if not isinstance(child.tag, str):
                inline.append(child.tail or "")
                continue
            if child.tag in tags or any(e.tag in tags for e in child.iterdescendants()):
                emit("p", "".join(inline))
                inline = []
                walk(child)
            else:
                inline.append(text_of(child))
            inline.append(child.tail or "")
        emit("p", "".join(inline))

    walk(tree)
    available = sum(len(v[2]) for v in blocks)
    records = []
    total = 0
    for kind, language, text in blocks:
        for offset in range(0, len(text), 12000):
            value = text[offset : offset + min(12000, limit - total)]
            if not value:
                break
            records.append(
                {
                    "id": str(len(records) + 1),
                    "type": kind,
                    "text": value,
                    "language": language,
                    "block_part": offset // 12000 + 1,
                }
            )
            total += len(value)
            if total >= limit:
                break
        if total >= limit:
            break
    if not records:
        raise GatewayError(
            "UPSTREAM_FORMAT_CHANGED", "文章主体未提取到文本，不能以页面导航代替正文"
        )
    truncated = available > total
    return records, {
        "status": "partial" if truncated else "complete",
        "chars_saved": total,
        "chars_in_visible_body": available,
        "truncated": truncated,
        "scope": "visible_article_body_only",
        "comments_included": False,
        "images_read": False,
    }


class CommunityAdapter:
    platform = ""
    hosts = frozenset()
    base = ""
    selector = ""

    def __init__(self, settings, store, task_id, request, *, transport=None):
        self.settings, self.store, self.task_id, self.request = settings, store, task_id, request
        self.http = transport or Transport(
            interval=max(1.5, settings.request_interval),
            max_requests=request.max_requests,
            concurrency=min(2, request.concurrency),
            api_base=self.base,
            allowed_hosts=self.hosts,
            auth_label=self.platform,
        )
        self.cache_hits = 0
        self.article_hints = {}
        self.http.client.headers["Referer"] = (
            "https://juejin.cn/" if self.platform == "juejin" else self.base + "/"
        )

    async def close(self):
        await self.http.close()

    async def preflight(self):
        pass  # Public endpoints; never probe or load the Bilibili account.

    def cached(self, kind, values):
        version = 3
        key = cache_key(f"{self.platform}:v{version}:{kind}", values)
        cached = None if self.request.refresh else self.store.cache_get(key)
        if cached:
            self.cache_hits += 1
        return key, cached

    def save(self, key, value, kind, ttl):
        self.store.cache_put(key, value, ttl)
        self.store.raw(self.task_id, f"{self.platform}_{kind}", value)
        return value

    async def page(self, url, params=None):
        logging.getLogger("httpx").setLevel(logging.WARNING)
        response = await self.http.get(url, params)
        page = html(response.content, str(response.url))
        title = str(page.css("title::text").get() or "")
        if any(s in title for s in ("安全验证", "访问验证", "人机验证")) or page.css(
            'form[action*="login"]'
        ):
            raise GatewayError("PLATFORM_ACCESS_REQUIRED", "平台要求本人登录或验证；停止采集")
        return page

    async def extract(self, value):
        identity = normalize_ref(self.platform, value)
        key, cached = self.cached("article", {"id": identity})
        if cached:
            return {**cached[0], "cache": {"cached": True, "fetched_at": cached[1]}}
        url = content_url(self.platform, identity)
        page = await self.page(url)
        canonical = page.css('link[rel="canonical"]::attr(href)').get()
        if not canonical or normalize_ref(self.platform, urljoin(url, canonical)) != identity:
            raise GatewayError(
                "CONTENT_IDENTITY_MISMATCH", "文章规范链接与请求内容不一致，拒绝归档"
            )
        root = page.css(self.selector).first
        if root is None:
            raise GatewayError("UPSTREAM_FORMAT_CHANGED", "缺少可验证的文章主体")
        title, published, author = self.metadata(page, identity)
        if not title:
            raise GatewayError("UPSTREAM_FORMAT_CHANGED", "文章缺少标题，拒绝不明内容")
        body, coverage = article_blocks(root)
        item = {
            "id": identity,
            "platform": self.platform,
            "url": url,
            "title": title,
            "description": "",
            "author": author,
            "published_at": published,
            "extracted_at": time.time(),
            "body": body,
            "status": coverage["status"],
            "coverage": {
                "all_platform_content": False,
                "body": coverage,
                "parser": "Scrapling Selector 0.5",
                "adaptive_selectors": False,
            },
            "cache": {"cached": False, "fetched_at": time.time()},
            "source_content_is_untrusted": True,
        }
        links = []
        for link in root.css("a[href]"):
            href = link.attrib.get("href", "")
            target = urljoin(url, href)
            parsed = urlsplit(target)
            if (
                not href.startswith("#")
                and parsed.scheme in ("http", "https")
                and not parsed.username
                and not parsed.password
                and len(target) <= 512
                and target not in links
            ):
                links.append(target)
            if len(links) == 30:
                break
        item["links"] = links
        hints = self.article_hints.get(identity, {})
        for field in ("description", "stats", "source_relationship"):
            if field in hints:
                item[field] = hints[field]
        return self.save(key, item, "article", 86400)


class JuejinAdapter(CommunityAdapter):
    platform = "juejin"
    hosts = frozenset({"juejin.cn", "api.juejin.cn"})
    base = "https://api.juejin.cn"
    selector = "#article-root"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cursors = {}

    async def search_page(self, keyword, page):
        key, cached = self.cached("search", {"keyword": keyword, "page": page})
        if cached:
            data = cached[0]
        else:
            if page == 1:
                cursor = "0"
            else:
                if (keyword, page) not in self.cursors:
                    await self.search_page(keyword, page - 1)
                cursor = self.cursors.get((keyword, page))
                if cursor is None:
                    raise GatewayError("PAGINATION_STALLED", "缺少平台返回的下一页游标")
            response = await self.http.get(
                "/search_api/v1/search",
                {"aid": 2608},
                method="POST",
                json_body={"search_type": 0, "key_word": keyword, "cursor": cursor, "limit": 20},
                headers={"Referer": "https://juejin.cn/"},
            )
            try:
                payload = response.json()
            except ValueError:
                raise GatewayError("UPSTREAM_FORMAT_CHANGED", "掘金未返回JSON搜索结果") from None
            if payload.get("err_no") != 0:
                raise GatewayError(
                    "PLATFORM_API_ERROR", "掘金搜索接口返回错误", api_code=payload.get("err_no")
                )
            if not isinstance(payload.get("data"), list) or not isinstance(
                payload.get("has_more"), bool
            ):
                raise GatewayError("UPSTREAM_FORMAT_CHANGED", "掘金搜索缺少结果/可信分页标志")
            rows = []
            for native_position, raw in enumerate(payload["data"], 1):
                if raw.get("result_type") != 2:
                    continue
                model = raw.get("result_model") or {}
                info = model.get("article_info") or {}
                native = str(info.get("article_id") or "")
                if not re.fullmatch(r"\d{10,25}", native):
                    raise GatewayError("UPSTREAM_FORMAT_CHANGED", "掘金文章结果缺少可信内容标识")
                identity = f"juejin:post:{native}"
                rows.append(
                    {
                        "id": identity,
                        "platform": self.platform,
                        "url": content_url(self.platform, identity),
                        "title": clean(info.get("title")),
                        "description": clean(info.get("brief_content")),
                        "author": str((model.get("author_user_info") or {}).get("user_name") or ""),
                        "published_at": info.get("ctime"),
                        "matched_keywords": [keyword],
                        "search_page": page,
                        "native_rank": native_position,
                        "rank_scope": "within_native_search_page",
                        "stats": {
                            k: info[k]
                            for k in ("view_count", "digg_count", "comment_count")
                            if k in info
                        },
                        "source_relationship": {
                            k: info[k]
                            for k in ("is_original", "original_type", "original_author", "link_url")
                            if k in info
                        },
                    }
                )
            data = {
                "rows": rows,
                "has_more": payload["has_more"],
                "next_cursor": payload.get("cursor"),
                "fetched_at": time.time(),
                "scope": "native_search_article_rows_only",
                "other_result_types_omitted": len(payload["data"]) - len(rows),
            }
            if data["has_more"] and (
                not isinstance(data["next_cursor"], str)
                or not data["next_cursor"]
                or data["next_cursor"] == cursor
            ):
                raise GatewayError("PAGINATION_STALLED", "掘金游标未前进")
            self.save(key, data, "search", 900)
        if data["has_more"]:
            self.cursors[(keyword, page + 1)] = data["next_cursor"]
        rows = [
            {**v, "cached": bool(cached), "fetched_at": data["fetched_at"]} for v in data["rows"]
        ]
        self.last_page_empty_is_filtered = bool(
            not rows and data.get("other_result_types_omitted") and data["has_more"]
        )
        self.article_hints.update((v["id"], v) for v in rows)
        return rows, 0 if data["has_more"] else page

    def metadata(self, page, identity):
        title = page.css("h1").first
        published = (
            page.css('meta[itemprop="datePublished"]::attr(content)').get()
            or page.css("time::attr(datetime)").get()
        )
        author = page.css(".author-name").first
        return (
            str(title.get_all_text(separator=" ", strip=True)) if title else "",
            published,
            str(author.get_all_text(strip=True)) if author else "",
        )


class SegmentFaultAdapter(CommunityAdapter):
    platform = "segmentfault"
    hosts = frozenset({"segmentfault.com"})
    base = "https://segmentfault.com"
    selector = "article.article-content"

    async def search_page(self, keyword, page):
        key, cached = self.cached("search", {"keyword": keyword, "page": page})
        if cached:
            data = cached[0]
        else:
            source = await self.page("/search", {"q": keyword, "page": page})
            try:
                result = next_state(source)["search"]["result"]
            except (KeyError, TypeError):
                raise GatewayError("UPSTREAM_FORMAT_CHANGED", "思否未提供原生搜索结果") from None
            if (
                result.get("q") != keyword
                or result.get("page") != page
                or not isinstance(result.get("rows"), list)
            ):
                raise GatewayError("CONTENT_IDENTITY_MISMATCH", "思否搜索关键词或页码不一致")
            total_pages = result.get("total_page")
            if not isinstance(total_pages, int) or total_pages < 0:
                raise GatewayError("UPSTREAM_FORMAT_CHANGED", "思否缺少可信分页信息")
            rows = []
            for native_position, raw in enumerate(result["rows"], 1):
                if raw.get("type") != "article":
                    continue
                info = raw.get("contents") or {}
                url = urljoin(self.base, info.get("url", ""))
                identity = normalize_ref(self.platform, url)
                rows.append(
                    {
                        "id": identity,
                        "platform": self.platform,
                        "url": content_url(self.platform, identity),
                        "title": clean(info.get("title")),
                        "description": clean(info.get("excerpt")),
                        "published_at": info.get("created"),
                        "matched_keywords": [keyword],
                        "search_page": page,
                        "native_rank": native_position,
                        "rank_scope": "within_native_search_page",
                        "stats": {k: info[k] for k in ("votes", "comments") if k in info},
                    }
                )
            data = {
                "rows": rows,
                "total_pages": total_pages,
                "fetched_at": time.time(),
                "scope": "native_search_article_rows_only",
                "other_result_types_omitted": len(result["rows"]) - len(rows),
            }
            self.save(key, data, "search", 900)
        rows = [
            {**v, "cached": bool(cached), "fetched_at": data["fetched_at"]} for v in data["rows"]
        ]
        self.last_page_empty_is_filtered = bool(
            not rows and data["other_result_types_omitted"] and data["total_pages"] > page
        )
        self.article_hints.update((v["id"], v) for v in rows)
        return rows, data["total_pages"]

    def metadata(self, page, identity):
        try:
            detail = next_state(page)["articleDetail"]["artDetail"][identity.rsplit(":", 1)[1]][
                "article"
            ]
        except (KeyError, TypeError):
            raise GatewayError("UPSTREAM_FORMAT_CHANGED", "思否文章详情结构已改变") from None
        # HTML canonical is checked separately; compare a detail identity when supplied.
        native = identity.rsplit(":", 1)[1]
        if str(detail.get("id")) != native:
            raise GatewayError("CONTENT_IDENTITY_MISMATCH", "思否文章详情ID不一致")
        author = detail.get("user") or {}
        self.article_hints.setdefault(identity, {})["source_relationship"] = {
            k: detail[k] for k in ("origin_url", "article_type", "license") if k in detail
        }
        return (
            clean(detail.get("title")),
            page.css('time[itemprop="datePublished"]::attr(datetime)').get(),
            str(author.get("name") or ""),
        )
