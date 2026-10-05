import importlib.util
import io
import json
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

import requests

import lib.octo as octo_module
from lib.config import load_config
from lib.llm_backend import OpenAIBackend
from lib.octo import OctoClient
from lib.parser_generic import extract_generic_text
from lib.parser_localization import extract_localization_text
from lib.parser_master import extract_master_text
from lib.parser_resource import build_resource_line, extract_resource_text
from lib.proto import octodb_pb2 as octop
from lib.text_utils import looks_like_japanese_source


PROJECT_ROOT = Path(__file__).parent.parent


from lib.text_utils import uid_label


def _uid_translations(*pairs: tuple[str, str]) -> str:
    """Structured response keyed by the request label derived from each uid."""
    return _json_translations(*((uid_label(uid), cn) for uid, cn in pairs))


def _load_stage(name: str):
    path = PROJECT_ROOT / "stages" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_tool(name: str):
    path = PROJECT_ROOT / "tools" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ResourceRegressionTests(unittest.TestCase):
    def test_build_replaces_every_choicegroup_text(self):
        line = "[choicegroup text=選択肢1 text=選択肢2 text=選択肢3]"

        built = build_resource_line(
            line,
            {"text[0]": "选项1", "text[1]": "选项2", "text[2]": "选项3"},
        )

        self.assertEqual(
            built,
            "[choicegroup text=选项1 text=选项2 text=选项3]",
        )

    def test_build_choicegroup_nested_choice_uses_plain_chinese(self):
        line = (
            "[choicegroup choices=[choice text=よく似合っています] "
            r"clip=\{...\}]"
        )

        built = build_resource_line(line, {"text[0]": "很适合你"})

        self.assertEqual(
            built,
            "[choicegroup choices=[choice text=很适合你] "
            r"clip=\{...\}]",
        )

    def test_build_choicegroup_multiline_keeps_literal_newline(self):
        line = (
            r"[choicegroup choices=[choice text=ダンスが\n上手い] "
            "choices=[choice text=可愛い] clip=none]"
        )

        built = build_resource_line(
            line,
            {"text[0]": r"跳舞\n很棒", "text[1]": "很可爱"},
        )

        self.assertEqual(
            built,
            r"[choicegroup choices=[choice text=跳舞\n很棒] "
            "choices=[choice text=很可爱] clip=none]",
        )

    def test_build_choicegroup_preserves_nested_choice_attributes(self):
        line = (
            "[choicegroup choices=[choice text=頑張れ hideMessage=true] "
            "choices=[choice text=いつも通りに] clip=none]"
        )

        built = build_resource_line(
            line,
            {"text[0]": "加油", "text[1]": "像平常一样"},
        )

        self.assertEqual(
            built,
            "[choicegroup choices=[choice text=加油 hideMessage=true] "
            "choices=[choice text=像平常一样] clip=none]",
        )

    def test_build_wraps_multiline_as_multi_segment(self):
        line = r"[message text=前列にいるのは、\n麻央さんのお友達ですか？ name={user}]"
        built = build_resource_line(
            line,
            {"text": r"前排的那些人，\n是麻央的朋友吗？"},
        )
        self.assertEqual(
            built,
            r"[message text=<r\=前列にいるのは、>前排的那些人，</r>\r\n"
            r"<r\=麻央さんのお友達ですか？>是麻央的朋友吗？</r> name={user}]",
        )

    def test_extract_choice_translation_preserves_wrapped_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "adv_choice.txt"
            path.write_text(
                r"[choicegroup choices=[choice text=<r\=独占したかったから>因为我想独占你</r> clip=none]]",
                encoding="utf-8",
            )

            item = extract_resource_text(path)[0]

        self.assertEqual(
            item["jp"], r"<r\=独占したかったから>因为我想独占你</r>"
        )

    def test_choice_translation_ignores_stray_choice_closer(self):
        extract = _load_stage("02_extract")

        self.assertEqual(
            extract._get_existing_resource_translation(
                "text[0]",
                r"<r\=独占したかったから]>因为我想独占你</r>",
                "独占したかったから",
            ),
            ("独占したかったから", "因为我想独占你"),
        )

    def test_nightly_resource_translation_supersedes_untranslated_primary(self):
        extract = _load_stage("02_extract")
        current = "独占したかったから"
        _, primary_cn = extract._get_existing_resource_translation(
            "text[0]", current, current
        )
        _, nightly_cn = extract._get_existing_resource_translation(
            "text[0]",
            r"<r\=独占したかったから]>因为我想独占你</r>",
            current,
        )

        self.assertFalse(primary_cn)
        self.assertEqual(nightly_cn, "因为我想独占你")

    def test_build_normalizes_llm_resource_markup(self):
        line = r"[message text=一行目\n『<r\=よみ>語</r>』 name=話者]"
        built = build_resource_line(
            line,
            {"text": "第一行\n《<r=よみ>词</r>》\n---"},
        )

        self.assertEqual(
            built,
            r"[message text=<r\=一行目>第一行</r>\r\n"
            r"<r\=『<r\=よみ>語</r>』>《<r\=よみ>词</r>》</r> name=話者]",
        )

    def test_resource_translation_split_detects_source_change(self):
        extract = _load_stage("02_extract")
        self.assertEqual(
            extract._split_resource_translation("<r\\=旧原文>旧译文</r\\>"),
            ("旧原文", "旧译文"),
        )
        self.assertEqual(
            extract._split_resource_translation("<r\\=お客さん、満席ですね。>观众们，满座呢。</r>"),
            ("お客さん、満席ですね。", "观众们，满座呢。"),
        )
        self.assertEqual(
            extract._split_resource_translation(
                r"<r\=前列にいるのは、>前排的那些人，</r>\r\n"
                r"<r\=麻央さんのお友達ですか？>是麻央的朋友吗？</r>"
            ),
            (r"前列にいるのは、\n麻央さんのお友達ですか？", r"前排的那些人，\n是麻央的朋友吗？"),
        )
        self.assertEqual(extract._split_resource_translation("plain"), ("plain", ""))

    def test_resource_translation_split_handles_embedded_em_tag(self):
        extract = _load_stage("02_extract")
        old_jp, cn = extract._split_resource_translation(
            "<r\\=——なら、<em\\=・・・>筋トレをがんばらないとね。>"
            "——那就要努力“锻炼肌肉”了呢。</r>"
        )

        self.assertEqual(old_jp, "——なら、<em\\=・・・>筋トレをがんばらないとね。")
        self.assertEqual(cn, "——那就要努力“锻炼肌肉”了呢。")
        self.assertTrue(
            extract._resource_sources_equal(
                old_jp,
                "――なら、<em\\=・・・>筋トレ</em>をがんばらないとね。",
            )
        )

    def test_resource_translation_split_handles_nested_ruby_tags(self):
        extract = _load_stage("02_extract")
        value = (
            r"<r\=（咲季さんは、プロジェクト『<r\=スターダスト>星屑</r>』を>"
            r"({user}) （咲季经历了“<r\=スターダスト>星屑</r>”企划……</r>\r\n"
            r"<r\=経て……大きな成果を上げた）>取得了巨大的成果）</r>"
        )

        self.assertEqual(
            extract._split_resource_translation(value),
            (
                r"（咲季さんは、プロジェクト『<r\=スターダスト>星屑</r>』を\n"
                r"経て……大きな成果を上げた）",
                r"({user}) （咲季经历了“<r\=スターダスト>星屑</r>”企划……\n"
                r"取得了巨大的成果）",
            ),
        )

    def test_resource_source_comparison_ignores_ruby_and_dash_variants(self):
        extract = _load_stage("02_extract")
        self.assertTrue(
            extract._resource_sources_equal(
                "この楽曲は、\\n可愛くて、カッコイイ——",
                "この楽曲は、\\n可愛くて、カッコイイ――",
            )
        )
        self.assertTrue(
            extract._resource_sources_equal(
                "はい。龍月真希さんの\\n所属している劇団です。",
                "はい。<r\\=りゅうげつまき>龍月真希</r>さんの\\n所属している劇団です。",
            )
        )

    def test_plain_chinese_choice_is_an_existing_translation(self):
        extract = _load_stage("02_extract")
        self.assertEqual(
            extract._get_existing_resource_translation(
                "text[0]", r"我会尊重\n偶像的意见", r"アイドルの意見を\n尊重します"
            ),
            (r"アイドルの意見を\n尊重します", r"我会尊重\n偶像的意见"),
        )
        self.assertEqual(
            extract._get_existing_resource_translation(
                "text[0]", "独占したかったから", "独占したかったから"
            ),
            ("独占したかったから", ""),
        )
        self.assertEqual(
            extract._get_existing_resource_translation("text[0]", "金星", "金星"),
            ("金星", "金星"),
        )
        self.assertEqual(
            extract._get_existing_resource_translation(
                "text[1]",
                r"源于谚语“立つ鳥跡を濁さず”\n的关系",
                r"立つ鳥跡を濁さず\nから",
            ),
            (r"立つ鳥跡を濁さず\nから", r"源于谚语“立つ鳥跡を濁さず”\n的关系"),
        )

    def test_plain_chinese_name_is_an_existing_translation(self):
        extract = _load_stage("02_extract")
        # The mod stores display names unwrapped; the upstream value wins over
        # the character_names fallback.
        self.assertEqual(
            extract._get_existing_resource_translation("name", "琴音", "ことね"),
            ("ことね", "琴音"),
        )
        self.assertEqual(
            extract._get_existing_resource_translation("name", "ことね", "ことね"),
            ("ことね", ""),
        )

    def test_changed_translation_is_skipped_by_default(self):
        translate = _load_stage("03_translate")
        changed = {"status": "changed"}
        new = {"status": "new"}

        self.assertFalse(translate._should_translate(changed, True))
        self.assertTrue(translate._should_translate(changed, False))
        self.assertTrue(translate._should_translate(new, True))


