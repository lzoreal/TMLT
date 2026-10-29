# AGENTS.md — D:\TMLT

TAL（This American Life）的字幕与播客源仓库：抓官网 → 生成 `docs/podcast.xml` /
`docs/podcast-zh.xml` / 校对稿 `docs/transcripts/`，并由音频重建逐词时间轴
`docs/timings/`。**`docs/` 是 GitHub Pages 的根目录**，1 GB 站点上限。

## 铁律

1. **校对稿文本与切分不可改。** 用户原话：“当前的 transcript 是校对过的，所以我想保持原文不变”“只重定时，不动切分”。`align_timings.py` 只补 `end` 与逐词时间，文字的增删改一律拒绝。
2. **音频只认 `playlist-data.archive`。** RSS/播放器音频按地区插播广告（偏移 +25~+97 秒），会让时间轴整体错位。只有明确要求时才用 `--allow-rss-audio`。
3. **重活不在本机跑。** 模型 1.6 GB、单集对齐 30–50 分钟，全部在 GitHub Actions 上跑；本机只做代码、测试、调度和取日志。
4. **不手改 `docs/timings/*.json`。** sidecar 的 `complete/attempts/rejected` 参与调度判定，人工改会被下一次对齐覆盖。
5. **大文件不进仓库、不进 `docs/`**（Pages 1 GB 上限）。模型、音频、日志一律放临时目录。
6. **改动先问再提交。** 用户偏好：改动默认留在工作区，明确说“提交/推送”才执行。

## 目录

| 路径 | 作用 |
|---|---|
| `generate.py` | 抓官网 → `docs/podcast.xml`、`docs/transcripts/*.vtt`、`docs/episodes.json`（`--base-url` 必填） |
| `translate_vtt.py` / `generate_zh_feed.py` | 中文翻译与 `docs/podcast-zh.xml`（Gemini，有 RPD 配额） |
| `align_timings.py` | 逐词时间轴 sidecar，核心脚本 |
| `.github/workflows/align-timings.yml` | 20 档矩阵 + 每小时 cron，跑批主力 |
| `.github/workflows/deploy.yml` / `translate.yml` | Pages 部署与翻译，共用 `tmlt-pages` 并发组 |
| `.github/workflows/align-timings.yml` 的 `lane` 输入 | 手工 run 的独立队列，见下 |

## align_timings.py

```bash
PY="C:/Users/游梦/AppData/Local/Programs/Python/Python310/python.exe"   # python3 是 WindowsApps 桩，别用
"$PY" -X utf8 align_timings.py --status            # 只统计，不加载模型（秒级）
"$PY" -X utf8 align_timings.py --plan --shards 20  # 待办摊成 20 份 JSON，供 check job
"$PY" -X utf8 align_timings.py --episodes 477 --force
"$PY" -X utf8 align_timings.py --offset-probe 40 --episodes 477 --force   # 只采样打偏移曲线，不写盘
"$PY" -X utf8 align_timings.py --episodes 2 --force --locate-first        # 恢复被拒集：先粗 ASR 定位
```

关键常量（[align_timings.py](align_timings.py)）：

| 常量 | 值 | 含义 |
|---|---|---|
| `PAD` / `TAIL_PAD` | 3 / 8 秒 | 每组窗口在官网 begin/end 之外留的余量 |
| `MAX_SPAN` / `MAX_CHARS` | 100 秒 / 900 字符 | 一个对齐组的规模上限 |
| `MAX_EARLY` / `MAX_LATE` | 8 秒 | 单句首词/末词相对期望起点的容忍带 |
| `AUDIO_MISMATCH_SECONDS` | 3 秒 | 整集残差中位超过即拒（`audio_mismatch`）|
| 丢弃率阈值 | 10% | 合理带外的 cue 超过即拒（`high_drop_rate`）|
| `MAX_LOCAL_SHIFT` | 15 秒 | 单句平移上限，超过按不可信丢弃 |
| `MAX_ATTEMPTS` | 2 | 瞬态失败的重试次数 |

**sidecar 语义**：`complete`（整集可用）/ `attempts`（已用次数）/ `rejected`（原因）。
`is_done = complete or rejected in GIVE_UP_REASONS or attempts >= MAX_ATTEMPTS`。
`GIVE_UP_REASONS = {audio_mismatch, high_drop_rate}` —— 这两类由母带决定，重跑必然
同样失败，**一次判定即终局**；瞬态原因（`no_archive`、`error:*`）保留 2 次。

## Actions 运行

- **手工 run 必须带 `lane`**（`probe` / `verify` / `recover`）。不带就会进 `tmlt-align`
  队列，而同一并发组里**待起的 run 会被下一小时的 schedule 顶掉**（`cancel-in-progress: false`
  只保住正在跑的那个）。
- 矩阵固定 20 档；`shards` 小于 20 时多余档位拿到空串自己 `exit 0`。
- 分片计划由 `check` job 每轮按当时的待办现算（`--plan`），align 只认自己那份 `--episodes`。
- 每集写完即提交推送；推送冲突靠 `git pull --rebase --autostash` 重试。
- **日志只有 job 结束后**才能通过 API 取（运行中返回 404）。
- 取日志/派发脚本在 `D:\tmp\tmlt-align\api.py`（`runs` / `status <id>` / `dispatch-json`），
  token 只经 `git credential fill` 取得，**绝不打印**。该目录是临时工作区，可能被清理，
  丢了按这个方法现写一个 60 行的 urllib 脚本即可（`ProxyHandler({})` 直连，别吃注册表代理）。

## 本机环境坑

- `python3` 是 WindowsApps 桩 → 用上面的 `PY` 全路径。
- Git Bash 会把以 `/` 开头的 API 路径改写成 `C:/Program Files/Git/...` → 加 `MSYS2_ARG_CONV_EXCL='*'`（同时脚本路径要用 `D:/...` 形式）。
- 控制台是 GBK 代码页 → Python 加 `-X utf8`，脚本里 `sys.stdout.reconfigure(encoding="utf-8")`。
- 往 `main` 推之前先 `git pull --rebase`（cron 波的 runner 也在推同一分支），失败就重试几轮。
- **不要**在未提交的工作树上跑 `git reset --hard`（会吃掉未提交改动）。

## 交接

当前进度、被拒集的根因、恢复配方与遗留问题见 [HANDOVER.md](HANDOVER.md)。
