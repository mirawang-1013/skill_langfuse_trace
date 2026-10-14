# trace-lookup

Claude Code skill：按 audio_id 或 arena 跑分，捞 VoiceCursor 的 PostHog `asr_metrics`、Langfuse trace（真实 ASR prompt）和 arena Supabase 库里的结果。

## 安装

```bash
git clone git@github.com:mirawang-1013/skill_langfuse_trace.git ~/.claude/skills/trace-lookup
cp ~/.claude/skills/trace-lookup/vc_bench.env.example ~/.config/vc_bench.env   # 填值（找管理员要）
chmod 600 ~/.config/vc_bench.env
pip3 install --user requests psycopg2-binary openpyxl
```

## 使用

在 Claude Code 里输入 `/trace-lookup <audio_id>`，或者直接说"帮我查下这个 audio_id 的 trace"。

也可以不经过 Claude，直接跑脚本：

```bash
python3 ~/.claude/skills/trace-lookup/scripts/trace_lookup.py --help
```

原理、表结构、Supabase SQL、已知坑见 [reference.md](reference.md)。
