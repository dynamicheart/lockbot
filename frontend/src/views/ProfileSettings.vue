<template>
  <div>
    <h2 style="margin-bottom: 20px">{{ $t('profile.title') }}</h2>

    <!-- User Info Card -->
    <el-card class="profile-card">
      <template #header>
        <span>{{ $t('profile.userInfo') }}</span>
      </template>
      <el-descriptions :column="1" border>
        <el-descriptions-item :label="$t('auth.username')">
          <div class="info-row">
            <el-avatar :size="24" style="background: var(--lb-color-primary); flex-shrink: 0">
              {{ authStore.user?.username?.charAt(0)?.toUpperCase() }}
            </el-avatar>
            <span>{{ authStore.user?.username }}</span>
          </div>
        </el-descriptions-item>
        <el-descriptions-item :label="$t('auth.email')">{{ authStore.user?.email || '-' }}</el-descriptions-item>
        <el-descriptions-item :label="$t('admin.role')">
          <el-tag v-if="authStore.isSuperAdmin" type="danger" size="small" effect="plain">{{ $t('admin.superAdmin') }}</el-tag>
          <el-tag v-else-if="authStore.isAdmin" type="warning" size="small" effect="plain">{{ $t('admin.adminRole') }}</el-tag>
          <el-tag v-else type="info" size="small" effect="plain">{{ $t('admin.userRole') }}</el-tag>
        </el-descriptions-item>
      </el-descriptions>
    </el-card>

    <!-- Change Password Card -->
    <el-card class="profile-card">
      <template #header>
        <span>{{ $t('profile.changePassword') }}</span>
      </template>
      <el-form ref="pwdFormRef" :model="pwdForm" :rules="pwdRules" label-width="120px">
        <el-form-item :label="$t('auth.currentPassword')" prop="current_password">
          <el-input v-model="pwdForm.current_password" type="password" show-password />
        </el-form-item>
        <el-form-item :label="$t('auth.newPassword')" prop="new_password">
          <el-input v-model="pwdForm.new_password" type="password" show-password />
        </el-form-item>
        <el-form-item :label="$t('auth.confirmPassword')" prop="confirm_password">
          <el-input v-model="pwdForm.confirm_password" type="password" show-password />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="pwdLoading" @click="handleChangePassword">{{ $t('common.save') }}</el-button>
        </el-form-item>
      </el-form>
    </el-card>
  </div>
</template>

<script setup>
import { ref, reactive } from 'vue'
import { useAuthStore } from '../stores/auth'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()
const authStore = useAuthStore()
const pwdFormRef = ref()
const pwdLoading = ref(false)

const pwdForm = reactive({ current_password: '', new_password: '', confirm_password: '' })

const pwdRules = {
  current_password: [{ required: true, message: () => t('auth.passwordRequired'), trigger: 'blur' }],
  new_password: [{ required: true, min: 6, message: () => t('profile.passwordMin'), trigger: 'blur' }],
  confirm_password: [
    { required: true, message: () => t('auth.passwordRequired'), trigger: 'blur' },
    {
      validator: (_rule, value, callback) => {
        if (value !== pwdForm.new_password) callback(new Error(t('profile.passwordMismatch')))
        else callback()
      },
      trigger: 'blur',
    },
  ],
}

async function handleChangePassword() {
  try { await pwdFormRef.value.validate() } catch { return }
  pwdLoading.value = true
  try {
    await authStore.changePassword(pwdForm.current_password, pwdForm.new_password)
    ElMessage.success(t('auth.passwordChanged'))
    pwdForm.current_password = ''
    pwdForm.new_password = ''
    pwdForm.confirm_password = ''
  } catch {
    // handled by interceptor
  } finally {
    pwdLoading.value = false
  }
}
</script>

<style scoped>
.profile-card {
  max-width: 560px;
  margin-bottom: 20px;
}
.info-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
</style>
