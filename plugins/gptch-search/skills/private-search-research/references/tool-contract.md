# 按需读取的工具契约

平台、能力及预算以可靠的会话能力信息和工具输入 schema 为准；必要时用 get_capabilities 核实。本文件是接口参考，不要求执行表格中的全部工具或遵循排列顺序，不维护固定支持平台名单。

| 工具 | 何时有用 | 重要边界 |
|---|---|---|
| get_capabilities | 能力未知、发生变化或相关失败需要核实；已知时可复用 | 本机实现信息，不请求平台、不验证登录；未列平台不可用 |
| search_archive | 需要定位历史资料时；已知档案ID可直接读取 | 词面 FTS5，不是语义搜索；没有结果不代表线上没有 |
| deep_search | 有价值的原生检索或选定来源采集 | 立即返回 task_id，数据还可能在采集；平台及标识格式取自能力报告 |
| get_task | 查询已知任务、取候选或归档摘要 | view 为 summary / candidates / items；offset / limit 分页 |
| fetch_content | 读取已保存的内容证据 | task_id、content_id、section，按 has_more / next_offset 续读；不访问平台 |
| manage_task | 无需继续时取消，或修复已知原因后恢复 | 保留已采集资料；不循环恢复风控任务 |
| inspect_video | 画面可以填补证据缺口时，少量预览或指定秒数/局部加密 | 真正PNG图片块及实际帧时间；不做语义分类；当前客户端是否看到需核实 |
| read_video_evidence | 已知evidence_id复用图片 | 本机缓存可能过期/淘汰，不请求平台、不读取任意路径 |
| auth_status | 用户要求检查会话或出现认证错误 | 只检查已实现的认证来源；不返回凭证，不是每次搜索的前置步骤 |
| open_login_panel | 首次选择平台、本人登录或失效后重新授权 | 提供本机状态/选择页；用户在网站内操作，AI不接触Cookie或验证码；无需登录全部平台 |
| ping_local_server | 用户要求连通检查或本机连接排障 | 正常研究不必调用，平台能力查询使用 get_capabilities |

## 请求选择

- `query` 是一组精炼检索词，`keywords` 是互补词组；query 自身也在总关键词预算内。
- `platforms` 使用能力报告中的标识；按 `request_limits.platforms.maxItems` 决定是否分任务，不能提交不支持的平台标识。
- 若平台提供 `article_body` / `fetch_sections=[metadata,body]`，候选筛选后以标准文章链接或平台前缀ID指定来源，再用body读正文与代码块。body的type=code保留源代码空白和可见语言标签，不保证源码可直接运行。metadata包含规范链接、发布/采集时间、正文覆盖、已提取的链接和平台明确提供的来源关系；关系字段是平台声明，缺失不等于原创或独立。不要请求该平台未支持的字幕/评论/音视频。
- `depth` 的默认页数和提取数量见 `search_modes`。可单独限制 pages_per_keyword、max_candidates、max_items、max_requests 和 timeout_seconds，不需要用满默认值。
- `max_items=0` 可只获取候选。筛选后另一个任务以 content_ids 指定少量来源，跳过关键词搜索；同时设置适当 max_items，避免只提取了部分指定来源却忽略截断。
- 按当前 `collection_budgets` / schema 分级采集，先选来源再选内容，避免几十个候选均执行全套提取。
- 当前支持 `comment_sorts=["hot"]`、`max_comments=20` / `100`、`reply_pages=0` 获取有限热门一级评论；hot遵循平台排序，不按点赞数重新排序。`comment_sorts=[]` 或 `max_comments=0` 禁用评论；`subtitle_tracks=0` 禁用字幕，`max_danmaku=0` 禁用弹幕。只需字幕时关闭评论和弹幕；只需讨论时关闭字幕，弹幕也可按需开启。
- 显式条数预算未指定 `comment_pages` 时按每页20估算并留2页余量（实测每页不足20），达到条数立即停止；可设置更低页数控制成本。最多保存100条/排序、最多请求10页/排序，仍受整个任务HTTP预算和限速约束。平台可见结束、分页/风控失败或预算不足时可能少于目标，读取 coverage 与错误，不能补造。
- 默认复用评论页缓存，扩大20→100只请求尚缺页面；弹幕按CID缓存完整当前池，换条数预算可从池中切片，不再下载。缓存有时间与登录范围限制；刷新仅用于确实需要时效的研究。旧请求保持双排序和楼中楼默认行为，模型应显式缩小无价值组件。
- 若来源是视频，保留用户指定的分P；不要将另一个 CID / 分P 的字幕当成该段内容。

## 状态与等待

queued / running 为进行中。completed / partial / failed / cancelled / interrupted 为终态。

`requires_login` 是提交前响应，没有 task_id，不可轮询。`auth_status(platform="all")` 只读最近观测的全平台状态；新平台默认不发请求，指定 `verify=true` 才做一次核验。无参数的B站核验继续兼容旧客户端。本人登录完成后重提任务，认证失效的既有任务可在原因解决后按需恢复。已保存、已核验、平台验证码/访问受限分别说明，不把Cookie存在当作可用证明。

仅在需要任务进度或结果时查询；按规模等候并逐渐拉长间隔，轮询受自身等待预算限制。已有归档可直接读取，不必为每次读取重新搜索或建立任务。`progress.search_truncated=true` 表示检索范围受页数或候选预算约束，即使任务 completed 也不能声称穷尽。

fetch_content 的 metadata 包含组件覆盖。某个组件 unavailable 可能是当前没有可用内容，AUTH_REQUIRED 表示认证缺口；failed 不应描述为“没有内容”。部分有效轨道或评论可以使用，但报告缺口。

HTTP 风控、认证、预算错误不值得用相同参数立即重跑。网络/平台失败与零结果分别解释；需要用户操作时明确具体原因，不要求提供 Cookie 或令牌。
