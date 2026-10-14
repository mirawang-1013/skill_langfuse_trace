---
name: trace-lookup
description: 按 audio_id 或 arena 跑分捞 VoiceCursor 的 trace —— PostHog asr_metrics（路由模型、languages、词典、延迟）+ Langfuse trace（真实 ASR prompt / promptVersion）+ arena Supabase 库（run 配置、每条输出、WER、实际发出的 language）。当用户给出 audio_id、想看某条转写/badcase 线上是怎么处理的、想查 prompt 或词典是否生效、或要排查某次 arena run / batch 的结果时使用。
argument-hint: "<audio_id...> | arena-db --dataset <名字> | arena-db --batch <batch_id>"
---

# trace-lookup

脚本：`${CLAUDE_SKILL_DIR}/scripts/trace_lookup.py`（下文简写 `$S`）。
表结构、Supabase 网页后台 SQL、已知坑详见 `${CLAUDE_SKILL_DIR}/reference.md`，遇到下文没覆盖的情况再读。

## 第 0 步：检查环境

1. 凭据文件 `~/.config/vc_bench.env`（或 `VC_BENCH_ENV` 指定的路径）是否存在。
   不存在就让用户照 `${CLAUDE_SKILL_DIR}/vc_bench.env.example` 建一个并 `chmod 600`，
   **不要替用户编造或从别处搬运密钥**。只查 `audio` 不需要 `ARENA_DATABASE_DSN`。
2. 依赖：`python3 -c "import requests"`；`arena-db` 还要 `psycopg2`（`pip3 install --user psycopg2-binary`），
   `arena --run-result` 要 `openpyxl`。缺了先告诉用户再装。

## 选哪个子命令

| 用户手上有什么 | 命令 |
|---|---|
| 一个或几个 audio_id（线上原始请求、badcase、数据集里的 id） | `python3 $S audio <id> [<id>...]` |
| 一个文件的 audio_id（jsonl 取 `audio_id` 字段，或每行一个 id 的 txt） | `python3 $S audio --ids-from <file> --out out.csv` |
| arena 跑分，知道数据集名 | `python3 $S arena-db --dataset <名字>` → 拿 batch_id |
| arena 跑分，有 batch_id | `python3 $S arena-db --batch <id> --with-posthog --out out.csv` |
| 只想看某条音频 / 某个候选 | 上一条再加 `--file <文件名前缀>` / `--candidate <whisper\|VoiceCursor…>` |
| 没有库权限，只有 arena 导出的 xlsx | `python3 $S arena --since <UTC> --until <UTC> --run-result x.xlsx --out out.csv`（兜底，按文本相似度匹配） |

需要完整 Langfuse trace JSON（prompt 全文）时给 `audio` / `arena` 加 `--fetch-traces traces.json`。
它约 4 秒一条（Langfuse 限流 15 次/分钟），条数多时先告诉用户预计耗时；中断后重跑会跳过已拉的。

## 结果怎么读、怎么汇报

- 单条 audio_id：直接汇报 `model`、`languages`、`use_dictionary` / `effective_vocabulary`、
  `asr_seconds` + `postprocess_seconds`、`ctx_app`，并附上 `langfuse` 链接。
  用户问 prompt 时，看 trace 里 `name='multimodal'` 的 generation。
- `audio` 输出 `NOT FOUND`：PostHog 和 Langfuse 都没有。先确认 id 有没有带扩展名或空格、
  是不是 arena 重放后的新 id（这种要用 `arena-db`）。
- arena-db：先看脚本打印的 run 语言和数据集语言是否一致（不一致会标 ⚠️），
  再按候选汇总 `sent_language` / `wer` / 失败数。

## 必须知道的坑

- arena 会重新上传音频，**arena 里的 `vc_audio_id` 和数据集里的原始 audio_id 不是同一个**，
  要看原始线上请求得用原 id 跑 `audio`。
- arena 用评测账号发请求，**原用户的词典不会生效**，所以 `effective_vocabulary` 为空是正常的。
- arena 只支持 en/zh/ja/es，其他语言（以及 `auto` / 空）的 run 都会被存成 `en`。
- whisper-1 等直连模型在 PostHog / Langfuse 里**没有 trace**，只有 arena 库里的记录。
- `ARENA_DATABASE_DSN` 是 postgres 主账号。脚本强制只读会话；**自己写 SQL 时也只跑 SELECT**。
- Langfuse `/api/public/traces` 公告 2026-11-16 下线。之后如果 `--fetch-traces` 或 `audio` 的 Langfuse 兜底报 404/410，
  要改用 `/api/public/v2/observations`。
