"""Package source without accounts, collected data, local config or upstream checkouts."""

import tomllib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parent.parent
version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
destination = root / "dist" / f"GPTCH-Search-MCP-v{version}-source.zip"
destination.parent.mkdir(exist_ok=True)
directories = {"src", "scripts", "tests", "docs", "plugins"}
files = [
    p
    for p in root.iterdir()
    if p.is_file()
    and (
        p.suffix in {".md", ".bat", ".command"}
        or p.name in {"pyproject.toml", "uv.lock", ".python-version", ".gitignore"}
    )
]
for directory in directories:
    files.extend(
        p
        for p in (root / directory).rglob("*")
        if p.is_file()
        and "__pycache__" not in p.parts
        and p.suffix != ".pyc"
        and p != root / "plugins" / "gptch-search" / "mcp.json"
    )
files.append(root / "references" / "manifest.json")
with ZipFile(destination, "w", compression=ZIP_DEFLATED) as archive:
    for path in sorted(set(files)):
        archive.write(path, Path("GPTCH-Search-MCP") / path.relative_to(root))
print(destination)
