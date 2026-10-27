# TM Index

Thoughts Memo 汉化组译文的摘要、关键词、原作者和双向引用索引。站点支持全文检索，并可按关键词、原作者和发布账号筛选。

点击「导出 TXT」可下载当前筛选结果，包含所有页的匹配文章，并按发表时间从早到晚排列。

展开「从文本提取备份并导出 Markdown」，粘贴包含备份链接的文本，可按链接首次出现的顺序合并文章正文；重复链接只导出一次，抓取失败可重试。导出结果中的文章备份链接会转换为对应的知乎文章或回答链接，图片保留为网络链接。

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
- `missing-zhihu-references.json` / `.csv`：原文链接有唯一译文匹配、但未发现指向该译文的知乎链接或注释的候选。其他译本或相关引用仍需逐条核对
- `missing-zhihu-reference-review-method.json`：候选的置信分级、类别规则与抽样审计
- `validation.json`：最终覆盖与完整性检查
- `user-corrections.json`：人工确认的范围和作者更正

页面当前收录 2,119 篇译文，原作者均已确认。摘要和初始关键词由 `gpt-6-luna`（max）逐篇生成，后续经过范围、作者和标签复核。

引用关系主要基于本地存档；个别文章使用已核验的较新知乎公开版，版本信息记录在 `references.json` 的元数据中。

## 权利说明

仓库中的摘要、索引和站点代码按 [CC0 1.0](LICENSE) 提供。链接所指原文、第三方内容、商标及署名不因此改变其权利归属。

