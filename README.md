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
src/picker.py          选片链（过滤 → 加权打分 → 作者判定），纯离线可测
src/metasrc.py         曲库查作者：QQ音乐 / Apple Music / 网易云（按优先级）
docs/选片规则_冻结版.md   选片规则唯一规格（冻结，带 sha256 校验）
libs/arm64-v8a/libffmpeg.so  静态 ffmpeg（伪装成 .so 以获得 exec 权限）
buildozer.spec      打包配置
.github/workflows/build.yml  CI 构建 APK
```

## 与 Windows 原版的差异

| 项 | 原版 | 这里 |
|---|---|---|
| 界面 | tkinter | Kivy（触摸友好） |
| ffmpeg | 外部 ffmpeg.exe | **内置**：15MB 静态 aarch64 二进制，随 APK 分发 |
| cookie | Edge 浏览器登录态 | 可选 `cookies.txt`（手机无浏览器 cookie 解密） |
| 输出目录 | 桌面 | `/sdcard/Music/SongGrab` |
| 打开目录 | explorer | Android Intent → 文件管理器 |
| 配置位置 | exe 同级 | 应用私有目录 |

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
费倪锦）在这里改掉即可，改名与 m4a 的 artist 标签都会跟着变；留空则是主动标
「未命名」，下载阶段不会去查也不会瞎写作者，文件名叫 `歌名+未命名.m4a`。

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
