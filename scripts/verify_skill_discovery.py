"""Read Codex's installed skill catalogue, without creating an inference turn."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
from pathlib import Path

from personal_search.storage import atomic_json


async def verify(root, report_dir=None):
    report_dir = report_dir or root / "docs/research-skill"
    report_dir.mkdir(parents=True, exist_ok=True)
    executable = shutil.which("codex")
    if not executable:
        raise RuntimeError("Codex CLI unavailable")
    process = await asyncio.create_subprocess_exec(
        executable,
        "app-server",
        "--listen",
        "stdio://",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )

    async def send(value):
        process.stdin.write((json.dumps(value) + "\n").encode())
        await process.stdin.drain()

    async def request(identifier, method, params):
        await send({"id": identifier, "method": method, "params": params})
        async with asyncio.timeout(30):
            while True:
                line = await process.stdout.readline()
                if not line:
                    raise RuntimeError("Codex app-server closed before returning the catalogue")
                value = json.loads(line)
                if value.get("id") == identifier:
                    if "error" in value:
                        raise RuntimeError(value["error"])
                    return value["result"]

    try:
        await request(
            1,
            "initialize",
            {
                "clientInfo": {"name": "gptch-skill-discovery-check", "version": "0.2.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        await send({"method": "initialized"})
        response = await request(2, "skills/list", {"cwds": [str(root)], "forceReload": True})
        matched, relevant_errors = [], []
        for entry in response.get("data", []):
            matched.extend(
                s for s in entry.get("skills", []) if "private-search-research" in s.get("name", "")
            )
            relevant_errors.extend(
                e
                for e in entry.get("errors", [])
                if "gptch" in json.dumps(e, ensure_ascii=False).lower()
            )
        report = {
            "surface": "Codex app-server skills/list",
            "inference_turns_started": 0,
            "skills": matched,
            "relevant_errors": relevant_errors,
            "automatic_selection_tested": False,
            "work_ui_verified": False,
        }
        atomic_json(report_dir / "product-skill-discovery.json", report)
        assert matched, "Installed skill missing from the product catalogue"
        assert all(s.get("enabled") for s in matched)
        assert not relevant_errors
        return report
    finally:
        process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.terminate()
            await process.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-dir", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(verify(Path(__file__).resolve().parent.parent, args.report_dir)),
            ensure_ascii=False,
            indent=2,
        )
    )
