#!/usr/bin/env python3
"""按 audio_id / arena 跑分，把 PostHog asr_metrics 和 Langfuse trace 捞出来。

两条线索（2026-10 验证过）：
  - PostHog `asr_metrics` 每次转写一条，properties 里有 audio_id、model、languages、
    audio_context、effective_vocabulary、transcription(raw)/processed、asr_seconds、
    postprocess_seconds，以及 langfuse_trace_id —— 这是连到 Langfuse 的钥匙。
  - Langfuse trace 的 sessionId == audio_id；trace 里 name='multimodal' 的 observation
    就是真正发给 ASR 模型的 prompt（含词典 / 光标 / 屏幕词 block）和 promptVersion。

arena 跑分：arena 会把音频重新上传，audio_id 是新的，和数据集对不上。
  它在 PostHog 里表现为 client_source='arena'（发请求的是 arena 的评测账号，
  不是原用户 —— 所以原用户的词典不会生效）。
  首选 `arena-db`：直接读 arena 的 Supabase Postgres，run 配置（arena_batches.language
  等）、每条结果（arena_results）都在库里；VoiceCursor 候选的
  arena_tasks.extra.provider_meta.audio_id 就是 PostHog 里那个新 audio_id，能精确关联。
  whisper-1 等直连模型在 PostHog/Langfuse 里没有 trace，只有库里这一份记录。
  `arena` 子命令（按时间窗口 + 文本相似度匹配 run_result）是没有库权限时的兜底。

凭据：默认读 ~/.config/vc_bench.env（export POSTHOG_* / LANGFUSE_* /
  ARENA_DATABASE_DSN 那种格式，可用 VC_BENCH_ENV 指定别的路径），环境变量优先。
  ARENA_DATABASE_DSN 是 postgres 主账号，脚本连库时强制只读会话。

Langfuse 限流：GET /api/public/traces 每分钟 15 次，所以每次请求后睡 4.1 秒，
  429 / 代理断线自动重试；--fetch-traces 每拉一条就落盘，中断后重跑只补缺的。
  注意：这个接口 Langfuse 公告 2026-11-16 下线，之后要换 /api/public/v2/observations。

用法:
  # 0) arena 跑分首选：先列最近的 run，再按 batch 导出每条结果 + PostHog/Langfuse 链接
  python3 trace_lookup.py arena-db --dataset 葡萄牙语
  python3 trace_lookup.py arena-db --batch <batch_id> \
      --with-posthog --out Benchmark_Portuguese/arena_db_pt.csv
  # 只有 ogg 文件名：跨所有 run 看这条音频在各候选下的返回
  python3 trace_lookup.py arena-db --file <audio_id>.ogg --candidate gemini-3.5-transcribe

  # 1) 查原始线上请求（数据集里的 audio_id）
  python3 trace_lookup.py audio <audio_id>
  python3 trace_lookup.py audio --ids-from Benchmark_Portuguese/testset_pt.jsonl --out prod.csv

  # 2) 捞一次 arena 跑分，并和 run_result 逐条对上（同时附上原始线上请求做对照）
  python3 trace_lookup.py arena --since 2026-10-07T15:00 --until 2026-10-07T17:00 \
      --run-result 葡萄牙语run_result.xlsx --text-col "VoiceCursorStreaming:voicecursor-streaming|raw_text" \
      --out Benchmark_Portuguese/arena_vs_prod_pt.csv

  # 3) 再加 --fetch-traces 把 Langfuse 完整 trace 拉到 json（慢，约 4 秒/条）
  python3 trace_lookup.py arena ... --fetch-traces Benchmark_Portuguese/langfuse_traces_pt.json
"""
import os, sys, json, time, csv, argparse, difflib, re

CRED_FILE = os.path.expanduser(os.environ.get("VC_BENCH_ENV", "~/.config/vc_bench.env"))

# asr_metrics 里要导出的字段
FIELDS = ["audio_id", "started_at", "client_source", "user_id", "model", "languages", "lang",
          "use_dictionary", "effective_vocabulary", "postprocess_model", "asr_seconds",
          "postprocess_seconds", "total_seconds", "streaming_parts", "stt_v2_implementation",
          "cleanup_format", "tone_id", "langfuse_prompt_name", "langfuse_trace_id"]


