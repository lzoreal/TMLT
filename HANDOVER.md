# 逐词时间轴 — 交接文档

> 状态时间：2026-11-02。铁律与目录见 [AGENTS.md](AGENTS.md)。

## 1. 现状

| 项 | 数值 |
|---|---|
| 校对稿 | 900 集（`docs/transcripts`，159 MB；含 #900）|
| 时间轴 sidecar | 900 个（`docs/timings`，232 MB）|
| 已完成 | **900 / 900** 集 `complete=true` |
| 被拒 / 待办 | **0 / 0**（每日 pipeline 巡检，无新集时 align 跳过）|
| 无逐词 cue | **14,932**：多词语音 3,288 / 单 token 1,754 / 非语音或其他 9,821 / 唱词 69 |
| `docs/` 总大小 | 仓库 **462 MB**；实际发布 **400.2 MiB / 1810 文件**（Pages 上限 1 GB）|
| 单集成本 | CI 上 30–50 分钟（0.7–5.7 sec/音频秒，机器差异大）|
| 波次 | 20 档矩阵；pipeline 固定 20 shards，手工可调 |

`complete=true` 不代表 0 丢弃：合理带外的 cue 仍可能 `end=null`、`words=[]`，正文保留，
消费方回退 VTT。丢弃是逐句质量标记，不参与整集可用性。多词语音缺口来自修复时的
ASR 不支持、压缩 run 或 >20 秒空档，属主动拒收，不会因重跑而消失。

## 2. 2026-10-29 之后落地的改造

### 2.1 恢复与对齐（`align_timings.py`）

| commit | 作用 |
|---|---|
| `ba634473` | cue 匹配从「精确 6 词前缀」改为 difflib，适应 ASR 漏词/错词 |
| `68cb552d` / `03c4e921` | located 平移上限放宽到 60 秒，并同步移动 cue start；空窗口按无匹配处理 |
| `a2946637` / `0a332542` | 失败组用邻组实测平移重试；孤立丢弃 cue 在邻居间隙里单句补对齐 |
| `807f54a7` / `1844d4d3` | 长 cue 用比例最长块匹配；一个连续锚点覆盖 35% 即可接受 |
| `8d50e7fd` | 实测位置与定位估计差 >8 秒且词长合理时，采纳实测值 |
| `617b4b95` | 平移与邻域不一致的锚点直接丢弃 |
| `08622c3e` / `4165565a` | 拒收前 dump 诊断；缓存整集 ASR 词流；recovery run 可上传 `timings-diag` artifact |
| `898bda81` / `891b50d9` / `00507092` | 末词 0 时长不判废；segment start 收进音频和首词之内；带标点的 `[BRACKET]` 也算非语音 |
| `8d39d3fa` | recovery 后重新统计失败数，避免分母沿用旧值 |
| `12801b70` | 逐 packet demux，跳过损坏音频包，不再整集 `InvalidDataError` |
| `8f2c4c18` / `f16ff6d8` | 对齐输入剥离说话人前缀和前导舞台标签（只影响匹配，不改正文） |
| `5e1144e0` | after-next / over-next 判定优先用下一句实测首词 |
| `33680ae5` | force 重跑被拒时，不用 rejected 覆盖已有 complete sidecar |
| `ef28dde9` | tqdm 进度条全部关闭，改为每组一行 `对齐 N/M 组` 文本进度；ASR 段用 Heartbeat |

### 2.2 时间轴清洗（全量迁移）

| commit | 数据量 | 内容 |
|---|---|---|
| `98aa010f` | 816 个 sidecar | segment start 归到对齐首词 |
| `6f91b1ba` + `abf2c4a5` | **18,487 段** | 首词压住上一句句尾 → 重排到上一句结束之后 |
| `66e5f47f` + `cc56129d` | **12,350 段** | 首词吞掉停顿 → 按自然词长贴到下一个词前（873 文件迁移）|
| `0f624c15` | 代码 | 句内被压成近零时长的 run，用 ASR 词流窗口重定时 |
| `4d8a252b` | 代码 | 压缩 run 匹配改为 SequenceMatcher 模糊匹配 + 中间插值（精确匹配命中率只有约 25%）|

**压缩 run 已知规模**：全库约 **1,231 处 / 641 集**（一串 ≤120 ms 词 + 后面大空档）。

### 2.3 播放器原型