class TranslateSkipMarkerTests(unittest.TestCase):
    def _prepare(self, items, with_stale_marker=False):
        translate = _load_stage("03_translate")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        cache_dir = Path(directory.name) / "cache"
        cache_dir.mkdir()
        (cache_dir / "extract.json").write_text(
            json.dumps(items, ensure_ascii=False), encoding="utf-8"
        )
        marker = cache_dir / "nothing_to_translate"
        if with_stale_marker:
            marker.write_text("", encoding="utf-8")
        translate.load_config = lambda: {
            "llm": {"skip_changed": True, "batch_size": 20, "max_concurrent": 5}
        }
        translate.resolve_paths = lambda cfg: {"server_cache": cache_dir / "server"}
        return translate, marker

    def test_nothing_to_translate_writes_skip_marker(self):
        translate, marker = self._prepare(
            [{"uid": "adv_x:1", "status": "existing", "cn": "旧译文"}]
        )

        with self.assertRaises(SystemExit) as cm:
            translate.main()

        self.assertEqual(cm.exception.code, 0)
        self.assertTrue(marker.exists())

    def test_pending_translation_removes_stale_skip_marker(self):
        translate, marker = self._prepare(
            [{"uid": "adv_x:1", "status": "new", "category": "master", "jp": "こんにちは"}],
            with_stale_marker=True,
        )
        translate.translate_group = lambda group: {group[0]["uid"]: "你好"}

        translate.main()

        self.assertFalse(marker.exists())


class LocalizationRegressionTests(unittest.TestCase):
    def test_japanese_localization_is_translated_and_chinese_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "localization.json"
            path.write_text(
                json.dumps({"jp": "みんなありがとう", "cn": "大家好"}, ensure_ascii=False),
                encoding="utf-8",
            )

            items = extract_localization_text(path)

        self.assertEqual(items[0]["status"], "new")
        self.assertEqual(items[0]["existing_cn"], "みんなありがとう")
        self.assertEqual(items[1]["status"], "existing")
        self.assertEqual(items[1]["existing_cn"], "大家好")
        self.assertTrue(looks_like_japanese_source("みんな"))
        self.assertFalse(looks_like_japanese_source("大家好"))
        self.assertFalse(
            looks_like_japanese_source("通过链接关注SNS！\n#学園アイドルマスター #学マス")
        )


