from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SearchDepth = Literal["quick", "standard", "deep"]
FetchSection = Literal[
    "metadata",
    "subtitles",
    "comments_hot",
    "comments_latest",
    "replies_hot",
    "replies_latest",
    "danmaku",
    "body",
]


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(
        min_length=1,
        max_length=300,
        description="一组精炼的平台原生搜索词；先把研究问题拆成检索概念。直接指定内容时可填任务主题。",
    )
    keywords: list[str] = Field(
        default_factory=list,
        max_length=6,
        description="补充关键词或同义词；与query去重后合计最多六组，不要逐句复制用户问题。",
    )
    platforms: list[Literal["bilibili", "juejin", "segmentfault", "xiaohongshu", "zhihu", "tieba", "wechat"]] = Field(
        default_factory=lambda: ["bilibili"],
        min_length=1,
        max_length=1,
        description="从当前可靠的能力信息选择平台，未知时可查询get_capabilities；当前每任务仅接收一个平台。",
    )
    depth: SearchDepth = Field(
        default="standard",
        description="quick/standard/deep是不同预算预设，不是强制流程或任务映射；模型按需选择并可单独调整。",
    )
    pages_per_keyword: int | None = Field(
        default=None,
        ge=1,
        le=5,
        description="每组关键词页数；默认由depth决定，复杂任务也可先限制为一页。",
    )
    max_candidates: int = Field(default=100, ge=1, le=300)
    max_items: int | None = Field(
        default=None,
        ge=0,
        le=20,
        description="深度提取数量；设0先只筛选候选，再以content_ids采集高价值来源。",
    )
    comment_sorts: list[Literal["hot", "latest"]] = Field(
        default_factory=lambda: ["hot", "latest"],
        max_length=2,
        description="按需采集的一级评论排序；[]禁用评论。hot使用平台热度，非本地按点赞重排。",
    )
    max_comments: int | None = Field(
        default=None,
        ge=0,
        le=100,
        description="每种排序最多保存的一级评论数，例如20/100；0禁用。默认兼容原页数预算，最多100。",
    )
    comment_pages: int | None = Field(
        default=None,
        ge=1,
        le=10,
        description="每排序最多请求页数；显式条数预算按每页20加2页余量估算，到数即停；旧请求默认1页。仍受HTTP总预算和限速约束。",
    )
    reply_pages: int = Field(default=1, ge=0, le=3)
    max_reply_threads: int = Field(default=3, ge=0, le=10)
    subtitle_tracks: int = Field(default=2, ge=0, le=5)
    video_pages: int = Field(default=1, ge=1, le=5)
    max_danmaku: int = Field(default=500, ge=0, le=5000)
    max_requests: int = Field(
        default=120,
        ge=5,
        le=300,
        description="整个任务的HTTP请求总预算，包含签名、重试和字幕双次校验；按信息价值设置。",
    )
    timeout_seconds: int = Field(
        default=300, ge=10, le=1800, description="任务最长执行秒数；get_task查询不会延长它。"
    )
    concurrency: int = Field(default=2, ge=1, le=3)
    refresh: bool = Field(
        default=False, description="默认复用缓存；只有时效要求或明确刷新时才设true。"
    )
    content_ids: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="直接提取当前platform的选定来源并跳过搜索；格式见get_capabilities。文章使用标准链接或平台前缀ID；视频明确p参数保留。",
    )

    @model_validator(mode="after")
    def normalize(self):
        self.comment_sorts = list(dict.fromkeys(self.comment_sorts))
        if self.comment_pages is None:
            self.comment_pages = ((self.max_comments + 19) // 20 + 2) if self.max_comments else 1
        self.query = self.query.strip()
        if not self.query or any(not k.strip() or len(k) > 300 for k in self.keywords):
            raise ValueError("关键词不能为空，且不超过 300 个字符")
        self.keywords = list(dict.fromkeys([self.query, *[k.strip() for k in self.keywords]]))
        if len(self.keywords) > 6:
            raise ValueError("query 与 keywords 合计最多六组关键词")
        if self.pages_per_keyword is None:
            self.pages_per_keyword = {"quick": 1, "standard": 2, "deep": 5}[self.depth]
        if self.content_ids:
            from .platforms import normalize_ref

            self.content_ids = list(
                dict.fromkeys(normalize_ref(self.platforms[0], v) for v in self.content_ids)
            )
        if self.max_items is None:
            from .platforms import content_key

            self.max_items = (
                len({content_key(self.platforms[0], v) for v in self.content_ids})
                if self.content_ids
                else {"quick": 0, "standard": 3, "deep": 10}[self.depth]
            )
        return self


TERMINAL = {"completed", "partial", "failed", "cancelled", "interrupted"}
