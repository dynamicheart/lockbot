# LockBot Agent 轨迹查看器

按测例查看各 Agent 的结果、原始会话和工具调用。页面内置一份合成样例。导入的轨迹只在当前页面读取，刷新后恢复样例。

在本目录用 Node.js 20 或更新版本构建。

```sh
npm ci
npm run build
```

用浏览器打开生成的 `dist/index.html`。查看自己的验收记录时，点击「打开本地 JSON」选择 LockBot 轨迹 v1 文件。页面可以离线使用。

保存真实测试轨迹时，请将其放到仓库外。公开页面使用 `sample.json` 中的合成记录。
