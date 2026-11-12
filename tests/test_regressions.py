import importlib.util
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

from lib.characters import resolve_character_cn
from lib.config import load_config, resolve_paths
from lib.http import extract_zip_atomically
from lib.llm_backend import (
    ContextOversizeError,
    FatalLLMError,
    OpenAIBackend,
    OutputTruncatedError,
)
from lib.octo import DownloadError, OctoClient
from lib.parser_generic import extract_generic_text
from lib.parser_localization import extract_localization_text
from lib.parser_master import extract_master_text
from lib.parser_resource import (
    build_resource_line,
    extract_resource_text,
    get_existing_resource_translation,
    parse_adv_context,
    resource_sources_equal,
    split_resource_translation,
)
from lib.proto import octodb_pb2 as octop
from lib.script_checker import check_script_text
from lib.text_utils import looks_like_japanese_source, uid_label
from stages import build, download, extract, package, translate

PROJECT_ROOT = Path(__file__).parent.parent


def _load_tool(name: str):
    path = PROJECT_ROOT / "tools" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _json_translations(*pairs: tuple[str, str]) -> str:
    return json.dumps(
        {"translations": [{"id": label, "translation": cn} for label, cn in pairs]},
        ensure_ascii=False,
    )


def _uid_translations(*pairs: tuple[str, str]) -> str:
    return _json_translations(*((uid_label(uid), cn) for uid, cn in pairs))


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


class _UidMapBackend:
    """Offline stub answering every uid it knows that appears in the prompt."""

    def __init__(self, translations):
        self.translations = translations
        self.prompts = []

    def translate(self, prompt):
        self.prompts.append(prompt)
        # Only match input lines `[16-hex]` in the prompt, never reference lines.
        rows = [
            {"id": uid_label(uid), "translation": cn}
            for uid, cn in self.translations.items()
            if f"[{uid_label(uid)}]" in prompt
        ]
        if not rows:
            raise AssertionError("stub received no known label")
        return json.dumps({"translations": rows}, ensure_ascii=False)


def _temp_paths(root: Path) -> dict[str, Path]:
    cache_dir = root / "cache"
    return {
        "cache_dir": cache_dir,
        "server_cache": cache_dir / "server",
        "mod_cache": cache_dir / "mod",
        "nightly_mod_cache": cache_dir / "nightly",
        "gkm_diff": cache_dir / "gkm-diff",
        "output": root / "output",
    }


