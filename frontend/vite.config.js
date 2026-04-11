import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { readFileSync } from 'fs'
import { resolve } from 'path'

// Read version from pyproject.toml (single source of truth)
const pyproject = readFileSync(resolve(__dirname, '../pyproject.toml'), 'utf-8')
const version = pyproject.match(/version\s*=\s*"([^"]+)"/)?.[1] ?? '0.0.0'

export default defineConfig(({ mode }) => {
  const isDemo = mode === 'demo'
  return {
    plugins: [vue()],
    base: isDemo ? '/lockbot/' : '/',
    define: {
      'import.meta.env.VITE_DEMO_MODE': JSON.stringify(isDemo ? 'true' : ''),
      __APP_VERSION__: JSON.stringify(version),
    },
    server: {
      port: 3000,
      strictPort: true,
      hmr: {
        protocol: 'ws',
        // 远程开发时取消注释下一行，替换为你浏览器实际访问的地址
        // host: 'your-remote-host:3000',
      },
      watch: {
        usePolling: false,
      },
      proxy: {
        '/api': {
          target: 'http://localhost:8000',
          changeOrigin: true,
        },
      },
    },
    build: {
      rollupOptions: {
        output: {
          manualChunks: {
            'element-plus': ['element-plus', '@element-plus/icons-vue'],
            'vue-vendor': ['vue', 'vue-router', 'pinia'],
          },
        },
      },
    },
  }
})
