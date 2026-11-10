# 第三方许可证与复用边界

日期：2026-10-15。以下内容记录实际仓库 LICENSE，不替代原许可证全文。

| 来源 | 实际许可 | 当前使用 |
|---|---|---|
| MediaCrawler | NON-COMMERCIAL LEARNING LICENSE 1.1 | 仅独立保留源码研究。许可限制为非商业学习/研究，禁止大规模采集或干扰平台；未经书面同意不得商业使用。没有将该许可代码复制进 src。 |
| adoresever/bilibili-mcp | MIT；Copyright 2026 AGI_Ananas | 源码参考；未复制运行模块。 |
| Zijian-Ni/bilibili-mcp-server | MIT；Copyright 2026 Zijian Ni | 适配 `auth.py` 为 `src/personal_search/vendor/wbi.py`，保留来源声明、MIT 全文 `vendor/LICENSE.bilibili-mcp-server`，文件头注明改动。 |
| xpzouying/xiaohongshu-mcp | Apache License 2.0 | 仅独立研究，未作为运行依赖、未复制代码。 |
| Scrapling 0.5（D4Vinci，Copyright 2024 Karim shoair） | BSD-3-Clause | 通过PyPI依赖复用Selector及AsyncStealthySession浏览器引擎、资源/广告过滤定义；未复制其调度/MCP服务；精确包哈希由uv.lock记录，原克隆工程只读，许可全文留存vendor/LICENSE.scrapling。 |
| 用户 CC Extractor | 实际 LICENSE 为 MIT；Copyright 2026 dyxiguajun | 从 Working Space 的 bili.py / app.py 提取纯字幕辅助函数到 `vendor/cc_ext.py`，保留 `vendor/LICENSE.cc-ext` 全文与来源/改动声明；未复制 UI、网络客户端、会话管理或重试循环，原工程未修改。 |

运行依赖使用 PyPI 发布包，精确版本和哈希由 `uv.lock` 记录。MCP SDK、httpx、Pydantic、defusedxml、qrcode、filelock、platformdirs、psutil 各包附带各自许可证；打包 wheel 保留 vendor MIT 文件，分发依赖时需保留它们的许可声明。工程仅面向用户个人研究，第一版没有发布、评论、点赞、关注、私信、上传或账户修改接口。
