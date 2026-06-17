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
      </el-form>
    </el-card>

    <!-- Backup Configuration -->
    <el-card style="margin-top: 20px">
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center">
          <span>{{ $t('backup.title') }}</span>
          <div>
            <el-button :loading="testingConn" @click="handleTestConnection">
              {{ $t('backup.testConnection') }}
            </el-button>
            <el-button type="warning" :loading="backingUp" @click="handleBackupNow">
              {{ $t('backup.backupNow') }}
            </el-button>
            <el-button type="primary" :loading="savingBackup" @click="handleSaveBackup">
              <el-icon><Check /></el-icon> {{ $t('common.save') }}
            </el-button>
          </div>
        </div>
      </template>

      <!-- Stats row -->
      <div style="margin-bottom: 16px; color: var(--el-text-color-secondary); font-size: 13px">
        {{ $t('backup.schedulerStatus') }}:
        <el-tag size="small" :type="schedulerStatus.running ? 'success' : 'danger'">
          {{ schedulerStatus.running ? $t('backup.running') : $t('backup.stopped') }}
        </el-tag>
        &nbsp;|&nbsp;
        {{ $t('backup.lastHeartbeat') }}:
        {{ formatStatusTime(schedulerStatus.last_heartbeat) || '-' }}
        &nbsp;|&nbsp;
        {{ $t('backup.nextRun') }}: {{ formatStatusTime(schedulerStatus.next_run_at) || '-' }}
        <template v-if="schedulerStatus.last_error">
          &nbsp;|&nbsp;
          {{ $t('backup.lastError') }}: {{ schedulerStatus.last_error }}
        </template>
        <br />
        {{ $t('backup.lastTime') }}: {{ backupForm.backup_last_time || $t('backup.never') }}
        &nbsp;|&nbsp;
        {{ $t('backup.lastStatus') }}: {{ backupForm.backup_last_status || '-' }}
        &nbsp;|&nbsp;
        {{ $t('backup.totalCount') }}: {{ backupForm.backup_total_count || '0' }}
      </div>

      <el-form v-loading="loadingBackup" label-width="140px">
        <el-form-item :label="$t('backup.method')">
          <el-select v-model="backupForm.backup_method" style="width: 200px">
            <el-option label="BOS" value="bos" />
          </el-select>
        </el-form-item>
        <template v-if="backupForm.backup_method === 'bos'">
          <el-form-item :label="$t('backup.bosAk')">
            <el-input v-model="backupForm.backup_bos_ak" type="password" show-password clearable />
          </el-form-item>
          <el-form-item :label="$t('backup.bosSk')">
            <el-input v-model="backupForm.backup_bos_sk" type="password" show-password clearable />
          </el-form-item>
          <el-form-item :label="$t('backup.bosEndpoint')">
            <el-input
              v-model="backupForm.backup_bos_endpoint"
              :placeholder="$t('backup.bosEndpointPlaceholder')"
              clearable
            />
          </el-form-item>
          <el-form-item :label="$t('backup.bosBucket')">
            <el-input
              v-model="backupForm.backup_bos_bucket"
              :placeholder="$t('backup.bosBucketPlaceholder')"
              clearable
            />
          </el-form-item>
          <el-form-item :label="$t('backup.bosPrefix')">
            <el-input
              v-model="backupForm.backup_bos_prefix"
              :placeholder="$t('backup.bosPrefixPlaceholder')"
              clearable
            />
          </el-form-item>
        </template>
        <el-form-item :label="$t('backup.zipPassword')">
          <el-input
            v-model="backupForm.backup_zip_password"
            :placeholder="$t('backup.zipPasswordPlaceholder')"
            clearable
          />
        </el-form-item>
        <el-form-item :label="$t('backup.frequency')">
          <el-select v-model="backupFreqType" style="width: 120px; margin-right: 10px">
            <el-option :label="$t('backup.daily')" value="daily" />
            <el-option :label="$t('backup.weekly')" value="weekly" />
            <el-option :label="$t('backup.monthly')" value="monthly" />
          </el-select>
          <el-select
            v-if="backupFreqType === 'weekly'"
            v-model="backupWeekday"
            style="width: 100px; margin-right: 10px"
          >
            <el-option v-for="d in 7" :key="d" :label="$t('backup.weekday' + d)" :value="d" />
          </el-select>
          <el-select
            v-if="backupFreqType === 'monthly'"
            v-model="backupDay"
            style="width: 100px; margin-right: 10px"
          >
            <el-option v-for="d in 28" :key="d" :label="d + $t('backup.dayUnit')" :value="d" />
          </el-select>
          <el-time-picker
            v-model="backupTime"
            format="HH:mm"
            :placeholder="'HH:MM'"
            style="width: 120px"
          />
        </el-form-item>
        <el-form-item :label="$t('backup.autoEnabled')">
          <el-switch v-model="autoEnabled" />
        </el-form-item>
      </el-form>
    </el-card>
  </div>
</template>

<script setup>
import { ref, watch, onMounted } from 'vue'
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

