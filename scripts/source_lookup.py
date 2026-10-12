#!/usr/bin/env python3
"""Look up pinned source passages; cache original texts outside the skill."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "references" / "sources.json"
MAX_BYTES = 2_000_000


class SourceError(Exception):
    """A source cannot be read or verified."""


def load_registry(path=REGISTRY):
    registry = json.loads(Path(path).read_text(encoding="utf-8"))
    if registry.get("schema_version") != 1:
        raise SourceError("不支持的来源索引版本")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", registry["repository"]):
        raise SourceError("来源仓库格式不正确")
    if not re.fullmatch(r"[0-9a-f]{40}", registry["revision"]):
        raise SourceError("来源必须固定到完整 commit SHA")
    seen = set()
    for source in registry["sources"]:
        sid = source["id"]
        if not re.fullmatch(r"S[0-9]{2,}", sid) or sid in seen:
            raise SourceError("来源编号无效或重复")
        seen.add(sid)
        if not re.fullmatch(r"[0-9a-f]{40}", source["blob_sha"]):
            raise SourceError(f"{sid} 缺少有效的文本校验值")
        path_parts = source["path"].split("/")
        if path_parts[:2] != ["docs", "src"] or ".." in path_parts:
            raise SourceError(f"{sid} 的来源路径不正确")
        if not source["quote"].strip():
            raise SourceError(f"{sid} 的短引为空")
    if not seen:
        raise SourceError("来源索引为空")
    return registry


def git_blob_sha(data):
    header = b"blob " + str(len(data)).encode("ascii") + b"\0"
    return hashlib.sha1(header + data).hexdigest()


def checked_text(data, source):
    if len(data) > MAX_BYTES:
        raise SourceError(f'{source["id"]} 文本超过读取上限')
    if git_blob_sha(data) != source["blob_sha"]:
        raise SourceError(f'{source["id"]} 文本校验失败，不能当作已核对的版本')
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourceError(f'{source["id"]} 不是有效的 UTF-8 文本') from exc


def raw_url(registry, source):
    return (
        "https://raw.githubusercontent.com/"
        + registry["repository"] + "/" + registry["revision"] + "/"
        + quote(source["path"], safe="/")
    )


def cache_directory():
    supplied = os.environ.get("MAO_THOUGHT_CACHE")
    path = Path(supplied).expanduser() if supplied else Path(tempfile.gettempdir()) / "mao-thought-practice"
    path = path.resolve()
    if path == ROOT or ROOT in path.parents:
        raise SourceError("原文缓存需放在仓库之外；请调整 MAO_THOUGHT_CACHE")
    return path


def fetch_bytes(url):
    request = Request(url, headers={"User-Agent": "mao-thought-practice/0.4"})
    with urlopen(request, timeout=20) as response:
        return response.read(MAX_BYTES + 1)


def get_text(registry, source, cache, offline=False):
    """Trust only the pinned blob. Online mode can repair a corrupt cache once."""
    cached = Path(cache) / registry["revision"] / (source["id"] + ".md")
    if cached.exists():
        try:
            return checked_text(cached.read_bytes(), source), "cache"
        except SourceError:
            if offline:
                raise
    elif offline:
        raise SourceError(f'{source["id"]} 无本地缓存，离线时无法核验原文')

    data = fetch_bytes(raw_url(registry, source))
    text = checked_text(data, source)
    cached.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replacement prevents concurrent lookups from reading a partial file.
    with tempfile.NamedTemporaryFile(dir=cached.parent, delete=False) as handle:
        temp_path = Path(handle.name)
        handle.write(data)
    try:
        temp_path.replace(cached)
    finally:
        temp_path.unlink(missing_ok=True)
    return text, "download"


def find_passages(text, needle, max_hits=3, max_chars=900):
    """Return bounded matches with source line numbers and explicit truncation."""
    if not needle.strip():
        raise SourceError("检索词不能为空")
    if len(needle) > max_chars:
        raise SourceError(f"检索词不能超过 {max_chars} 个字符")
    lines = text.splitlines()
    hits = [i for i, line in enumerate(lines) if needle in line]
    results = []
    for index in hits[:max_hits]:
        # One neighboring nonempty line on either side provides local context.
        first, last = index, index
        while first > 0:
            first -= 1
            if lines[first].strip():
                break
        while last + 1 < len(lines):
            last += 1
            if lines[last].strip():
                break
        raw_passage = "\n".join(lines[first:last + 1])
        focus = sum(len(line) + 1 for line in lines[first:index]) + lines[index].index(needle)
        focus -= len(raw_passage) - len(raw_passage.lstrip())
        passage = raw_passage.strip()
        start = max(0, focus - (max_chars - len(needle)) // 2)
        start = min(start, max(0, len(passage) - max_chars))
        end = min(len(passage), start + max_chars)
        results.append({
            "line_start": first + 1,
            "line_end": last + 1,
            "match_line": index + 1,
            "text": ("[…]" if start else "") + passage[start:end] + ("[…]" if end < len(passage) else ""),
            "truncated": start > 0 or end < len(passage),
        })
    return {
        "matching_lines": len(hits),
        "omitted_matching_lines": max(0, len(hits) - max_hits),
        "passages": results,
    }


def verify_sources(registry, cache, offline=False):
    results = []
    for source in registry["sources"]:
        try:
            text, origin = get_text(registry, source, cache, offline)
            if source["quote"] not in text:
                raise SourceError("短引未逐字匹配原文")
            results.append({"id": source["id"], "ok": True, "read_from": origin})
        except (SourceError, OSError, URLError) as exc:
            results.append({"id": source["id"], "ok": False, "error": str(exc)})
    verified = sum(item["ok"] for item in results)
    return {
        "revision": registry["revision"],
        "scope": "文本完整性与短引逐字匹配；不证明纸本版本、历史解释或生活建议正确",
        "total": len(results),
        "verified": verified,
        "ok": verified == len(results),
        "results": results,
    }


def emit(value, stream=None):
    print(json.dumps(value, ensure_ascii=False, indent=2), file=stream if stream is not None else sys.stdout)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="列出已收录的篇目，不联网")
    show = commands.add_parser("show", help="查阅原文段落；默认查找收录短引")
    show.add_argument("id", help="例如 S01")
    show.add_argument("--find", help="逐字检索词")
    show.add_argument("--offline", action="store_true", help="只用已核验缓存")
    verify = commands.add_parser("verify", help="检查所有篇目的完整性和短引")
    verify.add_argument("--offline", action="store_true", help="只用已核验缓存")
    args = parser.parse_args(argv)

    try:
        registry = load_registry()
        if args.command == "list":
            emit({
                "revision": registry["revision"],
                "sources": [
                    {key: source[key] for key in ("id", "title", "author", "date", "url")}
                    for source in registry["sources"]
                ],
            })
            return 0
        cache = cache_directory()
        if args.command == "verify":
            result = verify_sources(registry, cache, args.offline)
            emit(result)
            return 0 if result["ok"] else 1
        source = next((s for s in registry["sources"] if s["id"] == args.id.upper()), None)
        if source is None:
            raise SourceError(f"未收录 {args.id}；请用 list 查看可用编号")
        needle = args.find if args.find is not None else source["quote"]
        if not needle.strip() or len(needle) > 900:
            raise SourceError("检索词须为 1—900 个字符且不能全为空白")
        text, origin = get_text(registry, source, cache, args.offline)
        matches = find_passages(text, needle)
        emit({
            "id": source["id"],
            "title": source["title"],
            "author": source["author"],
            "date": source["date"],
            "url": source["url"],
            "pinned_text_url": raw_url(registry, source),
            "revision": registry["revision"],
            "read_from": origin,
            "query": needle,
            "found": bool(matches["passages"]),
            **matches,
        })
        return 0 if matches["passages"] else 1
    except (SourceError, OSError, URLError, ValueError, KeyError, TypeError) as exc:
        emit({"ok": False, "error": str(exc)}, stream=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