def load_env(need=("POSTHOG_PROJECT_ID", "POSTHOG_PERSONAL_API_KEY",
                   "LANGFUSE_PROJECT_ID", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")):
    if os.path.exists(CRED_FILE):
        for line in open(CRED_FILE, encoding="utf-8"):
            m = re.match(r'\s*(?:export\s+)?(\w+)=["\']?(.*?)["\']?\s*$', line)
            if m and m.group(1) not in os.environ:
                os.environ[m.group(1)] = m.group(2)
    miss = [k for k in need if not os.environ.get(k)]
    if miss:
        sys.exit(f"缺凭据 {miss}：写进 {CRED_FILE} 或 export")


def q(sql):
    import requests
    host = os.environ.get("POSTHOG_HOST", "https://us.posthog.com")
    pid = os.environ["POSTHOG_PROJECT_ID"]
    r = requests.post(f"{host}/api/projects/{pid}/query",
                      headers={"Authorization": f"Bearer {os.environ['POSTHOG_PERSONAL_API_KEY']}"},
                      json={"query": {"kind": "HogQLQuery", "query": sql}}, timeout=300)
    if r.status_code != 200:
        sys.exit(f"PostHog {r.status_code}: {r.text[:400]}")
    return r.json()["results"]


def lf_get(path, params=None):
    """Langfuse GET，带限流和断线重试。"""
    import requests
    base = os.environ.get("LANGFUSE_BASE_URL", "https://us.cloud.langfuse.com")
    auth = (os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"])
    for i in range(10):
        try:
            r = requests.get(base + path, auth=auth, params=params, timeout=60)
            if r.status_code == 429:
                time.sleep(6); continue
            r.raise_for_status()
            time.sleep(4.1)
            return r.json()
        except requests.exceptions.RequestException as e:
            print(f"  Langfuse 重试 {i}: {type(e).__name__}", file=sys.stderr)
            time.sleep(10)
    sys.exit(f"Langfuse 放弃: {path}")


def lf_link(tid):
    base = os.environ.get("LANGFUSE_BASE_URL", "https://us.cloud.langfuse.com")
    return f"{base}/project/{os.environ['LANGFUSE_PROJECT_ID']}/traces/{tid}" if tid else ""


def props(p):
    return json.loads(p) if isinstance(p, str) else p


def ctx_of(p):
    c = p.get("audio_context") or {}
    if isinstance(c, str):
        try: c = json.loads(c)
        except ValueError: c = {}
    return c


def flat(p, prefix=""):
    c = ctx_of(p)
    row = {prefix + k: (json.dumps(p.get(k), ensure_ascii=False) if isinstance(p.get(k), (list, dict)) else p.get(k))
           for k in FIELDS}
    row[prefix + "ctx_language"] = c.get("language")
    row[prefix + "ctx_app"] = (c.get("app") or {}).get("bundle_id")
    row[prefix + "ctx_keys"] = ",".join(sorted(c))
    row[prefix + "langfuse"] = lf_link(p.get("langfuse_trace_id"))
    return row


def prod_events(ids, arena=False):
    """按 audio_id 查 asr_metrics。默认只要原始线上请求（排除 arena 重放）；arena=True 反过来。"""
    out = {}
    src = "= 'arena'" if arena else "!= 'arena'"
    for i in range(0, len(ids), 200):
        inl = ",".join(f"'{x}'" for x in ids[i:i + 200])
        for _, p in q(f"SELECT timestamp, properties FROM events WHERE event='asr_metrics' "
                      f"AND properties.audio_id IN ({inl}) AND properties.client_source {src} "
                      f"AND timestamp >= '2026-01-01' ORDER BY timestamp"):
            p = props(p)
            out.setdefault(p["audio_id"], p)
    return out


def read_ids(a):
    ids = list(a.ids)
    if a.ids_from:
        if a.ids_from.endswith(".jsonl"):
            ids += [json.loads(l)["audio_id"] for l in open(a.ids_from, encoding="utf-8") if l.strip()]
        else:
            ids += [l.split("#")[0].strip() for l in open(a.ids_from, encoding="utf-8") if l.split("#")[0].strip()]
    return [os.path.splitext(os.path.basename(x))[0] for x in ids]


def write(rows, path):
    if not rows:
        print("没有结果"); return
    cols = list(dict.fromkeys(k for r in rows for k in r))
    if path:
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(rows)
        print(f"写出 {len(rows)} 行 → {path}")
    else:
        for r in rows:
            print(json.dumps(r, ensure_ascii=False, default=str))


def fetch_traces(pairs, path):
    """pairs: [(key, trace_id)]，逐条落盘，可断点续跑。"""
    out = json.load(open(path)) if os.path.exists(path) else {}
    todo = [(k, t) for k, t in pairs if t and k not in out]
    print(f"Langfuse trace：已有 {len(out)}，待拉 {len(todo)}（约 {len(todo) * 4.2 / 60:.0f} 分钟）")
    for k, t in todo:
        out[k] = lf_get(f"/api/public/traces/{t}")
        json.dump(out, open(path, "w"), ensure_ascii=False, default=str)
    print(f"→ {path}")


def cmd_audio(a):
    ids = read_ids(a)
    ev = prod_events(ids)
    rows = []
    for i in ids:
        p = ev.get(i)
        if p:
            rows.append(flat(p))
        else:
            # PostHog 没有时退回 Langfuse 按 sessionId 找
            d = lf_get("/api/public/traces", {"sessionId": i, "limit": 10}).get("data", [])
            rows.append({"audio_id": i, "langfuse": " ".join(lf_link(t["id"]) for t in d) or "NOT FOUND"})
    write(rows, a.out)
    if a.fetch_traces:
        fetch_traces([(r["audio_id"], r.get("langfuse_trace_id")) for r in rows], a.fetch_traces)


def cmd_arena(a):
    where = f"event='asr_metrics' AND properties.client_source='arena' " \
            f"AND timestamp >= '{a.since}' AND timestamp < '{a.until}'"
    if a.user:
        where += f" AND properties.user_id='{a.user}'"
    ev = [props(p) for _, p in q(f"SELECT timestamp, properties FROM events WHERE {where} ORDER BY timestamp")]
    print(f"arena 事件 {len(ev)} 条，账号 {sorted({e.get('user_id') for e in ev})}")
    if not a.run_result:
        write([flat(e) for e in ev], a.out); return

    import openpyxl
    # 不用 read_only：arena 导出的 xlsx dimension 写错了，只读模式只能读到第一列
    ws = openpyxl.load_workbook(a.run_result).active
    it = ws.iter_rows(values_only=True)
    head = list(next(it))
    ti, fi = head.index(a.text_col), head.index(a.file_col)
    samples = [(os.path.splitext(r[fi])[0], r[ti]) for r in it if r[fi]]
    prod = prod_events([s for s, _ in samples])

    rows, pairs = [], []
    for sid, text in samples:
        sim = lambda e: difflib.SequenceMatcher(None, text or "", e.get("processed") or "").ratio()
        e = max(ev, key=sim) if ev else {}
        row = {"sample": sid, "match": round(sim(e), 3) if e else 0}
        row.update(flat(e, "arena_"))
        row["arena_raw_eq_processed"] = e.get("transcription") == e.get("processed")
        if sid in prod:
            row.update(flat(prod[sid], "prod_"))
        rows.append(row)
        pairs += [(f"{sid}|arena", e.get("langfuse_trace_id")),
                  (f"{sid}|prod", (prod.get(sid) or {}).get("langfuse_trace_id"))]
    bad = [r["sample"] for r in rows if r["match"] < 0.95]
    if bad:
        print(f"⚠️ {len(bad)} 条文本匹配度 <0.95，可能时间窗口不对：{bad[:5]}")
    write(rows, a.out)
    if a.fetch_traces:
        fetch_traces(pairs, a.fetch_traces)


def db():
    import psycopg2  # pip3 install --user psycopg2-binary
    dsn = os.environ.get("ARENA_DATABASE_DSN")
    if not dsn:
        sys.exit(f"缺 ARENA_DATABASE_DSN：写进 {CRED_FILE}")
    c = psycopg2.connect(dsn, connect_timeout=20)
    c.set_session(readonly=True)  # 主账号，必须只读
    return c


def cmd_arena_db(a):
    cur = db().cursor()
    if not a.batch and not a.file:
        # 列 run：按数据集名 / 时间窗口筛
        sql = """SELECT b.id, b.created_at, d.name, d.primary_language, b.language, b.status, b.total_items,
                        jsonb_path_query_array(b.models, '$[*].candidate_key')
                 FROM arena_batches b LEFT JOIN arena_datasets d ON d.id = b.dataset_id
                 WHERE b.deleted_at IS NULL"""
        args = []
        if a.dataset:
            sql += " AND d.name ILIKE %s"; args.append(f"%{a.dataset}%")
        if a.since:
            sql += " AND b.created_at >= %s"; args.append(a.since)
        if a.until:
            sql += " AND b.created_at < %s"; args.append(a.until)
        cur.execute(sql + " ORDER BY b.created_at DESC LIMIT %s", args + [a.limit])
        print("batch_id | created_at | 数据集 | 数据集语言 | run语言 | 状态 | 条数 | 候选")
        for r in cur.fetchall():
            print(" | ".join(str(x) for x in r))
        print("\n下一步：trace_lookup.py arena-db --batch <batch_id> --with-posthog --out xxx.csv")
        return

    if a.batch:
        cur.execute("""SELECT b.language, d.name, d.primary_language, b.dictionary FROM arena_batches b
                       LEFT JOIN arena_datasets d ON d.id = b.dataset_id WHERE b.id = %s""", (a.batch,))
        meta = cur.fetchone()
        if not meta:
            sys.exit(f"找不到 batch {a.batch}")
        print(f"数据集 {meta[1]}（{meta[2]}）· run 语言 = {meta[0]!r}"
              + ("  ⚠️ 和数据集语言不一致" if (meta[0] or "").split("-")[0] != (meta[2] or "").split("-")[0] else "")
              + f" · 词典 {len(meta[3] or [])} 个词")
    # 链路：arena_audios.file_name（= 你手上的 ogg 名）→ arena_audios.id = arena_tasks.audio_id（arena 内部 id，
    # 不是 PostHog audio_id）→ arena_tasks.id = arena_results.task_id。一行 task = 音频 × 候选 × run，模型在 task 上区分。
    # 输入：音频 object_key / audio_context / 请求语言 / prompt / params；输出：asr_text(模型原始) / text(后处理后) / token_usage
    sql = """SELECT a.file_name, t.candidate_key, t.provider, t.model_id, t.status, t.error_message,
                    r.language, r.wer, r.latency, r.text, r.ground_truth_text, t.extra,
                    a.object_key, a.duration_seconds, a.audio_context, t.params,
                    t.prompt_id, t.multimodal_prompt_id, t.postprocess_model_id, t.postprocess_prompt_id,
                    r.asr_text, r.token_usage, t.batch_id, t.created_at, d.name, b.language, r.reused_from_run_id
             FROM arena_tasks t
             JOIN arena_audios a ON a.id = t.audio_id
             JOIN arena_batches b ON b.id = t.batch_id AND b.deleted_at IS NULL
             LEFT JOIN arena_datasets d ON d.id = b.dataset_id
             LEFT JOIN arena_results r ON r.task_id = t.id AND r.deleted_at IS NULL
             WHERE t.deleted_at IS NULL"""
    args = []
    if a.batch:
        sql += " AND t.batch_id = %s"; args.append(a.batch)
    elif a.dataset:
        sql += " AND d.name ILIKE %s"; args.append(f"%{a.dataset}%")
    if a.candidate:
        sql += " AND t.candidate_key ILIKE %s"; args.append(f"%{a.candidate}%")
    if a.file:
        sql += " AND a.file_name ILIKE %s"; args.append(f"%{a.file}%")
    cur.execute(sql + " ORDER BY a.file_name, t.created_at, t.candidate_key", args)
    rows = []
    js = lambda v: json.dumps(v, ensure_ascii=False) if v else ""
    for (f, ck, prov, mid, st, err, lang, wer, lat, text, gt, extra, key, dur, ctx, params,
         pr, mpr, ppm, ppr, asr_text, usage, bid, created, ds, blang, reused) in cur.fetchall():
        pm = (extra or {}).get("provider_meta") or {}
        rows.append({"file": f, "batch_id": bid, "run_at": created, "dataset": ds, "run_language": blang,
                     "candidate": ck, "provider": prov, "model": mid,
                     "server_model": pm.get("server_model"), "backend": pm.get("backend"),
                     "status": st, "error": err,
                     "sent_language": lang, "wer": wer, "latency": lat,
                     "asr_text": asr_text, "text": text, "ground_truth": gt, "token_usage": js(usage),
                     "prompt_id": pr or mpr, "postprocess": "/".join(x for x in (ppm, ppr) if x), "params": js(params),
                     "object_key": key, "duration_s": dur, "audio_context": ctx,
                     "vc_audio_id": pm.get("audio_id"), "provider_session_id": pm.get("session_id"),
                     "server_commit": pm.get("server_commit"), "reused_from_run_id": reused})
    if a.with_posthog:
        # VoiceCursor 候选：provider_meta.audio_id == PostHog asr_metrics.audio_id
        ev = prod_events([r["vc_audio_id"] for r in rows if r["vc_audio_id"]], arena=True)
        for r in rows:
            p = ev.get(r["vc_audio_id"])
            if p:
                r.update({k: v for k, v in flat(p, "vc_").items()
                          if k in ("vc_model", "vc_languages", "vc_effective_vocabulary", "vc_asr_seconds",
                                   "vc_postprocess_seconds", "vc_postprocess_model", "vc_langfuse")})
    write(rows, a.out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("audio", help="按数据集 audio_id 查原始线上请求")
    p1.add_argument("ids", nargs="*", default=[])
    p1.add_argument("--ids-from", help="jsonl(取 audio_id) 或每行一个 id 的 txt")
    p2 = sub.add_parser("arena", help="按时间窗口捞 arena 跑分并对回 run_result")
    p2.add_argument("--since", required=True, help="UTC，如 2026-10-07T15:00")
    p2.add_argument("--until", required=True)
    p2.add_argument("--user", help="arena 评测账号 user_id（可选）")
    p2.add_argument("--run-result", help="arena 导出的 xlsx")
    p2.add_argument("--text-col", default="VoiceCursorStreaming:voicecursor-streaming|raw_text")
    p2.add_argument("--file-col", default="audio_url")
    for p in (p1, p2):
        p.add_argument("--out", help="CSV 输出路径；不填就打印")
        p.add_argument("--fetch-traces", metavar="JSON", help="顺带把 Langfuse 完整 trace 拉到这个文件")
    p3 = sub.add_parser("arena-db", help="读 arena 的 Supabase 库：只给 --dataset/--since 列 run；"
                        "给 --batch 导出该 run 每条结果；只给 --file 跨所有 run 查这条音频")
    p3.add_argument("--batch", help="arena_batches.id")
    p3.add_argument("--dataset", help="按数据集名模糊筛（列 run，或 --file 跨 run 查时缩小范围）")
    p3.add_argument("--since", help="列 run 时的起始时间（UTC）")
    p3.add_argument("--until")
    p3.add_argument("--limit", type=int, default=20)
    p3.add_argument("--candidate", help="只看某个候选，如 whisper / VoiceCursor")
    p3.add_argument("--file", help="只看某条音频，填文件名（= 原 audio_id）或前缀；不带 --batch 时跨所有 run")
    p3.add_argument("--with-posthog", action="store_true", help="VoiceCursor 候选关联 PostHog + Langfuse 链接")
    p3.add_argument("--out", help="CSV 输出路径；不填就打印")
    a = ap.parse_args()
    if a.cmd == "arena-db":
        # 只连库时不强求 PostHog/Langfuse 凭据
        load_env(("ARENA_DATABASE_DSN", "POSTHOG_PROJECT_ID", "POSTHOG_PERSONAL_API_KEY", "LANGFUSE_PROJECT_ID")
                 if a.with_posthog else ("ARENA_DATABASE_DSN",))
        cmd_arena_db(a)
        return
    load_env()
    {"audio": cmd_audio, "arena": cmd_arena}[a.cmd](a)


if __name__ == "__main__":
    main()
