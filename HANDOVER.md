# 逐词时间轴 — 交接文档

> 状态时间：2026-11-01。铁律与目录见 [AGENTS.md](AGENTS.md)。

## 1. 现状

| 项 | 数值 |
|---|---|
| 校对稿 | 873 集（`docs/transcripts`，155 MB）|
| 时间轴 sidecar | 873 个（`docs/timings`，223 MB）|
| 已完成 | **873 / 873** 集 `complete=true` |
| 被拒 / 待办 | **0 / 0**（cron 每小时巡检，无新集时空跑退出）|
| `docs/` 总大小 | 约 **380 MB**（Pages 上限 1 GB）|
| 单集成本 | CI 上 30–50 分钟（0.7–5.7 sec/音频秒，机器差异大）|
| 波次 | 20 档矩阵，按 **10 个 shard** 分派 |

`complete=true` 不代表 0 丢弃：合理带外的 cue 仍可能 `end=null`、`words=[]`，正文保留，
消费方回退 VTT。丢弃是逐句质量标记，不参与整集可用性。

## 2. 2026-10-29 之后落地的改造

### 2.1 恢复与对齐（`align_timings.py`）

| commit | 作用 |
|---|---|
| `df1eada` | cue 匹配从「精确 6 词前缀」改为 difflib，适应 ASR 漏词/错词 |
| `6e01e5a` / `9df34f9` | located 平移上限放宽到 60 秒，并同步移动 cue start；空窗口按无匹配处理 |
| `699fe26` / `47fb7ba` | 失败组用邻组实测平移重试；孤立丢弃 cue 在邻居间隙里单句补对齐 |
| `2a8a6f1` / `b1c8ad7` | 长 cue 用比例最长块匹配；一个连续锚点覆盖 35% 即可接受 |
| `d1d27e5` | 实测位置与定位估计差 >8 秒且词长合理时，采纳实测值 |
| `09a4768` | 平移与邻域不一致的锚点直接丢弃 |
| `eaa4ed8` / `6f82876` | 拒收前 dump 诊断；缓存整集 ASR 词流；recovery run 可上传 `timings-diag` artifact |
| `8fff71a` / `8b1b7a3` / `4fd12c2` | 末词 0 时长不判废；segment start 收进音频和首词之内；带标点的 `[BRACKET]` 也算非语音 |
| `286655a` | recovery 后重新统计失败数，避免分母沿用旧值 |
| `66da59e` | 逐 packet demux，跳过损坏音频包，不再整集 `InvalidDataError` |
| `834422e` / `0473d00` | 对齐输入剥离说话人前缀和前导舞台标签（只影响匹配，不改正文） |
| `8a6f7d2` | after-next / over-next 判定优先用下一句实测首词 |
| `73acfed` | force 重跑被拒时，不用 rejected 覆盖已有 complete sidecar |
| `151a796` | tqdm 进度条全部关闭，改为每组一行 `对齐 N/M 组` 文本进度；ASR 段用 Heartbeat |

### 2.2 时间轴清洗（全量迁移）

| commit | 数据量 | 内容 |
|---|---|---|
| `3c8798b` | 816 个 sidecar | segment start 归到对齐首词 |
| `906e011` + `7bc816c` | **18,487 段** | 首词压住上一句句尾 → 重排到上一句结束之后 |
| `35f7f4f` + `4faee33` | **12,350 段** | 首词吞掉停顿 → 按自然词长贴到下一个词前（873 文件迁移）|
| `960ffc5` | 代码 | 句内被压成近零时长的 run，用 ASR 词流窗口重定时 |
| `7750b6d` | 代码 | 压缩 run 匹配改为 SequenceMatcher 模糊匹配 + 中间插值（精确匹配命中率只有约 25%）|

**压缩 run 已知规模**：全库约 **1,231 处 / 641 集**（一串 ≤120 ms 词 + 后面大空档）。

### 2.3 播放器原型

| commit | 内容 |
|---|---|
| `47cebc8` / `8934215` | `docs/prototype.html`：feed 播放、VTT 句级 + timings 词级、7 主题、悬浮播放器/抽屉/详情 |
| `52f5e5f` | 显示层把压缩 run 摊到后面的空档，避免一帧跳过（不改 sidecar）|
| `fcb869e` | 支持本地 JSON 试验包导入与恢复 |

## 3. 被拒集根因（2026-10-29 的历史结论）

当时 711 complete / 162 refused。排除项：不是音频版本差异（HLS 与 archive 差 0.1 秒）、
不是内容类型差异、不是「音频比稿件短」。真实成因两类：

1. **小幅阶梯**：母带与稿件在段界差 2–8 秒，段内稳定在 ±0.3 秒（#477、#455）。
2. **局部对不上**：同一窗口内各句首词偏移散布 7–16 秒，平移无解（#2/#74/#168）——
   稿件与音频本身不一致，或 ASR 在该段失效。

修复路线：`locate_cues()` 整集粗 ASR 定位 → 采纳实测位置 → 按句窗口 + 失败组重试 +
单句补对齐 + 压缩 run 回填。**到 10-31 全部 873 集已 complete，refused=0。**
`GIVE_UP_REASONS`（`audio_mismatch` / `high_drop_rate`）保持不变，只对真正无解集生效。

## 4. 当前状态（2026-11-01）

### 4.1 压缩 run 全量回填（C 波）— 金丝雀已过，全量未跑

