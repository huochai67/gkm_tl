import importlib.util
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

import lib.octo as octo_module
from lib.octo import OctoClient
from lib.parser_localization import extract_localization_text
from lib.parser_master import extract_master_text
from lib.parser_resource import build_resource_line, extract_resource_text
from lib.proto import octodb_pb2 as octop
from lib.text_utils import looks_like_japanese_source


PROJECT_ROOT = Path(__file__).parent.parent


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
            "[choicegroup text=<r\\=選択肢1>选项1</r> "
            "text=<r\\=選択肢2>选项2</r> "
            "text=<r\\=選択肢3>选项3</r>]",
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

    def test_changed_translation_is_skipped_by_default(self):
        translate = _load_stage("03_translate")
        changed = {"status": "changed"}
        new = {"status": "new"}

        self.assertFalse(translate._should_translate(changed, True))
        self.assertTrue(translate._should_translate(changed, False))
        self.assertTrue(translate._should_translate(new, True))


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
    def test_master_uses_record_index_for_duplicate_ids(self):
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

    def test_build_master_uses_record_index_for_idless_and_duplicate_records(self):
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
                        {"uid": "master:sample:0:repeated:name", "record_id": "repeated", "field": "name", "cn": "译文一"},
                        {"uid": "master:sample:1:repeated:name", "record_id": "repeated", "field": "name", "cn": "译文二"},
                        {"uid": "master:sample:2:_idx2:name", "record_id": "", "field": "name", "cn": "无 ID 译文"},
                    ],
                )
            finally:
                build.OUT = original_out
            data = json.loads(target.read_text(encoding="utf-8"))

        self.assertEqual(count, 3)
        self.assertEqual([record["name"] for record in data["data"]], ["译文一", "译文二", "无 ID 译文"])

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
                json.dumps({"master:sample:0:1:name": "旧原文"}, ensure_ascii=False),
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

    def test_master_uses_record_index_for_idless_nightly_entries(self):
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


if __name__ == "__main__":
    unittest.main()
