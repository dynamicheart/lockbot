<template>
  <el-container class="main-layout">
    <!-- Desktop sidebar -->
    <el-aside :width="isCollapsed ? '64px' : '200px'" class="sidebar desktop-sidebar">
      <div class="logo" @click="$router.push('/')">
        <div class="logo-main" :class="{ collapsed: isCollapsed }">
          <svg
            class="logo-icon"
            viewBox="0 0 32 32"
            width="24"
            height="24"
            xmlns="http://www.w3.org/2000/svg"
          >
            <!-- ears -->
            <rect x="1" y="13" width="4" height="6" rx="1.5" fill="#409eff" opacity="0.7" />
            <rect x="27" y="13" width="4" height="6" rx="1.5" fill="#409eff" opacity="0.7" />
            <!-- head -->
            <rect
              x="5"
              y="8"
              width="22"
              height="19"
              rx="4"
              fill="#e6f0ff"
              stroke="#409eff"
              stroke-width="1.8"
            />
            <!-- eyes -->
            <circle cx="12" cy="16" r="2.5" fill="#409eff" />
            <circle cx="20" cy="16" r="2.5" fill="#409eff" />
            <!-- eye highlights -->
            <circle cx="13" cy="15" r="0.8" fill="#fff" />
            <circle cx="21" cy="15" r="0.8" fill="#fff" />
            <!-- mouth -->
            <rect x="11" y="21.5" width="10" height="2" rx="1" fill="#409eff" />
            <!-- antenna -->
            <line
              x1="16"
              y1="8"
              x2="16"
              y2="3.5"
              stroke="#409eff"
              stroke-width="1.8"
              stroke-linecap="round"
            />
            <circle cx="16" cy="2.5" r="2" fill="#67c23a" />
          </svg>
          <span class="logo-text" :class="{ collapsed: isCollapsed }">LockBot</span>
        </div>
        <span class="logo-version" :class="{ collapsed: isCollapsed }">v{{ version }}</span>
      </div>
      <el-menu
        :default-active="$route.path"
        :collapse="isCollapsed"
        router
        background-color="var(--lb-bg-sidebar)"
        text-color="var(--lb-sidebar-text)"
        active-text-color="var(--lb-sidebar-active)"
      >
        <el-menu-item index="/bots">
          <el-icon><Monitor /></el-icon>
          <template #title>{{ $t('nav.myBots') }}</template>
        </el-menu-item>
        <template v-if="authStore.isAdmin">
          <el-menu-item-group :title="$t('nav.admin')">
            <el-menu-item index="/admin/users">
              <el-icon><User /></el-icon>
              <template #title>{{ $t('nav.adminUsers') }}</template>
            </el-menu-item>
            <el-menu-item index="/admin/bots">
              <el-icon><Setting /></el-icon>
              <template #title>{{ $t('nav.adminBots') }}</template>
            </el-menu-item>
            <el-menu-item v-if="authStore.isSuperAdmin" index="/admin/settings">
              <el-icon><Tools /></el-icon>
              <template #title>{{ $t('nav.adminSettings') }}</template>
            </el-menu-item>
          </el-menu-item-group>
        </template>
      </el-menu>
    </el-aside>

    <!-- Mobile drawer -->
    <el-drawer
      v-model="drawerOpen"
      direction="ltr"
      :with-header="false"
      size="220px"
      class="mobile-drawer"
    >
      <div class="logo" @click="goHome">
        <div class="logo-main">
          <svg
            class="logo-icon"
            viewBox="0 0 32 32"
            width="24"
            height="24"
            xmlns="http://www.w3.org/2000/svg"
          >
            <rect x="1" y="13" width="4" height="6" rx="1.5" fill="#409eff" opacity="0.7" />
            <rect x="27" y="13" width="4" height="6" rx="1.5" fill="#409eff" opacity="0.7" />
            <rect
              x="5"
              y="8"
              width="22"
              height="19"
              rx="4"
              fill="#e6f0ff"
              stroke="#409eff"
              stroke-width="1.8"
            />
            <circle cx="12" cy="16" r="2.5" fill="#409eff" />
            <circle cx="20" cy="16" r="2.5" fill="#409eff" />
            <circle cx="13" cy="15" r="0.8" fill="#fff" />
            <circle cx="21" cy="15" r="0.8" fill="#fff" />
            <rect x="11" y="21.5" width="10" height="2" rx="1" fill="#409eff" />
            <line
              x1="16"
              y1="8"
              x2="16"
              y2="3.5"
              stroke="#409eff"
              stroke-width="1.8"
              stroke-linecap="round"
            />
            <circle cx="16" cy="2.5" r="2" fill="#67c23a" />
          </svg>
          <span class="logo-text">LockBot</span>
        </div>
        <span class="logo-version">v{{ version }}</span>
      </div>
      <el-menu
        :default-active="$route.path"
        router
        background-color="var(--lb-bg-sidebar)"
        text-color="var(--lb-sidebar-text)"
        active-text-color="var(--lb-sidebar-active)"
        @select="drawerOpen = false"
      >
        <el-menu-item index="/bots">
          <el-icon><Monitor /></el-icon>
          <template #title>{{ $t('nav.myBots') }}</template>
        </el-menu-item>
        <template v-if="authStore.isAdmin">
          <el-menu-item-group :title="$t('nav.admin')">
            <el-menu-item index="/admin/users">
              <el-icon><User /></el-icon>
              <template #title>{{ $t('nav.adminUsers') }}</template>
            </el-menu-item>
            <el-menu-item index="/admin/bots">
              <el-icon><Setting /></el-icon>
              <template #title>{{ $t('nav.adminBots') }}</template>
            </el-menu-item>
            <el-menu-item v-if="authStore.isSuperAdmin" index="/admin/settings">
              <el-icon><Tools /></el-icon>
              <template #title>{{ $t('nav.adminSettings') }}</template>
            </el-menu-item>
          </el-menu-item-group>
        </template>
      </el-menu>
    </el-drawer>

    <el-container>
      <el-header class="header">
        <div class="header-left">
          <!-- Desktop: collapse/expand -->
          <el-icon class="collapse-btn desktop-only" @click="isCollapsed = !isCollapsed">
            <Fold v-if="!isCollapsed" />
            <Expand v-else />
          </el-icon>
          <!-- Mobile: open drawer -->
          <el-icon class="collapse-btn mobile-only" @click="drawerOpen = true">
            <Expand />
          </el-icon>
          <el-breadcrumb separator="/" class="breadcrumb">
            <el-breadcrumb-item v-for="item in breadcrumbs" :key="item.path" :to="item.to">
              {{ item.title }}
            </el-breadcrumb-item>
          </el-breadcrumb>
        </div>
        <div class="header-right">
          <!-- Desktop: GitHub link -->
          <el-tooltip content="GitHub" class="desktop-only">
            <a
              href="https://github.com/dynamicheart/lockbot"
              target="_blank"
              rel="noopener noreferrer"
              class="github-link"
            >
              <svg viewBox="0 0 16 16" width="20" height="20" fill="currentColor">
                <path
                  d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"
                />
              </svg>
            </a>
          </el-tooltip>
          <!-- Desktop: Theme toggle -->
          <el-tooltip :content="themeLabel" class="desktop-only">
            <el-button text circle @click="cycleTheme">
              <el-icon :size="20">
                <Sunny v-if="themeMode === 'light'" />
                <Moon v-else-if="themeMode === 'dark'" />
                <Monitor v-else />
              </el-icon>
            </el-button>
          </el-tooltip>
          <!-- Desktop: Locale switcher -->
          <el-dropdown trigger="click" class="desktop-only" @command="switchLocale">
            <el-button text circle>
              <span style="font-size: 14px; font-weight: 600">{{
                currentLocale === 'zh-CN' ? '中' : 'En'
              }}</span>
            </el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="zh-CN" :disabled="currentLocale === 'zh-CN'"
                  >中文</el-dropdown-item
                >
                <el-dropdown-item command="en" :disabled="currentLocale === 'en'"
                  >English</el-dropdown-item
                >
              </el-dropdown-menu>
            </template>
          </el-dropdown>

          <!-- User dropdown -->
          <el-dropdown trigger="click" @command="handleUserCommand">
            <span class="user-trigger">
              <el-avatar :size="28" style="background: var(--lb-color-primary)">
                {{ authStore.user?.username?.charAt(0)?.toUpperCase() }}
              </el-avatar>
              <span class="username desktop-only">{{ authStore.user?.username }}</span>
              <el-tag
                v-if="authStore.isSuperAdmin"
                size="small"
                type="danger"
                class="desktop-only"
                >{{ $t('admin.superAdmin') }}</el-tag
              >
              <el-tag
                v-else-if="authStore.isAdmin"
                size="small"
                type="warning"
                class="desktop-only"
                >{{ $t('admin.adminRole') }}</el-tag
              >
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="profile">
                  <el-icon><Setting /></el-icon>
                  {{ $t('auth.profileSettings') }}
                </el-dropdown-item>
                <el-dropdown-item divided command="switchUser">
                  <el-icon><Sort /></el-icon>
                  {{ $t('common.switchUser') }}
                </el-dropdown-item>
                <!-- Mobile-only: theme, locale, GitHub inside user menu -->
                <el-dropdown-item divided command="cycleTheme" class="mobile-only">
                  <el-icon>
                    <Sunny v-if="themeMode === 'light'" />
                    <Moon v-else-if="themeMode === 'dark'" />
                    <Monitor v-else />
                  </el-icon>
                  {{ themeLabel }}
                </el-dropdown-item>
                <el-dropdown-item command="toggleLocale" class="mobile-only">
                  <el-icon><Operation /></el-icon>
                  {{ currentLocale === 'zh-CN' ? 'English' : '中文' }}
                </el-dropdown-item>
                <el-dropdown-item command="github" class="mobile-only">
                  <el-icon><Link /></el-icon>
                  GitHub
                </el-dropdown-item>
                <el-dropdown-item divided command="logout">
                  <el-icon><SwitchButton /></el-icon>
                  {{ $t('common.logout') }}
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>
      <el-main class="content">
        <router-view />
      </el-main>
    </el-container>

    <!-- Switch User Dialog -->
    <el-dialog v-model="showSwitchDialog" :title="$t('common.switchUser')" width="400px">
      <div class="account-list">
        <div class="account-scroll">
          <div
            v-for="acc in savedAccounts"
            :key="acc.user.username"
            class="account-item"
            :class="{ active: acc.user.username === authStore.user?.username }"
            @click="handleSwitchTo(acc.user.username)"
          >
            <el-avatar :size="32" style="background: var(--lb-color-primary); flex-shrink: 0">
              {{ acc.user.username?.charAt(0)?.toUpperCase() }}
            </el-avatar>
            <div class="account-info">
              <span class="account-name">{{ acc.user.username }}</span>
              <span class="account-email">{{ acc.user.email }}</span>
            </div>
            <div class="account-right">
              <el-tag v-if="acc.user.role === 'super_admin'" size="small" type="danger">{{
                $t('admin.superAdmin')
              }}</el-tag>
              <el-tag v-else-if="acc.user.role === 'admin'" size="small" type="warning">{{
                $t('admin.adminRole')
              }}</el-tag>
              <el-icon
                v-if="acc.user.username !== authStore.user?.username"
                class="remove-btn"
                @click.stop="handleRemoveAccount(acc.user.username)"
                ><Close
              /></el-icon>
            </div>
          </div>
          <div
            v-if="savedAccounts.length === 0"
            style="text-align: center; color: var(--lb-text-secondary); padding: 20px 0"
          >
            {{ $t('common.noData') }}
          </div>
        </div>
        <el-button class="add-account-btn" @click="goToLogin">
          <el-icon><Plus /></el-icon> {{ $t('common.loginAnother') }}
        </el-button>
      </div>
    </el-dialog>

    <!-- Demo chat widget (floating bubble, only in demo mode) -->
    <DemoChat />
  </el-container>
