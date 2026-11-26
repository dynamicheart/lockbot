<template><ServiceTrafficPanel :show-agent-tokens="authStore.isSuperAdmin" /></template>
<script setup>
import ServiceTrafficPanel from '../../components/ServiceTrafficPanel.vue'
import { useAuthStore } from '../../stores/auth'

const authStore = useAuthStore()
</script>
