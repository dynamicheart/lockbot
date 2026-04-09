<template>
  <div class="login-page">
    <div class="login-card">
      <!-- Logo -->
      <div class="login-logo">
        <svg class="logo-icon" viewBox="0 0 32 32" width="48" height="48" xmlns="http://www.w3.org/2000/svg">
          <!-- ears -->
          <rect x="1" y="13" width="4" height="6" rx="1.5" fill="#409eff" opacity="0.7"/>
          <rect x="27" y="13" width="4" height="6" rx="1.5" fill="#409eff" opacity="0.7"/>
          <!-- head -->
          <rect x="5" y="8" width="22" height="19" rx="4" fill="#e6f0ff" stroke="#409eff" stroke-width="1.8"/>
          <!-- eyes -->
          <circle cx="12" cy="16" r="2.5" fill="#409eff"/>
          <circle cx="20" cy="16" r="2.5" fill="#409eff"/>
          <!-- eye highlights -->
          <circle cx="13" cy="15" r="0.8" fill="#fff"/>
          <circle cx="21" cy="15" r="0.8" fill="#fff"/>
          <!-- mouth -->
          <rect x="11" y="21.5" width="10" height="2" rx="1" fill="#409eff"/>
          <!-- antenna -->
          <line x1="16" y1="8" x2="16" y2="3.5" stroke="#409eff" stroke-width="1.8" stroke-linecap="round"/>
          <circle cx="16" cy="2.5" r="2" fill="#67c23a"/>
        </svg>
        <h1 class="login-title">LockBot</h1>
        <p class="login-subtitle">{{ $t('auth.register') }}</p>
      </div>

      <!-- Form -->
      <el-form ref="formRef" :model="form" :rules="rules" label-width="0" @submit.prevent="handleRegister">
        <el-form-item prop="username">
          <el-input v-model="form.username" :placeholder="$t('auth.username')" :prefix-icon="User" size="large" />
        </el-form-item>
        <el-form-item prop="email">
          <el-input v-model="form.email" :placeholder="$t('auth.email')" :prefix-icon="Message" size="large" />
        </el-form-item>
        <el-form-item prop="password">
          <el-input v-model="form.password" :placeholder="$t('auth.password')" type="password" show-password :prefix-icon="Lock" size="large" />
        </el-form-item>
        <el-form-item prop="confirmPassword">
          <el-input v-model="form.confirmPassword" :placeholder="$t('auth.confirmPassword')" type="password" show-password :prefix-icon="Lock" size="large" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" size="large" class="login-btn" :loading="loading" native-type="submit">
            {{ $t('auth.register') }}
          </el-button>
        </el-form-item>
      </el-form>

      <!-- Link to login -->
      <p class="register-hint">
        {{ $t('auth.hasAccount') }} <router-link to="/login" class="link">{{ $t('auth.login') }}</router-link>
      </p>

      <!-- Theme & locale -->
      <div class="login-footer">
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
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { User, Lock, Message } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { setLocale, getLocale } from '../i18n'
import { isDemoMode } from '../utils/demoMode'

const THEME_KEY = 'lockbot_theme'
const { t } = useI18n()
const router = useRouter()
const authStore = useAuthStore()
const formRef = ref()
const loading = ref(false)

const form = reactive({ username: '', email: '', password: '', confirmPassword: '' })

const validateConfirm = (rule, value, callback) => {
  if (value !== form.password) {
    callback(new Error(t('profile.passwordMismatch')))
  } else {
    callback()
  }
}

const rules = {
  username: [
    { required: true, message: () => t('auth.usernameRequired'), trigger: 'blur' },
    { min: 3, max: 64, message: () => t('auth.usernameLength'), trigger: 'blur' },
  ],
  email: [
    { required: true, message: () => t('admin.emailRequired'), trigger: 'blur' },
    { type: 'email', message: () => t('auth.emailInvalid'), trigger: 'blur' },
  ],
  password: [
    { required: true, message: () => t('auth.passwordRequired'), trigger: 'blur' },
    { min: 6, message: () => t('profile.passwordMin'), trigger: 'blur' },
  ],
  confirmPassword: [
    { required: true, message: () => t('auth.passwordRequired'), trigger: 'blur' },
    { validator: validateConfirm, trigger: 'blur' },
  ],
}

// --- Theme (sync with MainLayout & Login) ---
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

onMounted(() => {
  applyTheme(themeMode.value)
})

async function handleRegister() {
  if (!isDemoMode) {
    try { await formRef.value.validate() } catch { return }
  }
  loading.value = true
  try {
    await authStore.register(form.username, form.email, form.password)
    if (isDemoMode) {
      // In demo mode, register auto-logs in — go straight to dashboard
      await authStore.fetchUser()
      authStore.saveAccount({ token: authStore.token, user: authStore.user })
      ElMessage.success(t('auth.loginSuccess'))
      router.push('/')
    } else {
      ElMessage.success(t('auth.registerSuccess'))
      router.push('/login')
    }
  } catch {
    // error handled by interceptor
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-page {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--lb-bg-page);
  transition: background-color 0.3s;
}
.login-card {
  width: 380px;
  padding: 40px 36px 24px;
  background: var(--lb-bg-card);
  border-radius: 12px;
  box-shadow: var(--lb-shadow-card-hover);
  border: 1px solid var(--lb-border-color);
  transition: background-color 0.3s, border-color 0.3s, box-shadow 0.3s;
}
.login-logo {
  display: flex;
  flex-direction: column;
  align-items: center;
  margin-bottom: 32px;
}
.login-title {
  margin: 12px 0 0;
  font-size: 22px;
  font-weight: 700;
  color: var(--lb-text-primary);
}
.login-subtitle {
  margin: 4px 0 0;
  font-size: 14px;
  color: var(--lb-text-secondary);
}
.login-btn {
  width: 100%;
}
.register-hint {
  text-align: center;
  font-size: 13px;
  color: var(--lb-text-secondary);
  margin: 12px 0 0;
  line-height: 1.5;
}
.register-hint .link {
  color: var(--lb-color-primary);
  text-decoration: none;
}
.register-hint .link:hover {
  text-decoration: underline;
}
.login-footer {
  display: flex;
  justify-content: center;
  gap: 4px;
  margin-top: 8px;
}
</style>
