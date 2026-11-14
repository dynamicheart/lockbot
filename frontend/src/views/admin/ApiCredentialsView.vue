<template>
  <section v-if="authStore.isSuperAdmin">
    <h2 class="credentials-title">{{ $t('nav.apiCredentials') }}</h2>
    <el-card>
      <AuditCredentialManager
        :authorized="authStore.isSuperAdmin"
        :principal-id="authStore.user?.id"
        :session-revision="sessionRevision"
      />
    </el-card>
  </section>
</template>

<script setup>
import { ref, watch } from 'vue'
import { useAuthStore } from '../../stores/auth'
import AuditCredentialManager from '../../components/AuditCredentialManager.vue'

const authStore = useAuthStore()
const sessionRevision = ref(0)
watch(
  () => [authStore.user?.id, authStore.token, authStore.isSuperAdmin],
  () => {
    sessionRevision.value += 1
  },
  { flush: 'sync' }
)
</script>

<style scoped>
.credentials-title {
  margin: 0 0 20px;
}
</style>
