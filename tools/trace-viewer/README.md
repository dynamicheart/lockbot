# LockBot Agent 轨迹查看器

按测例查看各 Agent 的结果、原始会话和工具调用。在线页面直接读取服务数据，离线构建内置一份合成样例。

在本目录用 Node.js 20 或更新版本构建。

```sh
npm ci
npm run build
```

用浏览器打开生成的 `dist/index.html` 查看合成样例。页面可以离线使用。

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

新版执行夹具使用 `script`/`argv` 参数调用脚本，并把 CLI 内部请求标为 `source: packaged_cli`。这些请求单列为「CLI 内部 HTTP」，不计入模型工具调用；HTTP 总数包含模型直接请求与 CLI 内部请求。本地命令没有 HTTP 时计为零。CLI 次数只统计有实际 `argv` 的调用，校验失败且未执行脚本的请求不计入。原始轨迹按记录顺序保留参数、结果及标准输出、标准错误，不改写旧格式的历史记录。

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

`--plan` 为当前评测提供安装包版本、客户端和实际选中的测例清单。目录中的
行为测例只有在看板中尚无任何记录时才添加待评测占位，并明确是否在本次
运行中选中；更换安装包或客户端不会给已有测例重复追加计划。实际运行在
各自终态后作为独立记录追加，失败、历史计划和旧结果不会被覆盖。连接预检另列，不计为 Skill 行为通过。
不需要连接预检记录时省略 `--probe`，不需要等待时省略 `--follow`。

合并器保留基线的顶层 `version`、已有记录内容和数组位置；这些字段参与评论
锚点摘要，改变任意一项都会使旧评论失效。新记录用自身的 `package_version`
和 `source_run` 标明来源。重新运行命令会检查此前已合并记录的全部锚点，
并按来源去重追加，输入顺序变化也不会移动已有记录。

存在 `manual-semantic-review*.json` 时，只有与实际 `response.json` 文件 SHA-256
匹配的复核才会追加为单独记录。`semantic_status` 支持 `pass`、`fail` 和
`uncertain`；不确定结论保持待验收。更正侧车文件使用 `kind: "correction"`，
关联同一答复已有的语义记录，仅追加，不改写原记录或评论。夹具冲突使用
`reason: "fixture_conflict"` 并保持 `semantic_status: "uncertain"`，不能归因于模型。它不会改写原始运行的判定；局部语义核对通过
仍保留为待验收，不能推断整轮通过。

多阶段复核的 `reviews[]` 使用实际的 `phase`（1–4）和对应证据路径。
导出器核对矩阵中的测例、客户端、模型、阶段、安装包版本与冻结文件，
并校验答复文件摘要、可用的结果文件摘要和矩阵行摘要。矩阵行摘要使用
UTF-8、键排序、无空格 JSON 的 SHA-256。没有答复的阶段必须保持不确定，
由实际终态结果或带摘要的未执行矩阵行佐证；未执行阶段不补造提示或答复。
每条新复核明确标出阶段，只展示该阶段实际存在的消息，完整多阶段运行仍
保留在原记录。补充说明和独立的动作安全失败也保留，不会被语义通过掩盖。
旧版复核的来源标识与已发布记录保持不变。

`aggregation.json` 记录等待的运行、各文件字节数、SHA-256 和已保留的评论锚点
数量。两份输出通过 v1 结构检查后才替换本地文件，摘要限制 1 MiB，原始版
限制 32 MiB；超限时报错，不截断原始证据。原始版另外保留原生客户端协议
事件，以及失败阶段已经记录的部分回复。所有输出均为 `0600`，部署时仍需
使用已有授权流程，并保留服务端的 `review-comments.sqlite3`。

## 大矩阵的完整诊断文件

完整用户提示、最终回复和夹具工具的全部输入输出仍在原始快照中。原生客户端
协议还会重复包含这些内容；大矩阵可选择把**新增记录**的完整协议与 stderr
保存成独立的私有文件，避免超过原始快照的 32 MiB 限制。已有记录保持原样，
不会为了节省体积改写历史内容、位置、版本或评论锚点。

为 `merge_snapshots.py` 添加 `--external-native-evidence`。输出目录增加
`evidence/<sha256>` 文件；`aggregation.json` 的 `local_diagnostic_artifacts`
列出本地文件摘要和字节数。文件内容逐字节保留，目录权限为 `0700`，文件为
`0600`，每个文件上限 16 MiB。已有同名内容若被改写会报错，不能覆盖修复或
悄悄截断。普通 `--raw-local` 离线导出默认仍内嵌协议；也可显式使用：

```sh
python3 publish_snapshot.py /private/terminal-run /private/staging/raw.json \
  --raw-local --external-native-evidence
```

