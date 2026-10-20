"""Check the native production inventory and its distribution selection."""
import argparse
import json

from agent.plugins.static_manifest import load_static_plugin_manifest
from kirakira_agent.runtime import HEADLESS, ROOT
from scripts.check_upstream_kernel import verify


def inventory():
    pin = json.loads((ROOT / "UPSTREAM.json").read_text())
    modules = []
    for directory in sorted((ROOT / "plugins").iterdir()):
        if not (directory / "plugin.py").is_file():
            continue
        manifest = load_static_plugin_manifest(directory)
        relative = directory.relative_to(ROOT).as_posix()
        modules.append({"id": manifest.name, "directory": relative,
                        "api_version": manifest.api_version,
                        "upstream": relative + "/plugin.py" in pin["files"],
                        "headless_selected": manifest.name in HEADLESS,
                        "pinned_files": sum(name.startswith(relative + "/") for name in pin["files"])})
    return {"upstream_commit": pin["commit"], "native_files": len(pin["files"]),
            "scope": "production implementation and architecture; external integrations need separate configuration and validation",
            "plugins": modules}


def verify_inventory():
    count = verify()
    value = inventory()
    if json.loads((ROOT / "docs/status/plugin-inventory.json").read_text()) != value:
        raise RuntimeError("native inventory is stale; run --write")
    assert sum(row["upstream"] for row in value["plugins"]) == 70
    for path in ("agent/composition/root.py", "plugins/sources/admission.py", "plugins/reply_program/store.py",
                 "plugins/ledger/manager.py", "plugins/execution/service.py"):
        if (ROOT / path).is_file():
            raise RuntimeError("retired execution authority remains: " + path)
    return {"native_files": count, "upstream_plugins": 70,
            "distribution_plugins": len(value["plugins"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    if args.write:
        value = inventory()
        (ROOT / "docs/status/plugin-inventory.json").write_text(json.dumps(value, indent=2) + "\n")
        rows = ["# 插件组合", "", "此表记录随发行版提供的组件和默认选择；外部提供方需要独立配置与验证。", "",
                "| 插件 | 组件类型 | headless 选择 |", "| --- | --- | --- |"]
        rows.extend(f"| {row['id']} | {'运行组件' if row['upstream'] else '发行扩展'} | {'是' if row['headless_selected'] else '可选'} |" for row in value["plugins"])
        (ROOT / "docs/status/plugin-inventory.md").write_text("\n".join(rows) + "\n")
    print(json.dumps(verify_inventory()))


if __name__ == "__main__":
    main()
