# TM Index

Thoughts Memo 汉化组译文的摘要、关键词、原作者和双向引用索引。站点支持全文检索，并可按关键词、原作者和发布账号筛选；每篇译文也显示引用它的回答。

译文卡片默认等高，长总结只显示容器可容纳的开头部分，并标注当前可见的大致比例；点击「展开全部总结」可阅读全文，再点击「收起总结」恢复等高。短总结不显示展开按钮，搜索和导出始终使用完整总结。

点击「导出 TXT」可下载当前筛选结果，包含所有页的匹配文章，并按发表时间从早到晚排列。

TXT 和 Markdown 的导出文件名均附带导出时间戳（UTC，精确到毫秒），例如 `译文摘要汇编_2026-10-29T10-30-00-123Z.txt`。

译文列表默认按译文赞数从高到低排列，也可以按发布日期、被译文文章或回答引用次数，以及引用回答的最高或累计赞数排序；排序指标和升降序可分别选择。展开译文后，其引用回答也可以分别选择排序指标和方向。文章和回答的赞数来自本地存档，并非实时数据。

展开「从文本提取备份并导出 Markdown」，粘贴包含备份链接的文本，可按链接首次出现的顺序合并文章正文；可选择为每篇译文追加引用回答，默认只选引用程度为高的回答，可切换到中、低或全部；先按引用程度筛选，再按存档赞数排序，默认每篇最多 3 篇，数量留空表示不限。重复内容只导出一次，文本中直接提供的回答不受引用程度筛选和数量上限影响；抓取失败可重试。导出结果中的文章备份链接会转换为对应的知乎文章或回答链接，图片保留为网络链接。

- 网站：https://l-m-sherlock.github.io/tm-index/
- 知乎存档：https://l-m-sherlock.github.io/ZhiHuArchive/
- 翻译合集：https://l-m-sherlock.github.io/thoughts-memo-translation/

## 数据

公开数据位于 [`data/`](data/)：

- `catalog.json`：译文摘要、规范关键词、作者和来源记录
- `original-authors.jsonl`：逐篇原作者归属及证据
- `authors.json`：原作者索引
- `keywords.json`：规范关键词及文章数
- `keyword-aliases.json`：全局同义标签映射
- `keyword-per-article.json`：只适用于特定文章语境的标签更正
- `keyword-normalization-review.json`：标签合并理由与保留边界
- `references.json`：译文之间的引用边、匹配方式与歧义记录；同时识别正文链接和知乎原生引用注释
- `answer-references.json`：本地回答与译文之间的直接链接和经核验的正文关系，附引用程度、证据及回答链接
- `missing-zhihu-references.json` / `.csv`：原文链接有唯一译文匹配、但未发现指向该译文的知乎链接或注释的候选。其他译本或相关引用仍需逐条核对
- `missing-zhihu-reference-review-method.json`：候选的置信分级、类别规则与抽样审计
- `reference-editorial-decisions.json`：既有候选逐对编辑处置及待判断理由
- `missing-zhihu-reference-recheck.json`：基于更新后的本地备份，对既有候选逐对复核的结果、链接证据与来源哈希；独立于历史候选清单和页面计数
- `validation.json`：最终覆盖与完整性检查
- `user-corrections.json`：人工确认的范围和作者更正
- `summary-revisions.json`：长文总结的修订前后稿、来源哈希、主要论点覆盖与旧稿问题
- `summary-reviews.json`：长文逐篇主要观点清单、保留或重写理由，以及独立全文复核记录

页面当前收录 2,150 篇译文，原作者均已确认。初版摘要和初始关键词由 `gpt-6-luna`（max）逐篇生成，后续经过范围、作者和标签复核。总结的原始模型标注保留，后续修订及独立复核来源另记于 `summary_revision` 和审阅、修订记录。

2026-11-01 完整复核正文至少 20,000 字符的 60 篇译文（合计 2,150,982 字符），均发现主要观点、证据或限定覆盖不足并完成修订。写作与独立全文复核均使用 `gpt-6-luna`（max）。本轮总结平均长度由 207.4 增至 886.6 字符，中位数由 172 增至 855；修订后范围为 234–1,558 字符，按体裁与复杂度调整。此前两篇 Codex 修订稿保留在历史中，本轮也接受了独立复核和再次修订。

正文 8,000～20,000 字符译文的 259 篇全量复核已完成：205 篇重写、54 篇保留，总结平均长度从 157.0 增至 492.1 字符。分层统计、逐篇理由和前后稿见[全量复核报告](docs/summary-midlength-results.md)。此前首批 92 篇（70 篇重写、22 篇保留）的发布情况见[阶段报告](docs/summary-midlength-progress.md)。

文章间引用关系使用已核验的公开版及新增文章的备份快照；核验范围和时间记录在 `references.json` 的元数据中。数据不会随知乎文章更新自动变化。

回答反链覆盖本地存档中的 Thoughts Memo 和 Jarrett Ye 回答，按逐对证据区分低（提及标题或关键词）、中（化用观点或结论）、高（节选或全文引用）。无链接的关系依赖候选召回和模型审阅，可能有遗漏。其他账号的回答及存档之后的更新不在本次分析范围内。

## 待核对译文链接复核

2026-11-02 的两轮本地备份核验从既有候选中移除 83 条已有知乎引用的关系，留下 246 条原文链接对应关系。结合此前逐条编辑审阅，其中 243 条暂不添加，网站目前显示 3 条仍需判断的引用、涉及 3 篇文章；完整审阅决定保存在 [编辑处置记录](data/reference-editorial-decisions.json)。这不改变原有引用图谱，也不是新文章的全量扫描或知乎实时核验。历史核验见 [最新复核报告](docs/missing-zhihu-reference-recheck-20261102-1230.md) 和 [首轮历史报告](docs/missing-zhihu-reference-recheck.md)；当前队列见 [文章列表](docs/missing-zhihu-reference-articles.md) 和 [CSV 列表](data/missing-zhihu-reference-articles.csv)。

更新备份仓库后，可运行只读复核：

```sh
python3 scripts/audit_missing_zhihu_references.py --archive ../ZhiHuArchive --output data/missing-zhihu-reference-recheck.json --report docs/missing-zhihu-reference-recheck.md
```

已应用的核验记录受保护；再次复核须为输出和报告指定新文件名，不能覆盖已应用历史。应用脚本会校验候选版本、备份提交及正文文件哈希，保留已移除项与证据；重复应用不产生变化。第二轮应用命令：

```sh
python3 scripts/apply_reference_recheck.py --archive ../ZhiHuArchive --audit data/missing-zhihu-reference-recheck-20261102-1230.json --report docs/missing-zhihu-reference-recheck-20261102-1230.md
```

## 总结工作流

写作、独立复核和主代理验收规范见 [总结工作流](docs/summary-workflow.md)。先阅读全文并建立主要观点清单，按体裁、正文长度和内容复杂度决定篇幅，不设置 260 字的长文上限。结构、哈希和阅读区间校验不能代替语义复核。

`scripts/summary_workflow.py` 提供 `prepare`、`check`、`apply` 三个入口，不生成总结或调用模型 API。正文快照只读，工作文件存于忽略的 `.work/` 中；全部结果通过独立复核后才应用。导入旧目录须使用 `apply --import-catalog`，重放已验收的修订历史；未知总结或来源冲突会停止同步。

工具回归检查：`python3 -m unittest discover -s tests -v`。

## 权利说明

仓库中的摘要、索引和站点代码按 [CC0 1.0](LICENSE) 提供。链接所指原文、第三方内容、商标及署名不因此改变其权利归属。
