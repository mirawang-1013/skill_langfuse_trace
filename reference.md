# 怎么捞 arena 跑分的 trace

一次 arena 跑分的信息分散在三个地方：

| 想看什么 | 去哪看 | 适用候选 |
|---|---|---|
| run 配置（语言、模型、词典）、每条输出、WER、实际发出的 language | **arena 的 Supabase 库** | 所有候选（whisper-1 等直连模型**只有这里有记录**） |
| VoiceCursor 服务端怎么处理的：路由到哪个模型、languages、词典、延迟拆分 | PostHog `asr_metrics` | 只有 VoiceCursor / VoiceCursorStreaming |
| 真正发给 ASR 模型的 prompt（词典 / 光标 / 屏幕词 block、prompt 版本） | Langfuse trace | 只有 VoiceCursor / VoiceCursorStreaming |

串起来的钥匙：`arena_tasks.extra.provider_meta.audio_id` = PostHog `asr_metrics.audio_id`，
PostHog 事件里的 `langfuse_trace_id` = Langfuse trace id。

---

## 方式一：命令行（推荐）

凭据在 `~/.config/vc_bench.env`（模板见同目录 `vc_bench.env.example`）。

```bash
S=~/.claude/skills/trace-lookup/scripts/trace_lookup.py

# 1. 列最近的 run（按数据集名模糊搜），拿到 batch_id；run 语言和数据集语言不一致会标 ⚠️
python3 $S arena-db --dataset 葡萄牙语

# 2. 导出这个 run 的每条结果，VoiceCursor 候选自动带上 PostHog 字段 + Langfuse 链接
python3 $S arena-db --batch <batch_id> --with-posthog --out xxx.csv

# 只看某条音频 / 某个候选
python3 $S arena-db --batch <batch_id> --file <audio_id前缀> --candidate whisper
```

CSV 关键列：`sent_language`（实际传给 provider 的语言）、`wer`、`text`、`ground_truth`、
`vc_model` / `vc_languages` / `vc_effective_vocabulary`（VoiceCursor 服务端的路由和词典）、`vc_langfuse`（点开看 prompt）。

---

## 方式二：Supabase 网页后台

1. 打开 https://supabase.com/dashboard/project/<project_ref> → 左侧 **SQL Editor**
   （project ref 就是 DSN 用户名 `postgres.` 后面那段）。
2. 粘贴下面的 SQL 运行。

> ⚠️ 后台是 postgres 主账号，**只跑 SELECT**。不放心可以把语句包在
> `BEGIN READ ONLY; ... ; ROLLBACK;` 里。

### 表关系

```
arena_datasets (name, primary_language)
   └─ arena_batches  = 一次 run (language, models, dictionary, status)
        ├─ arena_audios   = run 里的每条音频 (file_name, audio_context, ground_truth)
        └─ arena_tasks    = 音频 × 候选模型 (candidate_key, status, error_message, extra.provider_meta)
             └─ arena_results = 输出 (text, language, wer, latency)   ← task_id 关联
```

### ① 找 run

```sql
SELECT b.id AS batch_id, b.created_at, d.name, d.primary_language AS dataset_lang,
       b.language AS run_lang, b.status, b.total_items,
       jsonb_path_query_array(b.models, '$[*].candidate_key') AS candidates
FROM arena_batches b
LEFT JOIN arena_datasets d ON d.id = b.dataset_id
WHERE b.deleted_at IS NULL
  AND d.name ILIKE '%葡萄牙语%'          -- 或者按时间：AND b.created_at >= '2026-10-07'
ORDER BY b.created_at DESC
LIMIT 20;
```

**先看 `run_lang` 和 `dataset_lang` 是否一致。** arena 目前只支持 en / zh / ja / es，
其他语言的数据集会被存成 `en`，直连模型就会收到 `language=en`。

### ② 看一个 run 的每条结果

```sql
SELECT a.file_name, t.candidate_key, t.status, t.error_message,
       r.language AS sent_language, round(r.wer::numeric, 3) AS wer, r.latency,
       r.text, r.ground_truth_text,
       t.extra -> 'provider_meta' ->> 'audio_id'   AS vc_audio_id,      -- VoiceCursor：拿去查 PostHog
       t.extra -> 'provider_meta' ->> 'session_id' AS provider_session  -- 直连模型
FROM arena_tasks t
JOIN arena_audios a ON a.id = t.audio_id
LEFT JOIN arena_results r ON r.task_id = t.id AND r.deleted_at IS NULL
WHERE t.batch_id = '<batch_id>'
  AND t.deleted_at IS NULL
  -- AND a.file_name ILIKE '<audio_id前缀>%'
  -- AND t.candidate_key ILIKE '%whisper%'
ORDER BY a.file_name, t.candidate_key;
```

### ③ 按候选汇总（语言、失败数、平均 WER）

```sql
SELECT t.candidate_key, r.language, count(*) AS n,
       count(*) FILTER (WHERE t.status <> 'succeeded') AS failed,
       round(avg(r.wer)::numeric, 3) AS mean_wer
FROM arena_tasks t
LEFT JOIN arena_results r ON r.task_id = t.id
WHERE t.batch_id = '<batch_id>'
GROUP BY 1, 2;
```

### ④ 拿 `vc_audio_id` 继续往下查

- **PostHog**：SQL 里用 HogQL
  ```sql
  SELECT timestamp, properties.model, properties.languages, properties.effective_vocabulary,
         properties.asr_seconds, properties.postprocess_seconds, properties.langfuse_trace_id
  FROM events
  WHERE event = 'asr_metrics' AND properties.audio_id = '<vc_audio_id>'
  ```
- **Langfuse**：`https://us.cloud.langfuse.com/project/<LANGFUSE_PROJECT_ID>/traces/<langfuse_trace_id>`，
  看 name=`multimodal` 的 generation（真实 prompt 和 promptVersion）。

---

## 已知坑

- **arena 发请求用的是评测账号**，原用户的词典不会生效；PostHog 里 `effective_vocabulary` 为空。
- **arena 会重新上传音频**，`vc_audio_id` 是新的，和数据集里的 audio_id 不同；要查原始线上请求用 `python3 $S audio <原 audio_id>`。
- **arena 后端 `auto` / 空语言会默认成 `en`**（`backend/service/asr_provider/language.go`）。
- **whisper-1 的 `provider_session_id`** 在 PostHog / Langfuse 里查不到，库里就是全部记录。
- **Langfuse `/api/public/traces` 接口**每分钟限 15 次，且官方公告 2026-11-16 下线，之后脚本要改用 `/api/public/v2/observations`。
- **arena 导出的 xlsx** 用 openpyxl 只读模式打开只能读到第一列，要用普通模式。
