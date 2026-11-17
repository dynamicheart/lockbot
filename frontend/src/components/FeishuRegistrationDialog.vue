<template>
  <el-dialog
    v-model="visible"
    :title="t('feishu.scan.title')"
    width="min(500px, 94vw)"
    :close-on-click-modal="false"
    :close-on-press-escape="!importing"
    :show-close="!importing"
    @closed="emit('closed')"
  >
    <el-form v-if="!registration" label-position="top" @submit.prevent="start">
      <p>{{ t('feishu.scan.review') }}</p>
      <el-form-item :label="t('feishu.name')">
        <el-input v-model="name" maxlength="128" :disabled="busy" />
      </el-form-item>
      <el-button type="primary" native-type="submit" :loading="busy" :disabled="!name.trim()">
        {{ t('feishu.scan.generate') }}
      </el-button>
    </el-form>

    <div v-else aria-live="polite">
      <p>{{ t(`feishu.scan.states.${registration.status}`) }}</p>
      <div v-if="registration.status === 'waiting'" class="scan-code">
        <img v-if="qrImage" :src="qrImage" :alt="t('feishu.scan.qrAlt')" width="248" height="248" />
        <a :href="registration.url" target="_blank" rel="noopener noreferrer">
          {{ t('feishu.scan.openFeishu') }}
        </a>
      </div>
      <code v-if="registration.status === 'confirmed'">{{ registration.app_id }}</code>
    </div>
    <p v-if="error" class="scan-error" role="alert">{{ t(`feishu.scan.${error}`) }}</p>

    <template #footer>
      <el-button :disabled="importing" @click="visible = false">
        {{ t(registration?.status === 'imported' ? 'common.close' : 'common.cancel') }}
      </el-button>
      <el-button v-if="error === 'statusFailed'" @click="refresh(generation)">
        {{ t('common.refresh') }}
      </el-button>
      <el-button
        v-if="registration?.status === 'confirmed'"
        type="primary"
        :loading="importing"
        @click="importApplication"
      >
        {{ t('feishu.scan.import') }}
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import api from '../utils/api'

const emit = defineEmits(['closed', 'imported'])
const { t } = useI18n()
const path = '/feishu/application-registrations'
const options = { _silent: true }
const active = new Set(['starting', 'waiting', 'importing'])
const cancellable = new Set(['starting', 'waiting', 'confirmed'])
const visible = ref(true)
const name = ref('')
const registration = ref(null)
const qrImage = ref('')
const error = ref('')
const busy = ref(false)
const importing = ref(false)
let generation = 0
let poll

function cancel(id) {
  return api.delete(`${path}/${id}`, options).catch(() => {})
}

function reset() {
  generation += 1
  clearTimeout(poll)
  const previous = registration.value
  registration.value = null
  qrImage.value = ''
  if (previous && cancellable.has(previous.status) && !importing.value) {
    void cancel(previous.id)
  }
}
watch(
  visible,
  (open) => {
    if (!open) reset()
  },
  { flush: 'sync' }
)
onBeforeUnmount(reset)

function accept(data, current) {
  if (current !== generation) return
  const previous = registration.value
  registration.value = data
  error.value = ''
  if (data.status === 'waiting' && data.url !== previous?.url) {
    void renderCode(data.url, current)
  } else if (data.status !== 'waiting') {
    qrImage.value = ''
  }
  if (active.has(data.status)) {
    poll = setTimeout(() => refresh(current), 2000)
  } else if (data.status === 'imported' && previous?.status !== 'imported') {
    emit('imported')
  }
}

async function renderCode(url, current) {
  try {
    const { default: qrcode } = await import('qrcode')
    const image = await qrcode.toDataURL(url, { width: 248, margin: 4 })
    if (current === generation && registration.value?.url === url) qrImage.value = image
  } catch {
    // The private confirmation link stays available when the device cannot render a QR image.
  }
}

async function start() {
  if (busy.value || !name.value.trim()) return
  const current = generation
  busy.value = true
  error.value = ''
  try {
    const { data } = await api.post(path, { name: name.value.trim() }, options)
    if (current !== generation) {
      if (cancellable.has(data.status)) void cancel(data.id)
      return
    }
    accept(data, current)
  } catch {
    if (current === generation) error.value = 'startFailed'
  } finally {
    busy.value = false
  }
}

async function refresh(current) {
  if (current !== generation || !registration.value) return
  clearTimeout(poll)
  try {
    const { data } = await api.get(`${path}/${registration.value.id}`, options)
    accept(data, current)
  } catch (cause) {
    if (current !== generation) return
    if (cause.response?.status === 404) {
      accept({ ...registration.value, status: 'expired', url: undefined }, current)
    } else {
      error.value = 'statusFailed'
    }
  }
}

async function importApplication() {
  if (importing.value || registration.value?.status !== 'confirmed') return
  const current = generation
  const record = registration.value
  importing.value = true
  error.value = ''
  try {
    const { data } = await api.post(`${path}/${record.id}/import`, undefined, options)
    accept({ ...record, status: 'imported', application: data }, current)
  } catch {
    if (current === generation) {
      await refresh(current)
      if (registration.value?.status !== 'imported') error.value = 'importFailed'
    }
  } finally {
    importing.value = false
  }
}
</script>

<style scoped>
p {
  margin: 0 0 16px;
}
.scan-code {
  display: grid;
  justify-items: center;
  gap: 12px;
}
.scan-code img {
  max-width: 100%;
  height: auto;
  border-radius: 8px;
}
.scan-code a {
  color: var(--el-color-primary);
}
.scan-error {
  margin-top: 16px;
  color: var(--el-color-danger);
}
</style>
