<template>
  <div>
    <div
      style="
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 20px;
      "
    >
      <h2 style="margin: 0">{{ $t('admin.siteSettings') }}</h2>
      <el-button type="primary" :loading="saving" @click="handleSave">
        <el-icon><Check /></el-icon> {{ $t('common.save') }}
      </el-button>
    </div>
    <el-card>
      <el-form v-loading="loading" label-width="140px">
        <el-form-item :label="$t('settings.platformUrl')">
          <el-input
            v-model="form.platform_url"
            :placeholder="$t('settings.platformUrlPlaceholder')"
            clearable
          />
        </el-form-item>
        <el-form-item :label="$t('settings.githubUrl')">
          <el-input
            v-model="form.github_url"
            :placeholder="$t('settings.githubUrlPlaceholder')"
            clearable
          />
        </el-form-item>
        <el-form-item :label="$t('settings.adminContact')">
          <el-input
            v-model="form.admin_contact"
            :placeholder="$t('settings.adminContactPlaceholder')"
            clearable
          />
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

        <el-divider />

        <!-- Enabled IM Platforms — visible to super_admin -->
        <el-form-item :label="$t('settings.enabledPlatforms')">
          <div>
            <el-checkbox-group v-model="form.enabled_platforms">
              <el-checkbox v-for="p in allPlatforms" :key="p" :value="p">{{ p }}</el-checkbox>
            </el-checkbox-group>
            <div style="color: var(--el-text-color-secondary); font-size: 12px; margin-top: 4px">
              {{ $t('settings.enabledPlatformsHint') }}
            </div>
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
const allPlatforms = ref([])
const form = ref({
  platform_url: '',
  github_url: '',
  admin_contact: '',
  news_content: '',
  enabled_platforms: ['Infoflow'],
})

async function fetchSettings() {
  loading.value = true
  try {
    const [settingsRes, platformsRes] = await Promise.allSettled([
      api.get('/admin/settings'),
      api.get('/platforms?all=true'),
    ])
    if (settingsRes.status === 'fulfilled') {
      for (const item of settingsRes.value.data) {
        if (item.key === 'enabled_platforms') {
          try {
            form.value.enabled_platforms = JSON.parse(item.value || '["Infoflow"]')
          } catch {
            form.value.enabled_platforms = ['Infoflow']
          }
        } else if (item.key in form.value) {
          form.value[item.key] = item.value || ''
        }
      }
    }
    if (platformsRes.status === 'fulfilled') {
      allPlatforms.value = platformsRes.value.data.platforms || []
    }
  } finally {
    loading.value = false
  }
}

async function handleSave() {
  saving.value = true
  try {
    const payload = {
      ...form.value,
      enabled_platforms: JSON.stringify(form.value.enabled_platforms),
    }
    await api.put('/admin/settings', { settings: payload })
    ElMessage.success(t('settings.saved'))
  } catch {
    // handled by api interceptor
  } finally {
    saving.value = false
  }
}

onMounted(fetchSettings)
</script>
