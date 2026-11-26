<template>
  <main class="retired-panel">
    <h1>{{ $t('personalWorkbench.retired') }}</h1>
    <p>{{ $t('personalWorkbench.retiredDescription') }}</p>
    <router-link to="/bots">{{ $t('personalWorkbench.console') }}</router-link>
  </main>
</template>

<style scoped>
.retired-panel {
  max-width: 560px;
  margin: clamp(32px, 10vh, 88px) auto;
  padding: 24px;
}

.retired-panel h1 {
  margin: 0 0 12px;
}

.retired-panel p {
  margin: 0 0 20px;
}
</style>
