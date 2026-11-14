# 肥鱼音乐 SongGrab（Kivy / Android）

| 主界面 | 设置 |
|---|---|
| ![main](docs/screenshots/main.png) | ![settings](docs/screenshots/settings.png) |


把 B 站歌单批量下载成带标签的 m4a。Android 原生界面（Kivy），由 GitHub Actions
用 buildozer 打包成 APK——**不需要本机有 Android SDK**。

## 目录

```
main.py             Kivy 入口：界面、事件泵、权限
songgrab.kv         全部布局（声明式）
src/songgrab_core.py   业务核心（搜索/选片/下载/台账），仅打了平台补丁
src/platform_compat.py 平台差异：路径、ffmpeg、打开目录、cookie
src/controller.py      编排层：线程 + 事件队列，不依赖 UI 框架
docs/选片规则_冻结版.md   选片规则唯一规格（冻结，带 sha256 校验）
libs/arm64-v8a/libffmpeg.so  静态 ffmpeg（伪装成 .so 以获得 exec 权限）
buildozer.spec      打包配置
.github/workflows/build.yml  CI 构建 APK
```

## 与 Windows 版的差异

**核心引擎是同一套**：手机版的 `src/songgrab_core.py` 就是 Windows 版
[`song-grab1`](https://gitee.com/originthe-very-beginning/song-grab1) v1.1.3 的移植，
只打了平台补丁（路径、ffmpeg、打开目录、cookie）。所以续传、校验、对账、
known_good、故障统计这些能力两边都有。

| 项 | Windows 版 | 这里 |
|---|---|---|
| 界面 | tkinter | Kivy（触摸友好） |
| ffmpeg | 外部 ffmpeg.exe | **内置**：15MB 静态 aarch64 二进制，随 APK 分发 |
| cookie | Edge 浏览器登录态 | 扫码登录 + 可选 `cookies.txt` |
| 输出目录 | 桌面 | `/sdcard/Music/SongGrab` |
| 打开目录 | explorer | Android Intent → 文件管理器 |
| 配置位置 | exe 同级 | 应用私有目录 |
| 输入 | 歌名（+歌手） | 歌名（+歌手）、**B 站链接 / BV 号 / av 号 / b23.tv 短链** |
| 手机端独有 | — | DASH 直链提速、多 P 选择、逐首确认、翻唱宽容 |

## 换用 Windows 版引擎之后

手机版原来的 core 是自研的轻量实现；Windows 版在 `song-grab1` 仓库里迭代到 1.1.3，
多出一批工程能力。现在两边**共用同一套 core**：

| 能力 | 说明 |
|---|---|
| 断点续传 | 停止后重开，接着上次的进度下（`resume.json`） |
| 完整性校验 | 体积须在「码率 × 时长」的 0.7–1.2 倍内，转封装后时长差 ≤5 秒，不达标删文件记失败 |
| 目录对账 | 启动时核对磁盘文件与 `catalog.json`，产出 `compare.json` |
| known_good / known_bad | 记住「这首歌哪个 BV 是对的」，下次直接命中；稿件被删则自动拉黑 |
| 故障统计 | `metrics.jsonl` → 可导出 `metrics.csv`、待补作者清单 |
| 输出格式 | m4a / mp3(320k) / mp4 三态 |
| 失败分类 | 12 类中文原因（风控 / 版权 / 无音轨 / 网络 …） |
| 搜索并发自适应 | 撞到 412/429 自动从 4 路降到 2 路、再到 1 路 |

保留的手机端专有能力：

- **DASH 直链 + 节点测速 + 8 线程分块**（见下一节）
- **多 P 视频**下载前选 P1 / P2 / 全部，文件名带 ` P2` 避免各集互相覆盖
- **逐首确认**：试搜 → 三档结果（完全命中 / 待确认 / 没有选中）→ 用户点选
- **翻唱宽容**：歌名自带「翻唱 / 现场 / 伴奏 / 钢琴」等词时，不做正片过滤，
  并且候选池被过滤光时给加权前三条待选，而不是直接报 0 结果

删掉的模块：`src/picker.py`、`src/metasrc.py` —— 选片链与曲库查作者都已并入 core，
不再有两套实现。

### 粘链接直下

输入框里粘 B 站链接或 BV 号，`scan_bvid()`（`src/songgrab_core.py`）认出来后
**跳过搜索与选片**，直接 `video_info()` 取标题 → `clean_video_title()` 洗成歌名 →
`process_song(bvid=...)` 下载。支持：

    BV1xx411c7mD
    https://www.bilibili.com/video/BV1xx411c7mD?p=1
    https://b23.tv/xxxxxxx          # 短链，跟随一次跳转
    av170001                        # av 号，走 view 接口换成 BV

标题清洗顺序：`《…》` 书名号优先 → 剥【】标签 → 反复剥尾缀（官方MV / 高清 / 4K /
2160P / 完整版 / 无损 / 修复版 / 现场版 / 歌词 …）→ 最多 40 字。

## 下载是怎么提速的

B 站同一个音频会给出 3–4 个 CDN 备用域名，实测速度能差 **10 倍**：
有的节点 3.6 MB/s，有的只有 360 KB/s，碰上 `mcdn.bilivideo.cn`（P2P 节点）
还会直接 `Connection refused`。yt-dlp 只认第一个 `baseUrl`，运气不好就慢。

所以下载改成自己走直链（`src/songgrab_core.py` 的 `_download_dash`）：

1. 走 `playurl` 拿 `dash.audio` 里音质最高那条的 `baseUrl` + `backupUrl`；
2. **并发测速**各节点（每个拉 256KB 计时），挑最快的；
3. **8 线程 Range 分块**并发下载，单块失败自动重试 3 次；
4. 直链这条路整体失败（比如老视频没有 DASH 流）才回退 yt-dlp。

实测（容器内，匿名访问）：同一首歌从 yt-dlp 的 ~100 KB/s / 80 秒，
降到 **2.5–3 MB/s / 2–3 秒**。

## 用起来是什么样

1. 歌单里一行一首；歌名后想指定作者就写 `歌名+歌手`（加号，不是空格）。
2. 点「开始下载」后进入**逐首确认**：先试搜第 1 首 → 弹窗列出候选 → 你点「就这首，继续」/「跳过这首」→ 自动接着试搜第 2 首，直到全部过完才真正开始下载。
3. 弹窗里可以点「曲师：XXX  改」改作者，留空 = 记为 `Unknown`；确认过的音源会记进 `confirm.json`，下次同一首歌不再问。
4. 底部「未确认：跳过 / 也下」是批量模式的开关；逐首确认模式下每首都会问你。

### 想要的就是翻唱 / 现场 / 钢琴版？

歌名里直接写出来（例如 `晴天 翻唱`、`晴天 钢琴`），选片会跳过「非正片」过滤，把这类版本当正片给分。
即使候选池被过滤得只剩翻唱，也会把加权前三交出来让你挑，不会直接报「没有选中」。

## 选片规则（冻结版）

选片链以 `docs/选片规则_冻结版.md` 为**唯一规格**，文件内容与用户给的冻结核对稿
逐字节一致，sha256 记在 `docs/选片规则_冻结版.sha256`：

```
3a6715f3aa6e2286bc79245a764ff67e66c55032c825c28f5e2573d0b153d79a
```

`tests/test_frozen_rules.py` 会校验哈希——改了规则文件测试就红，逼你先改文档的
「修订记录」再同步哈希。要点：

- 搜索走 WBI 签名接口，412/429/503 退避重试，同关键词缓存 5 分钟。
- 12 步硬过滤（非歌曲黑名单、器乐改编、二创壳、片段、专辑合集、超长…），
  剔光就报失败原因，**不硬选**。
- 加权评分里**播放量不进分**（只做 `_play_floor` 折扣与最终并列时的排序），
  所以 300 万播的鬼畜压不过 3 万播的正片。
- 作者链：只认用户 `+` 写明的作者当锚点；标题写着别人 → 判翻唱跳过。
- 流程：输入 → **试搜（三档）** → 用户确认音源 → 按 bvid 直下（下载阶段不再搜索）。

界面对应：试搜会弹「✅完全命中 / ❓待确认 / ❌没有选中」三档；待确认的行是琥珀色，
**点它**能翻候选并「用这首」；底栏「未确认：跳过 / 也下」控制未确认曲目要不要下。

弹窗顶部的「曲师：XXX（QQ音乐）  改」可以点——曲库把作者配错时（比如「显然」被判给
费倪锦）在这里改掉即可，作者会写进 m4a 的 artist / album_artist 标签。

**命名规则**：文件名只写歌名（`晴天.m4a`），作者只出现在音频标签里——这样播放器按
「歌名 / 歌手」两栏正确显示，也不会出现文件名里缺作者或作者写错就不好检索的问题。
曲师留空（或曲库查不到）时，标签记 `Unknown`，文件名仍是纯歌名。

## 构建

推送到 GitHub 的 `main` 分支即自动构建：

```bash
git add -A && git commit -m "feat: android port" && git push
```

在仓库 **Actions → Build APK** 页面等约 20–40 分钟（首次编译 Python+SDL2 慢），
构建完成后在 Artifacts 下载 `songgrab-apk`。

也可手动触发：Actions → Build APK → Run workflow。

## 安装后必做

1. 首次启动会请求存储权限，**全部允许**
2. 打开「设置 → 授予文件访问权限（写 /sdcard 必需）」→ 在系统页面开启「所有文件访问」
   （Android 11+ 不开启就写不进 Music 目录）
3. 回主界面粘贴歌单 → 开始下载

## ffmpeg 是怎么进去的（关键）

Android 10+ 的 SELinux **禁止从 app 私有目录 exec 二进制**（`files/bin/ffmpeg` 会 EACCES），
唯一允许执行的目录是 `nativeLibraryDir`（即 APK 里的 `lib/arm64-v8a/`）。

所以做法是：

1. 静态 aarch64 ffmpeg 改名为 `libs/arm64-v8a/libffmpeg.so`
2. `buildozer.spec` 里 `android.add_libs_arm64_v8a = libs/arm64-v8a/*.so`
3. 运行时用 `getApplicationInfo().nativeLibraryDir` 定位它，直接 exec
4. 取不到就降级为「直接落地 + mutagen 写标签」，**保证下载不失败**

来源：[hzw1199/Android-FFmpeg-Prebuilt](https://github.com/hzw1199/Android-FFmpeg-Prebuilt)
（FFmpeg 9.0，LGPL-2.1，静态链接，minSdk 28）。

> 若某些构建产物的 `nativeLibraryDir` 里没有该文件，通常是 manifest 的
> `android:extractNativeLibs` 被设成了 `false`，需要改回 `true`。

## 已知风险

- **`extractNativeLibs`**：若 APK 装完后 ffmpeg 报「无法执行」，检查此项（见上）
- `yt-dlp` 依赖 `pycryptodomex` / `brotli` 等 C 扩展，p4a 交叉编译可能需要试错
- `minapi = 28`（Android 9+），因为预编译 ffmpeg 要求
- 首次构建失败时看 Actions 里 `build-log` artifact
- `MANAGE_EXTERNAL_STORAGE` 只适合自用；上架 Google Play 会被拒

## 开源许可

本仓库的**代码**以 [MIT](LICENSE) 许可发布，可自由使用、修改、分发与商用，
保留版权声明即可。

仓库里还打包了第三方二进制/字体，它们**不适用 MIT**，各自遵循原许可：

| 内容 | 许可 | 来源 |
|---|---|---|
| `libs/arm64-v8a/libffmpeg.so` | LGPL-2.1 | 静态 FFmpeg，来自 [hzw1199/Android-FFmpeg-Prebuilt](https://github.com/hzw1199/Android-FFmpeg-Prebuilt) |
| `assets/fonts/KaiTi-SC-Regular.ttf` | SIL OFL 1.1 | 由 [LXGW WenKai（霞鹜文楷）](https://github.com/lxgw/LxgwWenKai) 子集化并改名而来，许可全文见 `assets/fonts/OFL.txt` |
| 用本工具下载的音频 | 归原视频作者与 B 站 | — |

## 免责声明

本项目仅用于**个人备份与离线收听**。下载的内容版权归原作者所有，请勿用于
二次分发或商业用途。请遵守 B 站用户协议与当地法律法规；因使用本工具产生的
任何版权纠纷由使用者自行承担。
