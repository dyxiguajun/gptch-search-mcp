"""Install a thin personal desktop plugin using the existing stdio gateway.

No search implementation, data migration, remote MCP endpoint or account mutation.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

NAME = "gptch-search"
MANIFEST_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def prepare_plugin(project: Path, home: Path):
    project, home = project.resolve(), home.resolve()
    source = project / "plugins" / NAME
    manifest = json.loads((source / "plugin.json").read_text(encoding="utf-8"))
    if manifest.get("$schema") != MANIFEST_SCHEMA or manifest.get("name") != NAME:
        raise ValueError("工程插件 Manifest 身份不符合预期")
    python = project / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not python.is_file():
        raise ValueError("原工程虚拟环境不存在；请先完成工程安装")
    if not (project / "storage" / "archive.db").is_file():
        raise ValueError("原 storage/archive.db 不存在；未创建替代数据库")
    mcp = {
        "$schema": MCP_SCHEMA,
        "mcpServers": {
            NAME: {
                "type": "stdio",
                "command": "cmd" if sys.platform == "win32" else "./launch.sh",
                "args": ["/d", "/c", "${PLUGIN_ROOT}/launch.cmd"]
                if sys.platform == "win32"
                else [],
                "env": {"GPTCH_DATA_DIR": str(project / "storage"), "GPTCH_PYTHON": str(python)},
                "cwd": "${PLUGIN_ROOT}",
            }
        },
    }
    write_json(source / "mcp.json", mcp)
    target = home / ".codex" / "plugins" / NAME
    marker = target / ".gptch-source.json"
    if target.exists() and any(target.iterdir()) and not marker.exists():
        raise ValueError("本机已有同名且不属于此安装器的目录；未覆盖")
    target.mkdir(parents=True, exist_ok=True)
    for filename in ("plugin.json", "mcp.json", "README.md", "launch.sh", "launch.cmd"):
        shutil.copy2(source / filename, target / filename)
    if (source / "skills").is_dir():
        shutil.copytree(
            source / "skills",
            target / "skills",
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
        )
    (target / "launch.sh").chmod(0o755)
    write_json(marker, {"plugin": NAME, "project": str(project)})
    return target


def ensure_marketplace(home: Path):
    path = home / ".agents" / "plugins" / "marketplace.json"
    catalog = (
        json.loads(path.read_text(encoding="utf-8"))
        if path.exists()
        else {
            "name": "gptch-personal",
            "interface": {"displayName": "GPTCH Local"},
            "plugins": [],
        }
    )
    if not isinstance(catalog.get("plugins"), list) or not catalog.get("name"):
        raise ValueError("个人 Marketplace 内容不符合预期；未覆盖")
    desired = {"source": "local", "path": "./.codex/plugins/gptch-search"}
    existing = next((p for p in catalog["plugins"] if p.get("name") == NAME), None)
    if existing and existing.get("source") != desired:
        raise ValueError("个人 Marketplace 已有来源不同的同名插件；未覆盖")
    if existing is None:
        catalog["plugins"].append(
            {
                "name": NAME,
                "source": desired,
                "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                "category": "Productivity",
            }
        )
    write_json(path, catalog)
    return path, catalog["name"]


def cli_json(command):
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(
            f"官方 CLI 执行失败（退出码 {result.returncode}）：{result.stderr.strip()[:500]}"
        )
    return json.loads(result.stdout)


def install(project: Path, home: Path, codex: str):
    target = prepare_plugin(project, home)
    marketplace, marketplace_name = ensure_marketplace(home)
    # Use the vendor CLI for registration/cache/config; never rewrite config.toml ourselves.
    registration = cli_json([codex, "plugin", "marketplace", "add", str(home), "--json"])
    installed = cli_json([codex, "plugin", "add", f"{NAME}@{marketplace_name}", "--json"])
    listed = cli_json([codex, "plugin", "list", "--marketplace", marketplace_name, "--json"])
    matched = [p for p in listed.get("installed", []) if p.get("name") == NAME]
    manifest = json.loads((target / "plugin.json").read_text(encoding="utf-8"))
    verified = any(p.get("version") == manifest["version"] and p.get("enabled") for p in matched)
    report = {
        "status": "installed" if verified else "installation_not_verified",
        "plugin_name": "GPTCH Search",
        "plugin_version": manifest["version"],
        "skills": sorted(p.parent.name for p in (target / "skills").glob("*/SKILL.md")),
        "marketplace": str(marketplace),
        "marketplace_name": marketplace_name,
        "source_path": str(target),
        "registration": registration,
        "installation": installed,
        "installed_entries": matched,
        "ordinary_chat_verified": False,
        "work_ui_verified": False,
        "codex_product_tool_call_verified": False,
        "restart_required": True,
    }
    write_json(project / "docs" / "desktop-integration" / "install.json", report)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", default=shutil.which("codex"))
    args = parser.parse_args()
    if not args.codex:
        parser.error("未找到官方 Codex CLI")
    print(
        json.dumps(
            install(Path(__file__).resolve().parent.parent, Path.home(), args.codex),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
