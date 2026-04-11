<template>
  <div>
    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px">
      <h2 style="margin: 0">{{ $t('admin.siteSettings') }}</h2>
      <el-button type="primary" @click="handleSave" :loading="saving">
        <el-icon><Check /></el-icon> {{ $t('common.save') }}
      </el-button>
    </div>
    <el-card>
      <el-form label-width="140px" v-loading="loading">
        <el-form-item :label="$t('settings.platformUrl')">
          <el-input v-model="form.platform_url" :placeholder="$t('settings.platformUrlPlaceholder')" clearable />
        </el-form-item>
        <el-form-item :label="$t('settings.githubUrl')">
          <el-input v-model="form.github_url" :placeholder="$t('settings.githubUrlPlaceholder')" clearable />
        </el-form-item>
        <el-form-item :label="$t('settings.adminContact')">
          <el-input v-model="form.admin_contact" :placeholder="$t('settings.adminContactPlaceholder')" clearable />
        </el-form-item>
        <el-form-item :label="$t('settings.newsContent')">
          <el-input
            v-model="form.news_content"
            type="textarea"
            :rows="3"
            :maxlength="30"
            show-word-limit
            :placeholder="$t('settings.newsContentPlaceholder')"
          />
          <div style="color: var(--el-text-color-secondary); font-size: 12px; margin-top: 4px">
            {{ $t('settings.newsHint') }}
          </div>
        </el-form-item>
      </el-form>
    </el-card>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { Check } from '@element-plus/icons-vue'
import api from '../../utils/api'

const { t } = useI18n()
const loading = ref(false)
const saving = ref(false)
const form = ref({
  platform_url: '',
  github_url: '',
  admin_contact: '',
  news_content: '',
})

async function fetchSettings() {
  loading.value = true
  try {
    const res = await api.get('/admin/settings')
    for (const item of res.data) {
      if (item.key in form.value) {
        form.value[item.key] = item.value || ''
      }
    }
  } catch {
    // handled by api interceptor
  } finally {
    loading.value = false
  }
}

async function handleSave() {
  saving.value = true
  try {
    await api.put('/admin/settings', { settings: form.value })
    ElMessage.success(t('settings.saved'))
  } catch {
    // handled by api interceptor
  } finally {
    saving.value = false
  }
}

onMounted(fetchSettings)
</script>
