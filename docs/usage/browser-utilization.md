# 网页利用率数据源

资源网页可以按需显示设备利用率。锁和排队信息仍来自 Lockbot，利用率由浏览器直接查询你选择的 Prometheus 服务。Lockbot 后端不采集、不代理、不存储利用率，也不会因此扫描所有机器人。

## 使用

由机器人所有者或平台管理员在后台的**机器人详情 → 集群状态 → 利用率**中统一配置。普通查看者不能修改，保存后该 bot 的授权网页会自动展示利用率；未配置时不展示利用率列、开关或配置入口。移除数据源后，网页下一次成功刷新状态时停止查询。

后台填写地址、指标、标签和映射，点击“验证并保存”，验证通过后单独保存数据源配置，无需进入机器人编辑页。测试从管理员当前浏览器执行实际指标查询，检查响应结构、样本新鲜度和资源映射，不只检查健康页；通过不代表其他用户的网络与 CORS 策略也一定可用。测试不会 lock、book 或改变资源状态。修改数据源无需重启机器人。

Lockbot 数据库仅保存这份非敏感的配置，不保存指标或数据源密钥。配置随现有备份恢复；旧数据库升级默认没有数据源，旧客户端省略此字段不会清空配置。已授权的只读网页能读取地址、指标及映射，因此这些字段不能包含秘密或私密信息。不要把 Token 放入地址、映射或名称。

如果数据源需要独立 Bearer token，管理员测试时和查看者点击“数据源验证”时分别填写自己的只读凭证，仅保存在当前页面内存，刷新后清除。管理员测试凭证不会共享给查看者。网页权限失效、切换 bot、隐藏页面时停止请求；失去权限或切换 bot 时清除凭证。Lockbot 原有个人 key、OP key、管理 JWT 和网页只读码不会被转发到数据源。

### 请管理员准备的信息

可把本节交给自己的 Agent 协助配置，提供以下**非敏感**信息：

1. 浏览器可访问的 Prometheus 根地址或入口前缀，以及 Lockbot 网页 origin（协议、IP／域名、端口）。
2. 三项指标的准确名称、显存单位、包含节点和稳定设备身份标签的一条脱敏样本；默认适配 DCGM，其他 exporter 可提供等价指标。
3. Lockbot 节点到指标节点标签值的映射；按卡资源还需 `node-a:dev0` 到物理设备 UUID 的映射。
4. 数据源采集间隔和用户访问范围；提供方如何做只读认证、CORS、限流及短缓存。

不要把 App Secret、Lockbot key 或共享数据源密钥发到群或写进配置示例。服务端保存配置不代表服务端代理查询；正式接入前，请用普通查看者浏览器测试同一个资源网页。

数据源需要允许浏览器访问，并配置自己的认证和资源权限。不要把 Lockbot key 填入数据源 Token。网络地址不能带用户名、密码、查询参数或 fragment；只支持 HTTP/HTTPS，HTTPS 页面要求 HTTPS 数据源。跨域或局域网权限失败时，页面只提示指标不可用，不影响队列。不要求安装浏览器扩展，也不执行远程 JavaScript。

## 标签与资源映射

Prometheus 地址填写服务根地址或入口前缀，例如 `https://metrics.example.com/prometheus`，组件会请求其 `/api/v1/query`。默认节点标签为 `instance`，设备标签为 `UUID`。

例如网页上有 `node-a`，实际 Prometheus `instance` 是 `host-a:9400`：

```json
{"node-a": "host-a:9400"}
```

按卡机器人还需将精确资源 ID 映射到设备 UUID：

```json
{
  "node-a:dev0": "GPU-stable-id-a",
  "node-a:dev1": "GPU-stable-id-b"
}
```

只按标签值匹配，不根据查询结果顺序、卡型号或用户名称猜测。重启或更换设备后请检查映射。导出器没有稳定 UUID 时可以显式选择其设备身份标签，但应自行保证逻辑卡与物理卡不被悄悄调换。

