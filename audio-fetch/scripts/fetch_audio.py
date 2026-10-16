"""按 audio_id / Langfuse trace id / S3 key 把线上原始音频下载到本地，元信息一并落盘。

    $PY fetch_audio.py <id> [<id> ...]            # 三种 id 可混着给
    $PY fetch_audio.py --from-file ids.txt        # 一行一个，# 开头为注释
    $PY fetch_audio.py <id> --days 90 --out-dir /tmp/x

id 识别规则：
    带横杠 UUID（1b2c3d4e-0000-...）        → properties.audio_id
    32 位纯 hex（0a1b2c3d4e5f6a7b...）       → properties.langfuse_trace_id
    含 / 或 .ogg 或 s3:// 的串               → 直接当 S3 引用下载（也会反查元信息）

依赖 ~/Desktop/badcase_debug_tool：用它的 .venv、.env（PostHog + AWS 凭据）和 core/s3_audio.py。
目录可用 BADCASE_TOOL_DIR 覆盖。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

TOOL_DIR = Path(os.getenv("BADCASE_TOOL_DIR") or Path.home() / "Desktop/badcase_debug_tool")
sys.path.insert(0, str(TOOL_DIR))

import httpx  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(TOOL_DIR / ".env")
from core import s3_audio  # noqa: E402

HOST = (os.getenv("POSTHOG_HOST") or "https://us.posthog.com").rstrip("/")
PID = os.getenv("POSTHOG_PROJECT_ID")
KEY = os.getenv("POSTHOG_API_KEY")

WINDOW_DAYS = 7      # 不带时间条件或窗口太大 PostHog 会 504，按周切
MIN_BYTES = 2000     # 低于此值基本是只有 OGG 容器头的空音频

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
HEX32_RE = re.compile(r"^[0-9a-f]{32}$", re.I)

FIELDS = """toString(properties.audio_id) AS audio_id,
            toString(properties.langfuse_trace_id) AS langfuse_trace_id,
            properties.s3_url AS s3_url, properties.s3_key AS s3_key,
            properties.transcription AS transcription, properties.processed AS processed,
            properties.lang AS lang, properties.model AS model,
            properties.scenario_key AS scenario_key, properties.client_source AS client_source,
            properties.audio_context AS audio_context, distinct_id, timestamp AS ts"""


def kind_of(x: str) -> str:
    if UUID_RE.match(x):
        return "audio_id"
    if HEX32_RE.match(x):
        return "langfuse_trace_id"
    if "/" in x or x.endswith(".ogg") or x.startswith("s3://"):
        return "s3_key"
    return "audio_id"


def run(query: str, values: dict) -> list[dict]:
    if not PID or not KEY:
        sys.exit(f"需要 POSTHOG_PROJECT_ID / POSTHOG_API_KEY（{TOOL_DIR}/.env）")
    resp = httpx.post(
        f"{HOST}/api/projects/{PID}/query/",
        headers={"Authorization": f"Bearer {KEY}"},
        json={"query": {"kind": "HogQLQuery", "query": query, "values": values}},
        timeout=120,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"PostHog HTTP {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    cols = [c["name"] if isinstance(c, dict) else c for c in body.get("columns", [])]
    return [dict(zip(cols, r)) for r in body.get("results", [])]


def lookup(kind: str, values: list[str], days: int) -> dict[str, dict]:
    """在 asr_metrics 里按周往回查，返回 输入值 → 事件。优先保留带 S3 引用的那条（重试事件不带）。"""
    col = {"audio_id": "toString(properties.audio_id)",
           "langfuse_trace_id": "toString(properties.langfuse_trace_id)",
           "s3_key": "toString(properties.s3_key)"}[kind]
    # asr_metrics 里 s3_key 是不带 bucket 的裸 key，s3:// 形式要先剥掉 bucket
    want = {re.sub(r"^s3://[^/]+/", "", v) if kind == "s3_key" else v: v for v in values}
    found: dict[str, dict] = {}
    end = dt.datetime.utcnow() + dt.timedelta(days=1)
    for k in range(0, days, WINDOW_DAYS):
        todo = [w for w in want if want[w] not in found or not (found[want[w]].get("s3_url") or found[want[w]].get("s3_key"))]
        if not todo:
            break
        hi, lo = end - dt.timedelta(days=k), end - dt.timedelta(days=min(k + WINDOW_DAYS, days))
        try:
            rows = run(f"SELECT {FIELDS} FROM events WHERE event = 'asr_metrics' "
                       f"AND timestamp >= {{lo}} AND timestamp < {{hi}} AND {col} IN {{ids}} "
                       f"ORDER BY timestamp DESC",
                       {"lo": lo.strftime("%Y-%m-%d %H:%M:%S"), "hi": hi.strftime("%Y-%m-%d %H:%M:%S"), "ids": todo})
        except RuntimeError as exc:
            print(f"  {lo:%m-%d}~{hi:%m-%d} 查询失败：{exc}", file=sys.stderr)
            continue
        for rec in rows:
            orig = want[str(rec[{"audio_id": "audio_id", "langfuse_trace_id": "langfuse_trace_id",
                                 "s3_key": "s3_key"}[kind]])]
            prev = found.get(orig)
            if prev is None or (not (prev.get("s3_url") or prev.get("s3_key")) and (rec.get("s3_url") or rec.get("s3_key"))):
                found[orig] = rec
        print(f"  {lo:%Y-%m-%d} ~ {hi:%Y-%m-%d}：累计命中 {len(found)}/{len(values)}", file=sys.stderr)
    return found


def verify(path: Path) -> tuple[bool, str]:
    size = path.stat().st_size
    if size == 0:
        return False, "0 字节"
    if path.read_bytes()[:4] != b"OggS":
        return False, "不是 OGG 容器"
    if size < MIN_BYTES:
        return False, f"只有 {size:,} 字节，疑似空音频（只有容器头）"
    return True, f"{size:,} 字节"


def stem_for(rec: dict) -> str:
    """<时间>_<audio_id前8位>，和 badcase_debug_tool/tools/fetch_*.py 一致。"""
    ts = str(rec.get("ts") or "")[:19].replace(":", "").replace("-", "").replace("T", "_")
    return f"{ts}_{str(rec.get('audio_id') or 'noid')[:8]}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--from-file")
    ap.add_argument("--days", type=int, default=60, help="往回查多少天（默认 60）")
    ap.add_argument("--out-dir", default=str(TOOL_DIR / "audio_downloads"))
    a = ap.parse_args()

    ids = list(a.ids)
    if a.from_file:
        ids += [l.split("#", 1)[0].strip() for l in Path(a.from_file).read_text(encoding="utf-8").splitlines()
                if l.split("#", 1)[0].strip()]
    ids = list(dict.fromkeys(x.strip() for x in ids))
    if not ids:
        ap.print_help(); sys.exit(1)

    groups: dict[str, list[str]] = {}
    for x in ids:
        groups.setdefault(kind_of(x), []).append(x)
    found: dict[str, dict] = {}
    for kind, vals in groups.items():
        print(f"按 {kind} 查 {len(vals)} 个（近 {a.days} 天）", file=sys.stderr)
        found.update(lookup(kind, vals, a.days))

    out = Path(a.out_dir); out.mkdir(parents=True, exist_ok=True)
    ok = bad = missing = 0
    for n, x in enumerate(ids, 1):
        rec = found.get(x)
        ref = (rec or {}).get("s3_url") or (rec or {}).get("s3_key") or (x if kind_of(x) == "s3_key" else None)
        print(f"\n[{n}/{len(ids)}] {x}  ({kind_of(x)})")
        if not ref:
            print("    ✗ 近 %d 天 asr_metrics 里没找到（试试加大 --days，或核对 id）" % a.days if not rec
                  else "    ✗ 事件里没有 s3_url/s3_key")
            missing += 1
            continue
        rec = rec or {"audio_id": Path(ref).stem}
        stem = stem_for(rec)
        audio_path = out / f"{stem}{Path(s3_audio.parse_s3_ref(ref)[1]).suffix or '.ogg'}"
        try:
            s3_audio.download(ref, str(audio_path))
        except Exception as exc:
            print(f"    ✗ 下载失败 {type(exc).__name__}: {exc}")
            bad += 1
            continue
        good, note = verify(audio_path)
        ok += good; bad += not good
        try:
            ctx = json.loads(rec.get("audio_context") or "{}")
        except (TypeError, ValueError):
            ctx = rec.get("audio_context")
        meta = {"input": x, "audio_id": rec.get("audio_id"), "langfuse_trace_id": rec.get("langfuse_trace_id"),
                "audio_ref": ref, "local_path": str(audio_path), "timestamp": rec.get("ts"),
                "distinct_id": rec.get("distinct_id"), "language": rec.get("lang"),
                "transcript": rec.get("transcription"), "processed": rec.get("processed"),
                "audio_context": ctx, "asr_model": rec.get("model"), "scenario_key": rec.get("scenario_key"),
                "client_source": rec.get("client_source"), "audio_ok": good,
                "audio_bytes": audio_path.stat().st_size}
        (out / f"{stem}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"    {'✓' if good else '!!'} {audio_path}  {note}")
        print(f"    audio_id={rec.get('audio_id')}  trace={rec.get('langfuse_trace_id')}  "
              f"{str(rec.get('ts'))[:19]}  {rec.get('client_source')}  {rec.get('model')}  lang={rec.get('lang')}")
        print(f"    转写: {(rec.get('transcription') or '')[:80]}")
        print(f"    成品: {(rec.get('processed') or '')[:80]}")

    print(f"\n完成：可用 {ok}，有问题 {bad}，没找到 {missing}（共 {len(ids)}）")


if __name__ == "__main__":
    main()