class ResourceRegressionTests(unittest.TestCase):
    def test_build_replaces_every_choicegroup_text(self):
        line = "[choicegroup text=選択肢1 text=選択肢2 text=選択肢3]"
        built = build_resource_line(
            line,
            {"text[0]": "选项1", "text[1]": "选项2", "text[2]": "选项3"},
        )
        self.assertEqual(built, "[choicegroup text=选项1 text=选项2 text=选项3]")

    def test_build_choicegroup_nested_choice_uses_plain_chinese(self):
        line = (
            "[choicegroup choices=[choice text=よく似合っています] "
            r"clip=\{...\}]"
        )
        built = build_resource_line(line, {"text[0]": "很适合你"})
        self.assertEqual(
            built,
            "[choicegroup choices=[choice text=很适合你] " r"clip=\{...\}]",
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

    def test_text_starting_with_brackets_and_containing_equals_roundtrips_cleanly(self):
        # Unescaped leading `[...]` in raw text is extracted whole instead of truncating at `]`.
        raw_unquoted = r'[message text=[速報]花海咲季が優勝！ name=アナウンサー clip=\{"_startTime":1.0,"_duration":2.0\}]'
        with tempfile.TemporaryDirectory() as directory:
            fp = Path(directory) / "adv_news.txt"
            fp.write_text(raw_unquoted + "\n", encoding="utf-8")
            items = extract_resource_text(fp)
        self.assertEqual(items[0]["jp"], "[速報]花海咲季が優勝！")

        # Valid Uguiss escaped brackets `\[...\]` plus bare `=` in LLM output roundtrip & pass script_checker.
        valid_line = r'[message text=\[速報\]花海咲季が優勝！ name=アナウンサー clip=\{"_startTime":1.0,"_duration":2.0\}]'
        built = build_resource_line(valid_line, {"text": "【速报】花海咲季夺冠！ Lv=1"})
        self.assertIn(r"<r\=\[速報\]花海咲季が優勝！>【速报】花海咲季夺冠！ Lv\=1</r>", built)
        report = check_script_text(built + "\n[timeline]\n", "adv_news.txt")
        self.assertTrue(report["ok"], report["issues"])
    def test_extract_choice_translation_preserves_wrapped_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "adv_choice.txt"
            path.write_text(
                r"[choicegroup choices=[choice text=<r\=独占したかったから>因为我想独占你</r> clip=none]]",
                encoding="utf-8",
            )
            item = extract_resource_text(path)[0]

        self.assertEqual(item["jp"], r"<r\=独占したかったから>因为我想独占你</r>")

    def test_choice_translation_ignores_stray_choice_closer(self):
        self.assertEqual(
            get_existing_resource_translation(
                "text[0]",
                r"<r\=独占したかったから]>因为我想独占你</r>",
                "独占したかったから",
            ),
            ("独占したかったから", "因为我想独占你"),
        )

    def test_nightly_resource_translation_supersedes_untranslated_primary(self):
        current = "独占したかったから"
        _, primary_cn = get_existing_resource_translation("text[0]", current, current)
        _, nightly_cn = get_existing_resource_translation(
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
        self.assertEqual(
            split_resource_translation("<r\\=旧原文>旧译文</r\\>"),
            ("旧原文", "旧译文"),
        )
        self.assertEqual(
            split_resource_translation("<r\\=お客さん、満席ですね。>观众们，满座呢。</r>"),
            ("お客さん、満席ですね。", "观众们，满座呢。"),
        )
        self.assertEqual(
            split_resource_translation(
                r"<r\=前列にいるのは、>前排的那些人，</r>\r\n"
                r"<r\=麻央さんのお友達ですか？>是麻央的朋友吗？</r>"
            ),
            (r"前列にいるのは、\n麻央さんのお友達ですか？", r"前排的那些人，\n是麻央的朋友吗？"),
        )
        self.assertEqual(split_resource_translation("plain"), ("plain", ""))

    def test_resource_translation_split_handles_embedded_em_tag(self):
        old_jp, cn = split_resource_translation(
            "<r\\=——なら、<em\\=・・・>筋トレをがんばらないとね。>"
            "——那就要努力“锻炼肌肉”了呢。</r>"
        )
        self.assertEqual(old_jp, "——なら、<em\\=・・・>筋トレをがんばらないとね。")
        self.assertEqual(cn, "——那就要努力“锻炼肌肉”了呢。")
        self.assertTrue(
            resource_sources_equal(
                old_jp,
                "――なら、<em\\=・・・>筋トレ</em>をがんばらないとね。",
            )
        )

    def test_resource_translation_split_handles_nested_ruby_tags(self):
        value = (
            r"<r\=（咲季さんは、プロジェクト『<r\=スターダスト>星屑</r>』を>"
            r"({user}) （咲季经历了“<r\=スターダスト>星屑</r>”企划……</r>\r\n"
            r"<r\=経て……大きな成果を上げた）>取得了巨大的成果）</r>"
        )
        self.assertEqual(
            split_resource_translation(value),
            (
                r"（咲季さんは、プロジェクト『<r\=スターダスト>星屑</r>』を\n"
                r"経て……大きな成果を上げた）",
                r"({user}) （咲季经历了“<r\=スターダスト>星屑</r>”企划……\n"
                r"取得了巨大的成果）",
            ),
        )

    def test_resource_source_comparison_ignores_ruby_and_dash_variants(self):
        self.assertTrue(
            resource_sources_equal(
                "この楽曲は、\\n可愛くて、カッコイイ——",
                "この楽曲は、\\n可愛くて、カッコイイ――",
            )
        )
        self.assertTrue(
            resource_sources_equal(
                "はい。龍月真希さんの\\n所属している劇団です。",
                "はい。<r\\=りゅうげつまき>龍月真希</r>さんの\\n所属している劇団です。",
            )
        )

    def test_plain_chinese_choice_is_an_existing_translation(self):
        self.assertEqual(
            get_existing_resource_translation(
                "text[0]", r"我会尊重\n偶像的意见", r"アイドルの意見を\n尊重します"
            ),
            (r"アイドルの意見を\n尊重します", r"我会尊重\n偶像的意见"),
        )
        self.assertEqual(
            get_existing_resource_translation(
                "text[0]", "独占したかったから", "独占したかったから"
            ),
            ("独占したかったから", ""),
        )
        self.assertEqual(
            get_existing_resource_translation("text[0]", "金星", "金星"),
            ("金星", "金星"),
        )
        self.assertEqual(
            get_existing_resource_translation(
                "text[1]",
                r"源于谚语“立つ鳥跡を濁さず”\n的关系",
                r"立つ鳥跡を濁さず\nから",
            ),
            (r"立つ鳥跡を濁さず\nから", r"源于谚语“立つ鳥跡を濁さず”\n的关系"),
        )

    def test_plain_chinese_name_is_an_existing_translation(self):
        self.assertEqual(
            get_existing_resource_translation("name", "琴音", "ことね"),
            ("ことね", "琴音"),
        )
        self.assertEqual(
            get_existing_resource_translation("name", "ことね", "ことね"),
            ("ことね", ""),
        )

    def test_shifted_adv_lines_align_without_cross_line_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = _temp_paths(root)
            (paths["server_cache"] / "res_raw").mkdir(parents=True)
            (paths["mod_cache"] / "local-files" / "resource").mkdir(parents=True)
            (paths["gkm_diff"]).mkdir(parents=True)

            # Server inserted a [wait] at line 1 AND a brand-new [message] at line 3.
            (paths["server_cache"] / "res_raw" / "adv_shift.txt").write_text(
                "[wait time=0.5]\n"
                "[message text=おはよう name=咲季]\n"
                "[message text=新台詞です name=ことね]\n"
                "[message text=ありがとう name=手毬]\n",
                encoding="utf-8",
            )
            (paths["mod_cache"] / "local-files" / "resource" / "adv_shift.txt").write_text(
                r"[message text=<r\=おはよう>早上好</r> name=花海咲季]" "\n"
                r"[message text=<r\=ありがとう>谢谢</r> name=月村手毬]" "\n",
                encoding="utf-8",
            )

            items = extract.run(load_config(), paths)

        by_uid = {it["uid"]: it for it in items}
        self.assertEqual(by_uid["adv_shift:2:text"]["status"], "existing")
        self.assertEqual(by_uid["adv_shift:2:text"]["existing_cn"], "早上好")
        self.assertEqual(by_uid["adv_shift:3:text"]["status"], "new")
        self.assertEqual(by_uid["adv_shift:3:text"]["existing_cn"], "")
        self.assertEqual(by_uid["adv_shift:3:name"]["existing_cn"], "藤田琴音")
        self.assertEqual(by_uid["adv_shift:4:text"]["status"], "existing")
        self.assertEqual(by_uid["adv_shift:4:text"]["existing_cn"], "谢谢")

    def test_character_registry_resolves_kana_and_new_actors_to_simplified_chinese(self):
        self.assertEqual(resolve_character_cn("ことね"), "藤田琴音")
        self.assertEqual(resolve_character_cn("リーリヤ"), "葛城莉莉娅")
        self.assertEqual(resolve_character_cn("広"), "筱泽广")
        self.assertEqual(resolve_character_cn("篠澤 広"), "筱泽广")
        self.assertEqual(resolve_character_cn("美鈴"), "秦谷美铃")
        self.assertEqual(resolve_character_cn("清夏"), "紫云清夏")
        self.assertEqual(resolve_character_cn("千奈"), "仓本千奈")
        self.assertEqual(resolve_character_cn("莉波"), "姬崎莉波")
        self.assertEqual(resolve_character_cn("nasr"), "根绪亚纱里")
        self.assertEqual(resolve_character_cn("krnh"), "贺阳燐羽")
        self.assertEqual(parse_adv_context("adv_dear_nasr_001")["character"], "nasr")

    def test_changed_translation_is_skipped_by_default(self):
        changed = {"status": "changed"}
        new = {"status": "new"}
        self.assertFalse(translate.should_translate(changed, True))
        self.assertTrue(translate.should_translate(changed, False))
        self.assertTrue(translate.should_translate(new, True))


class TranslateSkipMarkerTests(unittest.TestCase):
    def _prepare(self, items, with_stale_marker=False):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        paths = _temp_paths(root)
        paths["cache_dir"].mkdir(parents=True)
        (paths["cache_dir"] / "extract.json").write_text(
            json.dumps(items, ensure_ascii=False), encoding="utf-8"
        )
        marker = paths["cache_dir"] / "nothing_to_translate"
        if with_stale_marker:
            marker.write_text("", encoding="utf-8")
        cfg = {
            "llm": {"skip_changed": True, "batch_size": 20, "max_concurrent": 5},
            "character_names": {},
        }
        return cfg, paths, marker

    def test_nothing_to_translate_writes_skip_marker(self):
        cfg, paths, marker = self._prepare(
            [{"uid": "adv_x:1", "status": "existing", "existing_cn": "旧译文"}]
        )
        has_work = translate.run(cfg, paths)
        self.assertFalse(has_work)
        self.assertTrue(marker.exists())

    def test_pending_translation_removes_stale_skip_marker(self):
        cfg, paths, marker = self._prepare(
            [{"uid": "adv_x:1", "status": "new", "category": "master", "jp": "こんにちは"}],
            with_stale_marker=True,
        )
        stub = _UidMapBackend({"adv_x:1": "你好"})
        has_work = translate.run(cfg, paths, backend=stub)
        self.assertTrue(has_work)
        self.assertFalse(marker.exists())


class LocalizationRegressionTests(unittest.TestCase):
    def test_japanese_localization_is_marked_new_with_empty_existing_cn(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "localization.json"
            path.write_text(
                json.dumps({"jp": "みんなありがとう", "cn": "大家好"}, ensure_ascii=False),
                encoding="utf-8",
            )
            items = extract_localization_text(path)

        self.assertEqual(items[0]["status"], "new")
        self.assertEqual(items[0]["existing_cn"], "")
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
        self.assertEqual(
            [item["uid"] for item in items],
            ["master:sample:repeated#0:name", "master:sample:repeated#1:name"],
        )

    def test_master_matches_records_by_key_not_position(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            yaml_dir = root / "yaml"
            mod_dir = root / "mod"
            yaml_dir.mkdir()
            mod_dir.mkdir()
            (yaml_dir / "sample.yaml").write_text(
                "- id: a\n  name: 原文A\n- id: c\n  name: 原文C\n",
                encoding="utf-8",
            )
            (mod_dir / "sample.json").write_text(
                json.dumps(
                    {
                        "rules": {"primaryKeys": ["id"]},
                        "data": [
                            {"id": "a", "name": "译文A"},
                            {"id": "b", "name": "译文B"},
                            {"id": "c", "name": "译文C"},
                        ],
                    },
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
                    {
                        "rules": {"primaryKeys": ["supportCardId", "number"]},
                        "data": [
                            {"supportCardId": "card-0", "number": 9, "text": "旧卡"},
                            {"supportCardId": "card-1", "number": 1, "text": "译文一"},
                            {"supportCardId": "card-1", "number": 2, "text": "译文二"},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            items = extract_master_text(yaml_dir, mod_dir)

        self.assertEqual([item["existing_cn"] for item in items], ["译文一", "译文二"])
        self.assertEqual(
            [item["uid"] for item in items],
            ["master:sample:card-1|1:text", "master:sample:card-1|2:text"],
        )

    def test_master_ignores_relational_primary_key_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            yaml_dir = root / "yaml"
            mod_dir = root / "mod"
            yaml_dir.mkdir()
            mod_dir.mkdir()
            (yaml_dir / "sample.yaml").write_text(
                "- id: a\n  name: 原文A\n  descriptions:\n  - kind: Plain\n    text: 甲\n"
                "- id: b\n  name: 原文B\n  descriptions:\n  - kind: Plain\n    text: 乙\n",
                encoding="utf-8",
            )
            (mod_dir / "sample.json").write_text(
                json.dumps(
                    {
                        "rules": {"primaryKeys": ["id", "descriptions.text"]},
                        "data": [
                            {"id": "extra", "name": "多余"},
                            {"id": "a", "name": "译文A"},
                            {"id": "b", "name": "译文B"},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            items = extract_master_text(yaml_dir, mod_dir)

        self.assertEqual([item["existing_cn"] for item in items], ["译文A", "译文B"])

    def test_master_extracts_and_builds_string_list_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            yaml_dir = root / "yaml"
            mod_dir = root / "mod"
            out_dir = root / "out"
            yaml_dir.mkdir()
            mod_dir.mkdir()
            (yaml_dir / "Tutorial.yaml").write_text(
                "- tutorialType: GameStart\n"
                "  step: 6\n"
                "  texts:\n"
                "  - |-\n"
                "    レッスンクリアおめでとう！\n"
                "    報酬を獲得できます\n",
                encoding="utf-8",
            )
            (mod_dir / "Tutorial.json").write_text(
                json.dumps(
                    {
                        "rules": {"primaryKeys": ["tutorialType", "step"]},
                        "data": [{"tutorialType": "GameStart", "step": 1, "texts": []}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            items = extract_master_text(yaml_dir, mod_dir)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["field"], "texts[0]")
            self.assertEqual(items[0]["status"], "new")
            items[0]["cn"] = "训练完成，恭喜！\n可以获得报酬"

            target_dir = out_dir / "local-files" / "masterTrans"
            target_dir.mkdir(parents=True)
            (target_dir / "Tutorial.json").write_text(
                (mod_dir / "Tutorial.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            count, _ = build.apply_master(out_dir, "Tutorial.json", items)
            built = json.loads((target_dir / "Tutorial.json").read_text(encoding="utf-8"))

        self.assertEqual(count, 1)
        self.assertEqual(len(built["data"]), 2)
        self.assertEqual(
            built["data"][1],
            {
                "tutorialType": "GameStart",
                "step": 6,
                "texts": ["训练完成，恭喜！\n可以获得报酬"],
            },
        )

    def test_build_master_inserts_new_records_into_existing_table(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master_dir = root / "local-files" / "masterTrans"
            master_dir.mkdir(parents=True)
            target = master_dir / "Tower.json"
            target.write_text(
                json.dumps(
                    {
                        "rules": {"primaryKeys": ["id"]},
                        "data": [{"id": "tower-1", "title": "旧塔"}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            count, applied = build.apply_master(
                root,
                "Tower.json",
                [
                    {
                        "uid": "master:Tower:tower-1:title",
                        "record_id": "tower-1",
                        "key": ["tower-1"],
                        "key_occurrence": 0,
                        "field": "title",
                        "cn": "旧塔",
                    },
                    {
                        "uid": "master:Tower:tower-2:title",
                        "record_id": "tower-2",
                        "key": ["tower-2"],
                        "key_occurrence": 0,
                        "field": "title",
                        "cn": "新塔标题",
                    },
                    {
                        "uid": "master:Tower:tower-2:description",
                        "record_id": "tower-2",
                        "key": ["tower-2"],
                        "key_occurrence": 0,
                        "field": "description",
                        "cn": "新塔说明",
                    },
                ],
            )
            data = json.loads(target.read_text(encoding="utf-8"))

        self.assertEqual(count, 3)
        self.assertEqual(len(applied), 3)
        self.assertEqual(
            data["data"],
            [
                {"id": "tower-1", "title": "旧塔"},
                {"id": "tower-2", "title": "新塔标题", "description": "新塔说明"},
            ],
        )

    def test_build_master_matches_idless_and_duplicate_records_by_key(self):
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
            count, _ = build.apply_master(
                root,
                "sample.json",
                [
                    {"uid": "master:sample:repeated#0:name", "record_id": "repeated", "key": ["repeated"], "key_occurrence": 0, "field": "name", "cn": "译文一"},
                    {"uid": "master:sample:repeated#1:name", "record_id": "repeated", "key": ["repeated"], "key_occurrence": 1, "field": "name", "cn": "译文二"},
                    {"uid": "master:sample:_idx2:name", "record_id": "", "key": [None], "key_occurrence": 0, "field": "name", "cn": "无 ID 译文"},
                ],
            )
            data = json.loads(target.read_text(encoding="utf-8"))

        self.assertEqual(count, 3)
        self.assertEqual([record["name"] for record in data["data"]], ["译文一", "译文二", "无 ID 译文"])

    def test_build_master_creates_missing_non_id_and_id_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            count_id, _ = build.apply_master(
                root,
                "new-id.json",
                [{"uid": "master:new-id:r1:name", "record_id": "r1", "field": "name", "cn": "新译文"}],
            )
            count_type, _ = build.apply_master(
                root,
                "Work.json",
                [
                    {
                        "uid": "master:Work:MiniLive:name",
                        "key_paths": ["type"],
                        "key": ["MiniLive"],
                        "key_occurrence": 0,
                        "field": "name",
                        "cn": "迷你Live",
                    }
                ],
            )
            id_data = json.loads(
                (root / "local-files" / "masterTrans" / "new-id.json").read_text(encoding="utf-8")
            )
            type_data = json.loads(
                (root / "local-files" / "masterTrans" / "Work.json").read_text(encoding="utf-8")
            )

        self.assertEqual(count_id, 1)
        self.assertEqual(id_data["rules"]["primaryKeys"], ["id"])
        self.assertEqual(id_data["data"], [{"id": "r1", "name": "新译文"}])
        self.assertEqual(count_type, 1)
        self.assertEqual(type_data["rules"]["primaryKeys"], ["type"])
        self.assertEqual(type_data["data"], [{"type": "MiniLive", "name": "迷你Live"}])

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

    def test_master_empty_stub_in_primary_falls_back_to_nightly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            yaml_dir = root / "yaml"
            primary_dir = root / "primary"
            nightly_dir = root / "nightly"
            yaml_dir.mkdir()
            primary_dir.mkdir()
            nightly_dir.mkdir()
            (yaml_dir / "Tips.yaml").write_text("- id: comic_0001\n  title: 第1話\n", encoding="utf-8")
            (primary_dir / "Tips.json").write_text(
                json.dumps({"data": [{"id": "comic_0001", "title": ""}]}, ensure_ascii=False),
                encoding="utf-8",
            )
            (nightly_dir / "Tips.json").write_text(
                json.dumps({"data": [{"id": "comic_0001", "title": "第1话"}]}, ensure_ascii=False),
                encoding="utf-8",
            )
            items = extract_master_text(yaml_dir, primary_dir, fallback_mod_master_dir=nightly_dir)

        self.assertEqual(items[0]["existing_cn"], "第1话")
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

    def test_master_skips_yaml_parsing_when_file_has_no_japanese(self):
        import lib.parser_master as pm

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            yaml_dir = root / "yaml"
            mod_dir = root / "mod"
            yaml_dir.mkdir()
            mod_dir.mkdir()
            (yaml_dir / "ascii_only.yaml").write_text(
                "- id: 1\n  code: EFFECT_UP\n  value: 100\n",
                encoding="utf-8",
            )
            (yaml_dir / "nested_only.yaml").write_text(
                "- id: 1\n"
                "  produceDescriptions:\n"
                "  - produceDescriptionType: PlainText\n"
                "    text: アクティブスキルカード\n"
                "  order: 1\n",
                encoding="utf-8",
            )
            (yaml_dir / "has_jp.yaml").write_text(
                "- id: 1\n  name: テスト\n",
                encoding="utf-8",
            )

            loaded_texts = []
            orig_load = pm.yaml.load

            def spy_load(stream, *args, **kwargs):
                loaded_texts.append(stream)
                return orig_load(stream, *args, **kwargs)

            pm.yaml.load = spy_load
            try:
                items = extract_master_text(yaml_dir, mod_dir)
            finally:
                pm.yaml.load = orig_load

        self.assertEqual(len(loaded_texts), 1)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["jp"], "テスト")


class DownloadRegressionTests(unittest.TestCase):
    def test_failed_asset_raises_download_error_and_redownloads_on_object_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = OctoClient({"data_path": str(root / "octo")})
            self.addCleanup(client.close)
            database = octop.Database(urlFormat="https://assets/{o}")
            resource = database.resourceList.add()
            resource.name = "adv_test.txt"
            resource.objectName = "obj_v1"

            def fail_req(*args, **kwargs):
                return SimpleNamespace(status_code=500, content=b"", headers={}, close=lambda: None)

            def ok_v1(*args, **kwargs):
                return SimpleNamespace(status_code=200, content=b"v1", headers={}, close=lambda: None)

            def ok_v2(*args, **kwargs):
                return SimpleNamespace(status_code=200, content=b"v2", headers={}, close=lambda: None)

            with patch("lib.octo.request_with_retry", fail_req):
                with self.assertRaises(DownloadError):
                    client.download_adv_txts(database, root / "res")
            log = json.loads((root / "download_log.json").read_text(encoding="utf-8"))
            self.assertNotIn("adv_test.txt", log)
            self.assertFalse((root / "res" / "adv_test.txt").exists())

            with patch("lib.octo.request_with_retry", ok_v1):
                client.download_adv_txts(database, root / "res")
            log = json.loads((root / "download_log.json").read_text(encoding="utf-8"))
            self.assertEqual(log["adv_test.txt"], "obj_v1")
            self.assertEqual((root / "res" / "adv_test.txt").read_bytes(), b"v1")

            # When server updates existing adv_test.txt with new objectName, cache invalidates!
            resource.objectName = "obj_v2"
            with patch("lib.octo.request_with_retry", ok_v2):
                client.download_adv_txts(database, root / "res")
            self.assertEqual((root / "res" / "adv_test.txt").read_bytes(), b"v2")

    def test_zip_cache_replacement_removes_old_files_and_rejects_zip_slip(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "cache"
            destination.mkdir()
            (destination / "old.txt").write_text("old", encoding="utf-8")
            content = io.BytesIO()
            with zipfile.ZipFile(content, "w") as archive:
                archive.writestr("source/new.txt", "new")

            extract_zip_atomically(content.getvalue(), destination)

            self.assertEqual((destination / "new.txt").read_text(encoding="utf-8"), "new")
            self.assertFalse((destination / "old.txt").exists())

            malicious = io.BytesIO()
            with zipfile.ZipFile(malicious, "w") as archive:
                archive.writestr("../../evil.txt", "pwn")
            with self.assertRaises(ValueError):
                extract_zip_atomically(malicious.getvalue(), destination)

    def test_multi_source_manifest_skips_only_when_all_sources_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = _temp_paths(root)
            paths["cache_dir"].mkdir(parents=True)
            (paths["cache_dir"] / "source_manifest.json").write_text(
                json.dumps(
                    {
                        "octo_revision": 100,
                        "mod_version": "v1.0",
                        "gkm_diff_sha": "aaa111",
                    }
                ),
                encoding="utf-8",
            )

            db = octop.Database(revision=100, urlFormat="https://assets/{o}")
            dl_called = False

            class FakeClient:
                def __init__(self, cfg):
                    pass

                def close(self):
                    pass

                def load_local_db(self):
                    return None

                def fetch_database(self):
                    return db

                def save_local_db(self, _):
                    pass

                def download_adv_txts(self, *_):
                    nonlocal dl_called
                    dl_called = True
                    return 0

            with (
                patch.object(download, "OctoClient", FakeClient),
                patch.object(download, "_probe_mod_release", return_value=("v1.0", "https://example/mod.zip")),
                patch.object(download, "_probe_diff_sha", return_value="aaa111"),
            ):
                should_continue = download.run({"github": {"use_nightly": False}}, paths)

            self.assertFalse(should_continue)
            self.assertFalse(dl_called)
            self.assertTrue((paths["cache_dir"] / "nothing_to_translate").exists())

            # Now gakumasu-diff pushes delayed YAML (`bbb222`) while Octo revision stays 100!
            (paths["mod_cache"] / "local-files").mkdir(parents=True)
            (paths["mod_cache"] / "version.txt").write_text("v1.0", encoding="utf-8")
            (paths["gkm_diff"]).mkdir(parents=True)
            (paths["gkm_diff"] / "Work.yaml").write_text("[]", encoding="utf-8")
            (paths["gkm_diff"] / ".commit_sha").write_text("bbb222", encoding="utf-8")

            with (
                patch.object(download, "OctoClient", FakeClient),
                patch.object(download, "_probe_mod_release", return_value=("v1.0", "https://example/mod.zip")),
                patch.object(download, "_probe_diff_sha", return_value="bbb222"),
            ):
                should_continue = download.run({"github": {"use_nightly": False}}, paths)

            self.assertTrue(should_continue)
            self.assertTrue(dl_called)
            self.assertFalse((paths["cache_dir"] / "nothing_to_translate").exists())

    def test_nightly_fills_untranslated_and_missing_primary_entries(self):
        primary = [
            {"uid": "g:translated", "existing_cn": "主包译文", "status": "existing"},
            {"uid": "g:untranslated", "existing_cn": "", "status": "new"},
            {"uid": "g:both-untranslated", "existing_cn": "", "status": "new"},
        ]
        nightly = [
            {"uid": "g:translated", "existing_cn": "nightly译文", "status": "existing"},
            {"uid": "g:untranslated", "existing_cn": "nightly 的译文", "status": "existing"},
            {"uid": "g:both-untranslated", "existing_cn": "", "status": "new"},
            {"uid": "g:missing", "existing_cn": "nightly补充", "status": "existing"},
        ]

        items = extract.add_fallback_items(primary, nightly)

        self.assertEqual(
            items,
            [
                {"uid": "g:translated", "existing_cn": "主包译文", "status": "existing"},
                {"uid": "g:untranslated", "existing_cn": "nightly 的译文", "status": "existing"},
                {"uid": "g:both-untranslated", "existing_cn": "", "status": "new"},
                {"uid": "g:missing", "existing_cn": "nightly补充", "status": "existing"},
            ],
        )


class PackageRegressionTests(unittest.TestCase):
    def test_build_version_uses_ci_override(self):
        with patch.dict("os.environ", {"BUILD_VERSION": "nightly-2026-07-23"}):
            self.assertEqual(build.build_version(), "nightly-2026-07-23")

    def test_package_includes_local_files_directory_entry(self):
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
        extract_data = [
            {"uid": "new", "status": "new"},
            {"uid": "changed", "status": "changed"},
            {"uid": "existing", "status": "existing"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "custom" / "changed.json"
            count = export.export_items(extract_data, "changed", output)
            self.assertEqual(count, 1)
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8")), [extract_data[1]]
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

    def test_key_copy_is_new_with_empty_existing_cn_and_translated_is_existing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_generic(
                root,
                {
                    "こんにちは": "こんにちは",
                    "こんばんは": "晚上好",
                    "おやすみ": "",
                    "【カクシタワタシ】特訓段階": "【カクシタワタシ】特训阶段",
                    "青空に\r\n": "青空に\n蓝天下\r\n",
                    "一つの夢": "一つの夢",
                    "[__split__]倍！": "[__split__]倍！",
                },
            )
            items = extract_generic_text(root / "local-files" / "genericTrans")

        by_field = {item["field"]: item for item in items}
        self.assertEqual(by_field["こんにちは"]["status"], "new")
        self.assertEqual(by_field["こんにちは"]["existing_cn"], "")
        self.assertEqual(by_field["おやすみ"]["status"], "new")
        self.assertEqual(by_field["こんばんは"]["status"], "existing")
        self.assertEqual(by_field["こんばんは"]["existing_cn"], "晚上好")
        self.assertEqual(by_field["【カクシタワタシ】特訓段階"]["status"], "existing")
        self.assertEqual(by_field["青空に\r\n"]["status"], "existing")
        self.assertEqual(by_field["一つの夢"]["status"], "new")
        self.assertEqual(by_field["[__split__]倍！"]["status"], "existing")
        self.assertTrue(
            all(item["file"] == "local-files/genericTrans/sample.json" for item in items)
        )

    def test_build_generic_writes_existing_and_newly_added_keys_and_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "local-files" / "genericTrans" / "sample.json"
            target.parent.mkdir(parents=True)
            target.write_text(
                json.dumps({"こんにちは": "こんにちは"}, ensure_ascii=False),
                encoding="utf-8",
            )
            count = build.apply_generic(
                root,
                [
                    {
                        "uid": "generic:local-files/genericTrans/sample.json:こんにちは",
                        "category": "generic",
                        "file": "local-files/genericTrans/sample.json",
                        "field": "こんにちは",
                        "cn": "你好",
                    },
                    {
                        "uid": "generic:local-files/genericTrans/sample.json:新キー",
                        "category": "generic",
                        "file": "local-files/genericTrans/sample.json",
                        "field": "新キー",
                        "cn": "新键译文",
                    },
                    {
                        "uid": "generic:local-files/genericTrans/nightly_only.json:追加",
                        "category": "generic",
                        "file": "local-files/genericTrans/nightly_only.json",
                        "field": "追加",
                        "cn": "补充文件译文",
                    },
                ],
            )
            data = json.loads(target.read_text(encoding="utf-8"))
            extra = json.loads(
                (root / "local-files" / "genericTrans" / "nightly_only.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(count, 3)
        self.assertEqual(data, {"こんにちは": "你好", "新キー": "新键译文"})
        self.assertEqual(extra, {"追加": "补充文件译文"})
        self.assertFalse((root / "local-files" / "local-files").exists())


class LocalizationBuildRegressionTests(unittest.TestCase):
    def test_build_localization_writes_flat_and_nested_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "local-files" / "localization.json"
            target.parent.mkdir(parents=True)
            target.write_text(
                json.dumps({"a.b": "旧", "nested": {"c": "旧2"}}, ensure_ascii=False),
                encoding="utf-8",
            )
            count = build.apply_localization(
                root,
                [
                    {"uid": "localization:a.b", "category": "localization", "file": "localization.json", "field": "a.b", "cn": "新"},
                    {"uid": "localization:nested.c", "category": "localization", "file": "localization.json", "field": "nested.c", "cn": "新2"},
                    {"uid": "localization:missing.key", "category": "localization", "file": "localization.json", "field": "missing.key", "cn": "无关"},
                ],
            )
            data = json.loads(target.read_text(encoding="utf-8"))

        self.assertEqual(count, 2)
        self.assertEqual(data["a.b"], "新")
        self.assertEqual(data["nested"]["c"], "新2")


class LyricBuildRegressionTests(unittest.TestCase):
    def _apply(self, root, file_name, payload, items):
        target = root / "local-files" / "genericTrans" / file_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        build.apply_generic(root, items)
        return json.loads(target.read_text(encoding="utf-8"))

    def test_bilingual_lyrics_file_wraps_translation(self):
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
                        "cn": "蓝天下 手机送来消息",
                    }
                ],
            )
        self.assertEqual(data[key], "青空に\u3000スマホが届いた\n蓝天下 手机送来消息\r\n")

    def test_translation_only_file_keeps_value_without_source_line(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = "ラウンド1\n"
            data = self._apply(
                root,
                "index.json",
                {key: "第1轮\n"},
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
        self.assertEqual(
            translate.group_key({"category": "resource", "file": "a.txt"}),
            "resource:a.txt",
        )
        self.assertEqual(
            translate.group_key({"category": "master", "file": "m.json"}),
            "master:m.json",
        )

    def test_batches_never_mix_files_and_respect_char_budget(self):
        items = [
            {"uid": "b:2:text", "category": "resource", "file": "b.txt", "line": 2, "field": "text", "jp": "短"},
            {"uid": "a:2:text", "category": "resource", "file": "a.txt", "line": 2, "field": "text", "jp": "あ" * 40},
            {"uid": "a:1:text", "category": "resource", "file": "a.txt", "line": 1, "field": "text", "jp": "い" * 40},
        ]
        batches = translate.build_batches(items, 20, max_batch_chars=50)
        a_batches = [batch for batch in batches if batch[0]["file"] == "a.txt"]
        self.assertEqual([[it["uid"] for it in b] for b in a_batches], [["a:1:text"], ["a:2:text"]])

    def test_context_items_provide_bilingual_refs_and_exclude_untranslated_jp(self):
        all_extract = [
            {
                "uid": "g:done",
                "category": "generic",
                "file": "g.json",
                "field": "done",
                "jp": "選抜試験",
                "existing_cn": "选拔考试",
                "status": "existing",
            },
            {
                "uid": "g:pending",
                "category": "generic",
                "file": "g.json",
                "field": "pending",
                "jp": "最終試験に挑もう",
                "existing_cn": "",
                "status": "new",
            },
        ]
        pending = [it for it in all_extract if it["status"] == "new"]
        batch = translate.build_batches(pending, 5, context_items=all_extract)[0]

        self.assertEqual(batch.refs, [("選抜試験", "选拔考试")])
        prompt = translate.build_contextual_prompt(batch)
        self.assertIn("- 選抜試験 → 选拔考试", prompt)
        # Ensure reference UIDs are never printed as `[hash]` labels.
        self.assertNotIn(f"[{uid_label('g:done')}]", prompt)

    def test_non_positive_batch_size_is_rejected(self):
        with self.assertRaises(ValueError):
            translate.build_batches([], 0)
        with self.assertRaises(ValueError):
            translate.build_batches([], -3)


class ExtractLabelTests(unittest.TestCase):
    def test_items_are_tagged_with_the_label_the_translate_stage_uses(self):
        items = [
            {"uid": "adv_x:1:text"},
            {"uid": "generic:f:Lyrics: A\nComposer: B\n"},
        ]
        extract.tag_labels(items)
        labels = [item["hash"] for item in items]
        self.assertEqual(len(set(labels)), 2)
        for item in items:
            self.assertEqual(len(item["hash"]), 16)
            int(item["hash"], 16)
            self.assertEqual(translate.item_label(item), item["hash"])


class TranslatePromptTests(unittest.TestCase):
    def test_prompt_uses_batch_scene_context_and_chinese_speaker(self):
        group = [
            {"uid": "a:1:text", "category": "resource", "file": "adv_unit_01-01_01.txt", "jp": "おはよう", "field": "text"},
            {
                "uid": "a:2:text",
                "category": "resource",
                "file": "adv_unit_01-01_01.txt",
                "jp": "おはよう",
                "field": "text",
                "speaker": "ことね",
                "file_context": {"story_type": "unit", "character": "hski", "chapter": "01-01_01"},
            },
        ]
        prompt = translate.build_contextual_prompt(group)
        self.assertIn("文件: adv_unit_01-01_01.txt", prompt)
        self.assertIn("角色: 花海咲季", prompt)
        self.assertIn("场景类型: 团体", prompt)
        self.assertIn("章节: 01-01_01", prompt)
        self.assertIn("（藤田琴音） おはよう", prompt)
        self.assertIn("不要写进译文", prompt)

    def test_lyric_row_shows_label_without_repeating_the_key(self):
        item = {
            "uid": "generic:f:あのね。\r\n",
            "hash": "abcd1234abcd1234",
            "category": "generic",
            "file": "f.json",
            "field": "あのね。\r\n",
            "jp": "あのね。\r\n",
        }
        prompt = translate.build_contextual_prompt([item])
        self.assertIn("[abcd1234abcd1234] あのね。", prompt)
        self.assertNotIn("（あのね。", prompt)

    def test_prompt_carries_simplified_glossary_and_field_hints(self):
        prompt = translate.build_contextual_prompt(
            [
                {"uid": "master:m:1:name", "category": "master", "file": "m.json", "field": "name", "jp": "原文"},
                {"uid": "master:m:1:regexp", "category": "master", "file": "m.json", "field": "regexp", "jp": "麻央$"},
            ]
        )
        self.assertIn("筱泽广", prompt)
        self.assertIn("秦谷美铃", prompt)
        self.assertIn("姬崎莉波", prompt)
        self.assertIn("- name：名称", prompt)
        self.assertIn("- regexp：正则匹配片段", prompt)


class TranslateParseTests(unittest.TestCase):
    def setUp(self):
        self.group = [
            {"uid": "a:1:text", "hash": "1a2b"},
            {"uid": "b:2:text", "hash": "3c4d"},
        ]

    def test_labeled_entries_are_parsed(self):
        content = _json_translations(("1a2b", "你好"), ("3c4d", "世界"))
        self.assertEqual(
            translate.parse_translations(content, self.group),
            {"a:1:text": "你好", "b:2:text": "世界"},
        )

    def test_single_code_fence_and_bare_array_are_tolerated(self):
        fenced = "```json\n" + _json_translations(("1a2b", "你好")) + "\n```"
        self.assertEqual(translate.parse_translations(fenced, self.group), {"a:1:text": "你好"})
        bare = json.dumps([{"id": "3c4d", "translation": "世界"}], ensure_ascii=False)
        self.assertEqual(translate.parse_translations(bare, self.group), {"b:2:text": "世界"})

    def test_altered_duplicate_or_unknown_labels_are_rejected(self):
        content = _json_translations(("[1a2b]", "你好"), ("c9c9", "未知"), ("3c4d", "世界"))
        self.assertEqual(translate.parse_translations(content, self.group), {"b:2:text": "世界"})
        dup = _json_translations(("1a2b", "你好"), ("1a2b", "再会"), ("3c4d", "世界"))
        self.assertEqual(translate.parse_translations(dup, self.group), {"b:2:text": "世界"})


class TranslateNewlineAndValidationTests(unittest.TestCase):
    def _resource(self, jp, field="text"):
        return {"uid": "adv_x:1:text", "category": "resource", "jp": jp, "field": field}

    def test_json_newline_escape_is_rewritten_to_source_marker(self):
        item = self._resource(r"登場人物は\n麻央さん")
        self.assertEqual(
            translate.normalize_newline_form(item, "登场人物是\n麻央"),
            r"登场人物是\n麻央",
        )

    def test_validation_catches_unescaped_brackets_and_tag_drops_in_resource(self):
        item = self._resource("選択してください", field="text[0]")
        self.assertIsNotNone(translate.validate_translation(item, "[选项] 请选择"))
        self.assertIsNone(translate.validate_translation(item, "【选项】请选择"))

        ruby = self._resource(r"『<r\=よみ>語</r>』")
        self.assertIsNotNone(translate.validate_translation(ruby, "《词》"))
        self.assertIsNone(translate.validate_translation(ruby, r"『<r\=よみ>词</r>』"))

    def test_lyric_response_stripping(self):
        item = {
            "uid": "generic:file:青空に　スマホが届いた\r\n",
            "category": "generic",
            "jp": "青空に　スマホが届いた\r\n",
            "field": "青空に　スマホが届いた\r\n",
        }
        self.assertEqual(
            translate.strip_lyric_source_line(
                item, "青空に スマホが届いた\n蓝天下 手机送来消息\r\n"
            ),
            "蓝天下 手机送来消息",
        )


class TranslateGroupRetryTests(unittest.TestCase):
    @staticmethod
    def _item(uid, jp):
        return {
            "uid": uid,
            "category": "resource",
            "file": "adv_x.txt",
            "line": 1,
            "field": "text",
            "jp": jp,
            "status": "new",
        }

    def test_invalid_item_is_repaired_in_targeted_request(self):
        backend = _ScriptedBackend(
            [
                _uid_translations(("a:1:text", "早上好")),
                _uid_translations(("b:2:text", "晚上好")),
            ]
        )
        group = [self._item("a:1:text", "おはよう"), self._item("b:2:text", "こんばんは")]

        result = translate.translate_group(group, backend=backend)

        self.assertEqual(result, {"a:1:text": "早上好", "b:2:text": "晚上好"})
        self.assertEqual(len(backend.prompts), 2)
        self.assertIn(f"[{uid_label('b:2:text')}] 失败原因", backend.prompts[1])
        self.assertNotIn(f"[{uid_label('a:1:text')}]", backend.prompts[1])

    def test_truncated_output_salvages_prefix_and_splits_remaining(self):
        partial_json = (
            '{"translations":[{"id":"'
            + uid_label("a:1:text")
            + '","translation":"早上好"},{"id":"'
            + uid_label("b:2:text")
            + '","translat'
        )
        backend = _ScriptedBackend(
            [
                OutputTruncatedError("truncated", partial_content=partial_json),
                _uid_translations(("b:2:text", "晚上好")),
                _uid_translations(("c:3:text", "晚安")),
            ]
        )
        group = [
            self._item("a:1:text", "おはよう"),
            self._item("b:2:text", "こんばんは"),
            self._item("c:3:text", "おやすみ"),
        ]

        result = translate.translate_group(group, backend=backend)

        self.assertEqual(
            result,
            {"a:1:text": "早上好", "b:2:text": "晚上好", "c:3:text": "晚安"},
        )
        # First item was salvaged from the truncated response and never re-requested!
        for retry_prompt in backend.prompts[1:]:
            self.assertNotIn(f"[{uid_label('a:1:text')}]", retry_prompt)

    def test_oversize_batch_binary_splits_without_resending_full_prompt(self):
        backend = _ScriptedBackend(
            [
                ContextOversizeError("HTTP 413"),
                _uid_translations(("a:1:text", "早上好"), ("b:2:text", "中午好")),
                _uid_translations(("c:3:text", "晚上好"), ("d:4:text", "晚安")),
            ]
        )
        group = [
            self._item("a:1:text", "おはよう"),
            self._item("b:2:text", "こんにちは"),
            self._item("c:3:text", "こんばんは"),
            self._item("d:4:text", "おやすみ"),
        ]

        result = translate.translate_group(group, backend=backend)

        self.assertEqual(len(result), 4)
        self.assertEqual(len(backend.prompts), 3)

    def test_fatal_llm_error_propagates_immediately_without_per_item_storm(self):
        backend = _ScriptedBackend([FatalLLMError("HTTP 400: unsupported model")])
        group = [self._item(f"a:{i}:text", "おはよう") for i in range(10)]

        with self.assertRaises(FatalLLMError):
            translate.translate_group(group, backend=backend)
        self.assertEqual(len(backend.prompts), 1)


class TranslateIncrementalTests(unittest.TestCase):
    def _prepare(self, items, prior=None, checkpoint_jsonl=None, skip_changed=True):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        paths = _temp_paths(root)
        paths["cache_dir"].mkdir(parents=True)
        (paths["cache_dir"] / "extract.json").write_text(
            json.dumps(items, ensure_ascii=False), encoding="utf-8"
        )
        if prior is not None:
            (paths["cache_dir"] / "translated.json").write_text(
                json.dumps(prior, ensure_ascii=False), encoding="utf-8"
            )
        if checkpoint_jsonl is not None:
            translate.append_checkpoint(paths["cache_dir"], checkpoint_jsonl)
        cfg = {
            "character_names": {},
            "llm": {"skip_changed": skip_changed, "batch_size": 20, "max_concurrent": 2},
        }
        return cfg, paths

    @staticmethod
    def _changed():
        return {
            "uid": "adv_x:1:text",
            "category": "resource",
            "file": "adv_x.txt",
            "line": 1,
            "field": "text",
            "jp": "新しい原文",
            "existing_cn": "旧模组译文",
            "status": "changed",
        }

    @staticmethod
    def _new(uid, line, jp):
        return {
            "uid": uid,
            "category": "resource",
            "file": "adv_x.txt",
            "line": line,
            "field": "text",
            "jp": jp,
            "existing_cn": "",
            "status": "new",
        }

    def test_changed_item_uses_prior_translation_when_skipping(self):
        cfg, paths = self._prepare(
            [self._changed()],
            prior=[{"uid": "adv_x:1:text", "cn": "上次生成的译文"}],
            skip_changed=True,
        )
        has_work = translate.run(cfg, paths, backend=_ScriptedBackend([]))
        self.assertFalse(has_work)
        written = json.loads((paths["cache_dir"] / "translated.json").read_text(encoding="utf-8"))
        self.assertEqual(written[0]["cn"], "上次生成的译文")

    def test_jsonl_checkpoint_roundtrip_and_corrupt_line_protection(self):
        _, paths = self._prepare([])
        cache_dir = paths["cache_dir"]
        translate.append_checkpoint(cache_dir, {"a:1:text": "你好"})
        translate.append_checkpoint(cache_dir, {"b:2:text": "世界"})
        self.assertEqual(
            translate.load_checkpoint(cache_dir),
            {"a:1:text": "你好", "b:2:text": "世界"},
        )

        jsonl_fp = cache_dir / "translate_checkpoint.jsonl"
        with open(jsonl_fp, "a", encoding="utf-8") as fh:
            fh.write("{corrupt_line\n")
        with self.assertRaises(RuntimeError):
            translate.load_checkpoint(cache_dir)
        self.assertIn("{corrupt_line", jsonl_fp.read_text(encoding="utf-8"))

    def test_failed_item_preserves_jsonl_checkpoint_and_recovers_next_run(self):
        items = [
            self._new("adv_x:1:text", 1, "おはよう"),
            self._new("adv_x:2:text", 2, "こんばんは"),
        ]
        cfg, paths = self._prepare(items)
        backend1 = _ScriptedBackend(
            [
                _uid_translations(("adv_x:1:text", "早上好")),
                "",
                _uid_translations(("adv_x:2:text", " ")),
            ]
        )
        with self.assertRaises(RuntimeError):
            translate.run(cfg, paths, backend=backend1)

        self.assertEqual(
            translate.load_checkpoint(paths["cache_dir"]),
            {"adv_x:1:text": "早上好"},
        )

        backend2 = _ScriptedBackend([_uid_translations(("adv_x:2:text", "晚上好"))])
        translate.run(cfg, paths, backend=backend2)
        written = json.loads((paths["cache_dir"] / "translated.json").read_text(encoding="utf-8"))
        self.assertEqual([item["cn"] for item in written], ["早上好", "晚上好"])
        self.assertFalse((paths["cache_dir"] / "translate_checkpoint.jsonl").exists())


class PipelineEndToEndTests(unittest.TestCase):
    """End-to-end Stage 2 -> Stage 3 -> Stage 4 -> Stage 5 with 3-layer overlay merge."""

    def test_full_pipeline_merges_nightly_mod_and_new_master_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = _temp_paths(root)
            (paths["server_cache"] / "res_raw").mkdir(parents=True)
            (paths["server_cache"] / "res_raw" / "adv_test.txt").write_text(
                "[title title=プロローグ clip=none]\n"
                r'[message text=おはよう！ name=ことね clip=\{"_startTime":0.0,"_duration":1.0\}]' "\n"
                r'[choicegroup choices=[choice text=はい] choices=[choice text=いいえ] clip=\{"_startTime":1.0,"_duration":1.0\}]' "\n"
                "[timeline]\n",
                encoding="utf-8",
            )

            # Upstream MOD has id1, but lacks newly added id2 in unit.yaml AND lacks nightly_extra.json.
            mod = paths["mod_cache"]
            (mod / "local-files" / "resource").mkdir(parents=True)
            (mod / "local-files" / "masterTrans").mkdir(parents=True)
            (mod / "local-files" / "genericTrans").mkdir(parents=True)
            (mod / "local-files" / "masterTrans" / "unit.json").write_text(
                json.dumps(
                    {"rules": {"primaryKeys": ["id"]}, "data": [{"id": "id1", "name": "上游已有"}]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (mod / "local-files" / "genericTrans" / "x.json").write_text(
                json.dumps({"こんにちは": "こんにちは", "共通": "上游优先译文"}, ensure_ascii=False),
                encoding="utf-8",
            )
            (mod / "local-files" / "localization.json").write_text(
                json.dumps({"a.b": "こんにちは"}, ensure_ascii=False),
                encoding="utf-8",
            )

            # Nightly fallback has an extra generic file and an older value for "共通".
            nightly = paths["nightly_mod_cache"]
            (nightly / "local-files" / "genericTrans").mkdir(parents=True)
            (nightly / "local-files" / "genericTrans" / "x.json").write_text(
                json.dumps({"共通": "nightly旧译文"}, ensure_ascii=False),
                encoding="utf-8",
            )
            (nightly / "local-files" / "genericTrans" / "nightly_only.json").write_text(
                json.dumps({"メニュー": "菜单"}, ensure_ascii=False),
                encoding="utf-8",
            )

            # Master YAML adds brand-new record id2!
            paths["gkm_diff"].mkdir(parents=True)
            (paths["gkm_diff"] / "unit.yaml").write_text(
                "- id: id1\n  name: 既存\n- id: id2\n  name: 新ユニット\n",
                encoding="utf-8",
            )

            cfg = load_config()
            cfg["llm"]["max_concurrent"] = 2

            extracted = extract.run(cfg, paths)
            new_uids = {it["uid"] for it in extracted if it["status"] == "new"}
            self.assertIn("master:unit:id2:name", new_uids)

            stub_backend = _UidMapBackend(
                {
                    "adv_test:1:text": "序章",
                    "adv_test:2:text": "早上好！",
                    "adv_test:3:text[0]": "是",
                    "adv_test:3:text[1]": "否",
                    "master:unit:id2:name": "新组合",
                    "generic:local-files/genericTrans/x.json:こんにちは": "你好",
                    "localization:a.b": "你好世界",
                }
            )
            translate.run(cfg, paths, backend=stub_backend)
            out_dir = build.run(cfg, paths)
            zip_fp = package.run(cfg, paths)

            self.assertTrue(zip_fp.exists())
            resource_out = (out_dir / "local-files" / "resource" / "adv_test.txt").read_text(encoding="utf-8")
            self.assertIn(r"[title title=<r\=プロローグ>序章</r> clip=none]", resource_out)
            self.assertIn(r"<r\=おはよう！>早上好！</r> name=藤田琴音", resource_out)
            self.assertIn("text=是", resource_out)

            # Verify new Master row id2 was inserted alongside upstream id1!
            master_out = json.loads(
                (out_dir / "local-files" / "masterTrans" / "unit.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                master_out["data"],
                [{"id": "id1", "name": "上游已有"}, {"id": "id2", "name": "新组合"}],
            )

            # Verify 3-layer overlay kept nightly_only.json while MOD beat nightly on "共通"!
            generic_x = json.loads(
                (out_dir / "local-files" / "genericTrans" / "x.json").read_text(encoding="utf-8")
            )
            self.assertEqual(generic_x, {"こんにちは": "你好", "共通": "上游优先译文"})
            nightly_only = json.loads(
                (out_dir / "local-files" / "genericTrans" / "nightly_only.json").read_text(encoding="utf-8")
            )
            self.assertEqual(nightly_only, {"メニュー": "菜单"})

            loc_out = json.loads((out_dir / "local-files" / "localization.json").read_text(encoding="utf-8"))
            self.assertEqual(loc_out["a.b"], "你好世界")

    def test_build_resource_validation_gate_rejects_corrupted_script(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server_res = root / "res_raw"
            server_res.mkdir()
            (server_res / "adv_bad.txt").write_text(
                r'[message text=こんにちは {user} name=咲季 clip=\{"_startTime":0.0,"_duration":1.0\}]' "\n[timeline]\n",
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError):
                build.build_resource_file(
                    "adv_bad.txt",
                    [{"line": 1, "field": "text", "cn": "丢失占位符译文"}],
                    server_res_dir=server_res,
                    mod_dir=root / "mod",
                    out_res_dir=root / "out_res",
                    validate=True,
                )


class LLMConfigAndBackendTests(unittest.TestCase):
    def _write_config(self, root, content="llm:\n  base_url: https://example.invalid\n"):
        path = root / "config.yaml"
        path.write_text(content, encoding="utf-8")
        return path

    def test_env_overrides_and_explicit_cache_dir(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_config(Path(directory), "llm:\n  temperature: 0.1\n  skip_changed: true\n")
            with patch.dict(
                "os.environ",
                {
                    "LLM_TEMPERATURE": "0.7",
                    "LLM_REASONING_EFFORT": "high",
                    "LLM_SKIP_CHANGED": "false",
                    "PATHS_CACHE_DIR": "/tmp/custom_gkm_cache",
                    "PATHS_SERVER_CACHE": "/tmp/other_disk/server",
                },
            ):
                config = load_config(str(path))
                resolved = resolve_paths(config)

        self.assertEqual(config["llm"]["temperature"], 0.7)
        self.assertEqual(config["llm"]["reasoning_effort"], "high")
        self.assertFalse(config["llm"]["skip_changed"])
        self.assertEqual(resolved["cache_dir"], Path("/tmp/custom_gkm_cache"))
        self.assertEqual(resolved["server_cache"], Path("/tmp/other_disk/server"))
        self.assertEqual(config["character_names"]["shro"], "筱泽广")
        self.assertEqual(config["github"]["diff_branch"], "main")

    def test_openai_backend_params_truncation_and_retry(self):
        calls = []

        class FakeSession:
            def request(self, method, url, headers=None, json=None, timeout=None):
                calls.append((method, url, json))
                if len(calls) == 1:
                    return SimpleNamespace(
                        status_code=429,
                        headers={"Retry-After": "0"},
                        text="rate limit",
                        close=lambda: None,
                    )
                return SimpleNamespace(
                    status_code=200,
                    headers={},
                    text="",
                    json=lambda: {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]},
                    close=lambda: None,
                )

            def close(self):
                pass

        backend = OpenAIBackend(
            {
                "base_url": "https://example.invalid/v1",
                "api_key": "secret",
                "model": "m",
                "max_tokens": 123,
                "reasoning_effort": "low",
                "max_retries": 2,
                "timeout": 5,
            },
            session=FakeSession(),
        )
        self.assertEqual(backend.translate("hi"), "ok")
        self.assertEqual(len(calls), 2)
        payload = calls[-1][2]
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertNotIn("temperature", payload)
        self.assertEqual(payload["response_format"]["type"], "json_schema")

    def test_openai_backend_raises_output_truncated_on_length_finish_reason(self):
        class TruncatedSession:
            def request(self, *args, **kwargs):
                return SimpleNamespace(
                    status_code=200,
                    headers={},
                    text="",
                    json=lambda: {
                        "choices": [
                            {
                                "finish_reason": "length",
                                "message": {"content": '{"translations":[{"id":"1a","trans'},
                            }
                        ]
                    },
                    close=lambda: None,
                )

        backend = OpenAIBackend(
            {"base_url": "https://example.invalid/v1", "api_key": "k", "model": "m"},
            session=TruncatedSession(),
        )
        with self.assertRaises(OutputTruncatedError) as cm:
            backend.translate("hi")
        self.assertIn("translations", cm.exception.partial_content)


class ScriptCheckerRegressionTests(unittest.TestCase):
    def test_extract_and_build_preserve_text_starting_with_user_placeholder(self):
        line = (
            r"[message text={user}くん。\n先輩と呼んでくれたまえ？ name=麻央 "
            r'clip=\{"_startTime":106.9,"_duration":3.6\}]'
        )
        with tempfile.TemporaryDirectory() as directory:
            fp = Path(directory) / "adv_amao.txt"
            fp.write_text(line + "\n", encoding="utf-8")
            extracted = extract_resource_text(fp)

        self.assertEqual(
            extracted,
            [
                {
                    "line": 1,
                    "command": "message",
                    "field": "text",
                    "jp": r"{user}くん。\n先輩と呼んでくれたまえ？",
                },
                {"line": 1, "command": "message", "field": "name", "jp": "麻央"},
            ],
        )
        built = build_resource_line(
            line,
            {"text": r"{user}君。\n请叫我前辈吧？", "name": "有村麻央"},
        )
        self.assertEqual(
            built,
            r"[message text=<r\={user}くん。>{user}君。</r>\r\n"
            r"<r\=先輩と呼んでくれたまえ？>请叫我前辈吧？</r> name=有村麻央 "
            r'clip=\{"_startTime":106.9,"_duration":3.6\}]',
        )

    def test_valid_translated_script_passes_and_builds_timeline_summary(self):
        source = "\n".join(
            [
                "[backgroundgroup backgrounds=[background id=classroom src=env_3d_adv_classroom-00-00-noon]]",
                "[actorgroup actors=[actor id=amao body=mdl_chr_amao-schl-0000_body face=mdl_chr_amao-base-0000_face hair=mdl_chr_amao-base-0000_hair]]",
                r'[actorlayoutgroup layouts=[actorlayout id=amao transform=\{"position":\{"x":0.0,"y":0.0,"z":0.0\}\}] clip=\{"_startTime":0.0,"_duration":0.0\}]',
                r'[message text={user}くん、『<r\=セレクション>選抜試験</r>』\nお疲れ様。 name=麻央 se=sud_se_adv_message-01 clip=\{"_startTime":0.8,"_duration":3.2\}]',
                r'[voice voice=sud_vo_adv_dear_amao_001_amao-001 actorId=amao channel=1 clip=\{"_startTime":0.9,"_duration":3.0\}]',
                r'[actormotion id=amao motion=mot_all_chr_cmmn_talk-001_in transition=0.7 clip=\{"_startTime":0.8,"_duration":2.0\}]',
                r'[choicegroup choices=[choice text=ありがとう hideMessage=true] choices=[choice text=頑張ろう] clip=\{"_startTime":4.5,"_duration":1.0\}]',
                "[branchgroup type=Choice groupLength=2]",
                "[branch groupLength=1]",
                r'[message text=どういたしまして。 name=麻央 clip=\{"_startTime":5.5,"_duration":1.5\}]',
                "[branch groupLength=0]",
                "[timeline]",
            ]
        )
        translated = "\n".join(
            [
                "[backgroundgroup backgrounds=[background id=classroom src=env_3d_adv_classroom-00-00-noon]]",
                "[actorgroup actors=[actor id=amao body=mdl_chr_amao-schl-0000_body face=mdl_chr_amao-base-0000_face hair=mdl_chr_amao-base-0000_hair]]",
                r'[actorlayoutgroup layouts=[actorlayout id=amao transform=\{"position":\{"x":0.0,"y":0.0,"z":0.0\}\}] clip=\{"_startTime":0.0,"_duration":0.0\}]',
                r'[message text=<r\={user}くん、『<r\=セレクション>選抜試験</r>』>{user}君，“<r\=セレクション>选拔考试</r>”</r>\r\n<r\=お疲れ様。>辛苦了。</r> name=有村麻央 se=sud_se_adv_message-01 clip=\{"_startTime":0.8,"_duration":3.2\}]',
                r'[voice voice=sud_vo_adv_dear_amao_001_amao-001 actorId=amao channel=1 clip=\{"_startTime":0.9,"_duration":3.0\}]',
                r'[actormotion id=amao motion=mot_all_chr_cmmn_talk-001_in transition=0.7 clip=\{"_startTime":0.8,"_duration":2.0\}]',
                r'[choicegroup choices=[choice text=谢谢你 hideMessage=true] choices=[choice text=一起加油吧] clip=\{"_startTime":4.5,"_duration":1.0\}]',
                "[branchgroup type=Choice groupLength=2]",
                "[branch groupLength=1]",
                r'[message text=<r\=どういたしまして。>不客气。</r> name=有村麻央 clip=\{"_startTime":5.5,"_duration":1.5\}]',
                "[branch groupLength=0]",
                "[timeline]",
            ]
        )

        report = check_script_text(
            translated,
            "adv_dear_amao_001.txt",
            source_text=source,
            check_untranslated=True,
        )
        self.assertTrue(report["ok"], report["issues"])
        self.assertEqual(report["error_count"], 0)
        self.assertEqual(report["warning_count"], 0)

    def test_checker_detects_syntax_markup_and_wrapper_errors(self):
        broken = "\n".join(
            [
                "not_a_bracketed_command",
                "[unknowncmd foo=bar]",
                "[actor id=amao body=b face=f hair=h]",
                r'[message text=<r=よみ>語</r> name=咲季 clip=\{"_startTime":-1.0,"_duration":1.0\}]',
                r'[message text=<r\=おはよう>早上好 name=咲季 clip=\{"_startTime":1.0,"_duration":1.0\}]',
                r'[message text=<r\=こんにちは {user}>你好</r> name=咲季 clip=\{"_startTime":2.0,"_duration":1.0\}]',
                r'[message text=<r\=テスト></r> name=咲季 clip=\{"_startTime":3.0,"_duration":1.0\}]',
                r'[choicegroup choices=[choice text=<r\=独占したかったから]>因为我想独占你</r>] clip=none]',
                r'[fade clip=\{"_startTime":1.0]',
            ]
        )
        report = check_script_text(broken, "adv_broken.txt")
        self.assertFalse(report["ok"])
        codes = {issue["code"] for issue in report["issues"]}
        self.assertIn("unbracketed_line", codes)
        self.assertIn("unknown_command", codes)
        self.assertIn("nested_command_at_top", codes)
        self.assertIn("unescaped_tag_equals", codes)
        self.assertIn("invalid_clip_timing", codes)
        self.assertIn("unbalanced_ruby_tag", codes)
        self.assertIn("wrapper_placeholder_mismatch", codes)
        self.assertIn("empty_translation_text", codes)
        self.assertIn("unclosed_json", codes)

    def test_check_txt_cli_tool_exit_codes_and_json_report(self):
        check_tool = _load_tool("check_txt")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid_fp = root / "adv_valid.txt"
            valid_fp.write_text(
                r'[message text=<r\=おはよう>早上好</r> name=咲季 clip=\{"_startTime":0.0,"_duration":1.0\}]'
                "\n[timeline]\n",
                encoding="utf-8",
            )
            json_out = root / "report.json"
            self.assertEqual(check_tool.main([str(valid_fp), "--json", str(json_out)]), 0)
            bad_fp = root / "adv_bad.txt"
            bad_fp.write_text("[message text=<r=bad>x</r> clip=none]\n", encoding="utf-8")
            self.assertEqual(check_tool.main([str(bad_fp)]), 1)


if __name__ == "__main__":
    unittest.main()
