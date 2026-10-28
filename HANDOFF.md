# 临时开发交接文档（接手完成后删除）

> 本文件仅用于跨电脑开发交接，不属于长期项目文档。接手 agent 完成环境重建、问题确认和必要信息迁移后，应删除本文件，并在后续提交中记录删除。不得向本文件补充真实凭据、用户信息或原始日志。

## 交接范围

- 当前开发分支：`feishu`；本次交接重点为飞书适配。
- 项目支持设备、节点和队列资源管理，包含 FastAPI 后端、Vue 管理界面以及独立运行模式。
- 平台注册表包含 Feishu、DingTalk、Slack、Infoflow（百度如流）；适配器存在不代表各平台均已完成真实环境验收。
- 交接文档提交前有 9 个未提交文件。这些功能改动不会因提交本文档而自动提交；推送前需要另行提交，另一台电脑才能获取。

## 当前工作区的飞书改动

| 文件 | 交接关注点 |
| --- | --- |
| `python/lockbot/core/platforms/feishu.py` | 事件解析、群聊提及、回复目标、发送者姓名解析与缓存、回调验证 |
| `python/lockbot/backend/app/bots/router.py` | 按 message_id 优先、event_id 回退进行进程内 TTL 去重；运行及停止状态的回调处理 |
| `scripts/dev/feishu_echo.py` | 独立回声诊断、凭据检查、收发消息、用户名查询和日志 |
| `scripts/dev/feishu.env.example` | 脱敏配置模板和日志选项 |
| `frontend/src/i18n/en.js`、`frontend/src/i18n/zh-CN.js` | 飞书姓名查询权限与应用发布提示 |
| `tests/core/test_feishu_adapter.py` | 适配器回归测试 |
| `tests/backend/test_feishu_webhook.py` | 后端回调、停止状态验证及去重测试 |
| `tests/dev/test_feishu_echo.py` | 独立诊断脚本回归测试 |

关键入口：平台注册表在 `python/lockbot/core/platforms/__init__.py`；核心机器人逻辑在 `python/lockbot/core/`；后端配置在 `python/lockbot/backend/app/config.py`。

## 环境重建

