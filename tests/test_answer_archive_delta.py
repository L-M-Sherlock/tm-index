"""Exercise read-only Git planning and precise answer citation evidence."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import answer_archive_delta as delta


WHO = next(iter(delta.ACCOUNT_NAMES.items()))
ARTICLE = "https://zhuanlan.zhihu.com/p/"
ARCHIVE = "https://l-m-sherlock.github.io/ZhiHuArchive/"


class AnswerArchiveDeltaTests(unittest.TestCase):
    def setUp(self):
        resource = tempfile.TemporaryDirectory()
        self.addCleanup(resource.cleanup)
        self.root = Path(resource.name)
        self.git("init", "--quiet")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        (self.root / "answer").mkdir()

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args], text=True).strip()

    def answer(self, answer_id, body, votes=1, actor=WHO):
        record = {
            "id": answer_id, "question": {"id": "27", "title": "问题"},
            "author": {"id": actor[0], "name": actor[1]},
            "content": body, "created_time": 1794556433, "voteup_count": votes,
        }
        path = self.root / f"answer/{answer_id}.json"
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        return path

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "--quiet", "--allow-empty", "-m", "snapshot")
        return self.git("rev-parse", "HEAD")

    def edge(self, answer_id, article_id, via="zhihu"):
        return {"answer_id": answer_id, "article_id": article_id, "via": [via], "evidence": ["link"]}

    def test_direct_pair_evidence_distinguishes_native_card_and_anchor(self):
        html = (
            f'<blockquote><p>作者文字<sup data-draft-type="reference" data-url="{ARTICLE}101" '
            f'data-text="引文"><a href="{ARTICLE}101">[1]</a></sup>后文</p></blockquote>'
            f'<a data-draft-type="link-card" href="{ARCHIVE}101.html"><span>链接卡片</span></a>'
            '<a href="https://link.zhihu.com/?target=https%3A%2F%2Fzhuanlan.zhihu.com%2Fp%2F102">跳转</a>'
            '<a href="https://evil.example/p/103">假域名</a>'
            f'<a href="{ARTICLE}104/answer/5">错误路径</a>'
            f'<script><a href="{ARTICLE}105">脚本链接</script>'
        )
        first, second = delta.direct_pairs("1", html, {"101", "102", "103", "104", "105"})
        self.assertEqual(first["via"], ["archive", "zhihu"])
        self.assertEqual(first["evidence"], ["link_card", "native_reference"])
        native, link = first["occurrences"]
        self.assertEqual(native["label"], "引文")
        self.assertEqual(native["context"], "作者文字 后文")
        self.assertEqual(native["context_tag"], "blockquote")
        self.assertEqual(link["label"], "链接卡片")
        self.assertEqual(second["evidence"], ["link"])

    def test_direct_links_accept_known_article_route_variants_only(self):
        html = (
            '<a href="https://www.zhihu.com/api/v4/articles/101">API</a>'
            '<a href="https://zhuanlan.zhihu.com/appview/p/102">应用</a>'
            '<a href="https://api.zhihu.com/articles/103/">旧 API</a>'
            '<a href="https://api.zhihu.com/articles/104/attachment">非文章端点</a>'
            '<a href="https://www.zhihu.com:999/p/105">非标准端口</a>'
            '<a href="https://ghost@zhihu.com/p/106">凭证</a>'
        )
        candidates = delta.direct_pairs("1", html, {"101", "102", "103", "104", "105", "106"})
        self.assertEqual([pair["article_id"] for pair in candidates], ["101", "102", "103"])

    def test_plan_distinguishes_changes_skips_other_authors_and_reports_possible_removals(self):
        self.answer("1", f'<p>旧证据<a href="{ARTICLE}101">参考</a></p>')
        self.answer("2", f'<a href="{ARTICLE}101">参考</a>')
        deleted = self.answer("3", "待删除")
        base = self.commit()
        self.answer("1", f'<p>新证据<a href="{ARTICLE}102">参考</a></p>')
        self.answer("2", f'<a href="{ARTICLE}101">参考</a>', votes=20)
        deleted.unlink()
        added = self.answer("4", f'<a href="{ARTICLE}102">参考</a>')
        self.answer("5", f'<a href="{ARTICLE}103">无关帐号</a>', actor=("other", "Other Account"))
        tip = self.commit()
        edges = [
            self.edge("1", "101"), self.edge("1", "102", "inferred"),
            self.edge("1", "103", "inferred"), self.edge("2", "101"),
            self.edge("3", "103", "inferred"),
        ]
        ids = {"101", "102", "103"}
        plan = delta.make_plan(self.root, base, tip, ids, edges)
        self.assertEqual({key: row["change"] for key, row in plan["answers"].items()},
                         {"1": "body_changed", "2": "metadata_only", "3": "deleted", "4": "added"})
        self.assertEqual(plan["answers"]["1"]["removed_direct_pairs"], ["101"])
        self.assertEqual(plan["answers"]["1"]["recheck_inferred_pairs"], ["103"])
        self.assertEqual(plan["answers"]["2"]["retained_direct_pairs"], ["101"])
        self.assertEqual(plan["answers"]["2"]["after"]["answer"]["voteup_count"], 20)
        self.assertEqual(plan["answers"]["3"]["recheck_inferred_pairs"], ["103"])
        self.assertIsNone(plan["answers"]["3"]["after"])
        self.assertEqual(plan["answers"]["4"]["added_pairs"], ["102"])
        self.assertEqual(list(plan["excluded"]), ["5"])
        self.assertEqual(plan["population"]["answers_scanned"], 3)
        self.assertEqual(plan["population"]["skipped_other_authors"], 1)
        self.assertEqual([(row["answer_id"], row["article_id"], row["action"])
                          for row in plan["candidates"]], [("1", "102", "recheck"), ("4", "102", "add")])
        self.assertEqual(delta.verify_plan(plan, self.root, ids, list(reversed(edges))), plan)

        added.write_text("dirty working-tree replacement", encoding="utf-8")
        self.assertEqual(delta.answer_content(self.root, tip, "answer/4.json"),
                         f'<a href="{ARTICLE}102">参考</a>')
        self.assertEqual(delta.make_plan(self.root, base, tip, ids, edges), plan)
        modified = deepcopy(plan)
        modified["candidates"][0]["content_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "does not match"):
            delta.verify_plan(modified, self.root, ids, edges)

        partial = delta.make_plan(self.root, base, tip, ids, edges, changes="added")
        self.assertEqual(set(partial["answers"]), {"4"})
        self.assertEqual(partial["deferred"], ["answer/1.json", "answer/2.json", "answer/3.json"])
        self.assertEqual(partial["counts"]["deferred"], 3)

    def test_new_catalog_target_checks_historical_unchanged_answers(self):
        self.answer("1", f'<a href="{ARTICLE}101">参考</a><a href="{ARTICLE}102">新列入目录</a>')
        self.answer("2", f'<a href="{ARTICLE}101">参考</a>')
        base = self.commit()
        tip = self.commit()
        plan = delta.make_plan(self.root, base, tip, {"101", "102"}, [self.edge("1", "101")],
                               new_article_ids={"102"})
        self.assertEqual(list(plan["answers"]), ["1"])
        self.assertEqual(plan["answers"]["1"]["change"], "unchanged_backlinks")
        self.assertEqual(plan["answers"]["1"]["added_pairs"], ["102"])
        self.assertEqual(plan["answers"]["1"]["retained_direct_pairs"], ["101"])

    def test_only_image_source_refresh_keeps_citation_evidence_equivalent(self):
        self.answer("1", f'<blockquote><p>引用<a href="{ARTICLE}101">译文</a></p></blockquote>'
                         '<img src="https://img.example/old.jpg">')
        self.answer("2", f'<p>引用<a href="{ARTICLE}101">译文</a></p>')
        self.answer("3", f'<p>引用<sup data-draft-type="reference" data-url="{ARTICLE}101" '
                         'data-text="旧说明">[1]</sup></p>')
        base = self.commit()
        self.answer("1", f'<blockquote><p>引用<a href="{ARTICLE}101">译文</a></p></blockquote>'
                         '<img src="https://img.example/new.jpg" data-actualsrc="preview.png">')
        self.answer("2", f'<blockquote><p>引用<a href="{ARTICLE}101">译文</a></p></blockquote>')
        self.answer("3", f'<p>引用<sup data-draft-type="reference" data-url="{ARTICLE}101" '
                         'data-text="新说明">[1]</sup></p>')
        tip = self.commit()
        plan = delta.make_plan(self.root, base, tip, {"101"},
                               [self.edge(answer_id, "101") for answer_id in ("1", "2", "3")])
        self.assertEqual(plan["counts"]["citation_equivalent_body_changes"], 1)
        self.assertTrue(plan["answers"]["1"]["citation_equivalent"])
        self.assertEqual([row["answer_id"] for row in plan["candidates"]], ["2", "3"])

    def test_rejects_known_name_with_wrong_account_and_stale_baseline(self):
        base = self.commit()
        self.answer("1", "错误账号", actor=("unverified", WHO[1]))
        tip = self.commit()
        with self.assertRaisesRegex(ValueError, "known account name"):
            delta.make_plan(self.root, base, tip, {"101"})
        with self.assertRaisesRegex(ValueError, "not an ancestor"):
            delta.make_plan(self.root, tip, base, {"101"})


if __name__ == "__main__":
    unittest.main()
