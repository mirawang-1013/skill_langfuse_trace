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
| 只有 ogg 文件名，想看它在各模型 / 各次 run 的返回 | `python3 $S arena-db --file <xxx.ogg> [--candidate <模型名>] [--dataset <名字>]`（不带 `--batch` = 跨所有 run） |
| 某个 run 里只看某条音频 / 某个候选 | `arena-db --batch <id>` 再加 `--file <文件名前缀>` / `--candidate <whisper\|VoiceCursor…>` |
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
- 跨 run 查一条音频时，结果按 run 时间排序，每行带 `batch_id` / `run_at` / `dataset` / `run_language`。
  同一个模型出现多行是正常的：候选身份是整个 `candidate`（`provider:model|lang:…|mm-prompt:…|post:…|raw`），
  再乘上跑过的 run 次数。汇报时按"模型 → 配置 → 各次 run 是否稳定"组织，并点出配置差异（有无 mm-prompt、语言 auto 还是指定）。
- 用户问某个候选"用了什么模型、输入输出是什么"时，按这几列回答：
  - 模型：`model`，经 `voicecursor-asr` 网关的还要看 `server_model`（网关实际调用的模型）
  - 输入：`object_key`（音频）、`duration_s`、`sent_language`、`audio_context`、`prompt_id`（空 = 无自定义 prompt）、
    `postprocess`、`params`，以及脚本开头打印的 batch 词典数
  - 输出：`asr_text`（模型原始输出）和 `text`（后处理后）、`token_usage`、`latency`（ms）。
    **带 `mm-prompt` 的多模态候选 `asr_text` 常为空、结果只在 `text`**——两列都看，别报"没有输出"。

## 必须知道的坑

- arena 会重新上传音频，**arena 里的 `vc_audio_id` 和数据集里的原始 audio_id 不是同一个**，
  要看原始线上请求得用原 id 跑 `audio`。
- arena 用评测账号发请求，**原用户的词典不会生效**，所以 `effective_vocabulary` 为空是正常的。
- 早期（约 2026-10-07 前）arena 只支持 en/zh/ja/es，其他语言的 run 被存成了 `en`；
  后来已支持 pt-br 和 `auto`。一律以 `sent_language` 列为准，别凭记忆判断。
- **只有 VoiceCursor 系列候选有 PostHog / Langfuse trace**（靠 `vc_audio_id`）。第三方候选（OpenAI、Gemini、
  DashScope、Deepgram…）即使 `backend=voicecursor-asr` 走了网关，网关也不上报 PostHog / Langfuse，
  `provider_session_id` 在两边都搜不到（2026-10-14 验证）——别去搜，模型和输入输出直接看 arena 库那几列。
- 第三方候选的 `provider_session_id` 从 **2026-09-28** 才开始记录，之前的 run 都是空的；
  `reused_from_run_id` 非空的行是复用了别的 run 的结果，也没有自己的 session。
- 早期（约 2026-10-09 前）部分第三方候选的 `token_usage.output_tokens` 为 0 但有文字输出，是统计 bug，别据此下结论。
- `ARENA_DATABASE_DSN` 是 postgres 主账号。脚本强制只读会话；**自己写 SQL 时也只跑 SELECT**。
- Langfuse `/api/public/traces` 公告 2026-11-16 下线。之后如果 `--fetch-traces` 或 `audio` 的 Langfuse 兜底报 404/410，
  要改用 `/api/public/v2/observations`。