| commit | 内容 |
|---|---|
| `71de99c5` / `8934215` | `docs/prototype.html`：feed 播放、VTT 句级 + timings 词级、7 主题、悬浮播放器/抽屉/详情 |
| `096dd9f4` | 显示层把压缩 run 摊到后面的空档，避免一帧跳过（不改 sidecar）|
| `7752ce31` | 支持本地 JSON 试验包导入与恢复 |

### 2.4 无词 cue 定向补齐（2026-11-01/02）

只补 `words=[]` 的 cue，不改正文、切分与整集状态。

| commit | 作用 |
|---|---|
| `41d489df` | 新增 `--repair-wordless`、独立 ASR 复验与 `repair-wordless.yml` |
| `043e56c6` | 单句只用本句 VTT 区间，禁止借用邻居窗口；非语音屏障与 >20 秒空档直接拒收 |
| `65aaa851` | 独立 ASR 词流复验（覆盖率 ≥30%、中位偏差 ≤1.5 秒） |
| `2c7eeb3c` | 回滚未经 ASR 复验的修复，再按新门槛重跑 |

最终全量 run `41387245320`：**736 集 / 2,091 句修复，2,991 句拒收，0 结构错误**；
`words=[]` 从 17,000 降到 14,909（#900 加入后 14,932）。拒收原因主要是独立 ASR 不支持、
压缩 run 与 >20 秒空档。抽样 15/17 通过复验，2 例仅覆盖率报警、时间无平移。

### 2.5 单一流水线（2026-11-02）

| commit | 内容 |
|---|---|
| `d6cc0178` | `pipeline.yml`：每天 05:00 UTC 跑 `generate → align → feed`；align/deploy 改为 `workflow_call`，删除 align 每小时 cron 与 deploy 每周 cron |
| `7af75f02` | deploy 显式 `ref: main`：reusable workflow 默认落在调用方 SHA，会漏掉 align 刚提交的 sidecar |

`feed` 在 `generate` 成功时即使 align 失败也发布；发布前重跑 `generate.py`（挂上新的
`timings/<集>.json`）与 `generate_zh_feed.py`（只用现有 `translations.json`）。
首次真实 run `41403430421` 抓到 #900；修复后 run `41407224459` 完成 feed 与 Pages 部署。

### 2.6 分片提交历史整理（2026-11-02）

- 把 3,632 个并行分片提交按连续同类批次压成 **32 个提交**：
  `Repair wordless cues` 2,238 → 3（三次全量修复各一个），
  `Add word-level timings` 1,394 → 29；另有 3 个单独分片提交保留原样。
- 提交总数 **3,852 → 252**；最终树与重写前完全一致，非分片提交及其信息全部保留。
- `main` 已 force-with-lease 重写推送；重写前历史保留在本地分支
  `backup/pre-squash-2026-11-02`（未推送）。**本文所有哈希均为重写后的哈希。**

## 3. 被拒集根因（2026-10-29 的历史结论）

当时 711 complete / 162 refused。排除项：不是音频版本差异（HLS 与 archive 差 0.1 秒）、
不是内容类型差异、不是「音频比稿件短」。真实成因两类：

1. **小幅阶梯**：母带与稿件在段界差 2–8 秒，段内稳定在 ±0.3 秒（#477、#455）。
2. **局部对不上**：同一窗口内各句首词偏移散布 7–16 秒，平移无解（#2/#74/#168）——
   稿件与音频本身不一致，或 ASR 在该段失效。

修复路线：`locate_cues()` 整集粗 ASR 定位 → 采纳实测位置 → 按句窗口 + 失败组重试 +
单句补对齐 + 压缩 run 回填。**到 10-31 全部 873 集已 complete，refused=0；**
11-02 官网新增 #900，现为 **900/900**。`GIVE_UP_REASONS`（`audio_mismatch` /
`high_drop_rate`）保持不变，只对真正无解集生效。

## 4. 历史工作与当前环境（2026-11-02）

### 4.1 压缩 run 全量回填（C 波）— 金丝雀已过，全量仍未跑

- 金丝雀 2：run `41206590991`，10 集（130,157,164,256,240,282,71,732,122,135），全部 success。
- 8 集打印 `压缩 run 回填`，合计 **19 处**；#130、#135 没有回填行（无命中或原无压缩 run）。
- 日志：`D:/tmp/tmlt-align/run_41206590991.txt`。
- **641 集的全量重跑尚未派发**。

### 4.2 899 本地深度修复 — 已并入仓库侧重写

`docs/timings/899.json` 已在 11-01 的无词修复中被重写并提交（`e6c39c71`）；
下面的 `final5` 记录保留为当时的取证，**不再作为待写回项**，逐条差异以仓库版本为准。

