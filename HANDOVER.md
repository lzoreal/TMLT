# 逐词时间轴 — 交接文档

> 状态时间：2026-10-29。上游目标与铁律见 [AGENTS.md](AGENTS.md)。

## 1. 现状

| 项 | 数值 |
|---|---|
| 校对稿 | 873 集（`docs/transcripts`，155 MB）|
| 时间轴 sidecar | 873 个（`docs/timings`，182 MB）|
| 已完成 | **711** 集 `complete=true` |
| 被拒 | **162** 集：`high_drop_rate` 101 / `audio_mismatch` 60 / `error:InvalidDataError` 1（#566，瞬态类，会再试一次）|
| 待办 | 0（cron 每小时巡检，无新集时空跑退出）|
| 单集成本 | CI 上 30–50 分钟（1.5–5.7 sec/音频秒，机器差异 3–4 倍）|
| 波次 | 20 档并行，一轮约 195 集 / 5.6–5.9 小时 |

## 2. 已经落地的工具改造（2026-10-28/29）

| commit | 作用 | 为什么 |
|---|---|---|
| `cc394c0` | `check` job 每轮按当时待办现算 20 档计划，align 只认自己那份 `--episodes` | 静态 `pos % shards` 切片会把先跑完的档位永久钉成空转（实测每档待办 0–31，极差 31）|
| `1110b9b` | `audio_mismatch` / `high_drop_rate` 一次判定即终局（`GIVE_UP_REASONS`）| 第二次尝试的数字与第一次一字不差（#362 66/148 两次、#477 4.8 s 两次），单次 30–50 分钟，历史上白烧 151 个机时 |
| `afc0569` + `80c3e52` | `--offset-probe N`：每集采样 N 段只打偏移曲线；探针报 `left=0` 让分段循环收尾 | 中位数判不出偏移形状；探针不写盘、不提交 |
| `0e6e210` | `lane` 输入：手工 run 用独立并发组 | 同组里待起的 run 会被下一小时的 schedule 顶掉（`cancel-in-progress: false` 只保住正在跑的那个）|
| `04e3506` | 局部重锚：按实测平移重算期望起点，`audio_mismatch` 改判"残差" | 母带与稿件在段界处有几秒差，第一遍对齐其实找对了位置，错的是拿官网 begin 当期望值 |
| `1b9c1f2` | 平移超上限的组只丢本组，不连坐整集 | #455 的 −33.7 s、#526 的 +26.1 s 都来自单个异常组 |
| `5b3ba61` | 平移按句邻域取中位数，不再按组 | 官网 end 是合成的，常把组切成 **1–3 句**（探针 119 个采样里 73 个 n≤3），组中位数是噪声 |
| `287a51a` | `locate_cues()`：整集粗 ASR 一遍，按 6 词前缀单调匹配把每句钉到真实位置；`--locate-first` 先定位再切窗 | 剩余集的组内散布 7–16 秒，**没有任何平移能拟合**，问题在"这句到底在哪" |

## 3. 被拒集的根因（都有实测证据）

排除项：

- **不是音频版本差异**：页面只有一份 MP3，HLS 流（`stream.thisamericanlife.org/{ep}/{ep}_64k.m3u8`）累加时长与 archive 差 **0.1 秒**（#477 3460.2 vs 3460.1 秒）。
- **不是内容类型差异**：括号 cue（3.1% vs 3.7%）、任何括号、全大写、≤2 词短句的比例与成功集几乎一致。
- **不是"音频比稿件短 c 秒"**：`音频时长 − 稿件末句 end` 在拒绝集里 +54…−157 秒乱跳，与中位偏移无相关。

真实成因分两类：

1. **小幅阶梯**（可修）：母带与稿件在段落交界处差 2–8 秒。40 段探针实测 #477：0–22 分钟 +0.6 秒，24–29 分钟 +5.5，30–34 分钟 +7.9，35 分钟以后 +5.0，**每段内部稳定在 ±0.3 秒**；#455 是 0 → −1.5 → −3.2。
2. **局部对不上**（平移无解）：同一 ~100 秒窗口内各句首词偏移的散布中位 **7.3 / 13.6 / 16.0 秒**（#2 / #74 / #168），落在 ±3 秒内的采样只占 36% / 15% / **3%**。这段稿件与音频本身不一致（重剪、垫乐、或 ASR 在该段失效）。

14 集混合样本的恢复结果：**3 集**（#477、#425、#455，残差 0.13–0.23 秒、丢弃 1–5%）。三版平移方案（组级 → 野组隔离 → 按句）都停在这 3 集，所以判断平移路线到顶，转 ASR 定位。

## 4. 正在进行

**run `40902523875`**（`lane=verify`、`commit=false`、`--locate-first`）：8 集顽固样本（2、69、74、165、168、186、192、526）。要看四项：定位覆盖率、ASR 耗时、残差中位、丢弃率。

- 覆盖率高 + 残差 < 3 秒 + 丢弃 ≤10% → 放量恢复；
- 定位覆盖率低（<60%）时脚本会自动退回官网时间轴，不会写坏数据。

## 5. 恢复配方

```bash
# 1) 取被拒集清单
python -X utf8 - <<'EOF'
import json
from pathlib import Path
eps = [int(f.stem) for f in Path("docs/timings").glob("*.json")
       if not json.loads(f.read_text(encoding="utf-8")).get("complete")]
print(",".join(map(str, sorted(eps))))
EOF

# 2) 派一轮恢复（独立 lane，带提交）
POST /repos/lzoreal/TMLT/actions/workflows/align-timings.yml/dispatches
{"ref":"main","inputs":{
  "episodes":"<上一步的清单>","latest":"0","shards":"20","max_minutes":"300",
  "force":true,"commit":true,"probe":"0","lane":"recover","locate":true}}

# 3) 取每集结论（job 结束后才能拉日志）
GET /repos/lzoreal/TMLT/actions/jobs/<job_id>/logs
```

估算：162 集 ×（ASR 一遍 + 对齐一遍）≈ 150–200 机时，20 档并行约 **8–10 小时**。

## 6. 遗留问题

- **162 集被拒**是唯一的大缺口；`complete=true` 的 711 集里，`dropped` 仍可能在 10% 以内（这些 cue 的 `end` 为空，消费方回退 VTT）。
- **VTT 重写还没做**：修正后的 `end` 只存在于 sidecar，`docs/transcripts/*.vtt` 还是官网的虚报 end（每集虚报约 340 秒）。离线脚本从 sidecar 回写 VTT 是便宜的一步，但会改 `docs/transcripts` 的字节（文字不变，只有时间戳）。
- **空闲档位仍会上传 artifact**：一轮 20 档里没活的档位也会上传 `docs/timings`（实测 30,793,648 字节 / 428 个文件）。
- **Pages 预算**：`docs/` 现在约 337 MB（transcripts 155 + timings 182），站点上限 1 GB。再加中文翻译与 VTT 重写要留余量。
- AntennaPod 端读 `words` 还没接（侧车 shape：`segments[{start,end,speaker,text,words[[word,start_ms,dur_ms]]}]`），渲染时需要给 ≤20 ms 的词一个最小显示时长（约 60 ms），否则高亮会闪。

## 7. 下一步候选（按价值排序）

1. 恢复跑 + 验收（先 8 集，再 162 集）；
2. 离线 VTT 重写 + 一次 Pages 部署（改 end 后字幕不再"多挂 5.6 分钟"）；
3. AntennaPod 的 words 支持与高亮；
4. 空闲档位跳过 artifact 上传。