- 金丝雀 2：run `41206590991`，10 集（130,157,164,256,240,282,71,732,122,135），全部 success。
- 8 集打印 `压缩 run 回填`，合计 **19 处**；#130、#135 没有回填行（无命中或原无压缩 run）。
- 日志：`D:/tmp/tmlt-align/run_41206590991.txt`。
- **641 集的全量重跑尚未派发**。

### 4.2 899 本地深度修复 — 已完成，未写回仓库

产物：

- `D:/tmp/tmlt-align/local899/899_final5.json`（当前最终版）
- `D:/tmp/tmlt-align/local899/899_final5_report.txt`
- 相关临时脚本：`fix899_*.py`、`merge899.py`、`finalize899.py`（在 `D:/tmp/tmlt-align`，该目录可能被清理）

数值：391 segments / **380 句有逐词** / **8,595 词** / 0 丢弃 / `complete=true` / 音频 3613.23 秒。

验证：

- 句首到最近 ASR 词起点：p50 **0.00 s**、p90 0.20 s、p95 **0.30 s**；**374/380（98.4%）≤0.5 s**，
  377/380 ≤1.0 s。仅 65/68（喊叫）与 337（抢话）>1.0 s，属 ASR 无证据，不是对齐错位。
- 首词精确 token 命中（±1 s、3 词序列 ≥2）：329/380（86.6%）；未命中的多数是
  `OK/okay`、缩写、电话音叠在旁白下等 **ASR 用词差异**。

这轮修了 7 处（都有双模型或邻句证据）：

| cue | 修改 | 证据 |
|---|---|---|
| 2 | begin 30.46 → 32.66 | 首词压在上一句 `else.` 上；ASR `and`@32.94 |
| 30 | 321.92 → 328.78 | `--and` 被锚到上一句的 `and`；`just`@329.08 |
| 203 | 1766.62 → 1768.88 | 压在电话铃里；turbo/large-v3 均 `hello`@1768.9、`baby`@1769.5 |
| 264 | 句尾 2209.9 → 2217.35 | `I'm drunk.` 提前 7 秒；两模型一致 |
| 265 | 2212.06 → 2220.88 | 整句被压扁在静音里；ASR 14 词完全对应 2220.88–2226.72 |
| 268 | 2268.92 → 2273.34 | 前两词落在旁白里；ASR `nothing's`@2273.34、`nothing`@2274.02 |
| 372 | 3200.18 → 3209.24 | 首词落在静音里；`you?`@3209.42（与 371/373 抢话）|

`docs/timings/899.json` **仍是旧版**，尚未替换、未提交。

### 4.3 大模型验证环境

- 本地现有 turbo：`D:/TMLT/models/ct2-turbo`（1.6 GB）。
- 本轮新下 large-v3 非 turbo：`D:/tmp/tmlt-align/ct2-large-v3`（3.1 GB，int8 CPU 可跑）。
- `huggingface.co` 不可达；`hf-mirror.com` 约 0.8 MB/s；**ModelScope 约 3.5 MB/s**，
  同仓库 `Systran/faster-whisper-large-v3` 可直接下。
- 第三遍 large-v3 在 54 句上：确认 14、校正 2、仍无法识别 38（短促插话/抢话/混音）。

## 5. 运维配方

```bash
PY="C:/Users/游梦/AppData/Local/Programs/Python/Python310/python.exe"
"$PY" -X utf8 align_timings.py --status
"$PY" -X utf8 align_timings.py --plan --shards 10
"$PY" -X utf8 align_timings.py --episodes 477 --force
```

- 手工 run **必须带 `lane`**（`probe` / `verify` / `recover`），否则会进 `tmlt-align` 组，
  待起的 run 会被下一小时 schedule 顶掉。
- 分片固定 **10**；矩阵固定 20 档，多余档位拿空串自己退出。
- **日志只有 job 结束后**才能通过 API 取（运行中 404）。
- 取日志/派发/状态脚本：`D:/tmp/tmlt-align/api.py`（`runs` / `status <id>` / `dispatch-json`），
  token 只经 `git credential fill` 取得，**绝不打印**。目录丢了按 AGENTS.md 的方法重写。
- 批量监控：`monitor_quality.py` 先看 job 日志；推送失败或日志缺失时下载 `timings-shard-N`
  artifact，按 `attempts > main` 判定已写盘集，必要时补派。
- 本机环境坑（`python3` 桩、MSYS 路径改写、GBK、`git pull --rebase`）见 AGENTS.md。

## 6. 遗留问题（现状）

1. **`match_cues` 锚点语义**：锚点由最长公共块决定，可能锚到上一句的同一个词，把 cue 首词拖走
   （899 的 cue 2/12/30 都是这类）。
2. **验收口径偏松**：现有校验只要前 3 个词里任意一个命中即可，首词错位仍会通过
   （899 cue 2/268）。
3. **压缩 run 全量回填未跑**：全库 641 集，仅跑过 10 集金丝雀。
4. **899_final5 未写回**：`docs/timings/899.json` 仍为旧版。
5. **句内时长语义**：长停顿仍记在前一个词的 `dur` 上（高亮会停留数秒），未做全局时长裁剪。
6. **VTT 重写未做**：修正后的 `end` 只在 sidecar，`docs/transcripts/*.vtt` 仍是官网虚报值。
7. **Pages 预算**：`docs/` 380 MB；空闲档位仍会上传整份 `docs/timings` artifact。
8. **AntennaPod words 支持未接**：sidecar shape `segments[{start,end,speaker,text,words[[word,start_ms,dur_ms]]}]`；
   ≤20 ms 的词渲染时需约 60 ms 最小显示时长。
