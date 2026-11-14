# 接入与验证参考

## 维护文档

优先匹配安装版本的仓库 tag，而不是默认把 main 的新设计当作已安装功能：

- [独立采集与网关产出包](https://github.com/dynamicheart/lockbot/blob/main/docs/usage/telemetry-deployment.md)
- [控制台配置与浏览器验证](https://github.com/dynamicheart/lockbot/blob/main/docs/usage/browser-utilization.md)
- [管理员授权说明](https://github.com/dynamicheart/lockbot/blob/main/docs/usage/datasource-authorization.md)
- [技术授权边界](https://github.com/dynamicheart/lockbot/blob/main/docs/design/telemetry-authorization.md)
- [多数据源协议设计](https://github.com/dynamicheart/lockbot/blob/main/docs/design/metrics-datasource-protocol.md)

Prometheus 是目前适配器，不是唯一可能的数据源。现有非 Prometheus 服务先做协议/认证/网络评估，按通用协议转换；在适配器实现前只能交付计划，不能声称控制台已经接通。

## 生成配置的本地输入

输入只含非秘密字段，真实机器目录本身也应留在私有目录。以下为虚构示例，运行前换成已核验值：

```json
{
  "profile": "lockbot-v1",
  "name": "示例指标源",
  "endpoint": "https://metrics.example.invalid",
  "access_mode": "independent",
  "node_mapping": {"node-a": "source-node-a"},
  "device_mapping": {"node-a:dev0": "accelerator-uuid-a"},
  "interval_seconds": 300,
  "stale_seconds": 660
}
```

`profile` 支持 `lockbot-v1` 与 `dcgm`。后者生成当前自定义 Prometheus profile 的常见 DCGM 指标及 MiB 单位，必须通过实际源样本验证；不同驱动/exporter的指标名不一定相同。自定义指标需按控制台高级字段核对，脚本不猜名称。

`access_mode: short_lived` 仅在已安装源端网关并登记该源签名信任后选择。生成器不会建立该信任。300秒示例刷新对应低频硬件采集；与最长300秒授权是不同参数。stale_seconds应覆盖正常错峰周期但不掩盖真实故障。

## 实际部署与检查

1. 核对 Release 包 checksum，保留 manifest、安装版本和原配置；不运行未知安装脚本。
2. 在已批准部署路径安装，保留 Prometheus loopback监听，独立网关受限对外；systemd unit复制为 /etc 下普通文件，安装目录在挂载盘时设置 RequiresMountsFor，启用开机启动与Restart。只操作此次所属服务。
3. 多机采集核验账号和known_hosts，SSH非交互失败时报告，不自行改变网络或SSH策略。
4. 独立授权源的可分享配置不带token。自动授权用独立签名密钥和源注册表，按源隔离，不能覆盖或复用另一个信任域的密钥。
5. 检查匿名拒绝、合法范围成功、过期/越界拒绝、真实observed_at、0/缺失/失败/过期，以及全部配置卡。采样周期内检查缓存即可，不重复驱动查询。
6. 使用者浏览器验证CORS和可达性；只测试了Agent/管理员端则明确未验证其他用户。故障不能影响锁和队列。恢复配置不能把已撤销token重新变成有效。

## 交付示例

“已生成节点与设备映射；已验证采集器和网关，实际覆盖2节点8卡；配置尚未保存。浏览器网络与CORS待目标用户验证。签名文件由管理员私下安装，不包含在复制内容中。”

只有真的保存并从相同bot重新读取到一致配置，才把状态改为“已保存”；只有实际运行证据才报告已部署。不要以样例数据、文档检查或单一health 200代替这些结果。