class MasterRegressionTests(unittest.TestCase):
    def test_master_pairs_duplicate_ids_by_occurrence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            yaml_dir = root / "yaml"
            mod_dir = root / "mod"
            yaml_dir.mkdir()
            mod_dir.mkdir()
            (yaml_dir / "sample.yaml").write_text(
                "- id: repeated\n  name: 原文一\n- id: repeated\n  name: 原文二\n",
                encoding="utf-8",
            )
            (mod_dir / "sample.json").write_text(
                json.dumps(
                    {"data": [{"id": "repeated", "name": "译文一"}, {"id": "repeated", "name": "译文二"}]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            items = extract_master_text(yaml_dir, mod_dir)

        self.assertEqual([item["existing_cn"] for item in items], ["译文一", "译文二"])
        self.assertEqual([item["uid"] for item in items],
                         ["master:sample:repeated#0:name", "master:sample:repeated#1:name"])

    def test_master_matches_records_by_key_not_position(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            yaml_dir = root / "yaml"
            mod_dir = root / "mod"
            yaml_dir.mkdir()
            mod_dir.mkdir()
            # The overlay holds a row the source no longer has, exactly what
            # happens after a master update. Positional matching would pair
            # every later row with its neighbour.
            (yaml_dir / "sample.yaml").write_text(
                "- id: a\n  name: 原文A\n- id: c\n  name: 原文C\n",
                encoding="utf-8",
            )
            (mod_dir / "sample.json").write_text(
                json.dumps(
                    {"rules": {"primaryKeys": ["id"]},
                     "data": [{"id": "a", "name": "译文A"}, {"id": "b", "name": "译文B"},
                              {"id": "c", "name": "译文C"}]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            items = extract_master_text(yaml_dir, mod_dir)

        self.assertEqual([item["existing_cn"] for item in items], ["译文A", "译文C"])
        self.assertEqual([item["uid"] for item in items], ["master:sample:a:name", "master:sample:c:name"])

    def test_master_uses_composite_primary_keys_without_id_field(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            yaml_dir = root / "yaml"
            mod_dir = root / "mod"
            yaml_dir.mkdir()
            mod_dir.mkdir()
            (yaml_dir / "sample.yaml").write_text(
                "- supportCardId: card-1\n  number: 1\n  text: 原文一\n"
                "- supportCardId: card-1\n  number: 2\n  text: 原文二\n",
                encoding="utf-8",
            )
            (mod_dir / "sample.json").write_text(
                json.dumps(
                    {"rules": {"primaryKeys": ["supportCardId", "number"]},
                     "data": [{"supportCardId": "card-0", "number": 9, "text": "旧卡"},
                              {"supportCardId": "card-1", "number": 1, "text": "译文一"},
                              {"supportCardId": "card-1", "number": 2, "text": "译文二"}]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            items = extract_master_text(yaml_dir, mod_dir)

        self.assertEqual([item["existing_cn"] for item in items], ["译文一", "译文二"])
        self.assertEqual([item["uid"] for item in items],
                         ["master:sample:card-1|1:text", "master:sample:card-1|2:text"])

    def test_master_ignores_relational_primary_key_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            yaml_dir = root / "yaml"
            mod_dir = root / "mod"
            yaml_dir.mkdir()
            mod_dir.mkdir()
            (yaml_dir / "sample.yaml").write_text(
                "- id: a\n  name: 原文A\n  descriptions:\n    - text: 甲\n"
                "- id: b\n  name: 原文B\n  descriptions:\n    - text: 乙\n",
                encoding="utf-8",
            )
            (mod_dir / "sample.json").write_text(
                json.dumps(
                    {"rules": {"primaryKeys": ["id", "descriptions.text"]},
                     "data": [{"id": "extra", "name": "多余"},
                              {"id": "a", "name": "译文A"}, {"id": "b", "name": "译文B"}]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            items = extract_master_text(yaml_dir, mod_dir)

        self.assertEqual([item["existing_cn"] for item in items], ["译文A", "译文B"])

    def test_build_master_matches_idless_and_duplicate_records_by_key(self):
        build = _load_stage("04_build")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master_dir = root / "local-files" / "masterTrans"
            master_dir.mkdir(parents=True)
            target = master_dir / "sample.json"
            target.write_text(
                json.dumps(
                    {"data": [{"id": "repeated", "name": ""}, {"id": "repeated", "name": ""}, {"name": ""}]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            original_out = build.OUT
            build.OUT = root
            try:
                count = build._apply_master(
                    "sample.json",
                    [
                        {"uid": "master:sample:repeated#0:name", "record_id": "repeated", "key": ["repeated"], "key_occurrence": 0, "field": "name", "cn": "译文一"},
                        {"uid": "master:sample:repeated#1:name", "record_id": "repeated", "key": ["repeated"], "key_occurrence": 1, "field": "name", "cn": "译文二"},
                        {"uid": "master:sample:_idx2:name", "record_id": "", "key": [None], "key_occurrence": 0, "field": "name", "cn": "无 ID 译文"},
                    ],
                )
            finally:
                build.OUT = original_out
            data = json.loads(target.read_text(encoding="utf-8"))

        self.assertEqual(count, 3)
        self.assertEqual([record["name"] for record in data["data"]], ["译文一", "译文二", "无 ID 译文"])

    def test_build_master_places_translation_by_key_not_position(self):
        build = _load_stage("04_build")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master_dir = root / "local-files" / "masterTrans"
            master_dir.mkdir(parents=True)
            target = master_dir / "sample.json"
            # The overlay keeps a row the source no longer has; the translation
            # for `c` must land on `c`, not on the row sitting at its index.
            target.write_text(
                json.dumps(
                    {"rules": {"primaryKeys": ["id"]},
                     "data": [{"id": "a", "name": "译文A"}, {"id": "b", "name": "译文B"},
                              {"id": "c", "name": ""}]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            original_out = build.OUT
            build.OUT = root
            try:
                count = build._apply_master(
                    "sample.json",
                    [
                        {"uid": "master:sample:a:name", "record_id": "a", "key": ["a"], "key_occurrence": 0, "field": "name", "cn": "译文A"},
                        {"uid": "master:sample:c:name", "record_id": "c", "key": ["c"], "key_occurrence": 0, "field": "name", "cn": "译文C"},
                    ],
                )
            finally:
                build.OUT = original_out
            data = json.loads(target.read_text(encoding="utf-8"))

        self.assertEqual(count, 2)
        self.assertEqual([record["name"] for record in data["data"]], ["译文A", "译文B", "译文C"])

    def test_build_master_creates_missing_id_based_table(self):
        build = _load_stage("04_build")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original_out = build.OUT
            build.OUT = root
            try:
                count = build._apply_master(
                    "new-table.json",
                    [{"record_id": "record-1", "field": "name", "cn": "新译文"}],
                )
            finally:
                build.OUT = original_out
            data = json.loads(
                (root / "local-files" / "masterTrans" / "new-table.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(count, 1)
        self.assertEqual(data["rules"]["primaryKeys"], ["id"])
        self.assertEqual(data["data"], [{"id": "record-1", "name": "新译文"}])

    def test_master_snapshot_detects_changed_source_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            yaml_dir = root / "yaml"
            mod_dir = root / "mod"
            yaml_dir.mkdir()
            mod_dir.mkdir()
            (yaml_dir / "sample.yaml").write_text("- id: 1\n  name: 新原文\n", encoding="utf-8")
            (mod_dir / "sample.json").write_text(
                json.dumps({"data": [{"id": 1, "name": "旧译文"}]}, ensure_ascii=False),
                encoding="utf-8",
            )
            snapshot = root / "snapshot.json"
            snapshot.write_text(
                json.dumps({"master:sample:1:name": "旧原文"}, ensure_ascii=False),
                encoding="utf-8",
            )

            item = extract_master_text(yaml_dir, mod_dir, snapshot)[0]

        self.assertEqual(item["status"], "changed")

    def test_master_uses_nightly_only_when_primary_lacks_field(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            yaml_dir = root / "yaml"
            primary_dir = root / "primary"
            nightly_dir = root / "nightly"
            yaml_dir.mkdir()
            primary_dir.mkdir()
            nightly_dir.mkdir()
            (yaml_dir / "sample.yaml").write_text("- id: 1\n  name: 原文\n", encoding="utf-8")
            (primary_dir / "sample.json").write_text(
                json.dumps({"data": [{"id": 1, "name": "主包译文"}]}, ensure_ascii=False),
                encoding="utf-8",
            )
            (nightly_dir / "sample.json").write_text(
                json.dumps({"data": [{"id": 1, "name": "nightly译文"}]}, ensure_ascii=False),
                encoding="utf-8",
            )

            items = extract_master_text(yaml_dir, primary_dir, fallback_mod_master_dir=nightly_dir)

        self.assertEqual(items[0]["existing_cn"], "主包译文")

    def test_master_uses_nightly_when_primary_record_lacks_field(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            yaml_dir = root / "yaml"
            primary_dir = root / "primary"
            nightly_dir = root / "nightly"
            yaml_dir.mkdir()
            primary_dir.mkdir()
            nightly_dir.mkdir()
            (yaml_dir / "sample.yaml").write_text(
                "- id: 1\n  name: 原文\n", encoding="utf-8"
            )
            (primary_dir / "sample.json").write_text(
                json.dumps({"data": [{"id": 1}]}, ensure_ascii=False),
                encoding="utf-8",
            )
            (nightly_dir / "sample.json").write_text(
                json.dumps({"data": [{"id": 1, "name": "nightly译文"}]}, ensure_ascii=False),
                encoding="utf-8",
            )

            items = extract_master_text(yaml_dir, primary_dir, fallback_mod_master_dir=nightly_dir)

        self.assertEqual(items[0]["existing_cn"], "nightly译文")
        self.assertEqual(items[0]["status"], "existing")

    def test_master_uses_nightly_for_idless_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            yaml_dir = root / "yaml"
            primary_dir = root / "primary"
            nightly_dir = root / "nightly"
            yaml_dir.mkdir()
            primary_dir.mkdir()
            nightly_dir.mkdir()
            (yaml_dir / "sample.yaml").write_text("- title: 原文\n", encoding="utf-8")
            (nightly_dir / "sample.json").write_text(
                json.dumps({"data": [{"title": "nightly译文"}]}, ensure_ascii=False),
                encoding="utf-8",
            )

            item = extract_master_text(
                yaml_dir, primary_dir, fallback_mod_master_dir=nightly_dir
            )[0]

        self.assertEqual(item["existing_cn"], "nightly译文")
        self.assertEqual(item["status"], "existing")


class DownloadRegressionTests(unittest.TestCase):
    def test_failed_asset_is_not_cached_and_is_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = OctoClient({"data_path": str(root / "octo")})
            database = octop.Database(urlFormat="https://assets/{o}")
            resource = database.resourceList.add()
            resource.name = "adv_test.txt"
            resource.objectName = "test"

            original_request = octo_module._http_request
            try:
                octo_module._http_request = lambda *args, **kwargs: SimpleNamespace(
                    status=500, data=b"error", release_conn=lambda: None
                )
                client.download_adv_txts(database, root / "res")
                log = json.loads((root / "download_log.json").read_text(encoding="utf-8"))
                self.assertNotIn("adv_test.txt", log)
                self.assertFalse((root / "res" / "adv_test.txt").exists())

                octo_module._http_request = lambda *args, **kwargs: SimpleNamespace(
                    status=200, data=b"ok", release_conn=lambda: None
                )
                client.download_adv_txts(database, root / "res")
            finally:
                octo_module._http_request = original_request

            log = json.loads((root / "download_log.json").read_text(encoding="utf-8"))
            self.assertEqual(log["adv_test.txt"], "ok")
            self.assertEqual((root / "res" / "adv_test.txt").read_bytes(), b"ok")

    def test_zip_cache_replacement_removes_old_files(self):
        download = _load_stage("01_download")
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "cache"
            destination.mkdir()
            (destination / "old.txt").write_text("old", encoding="utf-8")
            content = io.BytesIO()
            with zipfile.ZipFile(content, "w") as archive:
                archive.writestr("source/new.txt", "new")

            download._extract_zip_atomically(content.getvalue(), destination)

            self.assertEqual((destination / "new.txt").read_text(encoding="utf-8"), "new")
            self.assertFalse((destination / "old.txt").exists())

    def test_nightly_items_only_fill_missing_primary_entries(self):
        extract = _load_stage("02_extract")
        primary = [{"uid": "resource:primary", "existing_cn": "主包译文"}]
        nightly = [
            {"uid": "resource:primary", "existing_cn": "nightly译文"},
            {"uid": "resource:nightly", "existing_cn": "nightly补充"},
        ]

        items = extract._add_fallback_items(primary, nightly)

        self.assertEqual(items, [primary[0], nightly[1]])


class PackageRegressionTests(unittest.TestCase):
    def test_build_version_uses_ci_override(self):
        build = _load_stage("04_build")

        with patch.dict("os.environ", {"BUILD_VERSION": "nightly-2026-07-23"}):
            self.assertEqual(build._build_version(), "nightly-2026-07-23")

    def test_package_includes_local_files_directory_entry(self):
        package = _load_stage("05_package")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "GakumasTranslationData"
            (root / "local-files" / "resource").mkdir(parents=True)
            (root / "version.txt").write_text("test", encoding="utf-8")
            (root / "local-files" / "resource" / "sample.txt").write_text(
                "sample", encoding="utf-8"
            )
            archive = Path(directory) / "translation.zip"

            package.create_package(root, archive)

            with zipfile.ZipFile(archive) as zip_file:
                self.assertTrue(zip_file.getinfo("local-files/").is_dir())
                self.assertEqual(zip_file.read("version.txt"), b"test")


class ExportPendingRegressionTests(unittest.TestCase):
    def test_status_argument_selects_export_status(self):
        export = _load_tool("export_pending")
        original_argv = sys.argv
        try:
            sys.argv = ["export_pending.py"]
            self.assertIsNone(export._parse_args().status)

            sys.argv = ["export_pending.py", "--status", "new"]
            self.assertEqual(export._parse_args().status, "new")

            sys.argv = ["export_pending.py", "--status", "changed"]
            self.assertEqual(export._parse_args().status, "changed")
        finally:
            sys.argv = original_argv

    def test_export_writes_only_selected_status_items(self):
        export = _load_tool("export_pending")
        extract = [
            {"uid": "new", "status": "new"},
            {"uid": "changed", "status": "changed"},
            {"uid": "existing", "status": "existing"},
        ]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "custom" / "changed.json"

            count = export.export_items(extract, "changed", output)

            self.assertEqual(count, 1)
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8")), [extract[1]]
            )


class ResourceFieldRegressionTests(unittest.TestCase):
    def test_title_line_is_extracted_from_title_field(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "adv_x.txt"
            path.write_text(
                "[title title=プロローグ clip=none]\n"
                "[message text=おはよう name=咲季]\n"
                "[narration text=ナレーション]\n",
                encoding="utf-8",
            )

            items = extract_resource_text(path)

        self.assertEqual(
            items,
            [
                {"line": 1, "command": "title", "field": "text", "jp": "プロローグ"},
                {"line": 2, "command": "message", "field": "text", "jp": "おはよう"},
                {"line": 2, "command": "message", "field": "name", "jp": "咲季"},
                {"line": 3, "command": "narration", "field": "text", "jp": "ナレーション"},
            ],
        )

    def test_title_line_is_built_into_title_field(self):
        built = build_resource_line(
            "[title title=プロローグ clip=none]", {"text": "序章"}
        )

        self.assertEqual(built, r"[title title=<r\=プロローグ>序章</r> clip=none]")

    def test_message_line_is_built_into_text_field(self):
        built = build_resource_line(
            "[message text=おはよう name=咲季]", {"text": "早上好"}
        )

        self.assertEqual(built, r"[message text=<r\=おはよう>早上好</r> name=咲季]")

    def test_line_without_matching_source_field_is_untouched(self):
        line = "[title clip=none]"

        self.assertEqual(build_resource_line(line, {"text": "序章"}), line)


class GenericRegressionTests(unittest.TestCase):
    def _write_generic(self, root, payload):
        fp = root / "local-files" / "genericTrans" / "sample.json"
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return fp

    def test_japanese_value_is_new_and_chinese_value_is_existing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_generic(root, {"こんにちは": "まだ日本語", "こんばんは": "晚上好", "おやすみ": ""})

            items = extract_generic_text(root / "local-files" / "genericTrans")

        by_field = {item["field"]: item for item in items}
        self.assertEqual(by_field["こんにちは"]["status"], "new")
        self.assertEqual(by_field["こんにちは"]["jp"], "こんにちは")
        self.assertEqual(by_field["こんばんは"]["status"], "existing")
        self.assertEqual(by_field["こんばんは"]["existing_cn"], "晚上好")
        self.assertEqual(by_field["おやすみ"]["status"], "new")
        self.assertTrue(
            all(
                item["file"] == "local-files/genericTrans/sample.json"
                for item in items
            )
        )

    def test_build_generic_writes_path_relative_to_output_root(self):
        build = _load_stage("04_build")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "local-files" / "genericTrans" / "sample.json"
            target.parent.mkdir(parents=True)
            target.write_text(
                json.dumps({"こんにちは": "まだ日本語"}, ensure_ascii=False),
                encoding="utf-8",
            )
            original_out = build.OUT
            build.OUT = root
            try:
                count = build._apply_generic([
                    {
                        "uid": "generic:local-files/genericTrans/sample.json:こんにちは",
                        "category": "generic",
                        "file": "local-files/genericTrans/sample.json",
                        "field": "こんにちは",
                        "cn": "你好",
                    }
                ])
            finally:
                build.OUT = original_out
            data = json.loads(target.read_text(encoding="utf-8"))

        self.assertEqual(count, 1)
        self.assertEqual(data["こんにちは"], "你好")
        self.assertFalse((root / "local-files" / "local-files").exists())


class LocalizationBuildRegressionTests(unittest.TestCase):
    def test_build_localization_writes_flat_and_nested_keys(self):
        build = _load_stage("04_build")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "local-files" / "localization.json"
            target.parent.mkdir(parents=True)
            target.write_text(
                json.dumps({"a.b": "旧", "nested": {"c": "旧2"}}, ensure_ascii=False),
                encoding="utf-8",
            )
            original_out = build.OUT
            build.OUT = root
            try:
                count = build._apply_localization([
                    {"uid": "localization:a.b", "category": "localization", "file": "localization.json", "field": "a.b", "cn": "新"},
                    {"uid": "localization:nested.c", "category": "localization", "file": "localization.json", "field": "nested.c", "cn": "新2"},
                    {"uid": "localization:missing.key", "category": "localization", "file": "localization.json", "field": "missing.key", "cn": "无关"},
                ])
            finally:
                build.OUT = original_out
            data = json.loads(target.read_text(encoding="utf-8"))

        self.assertEqual(count, 2)
        self.assertEqual(data["a.b"], "新")
        self.assertEqual(data["nested"]["c"], "新2")


class LyricBuildRegressionTests(unittest.TestCase):
    def _apply(self, root, file_name, payload, items):
        build = _load_stage("04_build")
        target = root / "local-files" / "genericTrans" / file_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        original_out = build.OUT
        build.OUT = root
        try:
            build._apply_generic(items)
        finally:
            build.OUT = original_out
        return json.loads(target.read_text(encoding="utf-8"))

    def test_bilingual_lyrics_file_wraps_translation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = "青空に\u3000スマホが届いた\r\n"
            data = self._apply(
                root,
                "srt_live_all-001.json",
                {"青空に\u3000スマホが届いた\r\n": "青空に\u3000スマホが届いた\n蓝天下 手机送来消息\r\n"},
                [
                    {
                        "uid": f"generic:local-files/genericTrans/srt_live_all-001.json:{key}",
                        "category": "generic",
                        "file": "local-files/genericTrans/srt_live_all-001.json",
                        "field": key,
                        "cn": "蓝天下 手机送来消息",
                    }
                ],
            )

        self.assertEqual(
            data[key], "青空に\u3000スマホが届いた\n蓝天下 手机送来消息\r\n"
        )

    def test_translation_only_file_keeps_value_without_source_line(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = "ラウンド1\n"
            data = self._apply(
                root,
                "index.json",
                {"ラウンド1\n": "第1轮\n"},
                [
                    {
                        "uid": f"generic:local-files/genericTrans/index.json:{key}",
                        "category": "generic",
                        "file": "local-files/genericTrans/index.json",
                        "field": key,
                        "cn": "第2轮",
                    }
                ],
            )

        self.assertEqual(data[key], "第2轮\n")

    def test_existing_value_is_written_verbatim(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = "青空に\u3000スマホが届いた\r\n"
            data = self._apply(
                root,
                "srt_live_all-001.json",
                {key: "青空に\u3000スマホが届いた\n蓝天下 手机送来消息\r\n"},
                [
                    {
                        "uid": f"generic:local-files/genericTrans/srt_live_all-001.json:{key}",
                        "category": "generic",
                        "file": "local-files/genericTrans/srt_live_all-001.json",
                        "field": key,
                        "existing_cn": "蓝天之下\r\n",
                    }
                ],
            )

        self.assertEqual(data[key], "蓝天之下\r\n")


class TranslateBatchingTests(unittest.TestCase):
    def test_group_key_is_file_scoped(self):
        translate = _load_stage("03_translate")

        self.assertEqual(
            translate._group_key({"category": "resource", "file": "a.txt"}),
            "resource:a.txt",
        )
        self.assertEqual(
            translate._group_key({"category": "master", "file": "m.json"}),
            "master:m.json",
        )
        self.assertEqual(
            translate._group_key(
                {"category": "generic", "file": "local-files/genericTrans/x.json"}
            ),
            "generic:local-files/genericTrans/x.json",
        )
        self.assertEqual(
            translate._group_key({"category": "localization", "file": "localization.json"}),
            "localization:localization.json",
        )

    def test_batches_never_mix_files_and_sort_resources_by_line(self):
        translate = _load_stage("03_translate")
        items = [
            {"uid": "b:2:text", "category": "resource", "file": "b.txt", "line": 2, "field": "text"},
            {"uid": "a:2:text", "category": "resource", "file": "a.txt", "line": 2, "field": "text"},
            {"uid": "a:1:text", "category": "resource", "file": "a.txt", "line": 1, "field": "text"},
            {"uid": "m:1:name", "category": "master", "file": "m.json", "field": "name"},
            {"uid": "g:1:key", "category": "generic", "file": "local-files/genericTrans/x.json", "field": "key"},
        ]

        batches = translate.build_batches(items, 2)

        for batch in batches:
            self.assertEqual(len({item["file"] for item in batch}), 1)
        a_batches = [batch for batch in batches if batch[0]["file"] == "a.txt"]
        self.assertEqual(
            [item["uid"] for batch in a_batches for item in batch],
            ["a:1:text", "a:2:text"],
        )

    def test_batches_split_inside_a_group_only(self):
        translate = _load_stage("03_translate")
        items = [
            {"uid": f"g:1:{index}", "category": "generic", "file": "g.json", "field": str(index)}
            for index in range(5)
        ]

        batches = translate.build_batches(items, 2)

        self.assertEqual([len(batch) for batch in batches], [2, 2, 1])
        self.assertEqual(
            [item["uid"] for batch in batches for item in batch],
            [f"g:1:{index}" for index in range(5)],
        )

    def test_non_positive_batch_size_is_rejected(self):
        translate = _load_stage("03_translate")

        with self.assertRaises(ValueError):
            translate.build_batches([], 0)
        with self.assertRaises(ValueError):
            translate.build_batches([], -3)

    def test_batch_metadata_stays_off_item_dicts(self):
        translate = _load_stage("03_translate")
        item = {
            "uid": "g:1:a", "category": "generic", "file": "g.json",
            "field": "a", "jp": "こんにちは", "existing_cn": "你好",
        }

        batch = translate.build_batches([item], 5)[0]

        self.assertEqual(batch.refs, [("g:1:a", "你好")])
        self.assertEqual(
            sorted(item),
            ["category", "existing_cn", "field", "file", "jp", "uid"],
        )


class ExtractLabelTests(unittest.TestCase):
    def test_items_are_tagged_with_the_label_the_translate_stage_uses(self):
        extract = _load_stage("02_extract")
        translate = _load_stage("03_translate")
        items = [
            {"uid": "adv_x:1:text"},
            {"uid": "generic:f:Lyrics: A\nComposer: B\n"},
        ]

        extract._tag_labels(items)

        labels = [item["hash"] for item in items]
        self.assertEqual(len(set(labels)), 2)
        for item in items:
            self.assertEqual(len(item["hash"]), 16)
            int(item["hash"], 16)
            self.assertEqual(translate._label(item), item["hash"])


class TranslatePromptTests(unittest.TestCase):
    def _translate(self):
        translate = _load_stage("03_translate")
        translate._config = lambda: {"char_map": {"hski": "花海咲季"}}
        return translate

    def test_prompt_uses_batch_scene_context_and_chinese_speaker(self):
        translate = self._translate()
        group = [
            {"uid": "a:1:text", "category": "resource", "file": "adv_unit_01-01_01.txt", "jp": "おはよう", "field": "text"},
            {
                "uid": "a:2:text", "category": "resource", "file": "adv_unit_01-01_01.txt",
                "jp": "おはよう", "field": "text", "speaker": "hski",
                "file_context": {"story_type": "unit", "character": "hski", "chapter": "01-01_01"},
            },
        ]

        prompt = translate.build_contextual_prompt(group)

        self.assertIn("文件: adv_unit_01-01_01.txt", prompt)
        self.assertIn("角色: 花海咲季", prompt)
        self.assertIn("场景类型: 团体", prompt)
        self.assertIn("章节: 01-01_01", prompt)
        self.assertIn("（花海咲季） おはよう", prompt)
        self.assertIn("不要写进译文", prompt)

    def test_prompt_maps_japanese_speaker_name_to_chinese(self):
        translate = self._translate()

        prompt = translate.build_contextual_prompt([
            {
                "uid": "a:2:text", "category": "resource", "file": "adv_unit_01-01_01.txt",
                "jp": "おはよう", "field": "text", "speaker": "咲季",
            },
        ])

        self.assertIn("（花海咲季） おはよう", prompt)

    def test_prompt_for_non_resource_shows_file_and_field(self):
        translate = self._translate()

        prompt = translate.build_contextual_prompt([
            {"uid": "master:m:0:1:name", "category": "master", "file": "m.json", "field": "name", "jp": "原文"},
        ])

        self.assertIn("文件: m.json", prompt)
        self.assertIn("类别: master", prompt)
        self.assertIn("字段: name", prompt)
        self.assertIn("（name） 原文", prompt)

    def test_prompt_lists_existing_translations_as_reference_only(self):
        translate = self._translate()
        item = {"uid": "g:1:a", "category": "generic", "file": "g.json", "field": "a", "jp": "こんにちは", "existing_cn": "你好"}

        prompt = translate.build_contextual_prompt([item])

        self.assertIn(f"[{translate._label(item)}] 你好", prompt)

    def test_lyric_row_shows_label_without_repeating_the_key(self):
        translate = self._translate()
        item = {
            "uid": "generic:f:あのね。\r\n", "hash": "abcd1234abcd1234", "category": "generic",
            "file": "f.json", "field": "あのね。\r\n", "jp": "あのね。\r\n",
        }

        prompt = translate.build_contextual_prompt([item])

        self.assertIn("[abcd1234abcd1234] あのね。", prompt)
        self.assertNotIn("（あのね。", prompt)

    def test_prompt_asks_for_json_structured_output(self):
        translate = self._translate()

        prompt = translate.build_contextual_prompt([
            {"uid": "master:m:1:name", "category": "master", "file": "m.json", "field": "name", "jp": "原文"},
        ])

        self.assertIn('{"translations":[{"id":', prompt)
        self.assertIn("只输出一个 JSON 对象", prompt)
        self.assertIn("不要 markdown 代码块", prompt)
        self.assertNotIn("--- 不属于译文", prompt)

    def test_prompt_carries_game_background_and_field_hints(self):
        translate = self._translate()

        prompt = translate.build_contextual_prompt([
            {"uid": "master:m:1:name", "category": "master", "file": "m.json", "field": "name", "jp": "原文"},
            {"uid": "master:m:1:regexp", "category": "master", "file": "m.json", "field": "regexp", "jp": "麻央$"},
        ])

        self.assertIn("学园偶像大师", prompt)
        self.assertIn("文本类型：游戏数据表", prompt)
        self.assertIn("主要角色中文名：花海咲季", prompt)
        self.assertIn("- name：名称", prompt)
        self.assertIn("- regexp：正则匹配片段", prompt)
        self.assertIn("{user}、{0}、{threshold} 等花括号占位符", prompt)

class TranslateParseTests(unittest.TestCase):
    def setUp(self):
        self.translate = _load_stage("03_translate")
        self.group = [
            {"uid": "a:1:text", "hash": "1a2b"},
            {"uid": "b:2:text", "hash": "3c4d"},
        ]

    def test_labeled_entries_are_parsed(self):
        content = _json_translations(("1a2b", "你好"), ("3c4d", "世界"))

        self.assertEqual(
            self.translate._parse_translations(content, self.group),
            {"a:1:text": "你好", "b:2:text": "世界"},
        )

    def test_single_code_fence_is_tolerated(self):
        content = "```json\n" + _json_translations(("1a2b", "你好")) + "\n```"

        self.assertEqual(
            self.translate._parse_translations(content, self.group),
            {"a:1:text": "你好"},
        )

    def test_bare_array_is_tolerated(self):
        content = json.dumps(
            [{"id": "3c4d", "translation": "世界"}], ensure_ascii=False
        )

        self.assertEqual(
            self.translate._parse_translations(content, self.group),
            {"b:2:text": "世界"},
        )

    def test_altered_label_is_rejected(self):
        content = _json_translations(("[1a2b]", "你好"), (" 1a2b ", "你好"), ("3c4d", "世界"))

        self.assertEqual(
            self.translate._parse_translations(content, self.group),
            {"b:2:text": "世界"},
        )

    def test_plain_text_response_is_rejected(self):
        self.assertEqual(
            self.translate._parse_translations("[1a2b] 你好\n---\n[3c4d] 世界", self.group),
            {},
        )
        self.assertEqual(
            self.translate._parse_translations(
                json.dumps({"translations": [{"uid": "1a2b", "translation": "你好"}]}, ensure_ascii=False),
                self.group,
            ),
            {},
        )
        self.assertEqual(
            self.translate._parse_translations("完全无法识别的响应", self.group), {}
        )

    def test_empty_translation_list_is_rejected(self):
        self.assertEqual(
            self.translate._parse_translations('{"translations": []}', self.group), {}
        )

    def test_unknown_label_is_rejected_not_remapped(self):
        content = _json_translations(("c9c9", "你好"), ("3c4d", "世界"))

        parsed = self.translate._parse_translations(content, self.group)

        self.assertEqual(parsed, {"b:2:text": "世界"})
        self.assertNotIn("a:1:text", parsed)

    def test_duplicate_label_is_rejected(self):
        content = _json_translations(
            ("1a2b", "你好"), ("1a2b", "再会"), ("3c4d", "世界")
        )

        self.assertEqual(
            self.translate._parse_translations(content, self.group),
            {"b:2:text": "世界"},
        )

    def test_group_with_repeated_label_is_rejected(self):
        group = [{"uid": "a:1:text", "hash": "1a2b"}, {"uid": "b:2:text", "hash": "1a2b"}]

        self.assertEqual(
            self.translate._parse_translations(_json_translations(("1a2b", "你好")), group),
            {},
        )

    def test_missing_label_stays_missing(self):
        self.assertEqual(
            self.translate._parse_translations(_json_translations(("1a2b", "你好")), self.group),
            {"a:1:text": "你好"},
        )

    def test_non_string_entries_are_rejected(self):
        content = json.dumps(
            {"translations": [{"id": "1a2b", "translation": 7}, {"id": 5, "translation": "你好"}]},
            ensure_ascii=False,
        )

        self.assertEqual(self.translate._parse_translations(content, self.group), {})

    def test_label_falls_back_to_uid_digest(self):
        group = [{"uid": "a:1:text"}, {"uid": "b:2:text"}]
        label = self.translate._label(group[0])

        self.assertEqual(len(label), 16)
        self.assertEqual(
            self.translate._parse_translations(_json_translations((label, "你好")), group),
            {"a:1:text": "你好"},
        )


class TranslateNewlineFormTests(unittest.TestCase):
    def test_json_newline_escape_is_rewritten_to_source_marker(self):
        translate = _load_stage("03_translate")
        item = {"uid": "adv_x:1:text", "category": "resource", "jp": r"登場人物は\n麻央さん"}

        # The model answers the source's literal \n with the JSON escape, which
        # decodes to a real newline; the game needs the literal marker back.
        self.assertEqual(
            translate._normalize_newline_form(item, "登场人物是\n麻央"),
            r"登场人物是\n麻央",
        )
        # Already literal, count mismatch and real-newline sources stay untouched.
        self.assertEqual(
            translate._normalize_newline_form(item, r"登场人物是\n麻央"),
            r"登场人物是\n麻央",
        )
        self.assertEqual(
            translate._normalize_newline_form(item, "登场人物是\n麻央\n以及"),
            "登场人物是\n麻央\n以及",
        )
        real = {"uid": "master:m:1:text", "category": "master", "jp": "一行目\n二行目"}
        self.assertEqual(
            translate._normalize_newline_form(real, "第一行\n第二行"), "第一行\n第二行"
        )
        no_break = {"uid": "m:1:name", "category": "master", "jp": "最終試験1位"}
        self.assertEqual(
            translate._normalize_newline_form(no_break, "最终考试第1名"), "最终考试第1名"
        )

    def test_group_retry_accepts_json_newline_escape(self):
        translate = _load_stage("03_translate")
        translate._config = lambda: {"char_map": {}}
        backend = _ScriptedBackend([_uid_translations(("a:1:text", "哇～\n好吃～"))])
        translate._BACKEND = backend
        self.addCleanup(lambda: setattr(translate, "_BACKEND", None))
        group = [{
            "uid": "a:1:text", "category": "resource", "file": "adv_x.txt",
            "line": 1, "field": "text", "jp": r"おお～！\n美味しそ～！", "status": "new",
        }]

        result = translate.translate_group(group)

        self.assertEqual(result, {"a:1:text": r"哇～\n好吃～"})
        self.assertEqual(len(backend.prompts), 1)


class TranslateValidationTests(unittest.TestCase):
    def setUp(self):
        self.translate = _load_stage("03_translate")

    def _resource(self, jp):
        return {"uid": "adv_x:1:text", "category": "resource", "jp": jp, "field": "text"}

    def test_valid_translation_is_accepted(self):
        item = self._resource(r"前列にいるのは、\n麻央さんのお友達ですか？ {user}")

        self.assertIsNone(
            self.translate._validate_translation(
                item, r"前排的那些人，\n是麻央的朋友吗？ {user}"
            )
        )

    def test_generic_terminal_line_ending_is_structural(self):
        item = {
            "uid": "generic:file:歌词\r\n",
            "category": "generic",
            "jp": "歌词\r\n",
        }

        self.assertIsNone(self.translate._validate_translation(item, "歌词"))
        self.assertIsNone(self.translate._validate_translation(item, "歌词\r\n"))

    def test_lyric_response_trailing_break_is_stripped(self):
        item = {
            "uid": "generic:file:青空に\r\n",
            "category": "generic",
            "jp": "青空に\r\n",
            "field": "青空に\r\n",
        }

        self.assertEqual(
            self.translate._strip_lyric_source_line(item, "蓝天下\n"), "蓝天下"
        )

    def test_lyric_response_echoing_source_line_is_reduced(self):
        item = {
            "uid": "generic:file:青空に　スマホが届いた\r\n",
            "category": "generic",
            "jp": "青空に　スマホが届いた\r\n",
            "field": "青空に　スマホが届いた\r\n",
        }

        self.assertEqual(
            self.translate._strip_lyric_source_line(
                item, "青空に　スマホが届いた\n蓝天下 手机送来消息\r\n"
            ),
            "蓝天下 手机送来消息",
        )
        # The source's inner break may come back as a space.
        self.assertEqual(
            self.translate._strip_lyric_source_line(
                item, "青空に スマホが届いた\n蓝天下 手机送来消息"
            ),
            "蓝天下 手机送来消息",
        )

    def test_non_lyric_response_keeps_inner_breaks(self):
        item = {"uid": "generic:file:回数\n残り", "category": "generic", "jp": "回数\n残り"}

        self.assertEqual(
            self.translate._strip_lyric_source_line(item, "次数\n剩余"), "次数\n剩余"
        )

    def test_actual_newline_is_rejected(self):
        item = self._resource(r"前列にいるのは、\n麻央さん")

        self.assertIsNotNone(
            self.translate._validate_translation(item, "前排的那些人，\n是麻央")
        )

    def test_literal_newline_count_change_is_rejected(self):
        item = self._resource(r"前列にいるのは、\n麻央さん")

        self.assertIsNotNone(
            self.translate._validate_translation(item, r"前排的那些人，是麻央")
        )

    def test_placeholder_count_change_is_rejected(self):
        item = self._resource("こんにちは {user} {user}")

        self.assertIsNotNone(
            self.translate._validate_translation(item, "你好 {user}")
        )

    def test_untranslated_source_is_rejected(self):
        item = self._resource("おはようございます")

        self.assertIsNotNone(
            self.translate._validate_translation(item, "おはようございます")
        )

    def test_dropped_resource_tag_is_rejected(self):
        item = self._resource(r"『<r\=よみ>語</r>』")

        self.assertIsNotNone(self.translate._validate_translation(item, "《词》"))
        self.assertIsNone(
            self.translate._validate_translation(item, r"『<r\=よみ>词</r>』")
        )

    def test_master_multiline_keeps_source_line_structure(self):
        item = {"uid": "master:m:0:1:text", "category": "master", "jp": "一行目\n二行目"}

        self.assertIsNone(self.translate._validate_translation(item, "第一行\n第二行"))
        self.assertIsNotNone(self.translate._validate_translation(item, "第一行第二行"))


class _ScriptedBackend:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    def translate(self, prompt):
        self.prompts.append(prompt)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _json_translations(*pairs: tuple[str, str]) -> str:
    """Structured response body as the pipeline expects it."""
    return json.dumps(
        {"translations": [{"id": label, "translation": cn} for label, cn in pairs]},
        ensure_ascii=False,
    )


class _UidMapBackend:
    """Offline stub answering every uid it knows that appears in the prompt."""

    def __init__(self, translations):
        self.translations = translations
        self.prompts = []

    def translate(self, prompt):
        self.prompts.append(prompt)
        rows = [
            {"id": uid_label(uid), "translation": cn}
            for uid, cn in self.translations.items()
            if f"[{uid_label(uid)}]" in prompt
        ]
        if not rows:
            raise AssertionError("stub received no known label")
        return json.dumps({"translations": rows}, ensure_ascii=False)


class TranslateGroupRetryTests(unittest.TestCase):
    def _translate_with(self, responses):
        translate = _load_stage("03_translate")
        translate._config = lambda: {"char_map": {}}
        backend = _ScriptedBackend(responses)
        translate._BACKEND = backend
        self.addCleanup(lambda: setattr(translate, "_BACKEND", None))
        return translate, backend

    @staticmethod
    def _item(uid, jp):
        return {
            "uid": uid, "category": "resource", "file": "adv_x.txt",
            "line": 1, "field": "text", "jp": jp, "status": "new",
        }

    def test_invalid_item_is_repaired_in_targeted_request(self):
        translate, backend = self._translate_with([
            _uid_translations(("a:1:text", "早上好")),
            _uid_translations(("b:2:text", "晚上好")),
        ])
        group = [self._item("a:1:text", "おはよう"), self._item("b:2:text", "こんばんは")]

        result = translate.translate_group(group)

        self.assertEqual(result, {"a:1:text": "早上好", "b:2:text": "晚上好"})
        self.assertEqual(len(backend.prompts), 2)
        self.assertIn(f"[{uid_label('b:2:text')}] 失败原因", backend.prompts[1])
        self.assertNotIn(f"[{uid_label('a:1:text')}]", backend.prompts[1])

    def test_split_single_failure_keeps_verified_translations(self):
        translate, backend = self._translate_with([
            "完全无法识别的响应",
            _uid_translations(("a:1:text", "早上好")),
            _uid_translations(("b:2:text", " ")),
        ])
        group = [self._item("a:1:text", "おはよう"), self._item("b:2:text", "こんばんは")]

        with self.assertRaises(translate.PartialTranslationError) as cm:
            translate.translate_group(group)

        error = cm.exception
        self.assertEqual(error.translations, {"a:1:text": "早上好"})
        self.assertIn("b:2:text", error.failures)
        self.assertEqual(len(backend.prompts), 3)

    def test_oversize_batch_splits_without_resending_full_prompt(self):
        oversize = requests.HTTPError(
            "413 request too large", response=SimpleNamespace(status_code=413)
        )
        translate, backend = self._translate_with([
            oversize, _uid_translations(("a:1:text", "早上好")), _uid_translations(("b:2:text", "晚上好")),
        ])
        group = [self._item("a:1:text", "おはよう"), self._item("b:2:text", "こんばんは")]

        result = translate.translate_group(group)

        self.assertEqual(result, {"a:1:text": "早上好", "b:2:text": "晚上好"})
        self.assertEqual(len(backend.prompts), 3)
        self.assertNotIn(f"[{uid_label('b:2:text')}]", backend.prompts[1])
        self.assertNotIn(f"[{uid_label('a:1:text')}]", backend.prompts[2])


class TranslateIncrementalTests(unittest.TestCase):
    def _prepare(self, items, prior=None, checkpoint=None, skip_changed=True):
        translate = _load_stage("03_translate")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        cache_dir = Path(directory.name) / "cache"
        cache_dir.mkdir()
        (cache_dir / "extract.json").write_text(
            json.dumps(items, ensure_ascii=False), encoding="utf-8"
        )
        if prior is not None:
            (cache_dir / "translated.json").write_text(
                json.dumps(prior, ensure_ascii=False), encoding="utf-8"
            )
        if checkpoint is not None:
            (cache_dir / "translate_checkpoint.json").write_text(
                json.dumps(checkpoint, ensure_ascii=False), encoding="utf-8"
            )
        llm = {"skip_changed": skip_changed, "batch_size": 20, "max_concurrent": 2}
        translate._config = lambda: {"char_map": {}, "llm": llm}
        translate.resolve_paths = lambda cfg: {"server_cache": cache_dir / "server"}
        self.addCleanup(lambda: setattr(translate, "_BACKEND", None))
        return translate, cache_dir

    @staticmethod
    def _changed():
        return {
            "uid": "adv_x:1:text", "category": "resource", "file": "adv_x.txt",
            "line": 1, "field": "text", "jp": "新しい原文",
            "existing_cn": "旧模组译文", "status": "changed",
        }

    @staticmethod
    def _new(uid, line, jp):
        return {
            "uid": uid, "category": "resource", "file": "adv_x.txt",
            "line": line, "field": "text", "jp": jp,
            "existing_cn": "", "status": "new",
        }

    def test_changed_item_uses_prior_translation_when_skipping(self):
        translate, cache_dir = self._prepare(
            [self._changed()],
            prior=[{"uid": "adv_x:1:text", "cn": "上次生成的译文"}],
            skip_changed=True,
        )
        calls = []
        translate.translate_group = lambda group: calls.append(list(group)) or {}

        with self.assertRaises(SystemExit) as cm:
            translate.main()

        self.assertEqual(cm.exception.code, 0)
        self.assertEqual(calls, [])
        written = json.loads((cache_dir / "translated.json").read_text(encoding="utf-8"))
        self.assertEqual(written[0]["cn"], "上次生成的译文")

    def test_changed_item_is_retranslated_when_skip_changed_is_false(self):
        translate, cache_dir = self._prepare(
            [self._changed()],
            prior=[{"uid": "adv_x:1:text", "cn": "上次生成的译文"}],
            skip_changed=False,
        )
        seen = []

        def fake(group):
            seen.append([item["uid"] for item in group])
            return {item["uid"]: "重译译文" for item in group}

        translate.translate_group = fake
        translate.main()

        self.assertEqual(seen, [["adv_x:1:text"]])
        written = json.loads((cache_dir / "translated.json").read_text(encoding="utf-8"))
        self.assertEqual(written[0]["cn"], "重译译文")

    def test_checkpoint_roundtrip_is_atomic_and_corrupt_file_is_kept(self):
        translate, cache_dir = self._prepare([])
        translate.CHECKPOINT = cache_dir / "translate_checkpoint.json"

        translate._save_checkpoint({"a:1:text": "你好"})

        self.assertEqual(translate._load_checkpoint(), {"a:1:text": "你好"})
        self.assertFalse((cache_dir / "translate_checkpoint.json.tmp").exists())

        (cache_dir / "translate_checkpoint.json").write_text("{broken", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            translate._load_checkpoint()
        self.assertEqual(
            (cache_dir / "translate_checkpoint.json").read_text(encoding="utf-8"),
            "{broken",
        )

    def test_checkpointed_item_is_recovered_and_backend_only_gets_pending(self):
        items = [
            self._new("adv_x:1:text", 1, "おはよう"),
            self._new("adv_x:2:text", 2, "こんばんは"),
        ]
        translate, cache_dir = self._prepare(
            items, checkpoint={"adv_x:1:text": "早上好"}
        )
        backend = _ScriptedBackend([_uid_translations(("adv_x:2:text", "晚上好"))])
        translate._BACKEND = backend

        translate.main()

        self.assertEqual(len(backend.prompts), 1)
        self.assertIn(f"[{uid_label('adv_x:2:text')}]", backend.prompts[0])
        self.assertNotIn(f"[{uid_label('adv_x:1:text')}]", backend.prompts[0])
        written = json.loads((cache_dir / "translated.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [(item["uid"], item["cn"]) for item in written],
            [("adv_x:1:text", "早上好"), ("adv_x:2:text", "晚上好")],
        )
        self.assertFalse((cache_dir / "translate_checkpoint.json").exists())

    def test_failed_item_keeps_checkpoint_and_exit_code(self):
        items = [
            self._new("adv_x:1:text", 1, "おはよう"),
            self._new("adv_x:2:text", 2, "こんばんは"),
        ]
        translate, cache_dir = self._prepare(items)
        translate._BACKEND = _ScriptedBackend([
            _uid_translations(("adv_x:1:text", "早上好")),
            "",
            _uid_translations(("adv_x:2:text", " ")),
        ])

        with self.assertRaises(SystemExit) as cm:
            translate.main()

        self.assertEqual(cm.exception.code, 1)
        checkpoint = json.loads(
            (cache_dir / "translate_checkpoint.json").read_text(encoding="utf-8")
        )
        self.assertEqual(checkpoint, {"adv_x:1:text": "早上好"})

        # The next run only retries the failed UID.
        translate._BACKEND = _ScriptedBackend([_uid_translations(("adv_x:2:text", "晚上好"))])
        translate.main()

        written = json.loads((cache_dir / "translated.json").read_text(encoding="utf-8"))
        self.assertEqual([item["cn"] for item in written], ["早上好", "晚上好"])
        self.assertFalse((cache_dir / "translate_checkpoint.json").exists())


class PipelineEndToEndTests(unittest.TestCase):
    """Stage 3 -> Stage 4 on a temp project with an offline stub backend."""

    def test_four_categories_are_translated_and_built(self):
        translate = _load_stage("03_translate")
        build = _load_stage("04_build")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache_dir = root / "cache"
            server = cache_dir / "server"
            (server / "res_raw").mkdir(parents=True)
            source = server / "res_raw" / "adv_test.txt"
            source.write_text(
                "[title title=プロローグ clip=none]\n"
                "[message text=おはよう！ name=咲季 clip=none]\n"
                "[choicegroup choices=[choice text=はい] choices=[choice text=いいえ] clip=none]\n",
                encoding="utf-8",
            )

            mod = cache_dir / "mod"
            (mod / "local-files" / "resource").mkdir(parents=True)
            (mod / "local-files" / "masterTrans").mkdir(parents=True)
            (mod / "local-files" / "genericTrans").mkdir(parents=True)
            (mod / "local-files" / "masterTrans" / "unit.json").write_text(
                json.dumps({"data": [{"id": "id1", "name": ""}]}, ensure_ascii=False),
                encoding="utf-8",
            )
            (mod / "local-files" / "genericTrans" / "x.json").write_text(
                json.dumps({"こんにちは": "こんにちは"}, ensure_ascii=False),
                encoding="utf-8",
            )
            (mod / "local-files" / "localization.json").write_text(
                json.dumps({"a.b": "こんにちは"}, ensure_ascii=False),
                encoding="utf-8",
            )

            resource_items = []
            for entry in extract_resource_text(source):
                item = dict(entry)
                item["uid"] = f"adv_test:{item['line']}:{item['field']}"
                item["file"] = "adv_test.txt"
                item["category"] = "resource"
                if item["field"] == "name":
                    item["existing_cn"] = "咲季"
                    item["status"] = "existing"
                else:
                    item["existing_cn"] = ""
                    item["status"] = "new"
                resource_items.append(item)
            generic_items = extract_generic_text(mod / "local-files" / "genericTrans")
            localization_items = extract_localization_text(
                mod / "local-files" / "localization.json"
            )
            extract = resource_items + [
                {
                    "uid": "master:unit:0:id1:name", "category": "master",
                    "file": "unit.json", "record_id": "id1", "field": "name",
                    "jp": "原文", "existing_cn": "", "status": "new",
                },
            ] + generic_items + localization_items
            (cache_dir / "extract.json").write_text(
                json.dumps(extract, ensure_ascii=False), encoding="utf-8"
            )

            translations = {
                "adv_test:1:text": "序章",
                "adv_test:2:text": "早上好！",
                "adv_test:3:text[0]": "是",
                "adv_test:3:text[1]": "否",
                "master:unit:0:id1:name": "译文",
                generic_items[0]["uid"]: "你好",
                localization_items[0]["uid"]: "你好世界",
            }
            backend = _UidMapBackend(translations)
            translate._config = lambda: {
                "char_map": {},
                "llm": {"skip_changed": True, "batch_size": 20, "max_concurrent": 2},
            }
            translate.resolve_paths = lambda cfg: {"server_cache": server}
            translate._BACKEND = backend
            try:
                translate.main()
            finally:
                translate._BACKEND = None

            written = json.loads(
                (cache_dir / "translated.json").read_text(encoding="utf-8")
            )
            by_uid = {item["uid"]: item["cn"] for item in written}
            self.assertEqual(by_uid["adv_test:1:text"], "序章")
            self.assertEqual(by_uid["adv_test:3:text[0]"], "是")
            self.assertEqual(by_uid["master:unit:0:id1:name"], "译文")
            self.assertEqual(by_uid[generic_items[0]["uid"]], "你好")
            self.assertEqual(by_uid[localization_items[0]["uid"]], "你好世界")
            self.assertTrue(backend.prompts)

            build.resolve_paths = lambda cfg: {
                "server_cache": server,
                "mod_cache": mod,
                "output": root / "output",
            }
            build.load_config = lambda: {}
            build.main()

            out = root / "output" / "GakumasTranslationData"
            resource_out = (
                out / "local-files" / "resource" / "adv_test.txt"
            ).read_text(encoding="utf-8")
            self.assertIn(r"[title title=<r\=プロローグ>序章</r> clip=none]", resource_out)
            self.assertIn(r"<r\=おはよう！>早上好！</r>", resource_out)
            self.assertIn("text=是", resource_out)
            self.assertIn("text=否", resource_out)

            master_out = json.loads(
                (out / "local-files" / "masterTrans" / "unit.json").read_text(encoding="utf-8")
            )
            self.assertEqual(master_out["data"][0]["name"], "译文")

            generic_out = json.loads(
                (out / "local-files" / "genericTrans" / "x.json").read_text(encoding="utf-8")
            )
            self.assertEqual(generic_out["こんにちは"], "你好")

            localization_out = json.loads(
                (out / "local-files" / "localization.json").read_text(encoding="utf-8")
            )
            self.assertEqual(localization_out["a.b"], "你好世界")
            self.assertFalse((out / "local-files" / "local-files").exists())


class LLMConfigRegressionTests(unittest.TestCase):
    def _write_config(self, root, content="llm:\n  base_url: https://example.invalid\n"):
        path = root / "config.yaml"
        path.write_text(content, encoding="utf-8")
        return path

    def test_env_overrides_temperature_and_skip_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_config(Path(directory), "llm:\n  temperature: 0.1\n  skip_changed: true\n")
            with patch.dict(
                "os.environ",
                {
                    "LLM_TEMPERATURE": "0.7",
                    "LLM_REASONING_EFFORT": "high",
                    "LLM_SKIP_CHANGED": "false",
                },
            ):
                config = load_config(str(path))

        self.assertEqual(config["llm"]["temperature"], 0.7)
        self.assertEqual(config["llm"]["reasoning_effort"], "high")
        self.assertFalse(config["llm"]["skip_changed"])

    def test_quality_defaults_are_applied(self):
        with tempfile.TemporaryDirectory() as directory:
            config = load_config(str(self._write_config(Path(directory))))

        self.assertEqual(config["llm"]["batch_size"], 20)
        self.assertEqual(config["llm"]["max_concurrent"], 5)
        self.assertEqual(config["llm"]["timeout"], 180)
        self.assertEqual(config["llm"]["max_tokens"], 4096)
        self.assertIsNone(config["llm"]["reasoning_effort"])
        self.assertEqual(config["llm"]["temperature"], 0.2)
        self.assertTrue(config["llm"]["skip_changed"])

    def test_invalid_env_value_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_config(Path(directory))
            with patch.dict("os.environ", {"LLM_SKIP_CHANGED": "maybe"}):
                with self.assertRaises(ValueError):
                    load_config(str(path))

    def test_invalid_reasoning_effort_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_config(Path(directory))
            with patch.dict("os.environ", {"LLM_REASONING_EFFORT": "extreme"}):
                with self.assertRaises(ValueError):
                    load_config(str(path))


class LLMBackendParamsTests(unittest.TestCase):
    def _capture_post(self, response):
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["payload"] = json
            return SimpleNamespace(
                raise_for_status=lambda: None, json=lambda: response
            )

        return captured, fake_post

    def test_openai_sends_max_tokens_and_temperature(self):
        captured, fake_post = self._capture_post(
            {"choices": [{"message": {"content": "ok"}}]}
        )
        backend = OpenAIBackend({
            "base_url": "https://example.invalid/v1", "api_key": "secret",
            "model": "m", "max_tokens": 123, "temperature": 0.2, "timeout": 5,
        })

        with patch("lib.llm_backend.requests.post", fake_post):
            self.assertEqual(backend.translate("hi"), "ok")

        self.assertEqual(captured["url"], "https://example.invalid/v1/chat/completions")
        self.assertEqual(captured["payload"]["max_tokens"], 123)
        self.assertEqual(captured["payload"]["temperature"], 0.2)

    def test_openai_sends_reasoning_effort_without_temperature(self):
        captured, fake_post = self._capture_post(
            {"choices": [{"message": {"content": "ok"}}]}
        )
        backend = OpenAIBackend({
            "base_url": "https://example.invalid/v1", "api_key": "secret",
            "model": "m", "max_tokens": 123, "temperature": 0.2,
            "reasoning_effort": "low", "timeout": 5,
        })

        with patch("lib.llm_backend.requests.post", fake_post):
            self.assertEqual(backend.translate("hi"), "ok")

        self.assertEqual(captured["payload"]["reasoning_effort"], "low")
        self.assertNotIn("temperature", captured["payload"])

    def test_openai_requests_json_schema_structured_output(self):
        captured, fake_post = self._capture_post(
            {"choices": [{"message": {"content": "{}"}}]}
        )
        backend = OpenAIBackend({
            "base_url": "https://example.invalid/v1", "api_key": "secret",
            "model": "m", "max_tokens": 123, "timeout": 5,
        })

        with patch("lib.llm_backend.requests.post", fake_post):
            backend.translate("hi")

        response_format = captured["payload"]["response_format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertTrue(response_format["json_schema"]["strict"])
        schema = response_format["json_schema"]["schema"]
        self.assertEqual(schema["required"], ["translations"])
        self.assertFalse(schema["additionalProperties"])
        item = schema["properties"]["translations"]["items"]
        self.assertEqual(item["required"], ["id", "translation"])
        self.assertFalse(item["additionalProperties"])



if __name__ == "__main__":
    unittest.main()