// Backup state
const loadingBackup = ref(false)
const savingBackup = ref(false)
const testingConn = ref(false)
const backingUp = ref(false)
const backupTime = ref(null)
const backupFreqType = ref('daily')
const backupWeekday = ref(1)
const backupDay = ref(1)
const autoEnabled = ref(false)
const backupForm = ref({
  backup_method: 'bos',
  backup_bos_ak: '',
  backup_bos_sk: '',
  backup_bos_endpoint: '',
  backup_bos_bucket: '',
  backup_bos_prefix: '',
  backup_zip_password: '',
  backup_frequency: '',
  backup_auto_enabled: 'false',
  backup_last_time: '',
  backup_last_status: '',
  backup_total_count: '0',
})
const schedulerStatus = ref({ running: false, last_heartbeat: '', next_run_at: '', last_error: '' })

function formatStatusTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return isNaN(d) ? iso : d.toLocaleString()
}

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

async function fetchBackupSettings() {
  loadingBackup.value = true
  try {
    const res = await api.get('/admin/backup/settings')
    Object.assign(backupForm.value, res.data)
    autoEnabled.value = res.data.backup_auto_enabled === 'true'
    // Parse frequency "daily:HH:MM" / "weekly:D:HH:MM" / "monthly:D:HH:MM"
    const freq = res.data.backup_frequency || ''
    const mDaily = freq.match(/^daily:(\d{2}):(\d{2})$/)
    const mWeekly = freq.match(/^weekly:(\d):(\d{2}):(\d{2})$/)
    const mMonthly = freq.match(/^monthly:(\d+):(\d{2}):(\d{2})$/)
    if (mDaily) {
      backupFreqType.value = 'daily'
      const d = new Date()
      d.setHours(+mDaily[1], +mDaily[2], 0, 0)
      backupTime.value = d
    } else if (mWeekly) {
      backupFreqType.value = 'weekly'
      backupWeekday.value = +mWeekly[1]
      const d = new Date()
      d.setHours(+mWeekly[2], +mWeekly[3], 0, 0)
      backupTime.value = d
    } else if (mMonthly) {
      backupFreqType.value = 'monthly'
      backupDay.value = +mMonthly[1]
      const d = new Date()
      d.setHours(+mMonthly[2], +mMonthly[3], 0, 0)
      backupTime.value = d
    }
    await fetchSchedulerStatus()
  } catch {
    // handled by api interceptor
  } finally {
    loadingBackup.value = false
    backupDirty.value = false
  }
}

async function fetchSchedulerStatus() {
  const res = await api.get('/admin/backup/scheduler/status')
  schedulerStatus.value = res.data
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

async function handleSaveBackup() {
  savingBackup.value = true
  try {
    // Build frequency string from time picker
    let freq = ''
    if (backupTime.value) {
      const d = new Date(backupTime.value)
      const hh = String(d.getHours()).padStart(2, '0')
      const mm = String(d.getMinutes()).padStart(2, '0')
      if (backupFreqType.value === 'daily') freq = `daily:${hh}:${mm}`
      else if (backupFreqType.value === 'weekly') freq = `weekly:${backupWeekday.value}:${hh}:${mm}`
      else freq = `monthly:${backupDay.value}:${hh}:${mm}`
    }
    const payload = {
      ...backupForm.value,
      backup_frequency: freq,
      backup_auto_enabled: autoEnabled.value ? 'true' : 'false',
    }
    // Don't send stats fields
    delete payload.backup_last_time
    delete payload.backup_last_status
    delete payload.backup_total_count
    await api.put('/admin/backup/settings', { settings: payload })
    await fetchSchedulerStatus()
    backupDirty.value = false
    ElMessage.success(t('backup.saved'))
  } catch {
    // handled by api interceptor
  } finally {
    savingBackup.value = false
  }
}

const backupDirty = ref(false)
watch(
  [backupForm, backupTime, backupFreqType, backupWeekday, backupDay, autoEnabled],
  () => {
    backupDirty.value = true
  },
  { deep: true }
)

async function handleTestConnection() {
  if (backupDirty.value) {
    ElMessage.warning(t('backup.saveFirst'))
    return
  }
  testingConn.value = true
  try {
    const res = await api.post('/admin/backup/test-connection')
    if (res.data.ok) {
      ElMessage.success(t('backup.testSuccess'))
    } else {
      ElMessage.error(t('backup.testFailed') + ': ' + (res.data.error || ''))
    }
  } catch {
    ElMessage.error(t('backup.testFailed'))
  } finally {
    testingConn.value = false
  }
}

async function handleBackupNow() {
  if (backupDirty.value) {
    ElMessage.warning(t('backup.saveFirst'))
    return
  }
  backingUp.value = true
  try {
    const res = await api.post('/admin/backup/run')
    if (res.data.success) {
      ElMessage.success(t('backup.backupSuccess'))
      fetchBackupSettings() // Refresh stats
    } else {
      ElMessage.error(t('backup.backupFailed') + ': ' + (res.data.error || ''))
    }
  } catch {
    ElMessage.error(t('backup.backupFailed'))
  } finally {
    backingUp.value = false
  }
}

onMounted(() => {
  fetchSettings()
  fetchBackupSettings()
})
</script>