部署时先把不可变文件追加到测试看板私有目录的 `evidence/`，保留旧文件并
核对权限，再原子替换 `raw.json` 和 `latest.json`。不要删除已有诊断文件，
也不要替换 `review-comments.sqlite3`。这些命令本身不会上传。

在线查看器的工具详情提供「下载完整诊断」。只有点击后才会请求
`GET /api/dev/trace-review/evidence/{sha256}`；每次下载重新检查当前超级管理员
身份。服务端只读取摘要命名的普通文件，拒绝目录/文件符号链接、非普通文件、
宽松权限、超限和摘要不符，并返回附件、`no-store` 与 `nosniff`。查看器核对
返回摘要与长度；安全浏览器上下文可额外校验内容 SHA-256。下载不会修改
快照记录，因此不会改变已有评论锚点。离线导入带独立诊断引用的索引时会
明确提示文件位置；需要单文件离线轨迹时继续使用默认内嵌导出。

累计多轮结果时，脱敏摘要也可能接近 1 MiB。显式添加
`--compact-new-summary`，仅让新增运行的 `latest.json` 记录保存判定、指标和
说明，并标注「摘要；完整对话和工具输入输出保留在原始轨迹」。新追加的单阶段复核也保留完整说明并使用明确摘要提示，其实际消息仍在原始版。已有摘要
记录与评论锚点保持不变；`raw.json` 的用户提示、最终答复和全部夹具工具
输入输出仍完整保留。此选项不会自动开启，也不会放宽大小限制。

若累计摘要仍接近上限，可显式使用 `--minimal-new-summary`。它只投影**新增**
摘要记录：保留模型、来源记录标识、安装包版本、阶段、判定、独立动作安全
结论和已测得的效率；完整说明、检查清单、证据摘要与单阶段消息保留在原始版。
已有摘要记录和其评论锚点不会改变，原始版内容也不因这个选项改变。每条新增
摘要明确提示完整信息见原始轨迹。CLI 数量仅在实际 `argv` 证据完整时记录，
缺失时保持未知；HTTP 数量和耗时沿用实际记录，不把连接预检当成行为通过。


## 独立且不可变的评测批次

后续完整矩阵可发布到独立批次，避免持续增长旧看板。以下命令只准备本地私有目录，
不上传、不改写根目录已有的 `raw.json`、`latest.json` 或评论数据库。

```sh
python3 publish_dataset.py /private/terminal-matrix /private/dataset-stage \
  --title '完整 Skill 验收批次' --minimal-summary
```

批次只包含该终态矩阵实际选中的测例与可验证的复核，不补造其他测例的计划。
原始快照完整保留实际提示词、答复及夹具工具输入输出；原生协议诊断独立保存在
同级 `evidence/<sha256>`。发布前逐阶段核对源提示、最终答复和夹具工具 I/O，
发现缺失或变化时拒绝登记。原始文件仍限 32 MiB，摘要仍限 1 MiB，单个诊断仍限
16 MiB；超限会失败，不截断。`--minimal-summary` 只缩减摘要中的重复信息。

批次标识是原始 `raw.json` 精确字节的 SHA-256，文件放在
`datasets/<dataset_id>/{raw.json,latest.json,metadata.json}`。目录权限为 `0700`，
文件为 `0600`。已有标识的内容不得覆盖；复核作为独立运行导出时会形成新的标识，旧批次和评论
继续保留。工具最后原子更新 `datasets/index.json`，格式为
`{"schema_version":1,"datasets":["<sha256>"]}`；同一文件标识不会重复登记。

经授权上传时先安装所需诊断文件，再安装完整不可变批次目录，最后更新登记表。
已有登记表须作为本地准备基线保留，不能用不含旧批次的临时登记表覆盖线上版本。
当前看板合并已登记的来源；直接来源链接使用 `dataset_id`，
原有 `case`、数字 `run`、`step` 链接继续指向历史看板。每次读取仍检查当前
超级管理员权限。测例和界面评论全批次共享；模型运行评论绑定所选批次，未保存
草稿不会自动挂到另一批次。评论数据库须保留。

## 在原运行上附加模型复核

资源查询和咨询夹具的通用语义检查清单不等于必须人工批准。对已知的
`resource-cli` v1/v2、咨询 v1/v2/v3，导出器单独记录自动行为结果、模型语义复核和
人工确认要求；未知清单或明确的人工门禁保持原处理。没有语义复核时显示「待复核」，
不宣称全面通过；有独立模型复核后显示「通过」「有建议」或「存疑」。自动失败、
超时和阻塞不会被模型复核覆盖。历史结果字段、运行数量、模型名和消息位置不改写。

先完成独立复核，再将明确的逐例结论绑定到实际证据。`assessment_overlay.py`
只生成旁证，不调用模型，也不从 Markdown 自动推断结论。目标 JSON 包含
`suite`、`scenario`、`client`、`model`、`phase`；首版只支持上述两类夹具的阶段 1。

