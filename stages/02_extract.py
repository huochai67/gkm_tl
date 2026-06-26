import sys, json, re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.parser_resource import extract_resource_text, parse_adv_context
from lib.parser_master import extract_master_text
from lib.parser_generic import extract_generic_text
from lib.parser_localization import extract_localization_text
from lib.config import load_config

CACHE = Path("cache")


def main():
    config = load_config()

    server_res = CACHE / "server" / "res_raw"
    mod_res = CACHE / "mod" / "local-files" / "resource"
    master_dir = CACHE / "gkm-diff"
    mod_master = CACHE / "mod" / "local-files" / "masterTrans"
    mod_generic = CACHE / "mod" / "local-files" / "genericTrans"

    all_items = []

    files = sorted(server_res.glob("adv_*.txt"))
    print(f"Processing {len(files)} resource files...", flush=True)
    for fi, fp in enumerate(files):
        if fi % 500 == 0:
            print(f"  [{fi}/{len(files)}] ...", flush=True)
        name = fp.stem
        mod_fp = mod_res / f"{name}.txt"
        server_items = extract_resource_text(fp)
        mod_items = {}
        if mod_fp.exists():
            for item in extract_resource_text(mod_fp):
                mod_items[(item["line"], item["field"])] = item["jp"]

        speaker_map = {}
        for item in server_items:
            if item["command"] == "message" and item["field"] == "name":
                speaker_map[item["line"]] = item["jp"]

        adv_ctx = parse_adv_context(name)

        for item in server_items:
            key = (item["line"], item["field"])
            existing = ""
            if key in mod_items:
                mod_val = mod_items[key]
                cn_m = re.search(r">([^<]+)</r>", mod_val)
                if cn_m:
                    existing = cn_m.group(1)
            item["uid"] = f"{name}:{item['line']}:{item['field']}"
            item["file"] = f"{name}.txt"
            item["category"] = "resource"
            item["existing_cn"] = existing
            item["status"] = "existing" if existing else "new"

            if item["field"] == "text":
                item["speaker"] = speaker_map.get(item["line"], "")
                item["file_context"] = adv_ctx

            if item["field"] == "name":
                if not item["existing_cn"]:
                    for cn_val in config["char_map"].values():
                        if cn_val and item["jp"] in cn_val:
                            item["existing_cn"] = cn_val
                            break
                    if not item["existing_cn"]:
                        item["existing_cn"] = item["jp"]
                item["status"] = "existing"

            all_items.append(item)

    print("  parsing master data...", flush=True)
    all_items += extract_master_text(master_dir, mod_master)
    print("  parsing generic data...", flush=True)
    all_items += extract_generic_text(mod_generic)
    print("  parsing localization data...", flush=True)
    all_items += extract_localization_text(CACHE / "mod" / "local-files" / "localization.json")

    (CACHE / "extract.json").write_text(json.dumps(all_items, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Extracted {len(all_items)} items ({sum(1 for i in all_items if i['status']=='new')} new)", flush=True)


if __name__ == "__main__":
    main()
