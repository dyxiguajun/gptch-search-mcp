# Adapted from the user's Bili CC Extractor (MIT), Copyright (c) 2026 dyxiguajun.
# Source: Working Space/bili-cc-extractor/backend/{bili.py,app.py}, commit 487f67d.
# Preserve LICENSE.cc-ext when redistributing.
# Changes: isolate pure helpers; pass duration explicitly instead of a FastAPI closure.
# Networking, credentials, retries and UI are supplied by the gateway adapter.

import hashlib
import re
import time


def parse_page_number(url: str) -> int:
    m = re.search(r"[?&]p=(\d+)", url)
    if not m:
        return 1
    return max(int(m.group(1)), 1)


def to_https(url: str) -> str:
    if url.startswith("//"):
        return "https:" + url
    return url.replace("http://", "https://")


def subtitle_json_to_text(sub_json: dict) -> str:
    items = sub_json.get("body", [])
    lines = []
    for it in items:
        content = (it.get("content") or "").strip()
        if content:
            lines.append(content)
    return "\n".join(lines)


def subtitle_max_to_seconds(sub_json: dict) -> float:
    body = sub_json.get("body", []) or []
    if not body:
        return 0.0
    return max(float(it.get("to", 0) or 0) for it in body)


def detect_lang_score(text: str, lang: str) -> float:
    """
    粗略判断字幕文本是否符合目标语言。
    返回 0~1 的比例，越高越像该语言。
    lang: 'ja' / 'en' / 'zh'
    """
    if not text:
        return 0.0

    s = text[:5000]  # 只看前5000字符，够用了
    total = len(s)
    if total == 0:
        return 0.0

    if lang == "ja":
        # 平假名/片假名
        m = re.findall(r"[\u3040-\u30ff]", s)
        return len(m) / total

    if lang == "en":
        m = re.findall(r"[A-Za-z]", s)
        return len(m) / total

    if lang == "zh":
        m = re.findall(r"[\u4e00-\u9fff]", s)
        return len(m) / total

    return 0.0


def ok_by_duration(sub_json: dict, chosen_track: dict, expected: float) -> bool:
    """用字幕最大时间戳和视频时长做基本一致性校验，过滤串台"""
    if not expected:
        return True

    max_to = subtitle_max_to_seconds(sub_json)

    lan = (chosen_track.get("lan") or "").lower()
    is_ai = lan.startswith("ai-") or chosen_track.get("ai_status") in (1, 2)

    if is_ai:
        # AI 字幕通常覆盖更完整：严格一些
        return (max_to >= expected * 0.85) and (max_to <= expected * 1.20)
    else:
        # UP 手动字幕可能不全：宽松一些
        return (max_to >= expected * 0.30) and (max_to <= expected * 1.50)


def subtitle_fingerprint(sub_json: dict) -> str:
    txt = subtitle_json_to_text(sub_json)
    # 只取前3000字符做指纹，够区分不同视频
    s = txt[:3000].encode("utf-8", errors="ignore")
    return hashlib.sha1(s).hexdigest()


def fresh_subtitle_url(url: str, nonce: int | None = None) -> str:
    """CC Ext's per-download cache refresh, without its synchronous HTTP client."""
    url = to_https(url).strip()
    if not url:
        raise ValueError("empty subtitle_url")
    sep = "&" if "?" in url else "?"
    nonce = int(time.time() * 1000) if nonce is None else nonce
    return f"{url}{sep}_ts={nonce}"
