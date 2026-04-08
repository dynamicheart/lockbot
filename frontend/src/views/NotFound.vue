<template>
  <div class="not-found">
    <div class="not-found-content">
      <h1>404</h1>
      <p>{{ $t('common.notFound') }}</p>
      <el-button type="primary" @click="$router.push('/')">{{ $t('common.backHome') }}</el-button>
    </div>
  </div>
</template>

<script setup>
</script>

<style scoped>
.not-found {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  text-align: center;
}
.not-found-content h1 {
  font-size: 72px;
  font-weight: 700;
  color: var(--lb-text-muted);
  margin: 0;
}
.not-found-content p {
  font-size: 16px;
  color: var(--lb-text-secondary);
  margin: 8px 0 24px;
}
</style>
