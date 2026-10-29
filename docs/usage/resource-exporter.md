# 独立资源指标采集器

本工具运行在 Lockbot 业务进程之外，使用独立 Prometheus 保存指标。网页和 Agent 查询是 best effort：不证明机器空闲、任务归属或锁仍有效，不触发释放、续期或终止进程。Lockbot 后端不采集、代理或保存指标。

## 开销与采样

配置明确的机器清单，不扫描网络。支持本机及集中 SSH 模式；后者发送内置只读 Python 探针，不在每台机器安装常驻服务。使用已有 SSH 密钥/agent，不将密钥放 JSON。SSH 使用 BatchMode 与主机指纹校验；首次连接记录指纹，已知指纹变更时拒绝。

默认每台机器 **300 秒**采集一次，完成后追加 0–30 秒随机错峰，首次分散在一个周期内。最多两台并发，同一台不重叠或堆积补采；高级配置硬下限 60 秒。多开网页或手动刷新不能提高 SMI 频率。XPU SMI 使用驱动全局锁，低频与超时降低干扰，但不保证零开销，需测实际驱动版本。

昆仑芯 XPU 使用已核对支持的 `xpu-smi -q -x`，NVIDIA 使用 `nvidia-smi -q -x`。解析 XML 明确索引和字段，不按表格行号认卡，不把机器平均值替代逐卡值。NVIDIA 路径已有合成 XML 回归，仍需真实 GPU 验收。设备更换或索引重排后，管理员需重新核对资源映射。

SMI 默认超时 10 秒，SSH 总超时 25 秒，超时终止对应子进程组。每台探针有文件锁，避免本工具重复采样；不能限制其他软件的 SMI。CPU 利用率是相邻 `/proc/stat` 差值的周期平均值，首个样本不输出假 0；主机内存为 `MemTotal - MemAvailable`。磁盘仅按配置路径采容量，使用非敏感别名，不表示 I/O 性能。

## 指标协议 v1

采用标准 Prometheus 文本指标和 HTTP 查询 API。`node` 是数据源配置的机器身份，`device` 是 SMI 明确设备索引，通过管理员映射对应 Lockbot 资源；不同厂家共用名称、单位。

| 指标 | 标签 | 含义 |
| --- | --- | --- |
| `lockbot_accelerator_utilization_percent` | node, device, vendor | 0–100 计算利用率 |
| `lockbot_accelerator_memory_used_bytes` / `lockbot_accelerator_memory_total_bytes` | node, device, vendor | 每卡已用/总显存，字节 |
| `lockbot_accelerator_process_count` / `lockbot_accelerator_container_count` | node, device, vendor | 去重后的进程/可识别容器数，无法判断时不输出 |
| `lockbot_accelerator_process_info` | node, device, vendor, pid, container_id, container_name, user, command | 值恒为 1，关联进程信息 |
| `lockbot_accelerator_process_info_truncated` | node, device, vendor | 1 表示截断或部分不可读，不是完整列表 |
| `lockbot_host_cpu_utilization_percent` | node | 主机 CPU 周期平均利用率 |
| `lockbot_host_memory_used_bytes` / `lockbot_host_memory_total_bytes` | node | 主机内存，字节 |
| `lockbot_host_filesystem_used_bytes` / `lockbot_host_filesystem_total_bytes` | node, filesystem | 指定文件系统容量，字节；filesystem 是别名 |
| `lockbot_sample_timestamp_seconds` | node, collector | 最近成功的源采集 Unix 秒；collector 为 host 或 accelerator |
| `lockbot_collector_success` | node, collector | 最近一次采集成功 1，失败 0 |
| `lockbot_collector_duration_seconds` | node, collector | 源采集耗时，不含网页请求时间 |

不为 Prometheus sample 附加 timestamp。Prometheus 抓取时间不代表 SMI 重新运行：用 `lockbot_sample_timestamp_seconds` 判断新鲜度，不能用 `timestamp(利用率指标)`冒充源时间。5 分钟采样建议超过 660–900 秒标记过期；失败立即显示采集异常。旧值可以有限保留，但不标为刚采集。未知、权限不足、N/A、无效值不转成 0。

## 容器、用户和命令

遍历每卡全部 XML 进程，按 PID 去重后读 cgroup，一次探针最多调用一次 `docker ps` 查询容器名。卡级显存采用 SMI 聚合，不把共享/重复进程显存求和。最多每机器 512 个不同 PID、每卡 128 条详情，超限或不可读标记为不完整。

授权资源视图可显示容器名、PID、占用卡、主机 POSIX 用户及命令摘要。**主机用户不等于 IM 用户，不能据此推断容器所有者**；未知保持未知。进程可能已退出，信息只代表源采样时刻。