NODE/QUEUE 整机行也逐张展示上报卡的计算利用率和已用/总显存，不把多卡平均成一个值。统一指标模式还显示 CPU、主机内存与文件系统占用；可展开每卡容器和进程。未上报的物理卡保持未知，不能据此证明整机空闲。DEVICE/QUEUE_DEVICE 对应精确卡显示计算利用率与已用/总显存。未知映射显示 `—`，不当作 0%。

## 指标模板

默认配置：

| 内容 | DCGM 指标 | 单位 |
| --- | --- | --- |
| 计算利用率 | `DCGM_FI_DEV_GPU_UTIL` | 百分比，0–100 |
| 已用显存 | `DCGM_FI_DEV_FB_USED` | MiB |
| 总显存 | `DCGM_FI_DEV_FB_TOTAL` | MiB |

其他导出器可在数据源侧提供等价的单卡 gauge；填入实际指标名并选择 Bytes 或 MiB。指标必须有相同的节点与设备身份标签。自定义模式不运行任意 PromQL 表达式，不显示历史曲线。统一指标模式按下文协议读取容器/进程摘要，不读取任意命令行或环境变量。

每轮对这三个指标分别查询值和 `timestamp(raw_metric_selector)`，同一轮固定同一个评估时间，一共六个批量查询，最多两个同时进行；查询数量不随卡数线性增长。节点标签匹配按已配置的当前 bot 节点构造并转义，查询使用 POST 表单，不把凭证放在 URL。每个响应最多 2048 条序列和 1 MiB，单轮超时 15 秒；上游 query timeout 为 4 秒。

`timestamp()` 用于读取 Prometheus 中原始样本时间，而不是将 instant-vector 的查询评估时间冒充采样时间。默认 90 秒未更新标为过期，超前本机时钟 30 秒以上视为异常；Prometheus 和浏览器主机应正确同步时间。该时间表示 Prometheus 样本时间，不证明 exporter 内部采集一定新鲜；如果 exporter 持续重复旧设备读数，必须由数据源自身暴露/保证真实采集状态。不要通过实时重算旧数据的 recording rule 伪造新鲜度。

计算负载以颜色辅助阅读：低于 30% 为绿色，30%–79% 为琥珀色，80% 及以上为红色；数字仍保留，颜色不替代数值。锁与排队状态独立显示，绿色低负载不代表资源可申请。

未返回、NaN/Inf、越界或重复设备身份均显示未知，过期样本明确标记；读失败显示“暂不可用”，不将旧值作为新的空闲判断。低利用率不代表锁可抢，也不触发释放、续期、任务中止或进程清理。

## 统一硬件协议（Lockbot v1）

数据源选择“Lockbot 统一指标”，适用于独立 exporter 的 XPU、GPU 和主机指标。配置仍是 Prometheus 类型，`profile: lockbot-v1`，节点标签 `node`，设备标签 `device`，显存单位 Bytes。节点和设备必须显式映射，NODE/QUEUE 无需为卡创建额外的锁资源。

- 卡：`lockbot_accelerator_utilization_percent`、`lockbot_accelerator_memory_used_bytes`、`lockbot_accelerator_memory_total_bytes`。
- 主机：`lockbot_host_cpu_utilization_percent`、`lockbot_host_memory_used_bytes`、`lockbot_host_memory_total_bytes`。
- 文件系统：`lockbot_host_filesystem_used_bytes`、`lockbot_host_filesystem_total_bytes`，标签 `filesystem` 是管理员别名。
- 新鲜度：`lockbot_sample_timestamp_seconds{collector="accelerator"|"host"}` 为真实采样时刻，`lockbot_collector_success` 表示最近采集是否成功。不能用 Prometheus 查询/抓取时刻替代它。
- 归属：`lockbot_accelerator_process_info` 的标签包含卡、PID、容器标识、容器名、主机用户、程序摘要；遍历多条进程，列表超限由 `lockbot_accelerator_process_info_truncated` 标记。主机用户不是经过核验的群聊身份或容器所有者。缺失信息不猜测。

