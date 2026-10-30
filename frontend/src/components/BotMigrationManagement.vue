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
    <div v-else-if="enabled && !operation && !pendingId" class="reset-layout">
      <el-form label-position="top" @submit.prevent>
        <el-form-item :label="$t('feishu.application')"
          ><el-select v-model="applicationId" :disabled="busy"
            ><el-option
              v-for="app in applications"
              :key="app.id"
              :value="app.id"
              :label="app.name" /></el-select
        ></el-form-item>
        <el-form-item :label="$t('feishu.group')"
          ><el-input v-model="chatId" placeholder="oc_…" :disabled="busy"
        /></el-form-item>
        <el-checkbox v-model="reviewedTarget" :disabled="busy">{{
          $t('migrationManagement.reviewTarget')
        }}</el-checkbox>
        <el-checkbox v-model="clearConfirmed" :disabled="busy" class="reset-ack">{{
          $t('migrationManagement.clearConfirm')
        }}</el-checkbox>
        <div class="management-actions">
          <el-button type="primary" :disabled="!canPlan" :loading="busy" @click="mutate('plan')">{{
            $t('migrationManagement.plan')
          }}</el-button
          ><el-button :disabled="busy" @click="load()">{{
            $t('migrationManagement.refreshCounts')
          }}</el-button>
        </div>
      </el-form>
      <section>
        <h3>{{ $t('migrationManagement.countsTitle') }}</h3>
        <table class="reset-counts">
          <tbody>
            <tr v-for="key in resetCountKeys" :key="key">
              <th>{{ $t(`migrationManagement.counts.${key}`) }}</th>
              <td>{{ capability.reset_preview.counts[key] }}</td>
            </tr>
          </tbody>
        </table>
        <p class="management-secondary">{{ $t('migrationManagement.preserve') }}</p>
      </section>
    </div>
    <section v-if="pendingId" class="management-operation">
      <dl class="management-facts">
        <dt>{{ $t('migrationManagement.operation') }}</dt>
        <dd>
          <code>{{ pendingId }}</code>
        </dd>
        <template v-if="operation"
          ><dt>{{ $t('migrationManagement.phase') }}</dt>
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
          <dd>
            <code>{{ operation.target_digest }}</code>
          </dd>
          <dt>{{ $t('migrationManagement.revision') }}</dt>
          <dd>{{ operation.source_revision }} → {{ operation.target_revision }}</dd>
          <dt>{{ $t('migrationManagement.resetStatus') }}</dt>
          <dd>
            {{
              $t(
                operation.reset_applied
                  ? 'migrationManagement.resetApplied'
                  : 'migrationManagement.resetPending'
              )
            }}
          </dd>
          <dt>{{ $t('migrationManagement.next') }}</dt>
          <dd>{{ $t(`migrationManagement.steps.${operation.next_action}`) }}</dd>
          <template v-if="operation.metrics_not_before"
            ><dt>{{ $t('migrationManagement.metricsWait') }}</dt>
            <dd>{{ operation.metrics_not_before }}</dd></template
          ></template
        >
      </dl>
      <table v-if="operation" class="reset-counts operation-counts">
        <tbody>
          <tr v-for="key in resetCountKeys" :key="key">
            <th>{{ $t(`migrationManagement.counts.${key}`) }}</th>
            <td>{{ operation.reset_counts[key] }}</td>
          </tr>
        </tbody>
      </table>
      <div class="management-actions">
        <el-button :disabled="busy || !enabled" :loading="busy" @click="inspect">{{
          $t('migrationManagement.refresh')
        }}</el-button
        ><template v-if="operation"
          ><el-button
            type="danger"
            :disabled="busy || !enabled || needsRefresh || stalePlan || !operation.can_resume"
            :loading="busy"
            @click="confirmAction('resume')"
            >{{
              $t(
                operation.reset_applied
                  ? 'migrationManagement.resume'
                  : 'migrationManagement.clearAndMigrate'
              )
            }}</el-button
          ><el-button
            :disabled="busy || !enabled || needsRefresh || !operation.can_abort"
            @click="confirmAction('abort')"
            >{{ $t('migrationManagement.abort') }}</el-button
          ><el-button v-if="operation.phase === 'aborted'" :disabled="busy" @click="load(true)">{{
            $t('migrationManagement.newReview')
          }}</el-button></template
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
import { ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { resetCountKeys, useMigrationManagement } from '../utils/useMigrationManagement'
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
  clearConfirmed,
  busy,
  error,
  pendingId,
  enabled,
  canPlan,
  needsRefresh,
  stalePlan,
  load,
  inspect,
  mutate,
} = useMigrationManagement(props)
async function confirmAction(kind) {
  const before = operation.value
  if (!before || busy.value) return
  const target = `${before.target_application_name || before.target_application_id} · ${before.target_chat_id}`
  let message = t(
    `migrationManagement.confirm.${kind === 'resume' && !before.reset_applied ? 'reset' : kind}`,
    { target, phase: before.phase }
  )
  if (kind === 'resume' && !before.reset_applied)
    message +=
      '\n' +
      resetCountKeys
        .map((key) => `${t(`migrationManagement.counts.${key}`)}: ${before.reset_counts[key]}`)
        .join('\n')
  try {
    await ElMessageBox.confirm(message, t('migrationManagement.title'), {
      type: 'warning',
      customClass: 'migration-reset-confirm',
      confirmButtonText: t(
        kind === 'resume' && !before.reset_applied
          ? 'migrationManagement.clearAndMigrate'
          : 'common.confirm'
      ),
    })
    if (operation.value === before && !busy.value) await mutate(kind)
  } catch {
    /* Cancel never sends a request. */
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
:global(.migration-reset-confirm .el-message-box__message p) {
  white-space: pre-line;
  overflow-wrap: anywhere;
}
.management-intro,
.management-secondary {
  color: var(--el-text-color-secondary);
}
.management-intro {
  margin-top: 0;
}
.reset-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 32px;
}
.reset-layout h3 {
  margin-top: 0;
  font-size: 14px;
}
.el-select {
  width: 100%;
}
.reset-ack {
  display: flex;
  margin-top: 12px;
  height: auto;
  white-space: normal;
  align-items: flex-start;
}
:deep(.reset-ack .el-checkbox__label) {
  white-space: normal;
}
.reset-counts {
  width: 100%;
  border-collapse: collapse;
  font-size: 14px;
}
.reset-counts th,
.reset-counts td {
  padding: 9px 10px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.reset-counts th {
  font-weight: 400;
  text-align: left;
  color: var(--el-text-color-regular);
}
.reset-counts td {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.operation-counts {
  max-width: 480px;
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
.management-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-top: 20px;
}
.management-actions .el-button + .el-button {
  margin-left: 0;
}
.management-error {
  color: var(--el-color-danger);
}
@media (max-width: 640px) {
  .reset-layout {
    grid-template-columns: 1fr;
    gap: 24px;
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
