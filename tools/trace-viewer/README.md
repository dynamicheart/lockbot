# LockBot Agent 轨迹查看器

按测例查看各 Agent 的结果、原始会话和工具调用。页面内置一份合成样例。导入的轨迹只在当前页面读取，刷新后恢复样例。

在本目录用 Node.js 20 或更新版本构建。

```sh
npm ci
npm run build
```

用浏览器打开生成的 `dist/index.html`。查看自己的验收记录时，点击「打开本地 JSON」选择 LockBot 轨迹 v1 文件。页面可以离线使用。

本地排障可以把完整轨迹直接嵌入一个离线 HTML。输入和生成文件都放在仓库外。此命令为新文件设置 `0600` 权限，原样读取 JSON 内容，并且无法与在线构建同时执行。

```sh
npm run build -- --local-trace /private/traces/full.json /private/traces/index.html
```

公开页面使用 `sample.json` 中的合成记录。

## 测试实例的在线摘要

在选定的测试实例配置 `LOCKBOT_TRACE_REVIEW_DIR` 指向一个位于前端静态目录之外的私有目录。运行 `npm run build -- --online`，将 `dist/online.html` 安装到该目录并命名为 `viewer.html`。前端使用 Vite development build 时，超级管理员可从工作台进入 `/#/agent-traces`；页面约每 8 秒读取一次摘要，原始轨迹约每分钟刷新一次。生产实例可继续沿用默认配置。

评测启动后用 Python 3.10 或更新版本运行发布工具。它每 2 秒查一次新增结果，按测例终态原子更新文件，待矩阵生成后退出。

```sh
python3 publish_snapshot.py /private/matrix-evidence /private/staging/latest.json --follow
```

通过现有授权传输工具将摘要送到测试实例，先暂存为同一目录下的临时文件，再原子替换 `latest.json`。如需每次变动自动送出，可传 `--on-update /private/transfer-argv.json`。配置是授权传输命令的 argv 数组，发布工具在末尾附加本地摘要路径。

发布器接受合成矩阵中的 `cli:` 测例，按安装与绑定、查询与工作台、资源操作分组。模型连接预检单列。每个测例按测试阶段数发布终态，并显示允许的模型名、判定、轮数、耗时、模拟人工操作次数和检查说明。

发布自己运行的合成夹具时，可加 `--include-dialogue`，附上可用且经脱敏的用户提示、模型答复和工具动作。工具详情展示允许的动作、用途和结果状态。Skill 读取还可显示文档名、偏移和分页长度。轨迹快照接口只提供读取；评审意见通过独立评论接口保存。

测试人员可把完整的轨迹 v1 JSON 放入同一个私有目录，文件命名为 `raw.json`，将顶层 `visibility` 设为 `raw`。通过 `/dev/trace-review?raw=true` 查看，使用相同的超级管理员鉴权，支持 32 MiB 以内的完整记录。原始版和脱敏版使用不同的文件入口。为合成评测导出原始对话与工具记录时，可先生成私有文件。

```sh
python3 publish_snapshot.py /private/matrix-evidence /private/staging/raw.json --raw-local
```

工具证据可含 `argv` 数组。它记录评测夹具实际调用的脚本和参数，夹具内通过 Python 直接调用。没有这个字段的旧记录只显示子命令和工具参数。

The administrator navigation exposes the configured trace viewer in a Vue/Vite development build to super administrators. The viewer opens raw traces for local review, and the snapshot API requires current super-administrator authorization.

页面和交互源码位于 `frontend/src/features/agent-traces/`，Vue 路由页为 `frontend/src/views/AgentTraces.vue`。本目录只维护离线/在线网页构建、轨迹投影与发布工具；它们复用同一份前端源码。

## 选中点评

在 Vue 页面中可以任意选中文字点评，也可以点击「点评测例」或工具、消息旁的点评按钮。测例点评独立于模型和运行记录；运行点评保存原始记录摘要及工具/消息位置，避免重新测试后误挂到新回答。侧栏可拖动调整宽度，测例名称切换内容，多模型展开箭头单独操作。

评论接口每次检查当前超级管理员身份。评论保存在私有目录的 `review-comments.sqlite3`，更新轨迹或部署看板时必须保留此文件。保存失败保留浏览器草稿；记录变更时返回 409，不把旧草稿自动挂到新记录。评论可以标记处理状态。

离线 HTML 的评论不会上传。请导出评论文件；Vue 页面中的本机草稿和已保存到测试服务的评论会分别提示。

## 保留历史评论的本地合并

多个安装包版本的结果可以追加到同一份私有看板。先取回测试实例当前实际的
`raw.json` 和 `latest.json` 作为基线；只追加已有 `matrix.json` 的终态运行，
活动运行继续显示为计划。以下命令仅生成本地文件，不会上传或修改评论数据库。

```sh
python3 merge_snapshots.py \
  --base-raw /private/board-base/raw.json \
  --base-latest /private/board-base/latest.json \
  --plan /private/current-run/run.json \
  --matrix /private/native-pilot \
  --matrix /private/current-run \
  --probe /private/connectivity-check \
  --output /private/merged-board --follow
```

`--plan` 为当前评测提供安装包版本和客户端清单；目录中的全部 80 个行为测例
都追加一条明确的计划记录，不代表已经执行或通过。实际运行在各自终态后作为
独立记录追加，失败和旧结果不会被覆盖。连接预检另列，不计为 Skill 行为通过。
不需要连接预检记录时省略 `--probe`，不需要等待时省略 `--follow`。

合并器保留基线的顶层 `version`、已有记录内容和数组位置；这些字段参与评论
锚点摘要，改变任意一项都会使旧评论失效。新记录用自身的 `package_version`
和 `source_run` 标明来源。重新运行命令会检查此前已合并记录的全部锚点，
并按来源去重追加，输入顺序变化也不会移动已有记录。

存在 `manual-semantic-review.json` 时，只有与实际 `response.json` 文件 SHA-256
匹配的复核才会追加为单独记录。它不会改写原始运行的判定；局部语义核对通过
仍保留为待验收，不能推断整轮通过。

`aggregation.json` 记录等待的运行、各文件字节数、SHA-256 和已保留的评论锚点
数量。两份输出通过 v1 结构检查后才替换本地文件，摘要限制 1 MiB，原始版
限制 32 MiB；超限时报错，不截断原始证据。原始版另外保留原生客户端协议
事件，以及失败阶段已经记录的部分回复。所有输出均为 `0600`，部署时仍需
使用已有授权流程，并保留服务端的 `review-comments.sqlite3`。
