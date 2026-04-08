<template>
  <div class="login-page">
    <div class="login-card">
      <div class="login-logo">
        <svg class="logo-icon" viewBox="0 0 32 32" width="48" height="48" xmlns="http://www.w3.org/2000/svg">
          <rect x="1" y="13" width="4" height="6" rx="1.5" fill="#409eff" opacity="0.7"/>
          <rect x="27" y="13" width="4" height="6" rx="1.5" fill="#409eff" opacity="0.7"/>
          <rect x="5" y="8" width="22" height="19" rx="4" fill="#e6f0ff" stroke="#409eff" stroke-width="1.8"/>
          <circle cx="12" cy="16" r="2.5" fill="#409eff"/>
          <circle cx="20" cy="16" r="2.5" fill="#409eff"/>
          <circle cx="13" cy="15" r="0.8" fill="#fff"/>
          <circle cx="21" cy="15" r="0.8" fill="#fff"/>
          <rect x="11" y="21.5" width="10" height="2" rx="1" fill="#409eff"/>
          <line x1="16" y1="8" x2="16" y2="3.5" stroke="#409eff" stroke-width="1.8" stroke-linecap="round"/>
          <circle cx="16" cy="2.5" r="2" fill="#67c23a"/>
        </svg>
        <h1 class="login-title">{{ $t('profile.forceChangeTitle') }}</h1>
        <p class="login-subtitle">{{ $t('profile.forceChangeHint') }}</p>
      </div>

      <el-form ref="formRef" :model="form" :rules="rules" label-width="0" @submit.prevent="handleSubmit">
        <el-form-item prop="new_password">
          <el-input v-model="form.new_password" :placeholder="$t('auth.newPassword')" type="password" show-password size="large" />
        </el-form-item>
        <el-form-item prop="confirm_password">
          <el-input v-model="form.confirm_password" :placeholder="$t('auth.confirmPassword')" type="password" show-password size="large" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" size="large" class="login-btn" :loading="loading" native-type="submit">
            {{ $t('profile.changePassword') }}
          </el-button>
        </el-form-item>
      </el-form>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { setLocale, getLocale } from '../i18n'
import api from '../utils/api'

const THEME_KEY = 'lockbot_theme'
const { t } = useI18n()
const router = useRouter()
const authStore = useAuthStore()
const formRef = ref()
const loading = ref(false)

const form = reactive({ new_password: '', confirm_password: '' })

const rules = {
  new_password: [{ required: true, min: 6, message: () => t('profile.passwordMin'), trigger: 'blur' }],
  confirm_password: [
    { required: true, message: () => t('auth.passwordRequired'), trigger: 'blur' },
    {
      validator: (_rule, value, callback) => {
        if (value !== form.new_password) callback(new Error(t('profile.passwordMismatch')))
        else callback()
      },
      trigger: 'blur',
    },
  ],
}

// --- Theme (sync with MainLayout) ---
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

const currentLocale = ref(getLocale())

function switchLocale(locale) {
  setLocale(locale)
  currentLocale.value = locale
}

onMounted(() => {
  applyTheme(themeMode.value)
})

async function handleSubmit() {
  try { await formRef.value.validate() } catch { return }
  loading.value = true
  try {
    const res = await api.put('/auth/force-change-password', form)
    authStore.token = res.data.access_token
    localStorage.setItem('token', res.data.access_token)
    await authStore.fetchUser()
    ElMessage.success(t('auth.passwordChanged'))
    router.push('/')
  } catch {
    // handled by interceptor
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
</style>
