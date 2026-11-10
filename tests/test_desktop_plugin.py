import importlib.util
import json
import shutil
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "install_desktop_plugin", ROOT / "scripts/install_desktop_plugin.py"
)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
verify_spec = importlib.util.spec_from_file_location(
    "verify_desktop_plugin", ROOT / "scripts/verify_desktop_plugin.py"
)
verifier = importlib.util.module_from_spec(verify_spec)
verify_spec.loader.exec_module(verifier)


def test_personal_marketplace_merge_preserves_other_plugins_and_is_idempotent(tmp_path):
    path = tmp_path / ".agents/plugins/marketplace.json"
    path.parent.mkdir(parents=True)
    original = {
        "name": "my-personal",
        "interface": {"displayName": "My plugins"},
        "plugins": [{"name": "other", "source": "./plugins/other"}],
        "custom": {"keep": True},
    }
    path.write_text(json.dumps(original))
    _, name = installer.ensure_marketplace(tmp_path)
    installer.ensure_marketplace(tmp_path)
    updated = json.loads(path.read_text())
    assert name == original["name"] and updated["custom"] == original["custom"]
    assert updated["plugins"][0] == original["plugins"][0]
    assert len(updated["plugins"]) == 2


def test_marketplace_same_name_other_source_is_not_overwritten(tmp_path):
    path = tmp_path / ".agents/plugins/marketplace.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"name": "existing", "plugins": [{"name": "gptch-search", "source": "./other"}]})
    )
    before = path.read_bytes()
    with pytest.raises(ValueError):
        installer.ensure_marketplace(tmp_path)
    assert path.read_bytes() == before


@pytest.fixture
def plugin_project(tmp_path):
    project = tmp_path / "project with spaces"
    source = project / "plugins/gptch-search"
    source.mkdir(parents=True)
    for name in ("plugin.json", "README.md", "launch.sh", "launch.cmd"):
        (source / name).write_bytes((ROOT / "plugins/gptch-search" / name).read_bytes())
    shutil.copytree(ROOT / "plugins/gptch-search/skills", source / "skills")
    runtime = (
        project
        / ".venv"
        / ("Scripts/python.exe" if installer.sys.platform == "win32" else "bin/python")
    )
    runtime.parent.mkdir(parents=True)
    runtime.touch()
    (project / "storage").mkdir()
    database = project / "storage/archive.db"
    database.write_bytes(b"fixture original database")
    return project, runtime, database


def test_thin_plugin_uses_original_runtime_and_data_directory(tmp_path, plugin_project):
    project, runtime, database = plugin_project
    source = project / "plugins/gptch-search"
    home = tmp_path / "home"
    target = installer.prepare_plugin(project, home)
    config = json.loads((target / "mcp.json").read_text())
    server = config["mcpServers"]["gptch-search"]
    assert server["command"] == ("cmd" if installer.sys.platform == "win32" else "./launch.sh")
    assert server["env"]["GPTCH_PYTHON"] == str(runtime)
    assert server["env"]["GPTCH_DATA_DIR"] == str(project / "storage")
    assert database.read_bytes() == b"fixture original database"
    for name, value in [
        ("plugin", json.loads((target / "plugin.json").read_text())),
        ("mcp", config),
    ]:
        schema = json.loads(
            (ROOT / "docs/desktop-integration/schemas" / f"{name}.schema.json").read_text()
        )
        jsonschema.Draft202012Validator(schema).validate(value)
    installer.prepare_plugin(project, home)
    assert not (target / "storage").exists()
    for name in ("SKILL.md", "agents/openai.yaml", "references/tool-contract.md"):
        relative = Path("skills/private-search-research") / name
        assert (target / relative).read_bytes() == (source / relative).read_bytes()
    assert verifier.resolve_command(server["command"], target)


def test_existing_unknown_local_plugin_is_preserved(tmp_path, plugin_project):
    project, _, _ = plugin_project
    target = tmp_path / ".codex/plugins/gptch-search"
    target.mkdir(parents=True)
    (target / "foreign.txt").write_text("preserve")
    with pytest.raises(ValueError, match="同名"):
        installer.prepare_plugin(project, tmp_path)
    assert (target / "foreign.txt").read_text() == "preserve"


def test_plugin_command_semantics_reject_absolute_path_despite_schema(tmp_path):
    runtime = tmp_path / "python"
    runtime.touch()
    with pytest.raises(ValueError, match="bare executable"):
        verifier.resolve_command(str(runtime), tmp_path)
    with pytest.raises(ValueError):
        verifier.resolve_command("./../python", tmp_path)
    assert verifier.resolve_command("python3", tmp_path) == "python3"