产物：

- `D:/tmp/tmlt-align/local899/899_final5.json`（当时的最终版）
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

上述 7 处中，cue 30（328.59 秒）与 cue 268（2273.46 秒）在仓库版本里可见；
其余位置已被后续整集重写覆盖，最终以 `e6c39c71` 的版本为准。

### 4.3 大模型验证环境

- 本地现有 turbo：`D:/TMLT/models/ct2-turbo`（1.6 GB）。
- 本轮新下 large-v3 非 turbo：`D:/tmp/tmlt-align/ct2-large-v3`（3.1 GB，int8 CPU 可跑）。
- `huggingface.co` 不可达；`hf-mirror.com` 约 0.8 MB/s；**ModelScope 约 3.5 MB/s**，
  同仓库 `Systran/faster-whisper-large-v3` 可直接下。
- 第三遍 large-v3 在 54 句上：确认 14、校正 2、仍无法识别 38（短促插话/抢话/混音）。

### 4.4 线上发布（2026-11-02）

- 站点：https://lzoreal.github.io/TMLT/，当前线上内容对应提交 `1618d75d`（重写后哈希）。
- Pages 内容 **1810 文件 / 400.2 MiB**：`timings/` 900、`transcripts/` 900
  （`zh/` 899 中译 + `900.vtt` 英文）、`audio/454.mp3` 1 个（67.9 MiB）、根目录 9 个。
- 899 个英文 VTT 因已有中译而不发布；仓库里仍保留。
- 两个 feed 的第 454 集 enclosure 都指向 `https://lzoreal.github.io/TMLT/audio/454.mp3`
  （`aa409697`）。[generate.py](generate.py) 的 `update_resolved_audio` 只在缺失或仍等
  于 `audio` 时重解析，因此每日 pipeline 不会改回官网 archive 地址。
- #900 于 11-02 进入：VTT 907 行、sidecar 302 段 / 3486.82 秒；尚无中译，
  中文 feed 仍是 899 集。

## 5. 运维配方

```bash
PY="C:/Users/游梦/AppData/Local/Programs/Python/Python310/python.exe"
"$PY" -X utf8 align_timings.py --status
"$PY" -X utf8 align_timings.py --plan --shards 20
"$PY" -X utf8 align_timings.py --episodes 477 --force
# 手动跑整条流水线
"$PY" -X utf8 D:/tmp/tmlt-align/api.py dispatch-workflow pipeline.yml \
  '{"refresh_archive": false, "max_minutes": "240"}'
```

- **调度只有 `pipeline.yml`**（每天 05:00 UTC）；align/deploy 自身的 cron 已删除。
- 手工 align run 建议带 `lane`（`probe` / `verify` / `recover`），避免和 pipeline 的
  `tmlt-align-pipeline` 队伍互挤；`cancel-in-progress: false` 只保住正在跑的 run。
- pipeline 固定 **20 shards**；手工可填 1–20，矩阵固定 20 档，多余档位拿空串自己退出。
- 翻译（`translate.yml`）与新集听写（`transcribe.yml`）仍是手工；feed 不会自动翻译。
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
3. **压缩 run 全量回填未跑**：全库约 1,231 处 / 641 集，仅跑过 10 集金丝雀。
4. **无词缺口仍有 3,288 句多词语音**：被独立 ASR、压缩 run 或 >20 秒空档拒收；
   再降需要先做压缩 run 回填或换更强的复验 ASR。
5. **句内时长语义**：长停顿仍记在前一个词的 `dur` 上（高亮会停留数秒），未做全局时长裁剪。
6. **VTT 重写未做**：修正后的 `end` 只在 sidecar，`docs/transcripts/*.vtt` 仍是官网虚报值。
7. **Pages 预算**：仓库 462 MB / 发布 400.2 MiB；每次 align 的 20 个档位仍各上传整份
   `docs/timings` artifact（约 232 MB × 20，保留 7 天），即使档位没分到集。
8. **AntennaPod words 支持未接**：sidecar shape `segments[{start,end,speaker,text,words[[word,start_ms,dur_ms]]}]`；
   ≤20 ms 的词渲染时需约 60 ms 最小显示时长。
9. **中文 feed 落后新集**：翻译仍手工，`translate.yml` 跑之前新集不会进中文 feed
   （当前 #900 未翻译，中文 feed 899 集）。
10. **feed 竞态窗口**：deploy 已显式 checkout main，但若 align 在 feed checkout 之后才提交
    新 sidecar，链接要等下一次 pipeline 才出现；窗口仅数十秒。
