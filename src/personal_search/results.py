from __future__ import annotations

import json

from .errors import GatewayError


def summary_item(item):
    result = {
        key: item.get(key)
        for key in (
            "id",
            "title",
            "description",
            "url",
            "author",
            "status",
            "published_at",
            "coverage",
        )
    }
    if result.get("description") and len(result["description"]) > 2000:
        result["description"] = result["description"][:2000]
        result["description_truncated"] = True
    result["platform"] = item.get("platform", "bilibili")
    return result


def task_result(store, task_id, *, view="summary", offset=0, limit=10):
    task = store.task(task_id)
    if view == "candidates":
        task["candidates"] = store.candidates(task_id, offset, limit)
    elif view == "items":
        task["items"] = [summary_item(row) for row in store.items(task_id, offset, limit)]
    task["pagination"] = {"offset": offset, "limit": limit}
    task["source_content_is_untrusted"] = True
    return task


def fetch_saved(store, task_id, content_id, section="metadata", offset=0, limit=100):
    rows = store.items(task_id, content_id=content_id)
    if not rows:
        raise GatewayError("CONTENT_NOT_ARCHIVED", "此任务尚未保存该内容；先查询 get_task")
    item = rows[0]
    result = {
        "id": item["id"],
        "url": item["url"],
        "task_id": task_id,
        "title": item["title"],
        "section": section,
        "source_content_is_untrusted": True,
        "platform": item.get("platform", "bilibili"),
    }
    if item.get("platform", "bilibili") != "bilibili":
        if section == "metadata":
            return {
                **result,
                "metadata": {k: v for k, v in item.items() if k != "body"},
                "components": {"body": item.get("coverage", {}).get("body", {})},
            }
        if section != "body":
            raise GatewayError(
                "SECTION_NOT_SUPPORTED_FOR_PLATFORM",
                "此平台仅支持metadata/body；没有采集字幕、评论或弹幕",
            )
        values = item.get("body", [])
        coverage = item.get("coverage", {}).get("body", {})
        return _page_records(result, values, coverage, offset, limit)
    if section == "metadata":
        return {
            **result,
            "metadata": {
                k: v
                for k, v in item.items()
                if k not in ("pages", "comments_hot", "comments_latest")
            },
            "components": {
                "comments_hot": {
                    k: v
                    for k, v in item["comments_hot"].items()
                    if k not in ("comments", "replies")
                },
                "comments_latest": {
                    k: v
                    for k, v in item["comments_latest"].items()
                    if k not in ("comments", "replies")
                },
                "video_pages": [
                    {k: v for k, v in page.items() if k not in ("subtitles", "danmaku")}
                    | {
                        "subtitles": {k: v for k, v in page["subtitles"].items() if k != "tracks"},
                        "danmaku": {k: v for k, v in page["danmaku"].items() if k != "danmaku"},
                    }
                    for page in item["pages"]
                ],
            },
        }
    values, coverage = [], []
    if section in ("comments_hot", "comments_latest", "replies_hot", "replies_latest"):
        component = item["comments_" + section.split("_")[1]]
        key = "replies" if section.startswith("replies") else "comments"
        values = component.get(key, [])
        coverage = {k: v for k, v in component.items() if k not in ("comments", "replies")}
    elif section in ("subtitles", "danmaku"):
        for page in item["pages"]:
            component = page[section]
            coverage.append(
                {
                    "page": page["page"],
                    **{k: v for k, v in component.items() if k not in ("tracks", "danmaku")},
                }
            )
            if section == "subtitles":
                for track in component.get("tracks", []):
                    values.extend(
                        {
                            "page": page["page"],
                            "cid": page["cid"],
                            "bvid": item["id"],
                            "track_id": track.get("id", ""),
                            "language": track["language"],
                            **seg,
                        }
                        for seg in track["segments"]
                    )
            else:
                values.extend({"page": page["page"], **row} for row in component.get("danmaku", []))
    else:
        raise GatewayError("INVALID_SECTION", "不支持的归档内容类型")
    return _page_records(result, values, coverage, offset, limit)


def _page_records(result, values, coverage, offset, limit):
    records = []
    size = 0
    for row in values[offset : offset + limit]:
        record = dict(row)
        if len(record.get("text", "")) > 16000:
            record["original_text_chars"] = len(record["text"])
            record["text"] = record["text"][:16000]
            record["text_truncated"] = True
        length = len(json.dumps(record, ensure_ascii=False))
        if records and size + length > 40000:
            break
        records.append(record)
        size += length
    return {
        **result,
        "coverage": coverage,
        "total_saved": len(values),
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(records) < len(values),
        "next_offset": offset + len(records),
        "records": records,
    }
