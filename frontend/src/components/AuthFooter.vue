<template>
  <div class="auth-footer">
    <a :href="githubUrl" target="_blank" class="github-link" title="GitHub">
      <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor">
        <path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0024 12c0-6.63-5.37-12-12-12z"/>
      </svg>
    </a>
    <el-tooltip :content="themeLabel">
      <el-button text circle @click="cycleTheme">
        <el-icon :size="18">
          <Sunny v-if="themeMode === 'light'" />
          <Moon v-else-if="themeMode === 'dark'" />
          <Monitor v-else />
        </el-icon>
      </el-button>
    </el-tooltip>
    <el-dropdown @command="switchLocale" trigger="click">
      <el-button text circle>
        <span style="font-size: 13px; font-weight: 600">{{ currentLocale === 'zh-CN' ? '中' : 'En' }}</span>
      </el-button>
      <template #dropdown>
        <el-dropdown-menu>
          <el-dropdown-item command="zh-CN" :disabled="currentLocale === 'zh-CN'">中文</el-dropdown-item>
          <el-dropdown-item command="en" :disabled="currentLocale === 'en'">English</el-dropdown-item>
        </el-dropdown-menu>
      </template>
    </el-dropdown>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { Sunny, Moon, Monitor } from '@element-plus/icons-vue'
import { useI18n } from 'vue-i18n'
import { setLocale, getLocale } from '../i18n'

const { t } = useI18n()

const githubUrl = import.meta.env.VITE_GITHUB_URL || 'https://github.com/dynamicheart/lockbot'

// --- Theme ---
const THEME_KEY = 'lockbot_theme'
const themeMode = ref(localStorage.getItem(THEME_KEY) || 'light')
const themeLabel = ref('')

function applyTheme(mode) {
  themeMode.value = mode
  localStorage.setItem(THEME_KEY, mode)
  const dark = mode === 'dark' || (mode === 'auto' && window.matchMedia('(prefers-color-scheme: dark)').matches)
  document.documentElement.classList.toggle('dark', dark)
  themeLabel.value = t(`theme.${mode}`)
}

function cycleTheme() {
  const order = ['light', 'dark', 'auto']
  const next = order[(order.indexOf(themeMode.value) + 1) % order.length]
  applyTheme(next)
}

// --- Locale ---
const currentLocale = ref(getLocale())

function switchLocale(locale) {
  setLocale(locale)
  currentLocale.value = locale
}

onMounted(() => applyTheme(themeMode.value))

defineExpose({ applyTheme, themeMode, currentLocale, switchLocale })
</script>

<style scoped>
.auth-footer {
  display: flex;
  justify-content: center;
  gap: 4px;
  margin-top: 8px;
}
.github-link {
  color: var(--lb-text-primary);
  transition: color 0.2s;
  display: flex;
  align-items: center;
  margin-right: 8px;
}
.github-link:hover {
  color: var(--lb-color-primary);
}
</style>
