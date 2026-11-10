"""Download pinned reviewed sources, without running upstream programs."""

import json
import subprocess
from pathlib import Path

project = Path(__file__).resolve().parent.parent
manifest = json.loads((project / "references" / "manifest.json").read_text(encoding="utf-8"))
for row in manifest:
    target = project / "references" / row["name"]
    if target.exists():
        print(f"保留现有仓库：{row['name']}")
        continue
    subprocess.run(
        ["git", "clone", "--no-checkout", "--filter=blob:none", row["url"], str(target)], check=True
    )
    subprocess.run(["git", "-C", str(target), "checkout", "--detach", row["commit"]], check=True)
