# Lockbot frontend

Vue 3, Vite and Element Plus UI for Infoflow/Feishu resource management. Run these commands from `frontend/` in a checkout of `main`:

```bash
npm ci
npm run dev
npm run lint
npx vitest run
npm run build
```

The development UI uses port 8001 and proxies `/api` to the backend at port 8002. Start the backend from the repository root with `bash scripts/dev/backend.sh` after installing `.[dev,feishu]`. Hot-reload scripts are for development. The production Docker image serves the built UI with the backend.

See the [main README](../README.md), [deployment guide](../docs/agents/deployment/AGENTS.md) and [contribution rules](../AGENTS.md).
