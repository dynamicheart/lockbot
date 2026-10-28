# 网页利用率数据源

资源网页可以按需显示设备利用率。锁和排队信息仍来自 Lockbot，利用率由浏览器直接查询你选择的 Prometheus 服务。Lockbot 后端不采集、不代理、不存储利用率，也不会因此扫描所有机器人。

## 使用

在资源网页点击“数据源”，填写地址、标签和映射，再“测试并启用”。默认使用 NVIDIA DCGM 的计算利用率与显存指标；可以修改为其他导出器的等价指标。测试会真正执行查询，检查响应格式、样本新鲜度及设备映射，不只检查健康页。

每次打开网页默认不请求指标。浏览器可以记住当前服务实例、bot 下的非敏感数据源配置；需要手动打开“显示利用率”。数据源凭证仅保存在当前页面内存，刷新后要重新填写。关闭页面或网页权限失效后停止指标请求。Lockbot 原有个人 key、OP key、管理 JWT 和网页只读码不会被转发到数据源。

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

NODE/QUEUE 的整机行展示匹配设备中“已上报卡最高”计算利用率和已上报卡数，hover 可查看逐卡数据；不会用平均值掩盖忙卡；未上报的物理卡不在该统计中，不能据此证明整机空闲。DEVICE/QUEUE_DEVICE 对应精确卡显示计算利用率与已用/总显存。未知映射显示 `—`，不当作 0%。

## 指标模板

默认配置：

| 内容 | DCGM 指标 | 单位 |
| --- | --- | --- |
| 计算利用率 | `DCGM_FI_DEV_GPU_UTIL` | 百分比，0–100 |
| 已用显存 | `DCGM_FI_DEV_FB_USED` | MiB |
| 总显存 | `DCGM_FI_DEV_FB_TOTAL` | MiB |

其他导出器可在数据源侧提供等价的单卡 gauge；填入实际指标名并选择 Bytes 或 MiB。指标必须有相同的节点与设备身份标签。V1 不运行任意 PromQL 表达式、不显示历史曲线，也不读取容器、进程或固件信息。

每轮对这三个指标分别查询值和 `timestamp(raw_metric_selector)`，同一轮固定同一个评估时间，一共六个批量查询，最多两个同时进行；查询数量不随卡数线性增长。节点标签匹配按已配置的当前 bot 节点构造并转义，查询使用 POST 表单，不把凭证放在 URL。每个响应最多 2048 条序列和 1 MiB，单轮超时 15 秒；上游 query timeout 为 4 秒。

`timestamp()` 用于读取 Prometheus 中原始样本时间，而不是将 instant-vector 的查询评估时间冒充采样时间。默认 90 秒未更新标为过期，超前本机时钟 30 秒以上视为异常；Prometheus 和浏览器主机应正确同步时间。该时间表示 Prometheus 样本时间，不证明 exporter 内部采集一定新鲜；如果 exporter 持续重复旧设备读数，必须由数据源自身暴露/保证真实采集状态。不要通过实时重算旧数据的 recording rule 伪造新鲜度。

未返回、NaN/Inf、越界或重复设备身份均显示未知，过期样本明确标记；读失败显示“暂不可用”，不将旧值作为新的空闲判断。低利用率不代表锁可抢，也不触发释放、续期、任务中止或进程清理。

## CORS、认证与负载

提供方需要允许网页 origin（含协议和端口）及 POST；如果使用 Bearer token，还需正确响应 `OPTIONS` 并允许 `Authorization` 和表单内容类型。浏览器请求使用 `credentials: omit`，不携带页面 cookies，也不支持将网页登录 cookies 自动转给 Prometheus。请使用提供方自己的只读短期 Token 或合适的访问入口。

CORS 不是权限控制。浏览器中的节点过滤只是节省查询，不是数据源的租户隔离边界。Prometheus 或前置服务必须限制用户可读范围和查询成本；不要公开完整集群指标以换取浏览器“能连通”。Lockbot 网页授权也不能代替上游指标权限。

默认至少 30 秒刷新一轮；手动刷新同样遵循间隔。单页仅一个采集任务，页面隐藏时暂停并取消请求，切换 bot、关闭指标或失去网页权限时停止。失败进行有上限的指数退避，429 遵循可读到的 `Retry-After`；跨域服务需暴露该响应头，浏览器才能读取。浏览器局域网权限和 HTTPS mixed-content 约束仍取决于部署及浏览器，不建议关闭保护设置来绕过。

多个页面各有自己的轮询预算。每 30 秒六次批量请求约为每页 0.2 RPS，100 个同时启用的页面约为数据源 20 RPS；不会转为 Lockbot 的指标请求。提供方应使用短缓存和限流承接并发；V1 没有跨标签页或跨用户统一缓存，也没有 Lockbot 代理作为兜底。指标服务无法连接时，禁用该插件即可继续使用原网页。

## 实现边界

借鉴 Grafana 的“数据源实例 + 配置编辑 + query/testDatasource + 独立渲染”分层，不移植 Grafana 的后端代理、凭证存储和完整插件加载系统。适配器返回内部 `schema_version: 1` 的逐卡快照；不要求上游部署 Lockbot 专用 API 或 Lockbot 命名的指标。

第一版只有浏览器 Prometheus 适配器。本地 fake provider 已用于浏览器测试；用户实际指标服务尚未指定，不能据此宣称真实集群、企业浏览器策略和真实上游鉴权已完成联调。

参考：[Grafana 数据源教程](https://grafana.com/developers/plugin-tools/tutorials/build-a-data-source-plugin)、[Prometheus HTTP API](https://prometheus.io/docs/prometheus/latest/querying/api/)、[Prometheus timestamp](https://prometheus.io/docs/prometheus/latest/querying/functions/#timestamp)、[MDN CORS](https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/CORS)。
