<template>
  <el-dialog
    :model-value="true"
    :title="$t('migrationManagement.title')"
    width="980px"
    top="24px"
    :close-on-click-modal="!busy"
    :close-on-press-escape="!busy"
    :show-close="!busy"
    class="migration-management-dialog"
    @close="$emit('close')"
  >
    <p class="management-intro">{{ $t('migrationManagement.intro') }}</p>
    <p v-if="error" role="alert" class="management-error">{{ $t(error) }}</p>
    <p v-if="capability && !capability.enabled">{{ $t('migrationManagement.disabled') }}</p>
    <p v-else-if="capability && !capability.supported_source && !operation">
      {{ $t('migrationManagement.unsupported') }}
    </p>
    <el-form v-else-if="enabled && !operation && !pendingId" label-position="top" @submit.prevent>
      <div class="management-target">
        <el-form-item :label="$t('feishu.application')">
          <el-select v-model="applicationId" :disabled="busy">
            <el-option
              v-for="app in applications"
              :key="app.id"
              :value="app.id"
              :label="app.name"
            />
          </el-select>
        </el-form-item>
        <el-form-item :label="$t('feishu.group')">
          <el-input v-model="chatId" placeholder="oc_…" :disabled="busy" />
        </el-form-item>
      </div>
      <el-checkbox v-model="reviewedTarget" :disabled="busy">{{
        $t('migrationManagement.reviewTarget')
      }}</el-checkbox>
      <div class="management-actions">
        <el-button type="primary" :disabled="!canPlan" :loading="busy" @click="mutate('plan')">{{
          $t('migrationManagement.plan')
        }}</el-button>
      </div>
    </el-form>
    <section v-if="pendingId" class="management-operation">
      <dl class="management-facts">
        <dt>{{ $t('migrationManagement.operation') }}</dt>
        <dd>
          <code>{{ pendingId }}</code>
        </dd>
        <template v-if="operation">
          <dt>{{ $t('migrationManagement.phase') }}</dt>
          <dd>
            {{ $t(`migrationManagement.phases.${operation.phase}`) }}
            <code>({{ operation.phase }})</code>
          </dd>
          <dt>{{ $t('migrationManagement.target') }}</dt>
          <dd>
            {{ operation.target_application_name || operation.target_application_id }} ·
            <code>{{ operation.target_chat_id }}</code>
          </dd>
          <dt>{{ $t('migrationManagement.digest') }}</dt>
          <dd class="management-digest">
            <code>{{ operation.target_digest }}</code>
          </dd>
          <dt>{{ $t('migrationManagement.revision') }}</dt>
          <dd>{{ operation.source_revision }} → {{ operation.target_revision }}</dd>
          <dt>{{ $t('migrationManagement.next') }}</dt>
          <dd>{{ $t(`migrationManagement.steps.${operation.next_action}`) }}</dd>
          <template v-if="operation.metrics_not_before"
            ><dt>{{ $t('migrationManagement.metricsWait') }}</dt>
            <dd>{{ operation.metrics_not_before }}</dd></template
          >
        </template>
      </dl>
      <el-button :disabled="busy || !enabled" :loading="busy" @click="inspect">{{
        $t('migrationManagement.refresh')
      }}</el-button>
      <div
        v-if="operation && ['source_challenge', 'target_challenge'].includes(operation.next_action)"
        class="management-review"
      >
        <el-form label-position="top" @submit.prevent>
          <el-form-item>
            <template #label
              >{{ $t('migrationManagement.evidence') }}
              <el-tooltip :content="$t('migrationManagement.evidenceHelp')"
                ><el-icon class="help-icon"><QuestionFilled /></el-icon></el-tooltip
            ></template>
            <el-input v-model="publicationRef" maxlength="32" :disabled="busy || !enabled" />
          </el-form-item>
          <el-checkbox v-model="publicationReviewed" :disabled="busy || !enabled">{{
            $t('migrationManagement.reviewPublication')
          }}</el-checkbox>
          <div class="management-actions">
            <el-button
              :disabled="!canChallenge"
              :loading="busy"
              @click="mutate('target-challenge')"
              >{{ $t('migrationManagement.challenge') }}</el-button
            >
          </div>
        </el-form>
      </div>
      <div v-if="challenge" class="management-challenge">
        <p>{{ $t('migrationManagement.challengeHelp') }}</p>
        <code>{{ challenge.command }}</code>
        <p class="management-secondary">
          {{ $t('migrationManagement.expires') }} {{ challenge.expires_at }}
        </p>
        <el-button @click="copyChallenge">{{ $t('common.copy') }}</el-button>
      </div>
      <div v-if="operation" class="management-actions">
        <el-button
          type="primary"
          :disabled="busy || !enabled || needsRefresh || !operation.can_resume"
          :loading="busy"
          @click="confirmAction('resume')"
          >{{ $t('migrationManagement.resume') }}</el-button
        >
        <el-button
          :disabled="busy || !enabled || needsRefresh || !operation.can_abort"
          @click="confirmAction('abort')"
          >{{ $t('migrationManagement.abort') }}</el-button
        >
      </div>
    </section>
    <template #footer
      ><el-button :disabled="busy" @click="$emit('close')">{{
        $t('common.close')
      }}</el-button></template
    >
  </el-dialog>
