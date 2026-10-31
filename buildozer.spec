[app]
# 应用名（显示在桌面图标下）
title = 肥鱼音乐
package.name = songgrab
package.domain = org.fishmusic

# 源码目录与打包进 APK 的文件类型
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,ttf,otf,pem
source.exclude_dirs = tests,bin,.buildozer,__pycache__,.github,.git,libs
source.exclude_patterns = *.pyc,*.spec.bak,*.md

version = 1.0.0

# ---------------------------------------------------------------- 依赖
# ffmpeg 以「伪装成 .so 的可执行文件」放进 nativeLibraryDir：
# Android 10+ 禁止从 app 私有目录 exec 二进制，只有 lib/ABI/ 允许执行。
android.add_libs_arm64_v8a = libs/arm64-v8a/*.so
requirements = python3,kivy,yt-dlp,mutagen,certifi,pyjnius,plyer,android

orientation = portrait
fullscreen = 0

# ---------------------------------------------------------------- Android
android.api = 34
android.minapi = 28
android.ndk = 25b
android.archs = arm64-v8a
android.accept_sdk_license = True
android.allow_backup = True
android.presplash_color = #0F1117

# 写 /sdcard/Music 需要「所有文件访问」；自用可行，上架会被 Google 拒
android.permissions = INTERNET,ACCESS_NETWORK_STATE,READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE,MANAGE_EXTERNAL_STORAGE,POST_NOTIFICATIONS

# 启动时申请运行时权限（Kivy 的 android 模块会读这个）
android.wakelock = True

[buildozer]
log_level = 2
warn_on_root = 0