```sh
python3 assessment_overlay.py --source /private/terminal-matrix \
  --target /private/review-target.json --report /private/model-review.md \
  --reviewer Astra --requested-model gpt-6-astra --status pass \
  --output /private/bound-review.json
python3 publish_dataset.py /private/terminal-matrix /private/dataset-stage \
  --minimal-summary --assessment-review /private/bound-review.json /private/model-review.md
```

`--status findings` 还需 `--findings` 指向显式 JSON 数组，每条含 `severity`、
`blocking`、`text`。请求模型名不推断运行时版本，也不会被记作人工审核。
每条记录绑定提示、答复、工具/HTTP 轨迹、阶段结果、矩阵行、冻结安装包和报告摘要。
变动、缺失、路径越界或符号链接会使导入失败；哈希证明证据对应关系，不证明判断正确。
复核更正使用 `--supersedes` 引用旧记录，保留旧旁证；冲突复核显示存疑。

旁证放在同级 `assessments/<sha256>.json`，登记表为
`assessments/index.json`。这是独立的追加式登记；**不会改变**
`datasets/<dataset_id>` 下三个文件的字节或批次标识，因此原评论 ID 和链接保持不变。
重复导入同一记录不会增加运行或登记项。授权部署时先上传旁证文件，最后更新旁证
登记表，并合并保留已有条目。后端在计算原运行 ID 后才附加这些元数据，仍需当前
超级管理员权限；旁证损坏时原始轨迹和评论仍可读取，损坏的旁证不参与判定。

CLI 发布夹具 v3/v4/v5/v6 可显式启用同一机制：为 `publish_dataset.py` 添加
`--release-assessments`，再通过 `--assessment-review` 附加已绑定的逐例模型复核。
默认导出不为历史 CLI 发布测例新增待复核状态。启用时仅接受已知七个场景和准确的
底层夹具映射，版本读取 `release_fixture_version`，不把底层 CLI 的 29/33 当作发布版本。
任何明确的人工门禁或非空旧复核要求都保留，不能借此免除。

动作自动检查与答复语义仍分别记录。自动通过不证明兼容性、成功说明或状态解读正确；
例如旧 SDK 返回的默认标志不构成线上兼容证据。更新咨询还绑定当前与已安装两个冻结
安装包的 manifest、Skill 和 ZIP；只检查更新不代表允许更新。原始判定和批次字节不改写，
失败、超时或阻塞不会被语义通过覆盖，阻塞性模型意见显示「有问题」。

### CLI35 整条运行复核

`--cli-assessments` 仅显式开启 CLI35 的十二个定向场景；名单和阶段数由
`lockbot.trace_cli_assessment.PHASE_COUNTS` 固定。旧版本、未知场景、未知复核要求和
任何显式人工门禁都不能借此免除。原有阶段 1 旁证继续使用 schema 1；CLI35 使用
schema 2，目标包含 `suite: "cli"`、`scenario`、`client`、`model` 和
`phase_scope: "all"`，绑定原始整条运行，而不是新增一次复核运行。

```sh
python3 assessment_overlay.py --source /private/terminal-matrix \
  --target /private/cli-run-target.json --report /private/model-review.md \
  --reviewer Astra --requested-model gpt-6-astra --status pass \
  --reviewed-phase 1 --reviewed-phase 2 --output /private/bound-cli-review.json
python3 publish_dataset.py /private/terminal-matrix /private/dataset-stage \
  --minimal-summary --cli-assessments \
  --assessment-review /private/bound-cli-review.json /private/model-review.md
```

`--reviewed-phase` 必须显式逐个给出实际复核过的阶段；不会根据文件存在自动推断。
整条运行的计划、矩阵、冻结包和每个预期阶段的输入、输出、工具/HTTP、原生客户端
记录都参与绑定；未执行阶段及缺失文件也有明确记录。修改后续阶段或添加原来缺失的
文件都会使旧复核失效。只有全部阶段已完成且已复核才能给出整条运行语义通过。
部分阶段的意见或存疑可附加，但未覆盖所有预期阶段时总语义结论保持存疑；运行缺失、
超时、受阻及自动失败的原始状态仍优先。复核依据中显示具体已复核阶段。

原生证据文件名由 `lockbot.trace_evidence_files` 统一声明，渲染器与 CLI35 旁证
共用协议和诊断别名。绑定也覆盖 `native-session.json`、`conversation.json`、
客户端调用参数及 fixture 原生记录；空文件与文件不存在分别绑定。
渲染器原生记录沿用 16 MiB 的严格常规文件读取限制。变更这些证据需要重新复核，
不能将旧旁证用于变更后的运行；schema 1 的历史旁证处理保持不变。
