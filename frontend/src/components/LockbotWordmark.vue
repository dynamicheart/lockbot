<template>
  <span class="lockbot-wordmark">
    <img src="/favicon.svg" alt="" width="24" height="24" />
    <span>LockBot</span>
  </span>
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
  white-space: nowrap;
}
.lockbot-wordmark img {
  display: block;
  flex: none;
}
</style>
