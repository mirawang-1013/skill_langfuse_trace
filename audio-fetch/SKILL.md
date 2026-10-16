---
name: audio-fetch
description: 根据 trace 拉线上原始音频 —— 输入 audio_id（UUID）、Langfuse trace id（32 位 hex）或 S3 key，经 PostHog asr_metrics 找到 s3_url，从 S3 下载 .ogg 到本地并附元信息 json（转写、成品、语言、模型、audio_context）。当用户说"拉音频 / 下载音频 / 听一下这条 / 根据 trace 捞音频"时使用。只查 trace 不下载音频用 trace-lookup。
argument-hint: "<audio_id | langfuse_trace_id | s3_key> [...]"
---

# audio-fetch

```bash
PY=~/Desktop/badcase_debug_tool/.venv/bin/python
S=~/.claude/skills/audio-fetch/scripts/fetch_audio.py
$PY $S <id> [<id>...]              # 三种 id 可混着给
$PY $S --from-file ids.txt         # 一行一个
$PY $S <id> --days 120             # 默认只往回查 60 天
```

依赖 `~/Desktop/badcase_debug_tool`：它的 `.venv`（httpx / boto3 / python-dotenv）、`.env`（PostHog + AWS 凭据）
和 `core/s3_audio.py`。不在默认位置时设 `BADCASE_TOOL_DIR`。**不要替用户编造凭据**。

产物默认在 `~/Desktop/badcase_debug_tool/audio_downloads/<YYYYMMDD_HHMMSS>_<audio_id前8位>.ogg` + 同名 `.json`。

## id 怎么识别

| 形态 | 当作 |
|---|---|
| 带横杠 UUID，如 `1b2c3d4e-0000-4000-8000-000000000000` | `properties.audio_id`（线上真正的 audio_id） |
| 32 位纯 hex，如 `0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d` | `properties.langfuse_trace_id` |
| 含 `/`、`.ogg` 或 `s3://` | S3 key，直接下载并反查元信息 |

## 汇报

给出本地路径、字节数、真实 audio_id（输入是 trace id 时要点出来）、时间、client_source、模型、语言、转写/成品。

## 坑

- **用户常把 32 位 Langfuse trace id 当成 audio_id 给**。trace-lookup 的 `audio` 子命令按 audio_id 查，会报 NOT FOUND——这时用本 skill。
- PostHog 查询必须带时间条件，脚本按 7 天一窗往回查；不带会 504。查不到先加大 `--days`。
- 同一 audio_id 的重试事件不带 s3_url，脚本已优先取带 S3 引用的那条。
- 线上有一批只有 100 多字节的空音频（只有 OGG 头），脚本标 `!!`，别拿去标注。
- `~/Desktop/badcase_debug_tool/tools/fetch_by_audio_id.py` 是旧版：只认 audio_id、无时间条件会 504。