命令摘要仅保留程序及可选脚本的文件名，其余参数统一省略；不发布完整任意参数、环境变量、密码、token、路径或 Docker 配置。进程读取前后核对开始时间，变动则放弃，避免常见 PID 重用误归属。容器名和用户仍是敏感运维信息，数据源需独立授权，不能匿名暴露全局指标。Agent 私人备注不因接入指标而公开。

进程标签会产生时序基数，应限制保留时间和容量并观察压力，不长期保留进程历史。仓库仅提交合成样例，不提交真实机器清单、XML、用户名、容器或路径。

## 部署与访问

示例在 [`docs/examples/resource-exporter`](../examples/resource-exporter)。配置选择单机或 SSH 清单：

```sh
python -m lockbot.metrics.exporter --config /private/path/fleet.json \
  --listen 127.0.0.1 --port 9911 --lock-file /run/lockbot-resource-exporter/collector.lock
```

也可将 `python/lockbot/metrics/exporter.py` 作为独立标准库脚本部署，无须导入或启动 Lockbot 后端。建议 Python 3.10+。目标需要 Python 3 和对应 SMI，部署权限尽量限制为只读采集；网页不能提交命令或目标地址。

Prometheus 每 60 秒抓内存缓存，不增加 SMI 调用。建议独立 TSDB、7 天/1 GB 上限，示例 service 替换实际安装路径、配置、权限后使用，不覆盖现有 Prometheus。配置文件与 SSH 身份保持私有。

Prometheus 和 exporter 绑定 loopback。浏览器使用独立受保护的只读入口，限制机器与指标，精确配置 CORS；不能开放完整查询/管理/写入接口。CORS 不是授权。数据源认证独立于 Lockbot，不向数据源发送 Lockbot JWT、个人 key、OP key 或网页只读码。

验收分别确认 exporter、Prometheus、源时间、故障回退、访问范围及网页显示。支持只读采集不代表正式入口、TLS、鉴权或真实 GPU 环境已经验收。