</template>

<script setup>
import { ref, computed } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Close, Plus, Operation, Link } from '@element-plus/icons-vue'
import { useI18n } from 'vue-i18n'
import { useAuthStore } from '../stores/auth'
import { setLocale, getLocale } from '../i18n'
import DemoChat from '../components/DemoChat.vue'
import { LS_KEYS } from '../utils/demoMode'
import { isDemoMode } from '../utils/demoMode'

const THEME_KEY = 'lockbot_theme'
const authStore = useAuthStore()
const router = useRouter()
const route = useRoute()
const { t } = useI18n()
const isCollapsed = ref(false)
const drawerOpen = ref(false)
const version = __APP_VERSION__

function goHome() {
  router.push('/')
  drawerOpen.value = false
}

// --- Breadcrumbs ---
const breadcrumbs = computed(() => {
  const path = route.path
  const items = []
  const routeMap = {
    '/bots': () => t('nav.myBots'),
    '/profile': () => t('profile.title'),
    '/admin/users': () => t('nav.adminUsers'),
    '/admin/bots': () => t('nav.adminBots'),
  }
  if (path.match(/^\/bots\/\d+/)) {
    items.push({ path: '/bots', title: t('nav.myBots'), to: '/bots' })
    items.push({ path, title: route.name === 'BotDetail' ? t('botDetail.config') : '...' })
  } else if (path.startsWith('/admin/')) {
    items.push({ path: '/admin', title: t('nav.admin') })
    if (routeMap[path]) items.push({ path, title: routeMap[path]() })
  } else if (routeMap[path]) {
    items.push({ path, title: routeMap[path]() })
  }
  return items
})

