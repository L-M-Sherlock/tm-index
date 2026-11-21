# 知乎备份增量工作流

每次收到备份更新，维护者先固定 ZhiHuArchive 的上次完整处理提交和本次截止提交，再按同一清单处理文章与回答。备份仓库只读，工作材料放在 `.work/`。使用隔离副本处理目录和网页。只有全部检查通过后才推进完整处理基线。

## 清点来源变化

1. 用 `scripts/archive_update.py prepare` 固定 Git 基线、截止提交、文章和回答的文件及正文哈希。已有完整处理记录时使用该记录的提交。首次运行明确填写经核实的 `--base`。按新增、删除、正文和元数据变化核对清单。
2. 为每个变化项记录处理结果和理由。与本站相关的元数据包括文章标题、账号、专栏、日期、赞数，以及回答的标题、作者、日期、赞数和链接。网页的排序和导出直接使用这些字段。把原始文件变化和可见内容变化分开核对，保存文章或回答采用的实际来源版本。
3. 在批次中逐项处理以下几类数据。局部补录可以单独验收并写明范围，完整批次仍须保留未处理项。

```sh
python3 scripts/archive_update.py prepare --archive "$archive" --base "$base" --tip "$tip" --plan "$work/inventory.json"
python3 scripts/archive_update_install.py capture --output "$work/site-baseline.json"
```

以下命令中的 `$archive` 是本地 ZhiHuArchive 路径，`$base` 和 `$tip` 是固定的 Git 提交，`$work` 是本批私有目录，`$stage` 是隔离站点根目录。已有完整处理记录时可省略 `--base`。

## 处理译文目录

- 用 `scripts/archive_translation_intake.py prepare` 固定新增文章。逐篇判断收录范围、确认原作者及归属，核对规范关键词，记录排除理由。用 `stage` 生成隔离目录、作者与关键词索引和网页数据。首份摘要按[总结工作流](summary-workflow.md)写作并独立通读复核。
- 对已收录译文，用 `archive_translation_intake.py refresh-metadata` 同步确认过的显示字段。需要替换正文来源时，在隔离副本中保留旧目录和摘要历史，按新版本完整复审摘要。为 `summary_workflow.py prepare` 传入显式文章清单及 `--source-transition-from` 所指定的旧目录。新版本的验收记录和旧版本历史一并保留。已有公开读回版本与备份冲突时，保留两版证据，核定采用版本后再验收。删除或下架逐篇判断，并处理对应的引用边、搜索、导出和历史记录。
- 已有摘要修订史的译文下架后，用 `summary_workflow.py retire --previous-catalog OLD.json --decisions RETIREMENTS.json` 归档。决定文件包含 `schema_version`、旧目录字节哈希 `previous_catalog_sha256`、完整提交 `archive_base` 和 `archive_tip`，以及逐篇写明 `id` 和 `reason` 的 `entries`。仍可复验全部旧稿。

## 处理两个引用索引

| 索引 | 本批次检查范围 |
|---|---|
| 译文间引用 | 变化的已收录文章、所有新增译文的出链、已收录文章对新增译文的反链。核对知乎文章、存档链接与原生注释。保留歧义原文链接和可能失效的旧引用供逐对判断。 |
| 回答反链 | 新增及变化的回答对整个译文目录的引用，以及既有回答对新增译文的引用。复核已消失的直链、正文变化影响的既有分级和推断关系。同步回答的显示元数据、反链计数和分级。 |

- 译文间引用用 `scripts/archive_article_reference_audit.py` 固定源文、核对缺失关系和原文链接候选。直接链接按目标 ID 去重。外链匹配先检查是否对应唯一译文，歧义和公开版本差异按证据裁定。旧边删除必须给出依据。保留既有待核对状态及处置历史。
- 回答反链用 `scripts/answer_reference_update.py`，整批运行使用 `all` 范围。按同一回答与译文的一对关系审阅。低指标题、链接或参考注释，中指答主正文可归属的化用，高指回答正文中可与译文核验的实质摘录或全文。原生注释标签中的摘句仍可证明直接链接。等级取决于答主正文怎样使用译文。实在无法判断时保留证据和未分级状态。
- 无链接关系由原标题、独特术语、特征性句段等召回候选，再阅读两端上下文并判定。把已核验的新增关系放入回答审阅包的 `inferred_reviews`。它使用相同的来源哈希和双边证据字段，2 级另记 `classification.contextual_attribution`，说明答主正文如何使用译文的主张。把检索范围、方法、结论和局限记到全局处置文件的 `unlinked_answer_review`。核对新增译文时，回答反链扫描应包含本批正文未变化的回答。

```sh
python3 scripts/archive_article_reference_audit.py prepare --root "$stage" --archive "$archive" \
  --base "$base" --tip "$tip" --plan "$work/article-reference-plan.json"
python3 scripts/archive_article_reference_audit.py apply --root "$stage" --archive "$archive" \
  --plan "$work/article-reference-plan.json" --reviews "$work/article-reference-reviews.json"

python3 scripts/answer_reference_update.py --root "$stage" prepare --archive "$archive" \
  --base "$base" --tip "$tip" --changes all --new-article-ids "$work/new-translation-ids.json" \
  --output "$work/answer-plan.json"
python3 scripts/answer_reference_update.py --root "$stage" apply --archive "$archive" \
  --plan "$work/answer-plan.json" --reviews "$work/answer-reviews.json"
```

没有新增译文时可以省略 `--new-article-ids`。局部补录新增回答可以用 `--changes added`，该范围只形成阶段记录。

## 验收和应用

1. 在隔离副本中应用各阶段已验收的数据。复核目录、作者与关键词索引、译文双向引用、回答反链、校验元数据和网页内嵌记录。核对既有审阅和修订历史、导出顺序、回答赞数排序与分级筛选。
2. 用 `scripts/archive_update.py check` 核对来源清单、逐项处置、阶段证据和站点派生数据。运行 `python3 -m unittest discover -s tests` 和 `node tests/test_pending_reference_ui.js`。用桌面和移动端检查搜索、双向引用、回答分级筛选、摘要显示以及 TXT 和 Markdown 导出。
3. 所有阶段通过后用 `complete` 生成完整处理记录。在正式工作区核对本轮基线文件哈希后安装整批数据。批次期间出现新提交就另外准备下一批，保持当前批次可重验。发布根据用户授权执行。

```sh
python3 scripts/archive_update.py --root "$stage" check --archive "$archive" \
  --plan "$work/inventory.json" --decisions "$work/decisions.json" \
  --answer-plan "$work/answer-plan.json" --answer-reviews "$work/answer-reviews.json" \
  --article-reference-plan "$work/article-reference-plan.json" \
  --article-reference-reviews "$work/article-reference-reviews.json"
```

验收后以同样的参数运行 `complete`。阶段校验命令读取原始计划及审阅文件，公开的完成记录保存来源和验收哈希。保留私有计划及复核文件供重验。

```sh
python3 scripts/archive_update_install.py install --stage-root "$stage" --baseline "$work/site-baseline.json"
```

2026-11-21 的回答增量采用固定备份提交 `61c5129d1`。它覆盖上次反链索引基线之后的 32 篇新增回答、既有回答显示字段和新增译文的历史回链检查，补录 117 条直接引用，涉及 20 篇回答和 87 篇既有译文。答复阶段已有单独验收记录。历史文章版本及无链接候选仍需按对应阶段核对，整站处理基线以完整验收为准。