使用 Python 3.10 或更新版本；项目 CI 覆盖 Python 3.10、3.11、3.12，前端 CI 使用 Node.js 20。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cd frontend
npm ci
cd ..
```

后端需要按本机环境配置 `DATA_DIR`（可写的数据目录）、`JWT_SECRET` 和 `ENCRYPTION_KEY`。后两者可在本机通过 `python tools/gen_keys.py` 生成，输出不得写入交接文档或 Git。新环境使用新测试数据即可；如确需迁移旧数据库，须单独安全传递数据库及其匹配的加密密钥。

开发入口：`bash scripts/dev/backend.sh` 和 `bash scripts/dev/frontend.sh`，分别在独立终端运行。后端脚本开启开发模式、自动创建开发管理员，且两个脚本均监听所有网卡；使用前按目标机器网络环境检查配置。后端默认 JWT 密钥和默认开发管理员密码只能用于本地开发，部署前应覆盖；真实部署关闭 `DEV_MODE`。

飞书独立诊断：

```bash
cp scripts/dev/feishu.env.example scripts/dev/feishu.env
# 在本机编辑 feishu.env，填入自己的测试应用凭据。
python scripts/dev/feishu_echo.py --env-file scripts/dev/feishu.env check-token
python scripts/dev/feishu_echo.py --env-file scripts/dev/feishu.env serve
```

诊断服务默认监听 `127.0.0.1:8001`，健康检查为 `/healthz`，回调为 `/feishu/events`。管理平台回调应使用机器人页面提供的地址，不要与诊断服务地址混用。公网回调入口由接手者在本机环境配置，不在仓库保存真实域名或隧道地址。

## 飞书配置与验收

- 使用自建应用并开启机器人能力，订阅 `im.message.receive_v1`，配置正确的事件回调地址。
- 在本机设置 App ID、App Secret、Verification Token；Encrypt Key 根据控制台加密设置保持一致。
- 当前实现允许未配置 Encrypt Key 的明文调试；这会跳过签名验证。Verification Token 留空也会关闭来源 Token 校验。
- 当前前端提示列出 `contact:contact.base:readonly` 和 `contact:user.base:readonly` 以支持姓名显示，修改权限后需要发布应用版本。实际租户授权、可见范围和姓名查询效果需真实环境验收。
- 分别验证明文/加密地址校验、私聊、群聊 @ 机器人、重复投递、非法 Token/签名拒绝、姓名查询失败回退、机器人停止状态回调。
- 执行真实资源命令前准备隔离的测试机器人和测试资源，避免影响现有使用者。

## 本次测试结果与待办

交接整理时执行：

```bash
.venv/bin/python -m pytest -q tests/core/test_feishu_adapter.py tests/backend/test_feishu_webhook.py tests/dev/test_feishu_echo.py
```

结果：**61 通过、3 失败**。失败均位于 `TestFeishuBuildReply`：

| 测试 | 当前行为与断言的差异 |
| --- | --- |
| `test_plain_text` | 群聊回复增加 @ 提及，原断言仅期望正文 |
| `test_list_of_strings` | 群聊回复增加 @ 提及，且末尾换行被去掉 |
| `test_none_content` | 空正文仍产生 @ 提及，原断言期望空字符串 |

接手者应先确认期望的群聊回复行为，再修正实现或测试，不能直接假定断言过时。另有 `datetime.utcnow()` 弃用警告，位于后端回调和日志代码。本次未运行完整测试、前端构建或真实飞书联调，不能据此视为已验收。

建议顺序：解决上述失败 → 完整 pytest 和 lint → 前端 lint/build → 隔离环境真实飞书验收。参考命令：

```bash
pytest -q
ruff check python/ tests/
ruff format --check python/ tests/
cd frontend
npm run lint
npm run build
```

目前去重为单进程内存实现；多 worker 或多实例部署的跨进程去重仍需设计和验证。`ROADMAP.md` 还列有云部署、云数据库和 Redis 状态持久化等未完成项，这些不代表本轮飞书交接必须全部完成。

## 脱敏和推送边界

以下本地材料已被 Git 忽略，保持本机保存，不要强制加入 Git，也不要打包整个工作目录传递：

- `scripts/dev/feishu.env`：应用凭据和回调 Token。
- `scripts/dev/lockbot.db`、`python/lockbot/data/`：数据库、机器人配置和运行状态。
- `python/lockbot/data/.encryption_key`：本地加密密钥。
- `logs/`：可能包含姓名、用户/群 ID、消息内容和调试数据。
- `.venv/`、`frontend/node_modules/` 等本机依赖与缓存。

诊断脚本的 `FEISHU_LOG_RAW` 默认关闭。即使启用后的结构化日志会屏蔽部分凭据字段，消息内容、姓名和标识符仍可能保留，不能视为可公开材料。分享问题时只提供人工脱敏的最小复现。

本次检查未在跟踪文件中发现真实飞书凭据或本地加密密钥；对本地可达 Git 历史的精确匹配也未发现现用 App ID、App Secret、Verification Token 和本地加密密钥。这不是对所有历史凭据的完整审计，也不覆盖远端独有历史、GitHub 附件或其他外部材料。

示例配置、测试和 mock 使用占位或测试数据。`pyproject.toml` 原有作者姓名和邮箱属于项目公开元数据，本次未改动；本文不复写这些信息。

推送前检查 `git status --short` 和暂存差异，确保只包含代码、测试、示例配置和本文档。新开发分支的普通 push 不一定触发现有 CI：当前 CI 仅配置了 `main`、`release/2.x` 的 push，以及以这些分支为目标的 pull request。

## 交接结束后的清理

- 接手者确认能重建环境并掌握上述问题后，将确需长期保留的信息迁移到 README 或对应开发文档。
- **删除 `HANDOFF.md`，并提交删除操作。** 不要将本文继续当作长期进度记录。
