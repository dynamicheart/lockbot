<template>
  <section>
    <h2 class="credentials-title">{{ $t('nav.apiCredentials') }}</h2>
    <ConfigurationCredentialManager
      :principal-id="authStore.user?.id"
      :session-revision="sessionRevision"
      :requested-bot="typeof route.query.bot === 'string' ? route.query.bot : ''"
      :requested-scope="typeof route.query.scope === 'string' ? route.query.scope : ''"
      @prefill-consumed="clearPrefill"
    />
  </section>
</template>

<script setup>
import { ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../../stores/auth'
import ConfigurationCredentialManager from '../../components/ConfigurationCredentialManager.vue'

const authStore = useAuthStore()
const route = useRoute()
const router = useRouter()
const sessionRevision = ref(0)
function clearPrefill() {
  const query = { ...route.query }
  delete query.bot
  delete query.scope
  router.replace({ query })
}
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