// --- Theme ---
const themeMode = ref(localStorage.getItem(THEME_KEY) || 'light')

const themeLabel = computed(() => t(`theme.${themeMode.value}`))

function applyTheme(mode) {
  themeMode.value = mode
  localStorage.setItem(THEME_KEY, mode)
  const dark =
    mode === 'dark' ||
    (mode === 'auto' && window.matchMedia('(prefers-color-scheme: dark)').matches)
  document.documentElement.classList.toggle('dark', dark)
}

function cycleTheme() {
  const order = ['light', 'dark', 'auto']
  const next = order[(order.indexOf(themeMode.value) + 1) % order.length]
  applyTheme(next)
}

// --- Locale ---
const currentLocale = ref(getLocale())

function switchLocale(locale) {
  setLocale(locale)
  currentLocale.value = locale
}

// --- Logout & Switch User ---
const showSwitchDialog = ref(false)

const savedAccounts = computed(() =>
  authStore.getSavedAccounts().filter((a) => isDemoMode || !a.token?.startsWith('demo:'))
)

async function handleUserCommand(cmd) {
  if (cmd === 'logout') {
    try {
      await ElMessageBox.confirm(t('common.logoutConfirm'), t('common.confirm'), {
        type: 'warning',
      })
    } catch {
      return
    }
    authStore.logout()
    ElMessage.success(t('common.logoutSuccess'))
    router.push('/login')
  } else if (cmd === 'profile') {
    router.push('/profile')
  } else if (cmd === 'switchUser') {
    showSwitchDialog.value = true
  } else if (cmd === 'cycleTheme') {
    cycleTheme()
  } else if (cmd === 'toggleLocale') {
    switchLocale(currentLocale.value === 'zh-CN' ? 'en' : 'zh-CN')
  } else if (cmd === 'github') {
    window.open('https://github.com/dynamicheart/lockbot', '_blank', 'noopener,noreferrer')
  }
}