浏览器每轮使用一个固定指标白名单的批量查询，按当前 bot 已映射节点过滤；最多 8192 条序列、4 MiB 响应、4 秒源查询 timeout。不得把前端过滤当作授权，数据源入口仍须按读者范围限制查询。

采集器默认每台 5 分钟并错开采样，Prometheus 抓缓存和页面刷新不会触发 SMI。网页建议 60 秒读取一次缓存，660 秒未采样显示过期；失败显示暂不可用，不以缓存旧值证明当前空闲。CPU 利用率由两次计数器差值计算；进程刚退出、PID 变化等均可能造成归属暂时缺失。

容器名和主机用户是管理员授权展示的运维归属信息。程序摘要只保留程序/脚本名称，不把秘密参数、环境变量或完整任务命令写入指标。Agent 私人任务备注不会被复制进这里。

已授权的 Agent 可从 `/api/agent/context.utilization` 发现同一份非敏感配置，再用数据源自己的凭据查询；可达性、成功、新鲜度和准确性均是 best effort。源查询失败不影响 Lockbot；不能据此释放、续期或抢占资源。

## 自动短期授权（可选）

授权流程图、组件责任及撤销边界见 [指标访问与授权设计](../design/telemetry-authorization.md)。Prometheus 本身不依赖 Lockbot；配套部署只开放受控查询网关，普通查看者使用短期只读授权。

对采用配套只读网关的数据源，管理员可选 `accessMode: short_lived`。Lockbot 只签发只读指标授权，不查询 Prometheus；浏览器直接访问指标网关，无需再输入源 key。

