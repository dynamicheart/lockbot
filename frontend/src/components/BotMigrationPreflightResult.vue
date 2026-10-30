<template>
  <section class="migration-result" aria-live="polite">
    <p class="migration-unavailable">{{ $t('migration.cannotMigrate') }}</p>
    <dl v-if="result.stored_obligations" class="migration-counts">
      <div v-for="field in countFields" :key="field">
        <dt>{{ $t(`migration.counts.${field}`) }}</dt>
        <dd>{{ result.stored_obligations[field] ?? '—' }}</dd>
      </div>
    </dl>
    <p v-else class="migration-note">{{ $t('migration.stateUnknown') }}</p>
    <h4>{{ $t('migration.blockersTitle') }}</h4>
    <ul>
      <li v-for="(item, index) in result.blockers" :key="index">
        {{ $t(item === 'unknown' ? 'migration.unknownBlocker' : `migration.blockers.${item}`) }}
      </li>
    </ul>
    <h4>{{ $t('migration.unverifiedTitle') }}</h4>
    <ul>
      <li v-for="(item, index) in result.unverified" :key="index">
        {{ $t(item === 'unknown' ? 'migration.unknownCheck' : `migration.unverified.${item}`) }}
      </li>
    </ul>
  </section>
</template>
<script setup>
defineProps({ result: { type: Object, required: true } })
const countFields = ['resources', 'holders', 'reservations', 'offers', 'non_idle']
</script>
<style scoped>
.migration-result {
  margin-top: 24px;
  border-top: 1px solid var(--el-border-color-lighter);
}
.migration-unavailable {
  font-weight: 600;
}
.migration-counts {
  display: flex;
  flex-wrap: wrap;
  gap: 20px;
}
.migration-counts dt,
.migration-note {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.migration-counts dd {
  margin: 4px 0 0;
  font-size: 18px;
}
h4 {
  margin-bottom: 8px;
}
ul {
  padding-left: 20px;
  line-height: 1.8;
}
</style>
