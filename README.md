# PreferenceWorldBench 评测界面 v3

本地运行的视频偏好评测工具，支持题库检查、A/B 视频盲评、评测任务分配、断点续答、结果导入与汇总。

## 下载已有题库并使用

前往本仓库的 [Releases](https://github.com/Advancer137/wm_evaluation-v3/releases)，下载 v3.0.0 发布中的 **全部 7 个文件**：

```text
评测界面_v3-无视频有batch.z01
评测界面_v3-无视频有batch.z02
评测界面_v3-无视频有batch.z03
评测界面_v3-无视频有batch.z04
评测界面_v3-无视频有batch.z05
评测界面_v3-无视频有batch.z06
评测界面_v3-无视频有batch.zip
```

将它们放在同一文件夹，保持文件名不变，用 Bandizip 等支持分卷 ZIP 的工具打开最后的 `.zip` 并解压。`.zip` 是最后一卷，不是独立完整包，也不要改名成 `.z07`。不需要下载 GitHub 自动生成的 `Source code` 压缩包来代替这些分卷。

使用要求：Windows、Python 3.10 或更高版本、现代浏览器。项目附带 Windows x64 FFmpeg；其他系统需自行安装 FFmpeg。

解压后找到 `evaluation_base/启动管理界面.cmd`，双击启动，并保持启动窗口运行。浏览器会打开 `http://127.0.0.1:8766/`。也可在 `evaluation_base` 目录运行：

```console
python server.py manage --open
```

Release 包包含已有固定题库。若要校验或补做已有结果，在管理页“02 在这台电脑开始评测”展开“从已有结果开始”，选择所需结果 JSON 后启动。独立收集新标注时，应通过管理界面分配并生成新的学生评测包，避免把已有作答当作新标注。

## 仓库里的纯净版

仓库提供程序、题目文本、参考译文、说明文档及随项目附带的 FFmpeg；不包含以下内容：

- `data_source/question_video/` 内的视频文件；
- `evaluation_base/batches/` 内的固定题库；
- 本机作答数据库、生成的结果、缓存及运行报告。

直接下载源码只能获得纯净版。若要直接查看已有视频题库，请使用上面的 Release 分卷包。若要自行建题库，将视频按原目录结构放入 `data_source/question_video/`，启动管理页后检查数据并创建题库。

完整操作说明见 [evaluation_base/README.md](evaluation_base/README.md)。Release 文件校验值见 [SHA256SUMS.txt](SHA256SUMS.txt)。