- 单数据源部署可保留 `METRICS_GRANT_SECRET_FILE`；跨信任域部署使用 `METRICS_GRANT_SIGNERS_FILE`，按精确 audience 选择各自独立签名文件。两种模式均不能复用登录 JWT 密钥、个人 key、OP key 或网页码；参见[多数据源签名配置](telemetry-deployment.md#source-specific-signing-keys)。
- 网关配置固定 audience 为自身规范 URL、允许的 Lockbot instance issuer、bot UUID 及对应节点白名单；保存同一独立签名 key 的私有副本。
- JWT 只使用 HS256，最长 300 秒，包含固定 audience、issuer、bot UUID 和节点集合。网关校验签名、时间及该 bot 的允许节点；与后台管理 JWT 不互通。
- 网页需先通过原本只读链接校验，才能从资源状态响应取得短授权；授权到期不晚于该网页查看码。Agent 从已绑定 context 取得同类授权；关闭 Agent 接入后不再签发给该 key。
- 短授权不写浏览器持久存储，按原资源状态刷新取得新值，不会触发硬件采样。已经签发的授权通常在最长 5 分钟内自然到期；如需立即停止指标访问，应在网关撤销 bot 范围/issuer。Lockbot key 撤销不等于提前撤销已共享的网页码。
- 管理员“测试并保存”仅向 Lockbot 请求候选配置的短授权；真正的测试查询仍从当前浏览器发出。没有签名配置时显示授权不可用，不把管理凭证当源 key 重试。

对第三方数据源保留 `accessMode: independent`，使用源自己的独立凭证，不要求第三方接受本协议。源故障/授权失败都只影响指标，Lockbot 资源功能保持可用。

## CORS、认证与负载

提供方需要允许网页 origin（含协议和端口）及 POST；如果使用 Bearer token，还需正确响应 `OPTIONS` 并允许 `Authorization` 和表单内容类型。浏览器请求使用 `credentials: omit`，不携带页面 cookies，也不支持将网页登录 cookies 自动转给 Prometheus。请使用提供方自己的只读短期 Token 或合适的访问入口。

CORS 不是权限控制。浏览器中的节点过滤只是节省查询，不是数据源的租户隔离边界。Prometheus 或前置服务必须限制用户可读范围和查询成本；不要公开完整集群指标以换取浏览器“能连通”。Lockbot 网页授权也不能代替上游指标权限。

默认至少 30 秒刷新一轮；手动刷新同样遵循间隔。单页仅一个采集任务，页面隐藏时暂停并取消请求，切换 bot、移除数据源或失去网页权限时停止。失败进行有上限的指数退避，429 遵循可读到的 `Retry-After`；跨域服务需暴露该响应头，浏览器才能读取。浏览器局域网权限和 HTTPS mixed-content 约束仍取决于部署及浏览器，不建议关闭保护设置来绕过。

多个页面各有自己的轮询预算。每 30 秒六次批量请求约为每页 0.2 RPS，100 个同时启用的页面约为数据源 20 RPS；不会转为 Lockbot 的指标请求。提供方应使用短缓存和限流承接并发；V1 没有跨标签页或跨用户统一缓存，也没有 Lockbot 代理作为兜底。指标服务无法连接时仍可使用原网页；管理员也可移除数据源配置。

## 实现边界

借鉴 Grafana 的“数据源实例 + 配置编辑 + query/testDatasource + 独立渲染”分层，仅在 Lockbot 保存 bot 级非敏感配置，不移植 Grafana 的后端代理、共享凭证存储和完整插件加载系统。适配器返回内部 `schema_version: 1` 的逐卡快照；不要求上游部署 Lockbot 专用 API 或 Lockbot 命名的指标。

当前提供 Prometheus 适配器及独立部署的采集器、Prometheus 和只读查询网关，已有真实 XPU 集群的逐卡、主机内存及短期授权联调记录；fake provider 用于可重复的界面测试。每个客户端的网络、CORS 和数据源授权仍须单独验证。更广泛的 NVIDIA 部署和采集对驱动锁/工作负载的影响继续在 Issue #28 跟踪。

参考：[Grafana 数据源教程](https://grafana.com/developers/plugin-tools/tutorials/build-a-data-source-plugin)、[Prometheus HTTP API](https://prometheus.io/docs/prometheus/latest/querying/api/)、[Prometheus timestamp](https://prometheus.io/docs/prometheus/latest/querying/functions/#timestamp)、[MDN CORS](https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/CORS)。

### Template setup and troubleshooting

Open the bot detail page → Configuration → Data source. Choose **Lockbot collector** for the unified XPU/GPU/host protocol, **NVIDIA DCGM** for its standard GPU metrics, or **Custom Prometheus** for advanced metric/label overrides. Presets provide metric names, labels and units. Enter the endpoint, then match the already configured bot nodes and devices in the mapping table. Standard device indices are suggestions: verify them against the collector's actual identities. Advanced node JSON import is optional.

Validation queries only the configured node scope from the current browser. Every configured node and device must have fresh, successful samples before saving; failures identify affected resources. Browser network/CORS errors and source authentication errors are reported separately. These checks do not guarantee future reachability. Access credentials remain independent of Lockbot keys; automatic short-lived grants require the existing server/gateway signing configuration described above.

Both the authorized resource page and the administrator's bot detail show the same metrics and resource ordering. Remaining lease time appears beneath each holder. A shared status line shows automatic updates, the earliest observed sample time and affected nodes when samples are missing, failed, invalid or stale. Ask the bot administrator to investigate; Lockbot continues managing queues independently. Frontend polling pauses when the page is hidden and backs off on errors; it reads cached samples rather than initiating hardware collection. Optional host metrics include available memory and buffers/cache; see the exporter guide for exact definitions and OOM limitations.

Multiple isolated sources must not share an HMAC signing secret. Configure the deployment-only signer registry before enabling automatic grants for another trust domain; unknown candidate endpoints fail closed. The registry never enters public bot configuration, Agent discovery or application backups. Per-source signing does not provide client network access or a transport override. See the [deployment runbook](telemetry-deployment.md#source-specific-signing-keys).
