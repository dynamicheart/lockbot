import js from '@eslint/js'
import pluginVue from 'eslint-plugin-vue'
import configPrettier from 'eslint-config-prettier'
import globals from 'globals'

export default [
  js.configs.recommended,
  ...pluginVue.configs['flat/recommended'],
  configPrettier,
  {
    languageOptions: {
      globals: {
        ...globals.browser,
        __APP_VERSION__: 'readonly',
        BOT_ID: 'readonly',
        BOT_OWNER: 'readonly',
      },
    },
    rules: {
      // Allow single-word component names (e.g. <DemoChat>)
      'vue/multi-word-component-names': 'off',
      // Allow v-html (we don't use it, but avoids noise if ever needed)
      'vue/no-v-html': 'off',
      // Warn on unused vars but allow _ prefix to suppress
      'no-unused-vars': ['warn', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
    },
  },
]
