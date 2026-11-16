<template>
  <section>
    <h2 class="credentials-title">{{ $t('nav.apiCredentials') }}</h2>
    <el-tabs v-if="authStore.isSuperAdmin" v-model="activeTab">
      <el-tab-pane :label="$t('configurationCredentials.title')" name="configuration" />
      <el-tab-pane :label="$t('configurationCredentials.auditTab')" name="audit" />
    </el-tabs>
    <ConfigurationCredentialManager
      v-if="activeTab === 'configuration'"
      :can-provision="authStore.isAdmin"
      :principal-id="authStore.user?.id"
      :session-revision="sessionRevision"
    />
    <AuditCredentialManager
      v-else-if="authStore.isSuperAdmin"
      :authorized="authStore.isSuperAdmin"
      :principal-id="authStore.user?.id"
      :session-revision="sessionRevision"
    />
  </section>
</template>

<script setup>
import { ref, watch } from 'vue'
import { useAuthStore } from '../../stores/auth'
import AuditCredentialManager from '../../components/AuditCredentialManager.vue'
import ConfigurationCredentialManager from '../../components/ConfigurationCredentialManager.vue'

const authStore = useAuthStore()
const sessionRevision = ref(0)
const activeTab = ref('configuration')
watch(
  () => [authStore.user?.id, authStore.token, authStore.user?.role],
  () => {
    sessionRevision.value += 1
    activeTab.value = 'configuration'
  },
  { flush: 'sync' }
)
</script>

<style scoped>
.credentials-title {
  margin: 0 0 20px;
}
</style>
