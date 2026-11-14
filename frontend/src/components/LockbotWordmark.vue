<template>
  <router-link class="lockbot-wordmark" to="/">
    <img src="/favicon.svg" alt="" width="24" height="24" />
    <span>LockBot</span>
  </router-link>
</template>

<style scoped>
.lockbot-wordmark {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  width: max-content;
  flex-shrink: 0;
  color: var(--lb-text-primary, #303133);
  font-weight: 700;
  text-decoration: none;
  white-space: nowrap;
}
.lockbot-wordmark img {
  display: block;
  flex: none;
}
.lockbot-wordmark:hover {
  color: var(--lb-color-primary, #409eff);
}
</style>
