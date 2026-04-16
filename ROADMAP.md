# Roadmap

## 2025 H1 — v2.0 Open Source Release

- [x] Bot class: classmethod -> instance mode (multi-bot per process)
- [x] Config: class singleton -> instance config
- [x] Core library decoupled from Flask server
- [x] Multi-instance data isolation tests
- [x] FastAPI backend: auth (JWT), bot CRUD, bot lifecycle, webhook routing
- [x] Sensitive data encryption (Fernet)
- [x] Vue 3 frontend: login/register, bot management, admin panel, logs
- [x] i18n: bilingual (zh-CN / en) for both bot replies and frontend UI
- [x] Role-based access control (super_admin / admin / user)
- [x] Database backup (SQLite)
- [x] JSONL file-based logging
- [x] Auto-detect heterogeneous GPU nodes
- [x] GitHub Actions CI + PyPI auto-publish
- [x] Docker multi-stage build
- [x] Open source cleanup (LICENSE, README, pyproject.toml)

## 2026 Q3 — Multi-Platform Support

- [ ] IM adapter abstraction layer (`core/platforms/`)
- [ ] Slack adapter
- [ ] DingTalk adapter
- [ ] Feishu/Lark adapter
- [ ] WeChat Official Account adapter
- [x] Audit log system (operation history, admin/super_admin visible)

## 2026 Q4 — Cloud & Scale

- [ ] Serverless deployment (Vercel / Cloud Functions)
- [ ] Cloud database support (PostgreSQL / PlanetScale / Neon)
- [ ] Redis-based state persistence for distributed deployment
- [x] Rate limiting and abuse prevention (slowapi, Redis-optional)