</template>

<script setup>
import { QuestionFilled } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { useMigrationManagement } from '../utils/useMigrationManagement'
const props = defineProps({
  botId: { type: [Number, String], required: true },
  botUuid: { type: String, required: true },
  identityRevision: { type: Number, default: 0 },
  authorized: { type: Boolean, default: false },
  authScope: { type: String, default: '' },
})
defineEmits(['close'])
const { t } = useI18n()
const {
  capability,
  applications,
  operation,
  applicationId,
  chatId,
  reviewedTarget,
  publicationRef,
  publicationReviewed,
  challenge,
  busy,
  error,
  pendingId,
  enabled,
  canPlan,
  canChallenge,
  needsRefresh,
  inspect,
  mutate,
} = useMigrationManagement(props)
async function confirmAction(kind) {
  const before = operation.value
  if (!before || busy.value) return
  try {
    await ElMessageBox.confirm(
      t(`migrationManagement.confirm.${kind}`, { phase: before.phase }),
      t('migrationManagement.title'),
      { type: kind === 'abort' ? 'warning' : 'info' }
    )
    // A confirmation opened for another operation/phase cannot approve a fresh transition.
    if (operation.value === before && !busy.value) await mutate(kind)
  } catch {
    /* Closing the confirmation does not issue a request. */
  }
}
async function copyChallenge() {
  const value = challenge.value
  if (!value || Date.parse(value.expires_at) <= Date.now()) return
  try {
    await navigator.clipboard.writeText(value.command)
    ElMessage.success(t('common.copied'))
  } catch {
    ElMessage.error(t('common.requestFailed'))
  }
}
</script>

<style scoped>
:global(.migration-management-dialog.el-dialog) {
  display: flex;
  flex-direction: column;
  max-width: calc(100vw - 32px);
  max-height: calc(100dvh - 48px);
  margin-bottom: 24px;
}
:global(.migration-management-dialog .el-dialog__body) {
  overflow-y: auto;
  min-height: 0;
}
:global(.migration-management-dialog .el-dialog__header),
:global(.migration-management-dialog .el-dialog__footer) {
  flex-shrink: 0;
}
.management-intro,
.management-secondary {
  color: var(--el-text-color-secondary);
}
.management-intro {
  margin-top: 0;
}
.management-target {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 20px;
}
.el-select {
  width: 100%;
}
.management-facts {
  display: grid;
  grid-template-columns: 130px minmax(0, 1fr);
  gap: 12px 20px;
  margin: 20px 0;
}
.management-facts dt {
  color: var(--el-text-color-secondary);
}
.management-facts dd {
  margin: 0;
  overflow-wrap: anywhere;
}
.management-digest {
  font-size: 13px;
}
.management-review,
.management-challenge {
  border-top: 1px solid var(--el-border-color-lighter);
  padding-top: 20px;
  margin-top: 20px;
}
.management-review .el-form-item {
  max-width: 420px;
}
.management-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-top: 20px;
}
.management-actions .el-button + .el-button {
  margin-left: 0;
}
.management-challenge code {
  display: block;
  overflow-wrap: anywhere;
  font-size: 13px;
}
.management-error {
  color: var(--el-color-danger);
}
.help-icon {
  color: var(--el-text-color-secondary);
  font-size: 13px;
  vertical-align: middle;
}
@media (max-width: 640px) {
  .management-target {
    grid-template-columns: 1fr;
    gap: 0;
  }
  .management-facts {
    grid-template-columns: 1fr;
    gap: 6px;
  }
  .management-facts dd {
    margin-bottom: 8px;
  }
}
</style>
