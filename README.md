# OrangeMonitor

微博与 X 本地归档桌面程序。此仓库是从干净源文件建立的发布工程，不含原电脑的归档、登录状态、日志或数据库历史。

## 下载与首次启动

在 [Releases](https://github.com/WT-0610/monitor/releases) 下载对应系统的压缩包。Windows 解压后运行 `OrangeMonitor.exe`；macOS 解压后打开 `OrangeMonitor.app`。首次启动会在浏览器中打开本地界面，引导登录微博或 X。

程序数据写入系统用户目录：

- Windows：`%LOCALAPPDATA%\OrangeMonitor`
- macOS：`~/Library/Application Support/OrangeMonitor`

压缩包不含用户数据。更换程序包时，已有归档仍留在上述目录。Mac Intel 版不提供离线图片自动打标；其他归档功能正常。macOS 安装包尚未使用 Apple 开发者证书签名。

## 从源码构建

需要 Python 3.12、Node.js 22。安装 `backend/requirements.txt` 和 PyInstaller 6.22.3 后运行 `python build_release.py`。GitHub Actions 分别构建 Windows x64、Mac Intel 和 Apple 芯片版本。构建前会检查模板与工程中是否混入用户数据。
