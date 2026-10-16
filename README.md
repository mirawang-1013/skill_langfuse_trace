# trace-lookup

一个 Claude Code skill，用来追查 VoiceCursor 一条转写的来龙去脉：

- **线上请求**：给一个 audio_id，查到它路由到的模型、语言、词典、延迟，以及 Langfuse 里真正发给模型的 prompt。
- **arena 跑分**：给一个 ogg 文件名或一次 run，查到每个候选模型（VoiceCursor、Gemini、OpenAI、DashScope…）的输入、输出、WER、session_id。

## 数据从哪来

| 想看什么 | 数据源 | 适用范围 |
|---|---|---|
| 服务端路由到哪个模型、languages、词典、延迟拆分 | PostHog `asr_metrics` | 线上请求、arena 里的 VoiceCursor 候选 |
| 真正发给 ASR 模型的 prompt、promptVersion | Langfuse trace | 同上 |
| run 配置、每个候选的输入 / 输出 / WER / 实际发出的语言 / session_id | arena 的 Supabase 库 | **所有候选**（第三方模型只有这里有记录） |

串起来的钥匙：

```
线上 audio_id ──PostHog──▶ langfuse_trace_id ──Langfuse──▶ prompt

ogg 文件名 ──arena_audios.file_name──▶ arena_tasks（音频 × 候选 × run）──▶ arena_results（输出）
                                           └─ VoiceCursor 候选：provider_meta.audio_id ──PostHog──▶ Langfuse
                                           └─ 第三方候选：provider_meta.session_id（PostHog / Langfuse 里没有）
```

## 安装

```bash
git clone git@github.com:mirawang-1013/skill_langfuse_trace.git ~/.claude/skills/trace-lookup
cp ~/.claude/skills/trace-lookup/vc_bench.env.example ~/.config/vc_bench.env
chmod 600 ~/.config/vc_bench.env      # 然后填值：key 和项目 ID 找管理员私下要
pip3 install --user requests psycopg2-binary openpyxl
```

只查线上 audio_id 的话，可以不填 `ARENA_DATABASE_DSN`，也不用装 `psycopg2-binary`。

## 使用

### 在 Claude Code 里

输入 `/trace-lookup <audio_id>`，或者直接用自然语言问，比如：

- "帮我查下 6701d920-… 这条线上是怎么处理的，prompt 是什么"
- "xxx.ogg 在 qwen3.5-omni-flash 和 gemini-3.5-transcribe 下分别返回了什么"
- "葡萄牙语那次 run 里 whisper 的结果导出来"

Claude 会自己选子命令，跑完后解读结果。

### 直接跑脚本

```bash
S=~/.claude/skills/trace-lookup/scripts/trace_lookup.py

# 线上请求：按 audio_id 查（可以给多个，也可以 --ids-from 文件）
python3 $S audio <audio_id>
python3 $S audio --ids-from ids.txt --out prod.csv --fetch-traces traces.json   # 顺带拉完整 Langfuse trace，约 4 秒/条

# arena：只有 ogg 文件名，跨所有 run 看它在各个模型下的返回
python3 $S arena-db --file <audio_id>.ogg --candidate gemini-3.5-transcribe

# arena：先按数据集名列出 run，再导出某个 run 的全部结果
python3 $S arena-db --dataset 葡萄牙语
python3 $S arena-db --batch <batch_id> --with-posthog --out run.csv
```

不加 `--out` 时每行打印一条 JSON，加了就写成 CSV。

### arena-db 输出的关键列

| 类别 | 列 |
|---|---|
| 是哪次 run | `batch_id`、`run_at`、`dataset`、`run_language` |
| 模型 | `candidate`（完整候选配置）、`model`、`server_model`（经网关实际调用的模型） |
| 输入 | `object_key`（音频）、`duration_s`、`sent_language`、`audio_context`、`prompt_id`、`postprocess`、`params` |
| 输出 | `asr_text`（模型原始输出）、`text`（后处理后）、`wer`、`latency`、`token_usage` |
| 追踪 | `vc_audio_id`（VoiceCursor → PostHog）、`provider_session_id`（第三方）、`reused_from_run_id` |

## 附带 skill：audio-fetch（下载原始音频）

`audio-fetch/` 是另一个独立 skill：给 audio_id（UUID）、Langfuse trace id（32 位 hex）或 S3 key，
经 PostHog 找到 S3 地址，把 `.ogg` 和元信息 json 下载到本地。

```bash
ln -s ~/.claude/skills/trace-lookup/audio-fetch ~/.claude/skills/audio-fetch
```

依赖本机的 `badcase_debug_tool`（默认 `~/Desktop/badcase_debug_tool`，可用 `BADCASE_TOOL_DIR` 改）：
用它的 `.venv`、`.env`（PostHog + AWS 凭据）和 `core/s3_audio.py`。用法见 [audio-fetch/SKILL.md](audio-fetch/SKILL.md)。

在 Claude Code 里说"帮我拉一下这条音频：<id>"或 `/audio-fetch <id>` 即可。

## 已知坑

- arena 会重新上传音频，所以 VoiceCursor 候选的 `vc_audio_id` 是新的，和原始线上 audio_id 不是同一个。
- arena 用评测账号发请求，原用户的词典不会生效。
- 第三方候选没有 PostHog / Langfuse trace，经过 `voicecursor-asr` 网关的也一样；它们的 `session_id` 从 2026-09-28 才开始记录。
- 带 `mm-prompt` 的多模态候选，`asr_text` 常为空，结果在 `text` 里。
- 早期 run 的语言可能被存成 `en`，一律以 `sent_language` 为准。

更多细节（表结构、可以直接在 Supabase 网页后台跑的 SQL）见 [reference.md](reference.md)。

## 安全

- 仓库里没有任何密钥，所有凭据和项目 ID 都从 `~/.config/vc_bench.env` 读取。**不要把填好值的 env 文件提交上来，也不要发到群里。**
- `ARENA_DATABASE_DSN` 权限很高。脚本连库时会强制只读；自己在后台写 SQL 时也只跑 `SELECT`。