参考：[Prometheus 指标格式](https://prometheus.io/docs/instrumenting/exposition_formats/)、[HTTP 查询 API](https://prometheus.io/docs/prometheus/latest/querying/api/)。

## 独立只读查询入口

`python -m lockbot.metrics.gateway --config /private/path/gateway.json` 可以作为独立指标服务提供 `POST /api/v1/query`，不是 Lockbot 后端代理。默认示例没有 reader，拒绝所有数据查询。由管理员发放独立随机只读 Bearer，配置仅保存 SHA-256 摘要和允许的源 node 集合，原 key 单独安全交付；不复用 Lockbot 的任何凭证。

只接受固定协议指标集合与明确 node 子集的 selector；不支持任意 PromQL、正则通配、聚合表达式、管理/写入接口。原始 Prometheus 继续只监听 loopback。请求仅允许 `query/time/timeout`，重复字段、未知字段、超过 32 KiB 的正文、超过 4 秒的查询超时均拒绝。一次响应最多 2 MiB/4096 条时序，超限不截断伪装完整。调用有每 reader 30 次/分钟、全局 120 次/分钟限制和 15 秒缓存，认证与范围校验在缓存前执行。

每次 POST 重新读取 reader 配置，撤销或缩小范围后不再使用旧缓存。拒绝上游返回范围外 node/metric，丢弃不在协议内的标签。仅对管理员配置的精确 origin 返回 CORS，预检可无 Bearer，实际数据请求仍必须认证。原 key 不放 URL 或公共 bot 配置；浏览器只在当前会话内存中使用源凭证，离开/切换来源后清除。

模板 service 默认监听 loopback；管理员开放独立入口时需选择监听地址、访问网段与 HTTPS。裸 IP 的内网 HTTP 部署并不加密源 Bearer，生产环境应根据网络边界选择 TLS/受控入口。私有认证配置不能随示例或容器名样例上传仓库。

## 验证记录与限制

开发验收使用经授权的既有清单，已完成 16 台机器、64 张卡的真实采样；单次 SMI 观测耗时低于 0.2 秒。这是当前测试环境的观测，不是所有驱动和负载的性能承诺。集中采集和数据查询分离后，100 次读取缓存也不会调用 SMI。升级修正了大数输出精度，Unix 时间及大容量字节使用完整数值，不用默认六位有效数字格式化。

NVIDIA 的真实硬件、不同 XML 版本、MIG/虚拟卡映射，以及不同企业网络下的 HTTPS/浏览器权限仍需要各自验收。机柜连接配置、私有只读 key 和实际部署地址不在本公开文档中。部署成功也不代表每个 Lockbot bot 已配置好数据源或所有客户端都有源访问权限。

### 网页自动短期授权

网关还支持由获信任的 Lockbot 实例签发的短期 HS256 JWT。使用独立签名 key，不复用管理 JWT_SECRET、个人 key、OP key 或网页只读码。仓库已依赖 PyJWT，独立网关运行环境也必须安装它；不手写 JWT 加密。

配置 `audience` 为网关规范地址（无尾斜杠），并为每个受信实例设置：

```json
{
  "audience": "https://metrics.example.invalid",
  "issuers": {
    "service-instance-id": {
      "key_file": "/private/grant-keys/service.key",
      "bots": {"bot-uuid": ["source-node-a", "source-node-b"]}
    }
  }
}
```

JWT 使用 `Authorization: Bearer`，必须包含 `iss`（实例标识）、`sub`（bot UUID）、`aud`（网关地址）、整数 `iat/exp` 和去重的明确 `nodes` 数组（最多 512 项）。`exp > iat`，最长 300 秒；校验当前时间及可选整数 `nbf`，时钟余量 15 秒。请求节点必须属于这个 issuer/bot 的管理端允许集合；签名有效不代表可以读其他机器。

网关每次读当前 issuer/bot 配置和 key，先验证再查缓存；限流主体为 issuer + bot，不因每 30 秒换一份短期 token 获得新预算。原不透明只读 key 保留作运维后备。网页如何取得授权、网页只读权限到期/撤销，以及 Lockbot 侧签发接口单独实现；仅部署本模块不等于已经接通网页自动授权。

key 文件必须是普通文件、0600、32–4096 字节，按原始 bytes 读取，不 strip 或解码。建议独立系统用户运行网关，key 目录 0700 属该用户，key 0600 属该用户；公共配置只给该用户只读权限。不要与 Prometheus 共用能读取签名 key 的服务身份。仅添加 QA issuer/bot 的测试不能当作正式授权配置完成。

### Host memory and cache

The additive gauges `lockbot_host_memory_available_bytes` and `lockbot_host_memory_cache_bytes` use `/proc/meminfo` during the existing scheduled host probe (default 300 seconds). Available memory is `MemAvailable`; buffers/cache is `Buffers + Cached + SReclaimable`, in bytes. This is a documented counter definition, not a guarantee that every distribution's `free` release calculates its display identically. Existing used memory remains `MemTotal - MemAvailable`. Missing optional cache counters are omitted, never replaced with zero. Older exporters remain usable and the browser displays unknown supplemental counters.

Cache size alone does not diagnose driver OOM or allocation failures. Inspect available memory alongside cache and investigate driver logs separately; Lockbot never drops caches or triggers additional SMI probes on refresh.

## 部署组成与交付内容

数据链路：`xpu-smi / nvidia-smi XML → exporter → Prometheus → 只读查询网关 → 浏览器 / Agent`。Prometheus 不解析厂商 XML。采集器 `python/lockbot/metrics/exporter.py` 执行 SMI 并解析 XML；XPU 使用 `xpu-smi -q -x`，NVIDIA 使用 `nvidia-smi -q -x`，主机信息来自 procfs。SSH 集中模式将相同探测代码发送给配置的目标执行，不要求每台机器安装整个 Lockbot 服务。

只复制 Prometheus YAML 不够，需要以下部分：

- 采集器脚本或已安装的 Lockbot Python 包、私有机器清单、SSH 访问配置和目标 SMI。采集器使用 Python 标准库。
- Prometheus 官方发行的程序（或官方 `prom/prometheus` 容器）、抓取配置，以及独立 TSDB 持久化目录。
- 只读查询网关及 PyJWT 依赖、私有授权范围、独立签名 key 和 CORS 配置。
- Lockbot 中的数据源模板、节点/设备映射和签名 key 文件配置。

`docs/examples/resource-exporter/` 提供脱敏配置和 systemd 模板。Python 模块随 Lockbot wheel 发布，无需单独的 XML 解析产出包；可安装同版本 wheel 后分别运行 `python -m lockbot.metrics.exporter` 和 `python -m lockbot.metrics.gateway`。部署独立脚本时须同步依赖和版本。

当前验收部署使用 systemd 管理 exporter、Prometheus 和网关；Prometheus 不是容器运行。程序、私有配置和 TSDB 独立于 Lockbot 容器。官方容器是可选部署方式，当前交付的是 systemd 模板，尚未验证容器部署。无论选择哪种方式，都需要保留数据目录。
