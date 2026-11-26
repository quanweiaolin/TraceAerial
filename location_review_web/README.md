# 地址人工复核 前端

这是地址人工复核工具的管理后台，消费`aerial_backend`的`/location-review/*`
接口：复核队列（截图vs候选地点参考图对比、人工决策、解锁）、文件夹审查、
地点管理、分组管理、LLM与人工一致率看板。详见设计文档
`docs/superpowers/specs/2026-11-26-location-review-frontend-design.md`。

## 鉴权

后端是纯Bearer token鉴权，没有账号密码。先在`aerial_backend`目录下给自己
发一个token：

```bash
cd aerial_backend
uv run python scripts/issue_location_reviewer_token.py --operator-username <你的operator用户名>
```

明文token只在这次脚本运行时打印一次，记下来，贴到前端登录页的输入框里
（存在`localStorage`，刷新页面不用重新贴）。

## 运行

```bash
npm install
cp .env.example .env   # 按需修改VITE_API_BASE_URL，默认指向本地8088端口
npm run dev
```

开发模式下`vite.config.ts`已经把`/location-review`代理到
`http://localhost:8088`，本地起后端服务即可直接用，不需要额外配CORS。

## 测试

```bash
npm test
# 或
npx vitest run
```

## 构建

```bash
npm run build
```
