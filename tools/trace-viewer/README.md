# LockBot Agent 轨迹查看器

按测例查看各 Agent 的结果、原始会话和工具调用。页面内置一份合成样例。导入的轨迹只在当前页面读取，刷新后恢复样例。

在本目录用 Node.js 20 或更新版本构建。

```sh
npm ci
npm run build
```

用浏览器打开生成的 `dist/index.html`。查看自己的验收记录时，点击「打开本地 JSON」选择 LockBot 轨迹 v1 文件。页面可以离线使用。

保存真实测试轨迹时，请将其放到仓库外。公开页面使用 `sample.json` 中的合成记录。

## 测试实例的在线摘要

在选定的测试实例配置 `LOCKBOT_TRACE_REVIEW_DIR` 指向一个位于前端静态目录之外的私有目录。运行 `npm run build -- --online`，将 `dist/online.html` 安装到该目录并命名为 `viewer.html`。使用同源的 LockBot 超级管理员会话打开 `/dev/trace-review`，页面约每 8 秒读取一次摘要。生产实例可继续沿用默认配置。

评测启动后运行发布工具。它每 2 秒查一次新增结果，按测例终态原子更新文件，待矩阵生成后退出。

```sh
python3 publish_snapshot.py /private/matrix-evidence /private/staging/latest.json --follow
```

通过现有授权传输工具将摘要送到测试实例，先暂存为同一目录下的临时文件，再原子替换 `latest.json`。如需每次变动自动送出，可传 `--on-update /private/transfer-argv.json`。配置是授权传输命令的 argv 数组，发布工具在末尾附加本地摘要路径。

发布器接受合成矩阵中的 `cli:` 测例，按安装与绑定、查询与工作台、资源操作分组。模型连接预检单列。每个测例按测试阶段数发布终态，并显示允许的模型名、判定、轮数、耗时、模拟人工操作次数和检查说明。

发布自己运行的合成夹具时，可加 `--include-dialogue`，附上找回、首次绑定和重置用例中经脱敏的用户提示、模型答复和工具动作。工具详情展示允许的动作、用途和结果状态。真实用户对话、原始工具输出及完整私有轨迹留在本地。在线接口只提供读取。