async function handleSwitchTo(username) {
  if (username === authStore.user?.username) return
  const ok = await authStore.switchAccount(username)
  if (ok) {
    showSwitchDialog.value = false
    ElMessage.success(t('common.switchUserSuccess'))
    location.reload()
  } else {
    ElMessage.warning(t('common.accountExpired'))
  }
}

function goToLogin() {
  showSwitchDialog.value = false
  // Clear active session but keep saved accounts for quick switch
  authStore.token = ''
  authStore.user = null
  localStorage.removeItem(LS_KEYS.token)
  localStorage.removeItem(LS_KEYS.user)
  router.push('/login')
}

function handleRemoveAccount(username) {
  authStore.removeAccount(username)
}
</script>

<style scoped>
.main-layout {
  height: 100vh;
}
.sidebar {
  background: var(--lb-bg-sidebar);
  border-right: 1px solid var(--lb-sidebar-border);
  box-shadow: var(--lb-shadow-sidebar);
  transition: width 0.3s;
  overflow: hidden;
}
.logo {
  height: 60px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 2px;
  color: var(--lb-text-primary);
  font-size: 18px;
  font-weight: 600;
  cursor: pointer;
  border-bottom: 1px solid var(--lb-sidebar-border);
}
.el-menu {
  border-right: none;
}
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: var(--lb-bg-header);
  border-bottom: 1px solid var(--lb-border-color);
  box-shadow: var(--lb-shadow-header);
  padding: 0 20px;
  height: 60px;
}
.collapse-btn {
  font-size: 20px;
  cursor: pointer;
  color: var(--lb-text-secondary);
}
.header-left {
  display: flex;
  align-items: center;
  gap: 12px;
}
.breadcrumb {
  font-size: 14px;
}
.header-right {
  display: flex;
  align-items: center;
  gap: 8px;
}
.github-link {
  display: flex;
  align-items: center;
  color: var(--el-text-color-regular);
  transition: color 0.2s;
  margin-right: 4px;
}
.github-link:hover {
  color: var(--el-color-primary);
}
.user-trigger {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  padding: 4px 8px;
  border-radius: 6px;
  transition: background 0.2s;
}
.user-trigger:hover {
  background: var(--lb-bg-hover);
}
.username {
  font-weight: 500;
  color: var(--lb-text-primary);
}
.content {
  background: var(--lb-bg-page);
  min-height: 0;
  overflow-y: auto;
}
.logo-text {
  opacity: 1;
  width: auto;
  overflow: hidden;
  white-space: nowrap;
  transition: opacity 0.2s;
}
.logo-text.collapsed {
  opacity: 0;
  width: 0;
}
.logo-main {
  display: flex;
  align-items: center;
  gap: 8px;
}
.logo-main.collapsed {
  gap: 0;
}
.logo-version {
  font-size: 11px;
  color: var(--lb-text-muted);
  line-height: 1;
  transition: opacity 0.2s;
}
.logo-version.collapsed {
  opacity: 0;
  height: 0;
  overflow: hidden;
  width: 0;
}
.sidebar :deep(.el-menu) {
  transition: none;
}
.sidebar :deep(.el-menu-item),
.sidebar :deep(.el-menu-item-group__title) {
  transition: none;
}
.sidebar :deep(.el-menu-item:hover),
.sidebar :deep(.el-menu-item:focus) {
  background-color: var(--lb-sidebar-hover) !important;
}
.sidebar :deep(.el-menu-item.is-active) {
  background-color: var(--lb-sidebar-hover) !important;
}
.account-list {
  display: flex;
  flex-direction: column;
}
.account-scroll {
  max-height: 320px;
  overflow-y: auto;
}
.account-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 12px;
  border-radius: 8px;
  cursor: pointer;
  transition: background 0.2s;
}
.account-item:hover {
  background: var(--lb-bg-hover);
}
.account-item.active {
  background: var(--lb-bg-hover);
  border: 1px solid var(--lb-color-primary);
}
.account-info {
  flex: 1;
  min-width: 0;
}
.account-name {
  display: block;
  font-weight: 500;
  font-size: 14px;
  color: var(--lb-text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.account-email {
  display: block;
  font-size: 12px;
  color: var(--lb-text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.account-right {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}
.remove-btn {
  color: var(--lb-text-muted);
  cursor: pointer;
  border-radius: 4px;
  padding: 2px;
}
.remove-btn:hover {
  color: var(--lb-color-danger);
}
.add-account-btn {
  width: 100%;
  margin-top: 8px;
  border-style: dashed;
}

/* Responsive: desktop-only elements hidden on mobile */
@media (max-width: 768px) {
  .desktop-only {
    display: none !important;
  }
  .desktop-sidebar {
    display: none !important;
  }
  .header {
    padding: 0 12px;
  }
}

/* Mobile-only elements hidden on desktop */
@media (min-width: 769px) {
  .mobile-only {
    display: none !important;
  }
}

/* Drawer sidebar styles */
.mobile-drawer :deep(.el-drawer) {
  background: var(--lb-bg-sidebar) !important;
  border-right: 1px solid var(--lb-sidebar-border);
  padding: 0;
}
.mobile-drawer :deep(.el-drawer__body) {
  padding: 0;
  overflow: hidden;
}
.mobile-drawer :deep(.el-menu) {
  border-right: none;
}
</style>
