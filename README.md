# This American Life → Podcasting 2.0 Transcript RSS

把 This American Life 官网的节目 RSS/页面转换成一个可供 AntennaPod 等 Podcasting 2.0 播放器订阅的 RSS，并为每集生成 WebVTT transcript。

TAL 官网 transcript 页面提供 “Audio and Transcript Sync”。本项目尝试从页面嵌入的数据或时间戳属性中提取同步信息；如果页面结构变化导致无法取得真实时间戳，程序会跳过该集，不会伪造时间码。

## 功能
- 抓取 TAL 官方 transcript 页面
- 提取同步时间轴
- 生成 `transcripts/<episode>.vtt`
- 生成 `podcast.xml`
- 写入 `<podcast:transcript type="text/vtt" ... />`
- GitHub Actions 定时更新
- GitHub Pages 托管 RSS + VTT

## 本地测试
```bash
python -m pip install -r requirements.txt
python generate.py --episodes 20
```

## GitHub Pages
1. 新建 GitHub repository。
2. 上传本项目。
3. Settings → Pages → Source 选择 GitHub Actions。
4. Actions 运行后，订阅：
`https://你的用户名.github.io/你的仓库/podcast.xml`
5. 在 AntennaPod 中添加这个 RSS。

本项目不重新托管 TAL 音频，而是继续引用官方 RSS 的 enclosure URL。

本项目是非官方工具，不代表 This American Life、WBEZ 或 PRX。
