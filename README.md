# TM Index

Thoughts Memo 汉化组译文的摘要、关键词、原作者和双向引用索引。站点支持全文检索，并可按关键词、原作者和发布账号筛选。

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
- `references.json`：译文之间的引用边、匹配方式与歧义记录
- `validation.json`：最终覆盖与完整性检查
- `user-corrections.json`：人工确认的范围和作者更正

页面当前收录 2,119 篇译文，原作者均已确认。摘要和初始关键词由 `gpt-6-luna`（max）逐篇生成，后续经过范围、作者和标签复核。

## 权利说明

仓库中的摘要、索引和站点代码按 [CC0 1.0](LICENSE) 提供。链接所指原文、第三方内容、商标及署名不因此改变其权利归属。

