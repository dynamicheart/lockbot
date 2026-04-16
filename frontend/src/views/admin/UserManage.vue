<template>
  <div>
    <div
      style="
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 20px;
      "
    >
      <h2 style="margin: 0">
        {{ $t('admin.userManagement') }}
        <span style="font-size: 14px; font-weight: 400; color: var(--lb-text-secondary)"
          >({{ users.length }})</span
        >
      </h2>
      <div style="display: flex; gap: 8px">
        <el-button type="primary" @click="openCreateDialog">
          <el-icon><Plus /></el-icon> {{ $t('admin.addUser') }}
        </el-button>
        <el-button v-if="authStore.isSuperAdmin" @click="handleBackup">
          <el-icon><Download /></el-icon> {{ $t('admin.backupDatabase') }}
        </el-button>
      </div>
    </div>
    <el-card>
      <div style="margin-bottom: 16px">
        <el-input v-model="searchQuery" :placeholder="$t('admin.searchPlaceholder')" clearable>
          <template #prefix
            ><el-icon><Search /></el-icon
          ></template>
        </el-input>
      </div>
      <el-table v-loading="loading" :data="filteredUsers" stripe>
        <el-table-column prop="id" label="ID" width="60" />
        <el-table-column prop="username" :label="$t('auth.username')" min-width="120" />
        <el-table-column prop="email" :label="$t('auth.email')" min-width="180" />
        <el-table-column prop="role" :label="$t('admin.role')" width="120">
          <template #default="{ row }">
            <el-tag v-if="row.role === 'super_admin'" type="danger" size="small" effect="plain">{{
              $t('admin.superAdmin')
            }}</el-tag>
            <el-tag v-else-if="row.role === 'admin'" type="warning" size="small" effect="plain">{{
              $t('admin.adminRole')
            }}</el-tag>
            <el-tag v-else type="info" size="small" effect="plain">{{
              $t('admin.userRole')
            }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column
          prop="max_running_bots"
          :label="$t('admin.maxBots')"
          width="110"
          align="center"
        >
          <template #default="{ row }">
            <span v-if="row.role === 'admin' || row.role === 'super_admin'">∞</span>
            <span v-else>{{ row.max_running_bots }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="$t('admin.botCount')" width="100" align="center">
          <template #default="{ row }">{{ row.bot_count ?? 0 }}</template>
        </el-table-column>
        <el-table-column :label="$t('admin.runningCount')" width="110" align="center">
          <template #default="{ row }">
            <span :class="{ 'running-highlight': (row.running_count ?? 0) > 0 }">{{
              row.running_count ?? 0
            }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="$t('common.actions')" width="210" align="center">
          <template #default="{ row }">
            <el-tooltip :content="$t('admin.editUser')">
              <el-button
                type="primary"
                size="small"
                :disabled="!canManage(row)"
                @click="openEditDialog(row)"
              >
                <el-icon><EditPen /></el-icon>
              </el-button>
            </el-tooltip>
            <el-tooltip :content="$t('admin.copyInfo')">
              <el-button size="small" @click="handleCopyUserInfo(row)">
                <el-icon><CopyDocument /></el-icon>
              </el-button>
            </el-tooltip>
            <el-tooltip :content="$t('admin.resetPassword')">
              <el-button
                type="warning"
                size="small"
                :disabled="!canManage(row)"
                @click="handleResetPassword(row)"
              >
                <el-icon><RefreshRight /></el-icon>
              </el-button>
            </el-tooltip>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- Create User Dialog -->
    <el-dialog v-model="showCreate" :title="$t('admin.addUser')" width="480px" class="user-dialog">
      <div class="dialog-avatar-section">
        <el-avatar :size="56" class="dialog-avatar">{{
          createForm.username?.charAt(0)?.toUpperCase() || '?'
        }}</el-avatar>
        <div class="dialog-avatar-name" :class="{ placeholder: !createForm.username }">
          {{ createForm.username || $t('admin.usernamePlaceholder') }}
        </div>
        <el-tag
          v-if="createForm.role === 'super_admin'"
          type="danger"
          size="small"
          effect="plain"
          >{{ $t('admin.superAdmin') }}</el-tag
        >
        <el-tag
          v-else-if="createForm.role === 'admin'"
          type="warning"
          size="small"
          effect="plain"
          >{{ $t('admin.adminRole') }}</el-tag
        >
        <el-tag v-else type="info" size="small" effect="plain">{{ $t('admin.userRole') }}</el-tag>
      </div>
      <el-divider />
      <el-form ref="createFormRef" :model="createForm" :rules="createRules" label-position="top">
        <el-form-item :label="$t('auth.username')" prop="username">
          <el-input
            v-model="createForm.username"
            :prefix-icon="UserIcon"
            :placeholder="$t('admin.usernamePlaceholder')"
          />
        </el-form-item>
        <el-form-item :label="$t('auth.email')" prop="email">
          <el-input
            v-model="createForm.email"
            :prefix-icon="MessageIcon"
            :placeholder="$t('admin.emailPlaceholder')"
          />
        </el-form-item>
        <el-form-item :label="$t('admin.role')" prop="role">
          <el-select v-model="createForm.role" style="width: 100%">
            <el-option :label="$t('admin.userRole')" value="user" />
            <el-option v-if="authStore.isSuperAdmin" :label="$t('admin.adminRole')" value="admin" />
          </el-select>
          <div v-if="authStore.isSuperAdmin" class="field-hint">
            {{ $t('admin.superAdminHint') }}
          </div>
        </el-form-item>
        <el-form-item :label="$t('admin.maxBots')" prop="max_running_bots">
          <el-input
            v-if="createForm.role !== 'user'"
            model-value="∞"
            disabled
            class="infinity-input"
            style="width: 120px"
          />
          <el-input-number
            v-else
            v-model="createForm.max_running_bots"
            :min="0"
            :max="999"
            style="width: 120px"
          />
          <div v-if="createForm.role !== 'user'" class="field-hint">
            {{ $t('admin.adminNoLimit') }}
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showCreate = false">{{ $t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="createLoading" @click="handleCreate">{{
          $t('common.create')
        }}</el-button>
      </template>
    </el-dialog>

    <!-- Edit User Dialog -->
    <el-dialog v-model="showEdit" :title="$t('admin.editUser')" width="480px" class="user-dialog">
      <div class="dialog-avatar-section">
        <el-avatar :size="56" class="dialog-avatar">{{
          editForm.username?.charAt(0)?.toUpperCase() || '?'
        }}</el-avatar>
        <div class="dialog-avatar-name">{{ editForm.username || '—' }}</div>
        <el-tag v-if="editForm.role === 'super_admin'" type="danger" size="small" effect="plain">{{
          $t('admin.superAdmin')
        }}</el-tag>
        <el-tag v-else-if="editForm.role === 'admin'" type="warning" size="small" effect="plain">{{
          $t('admin.adminRole')
        }}</el-tag>
        <el-tag v-else type="info" size="small" effect="plain">{{ $t('admin.userRole') }}</el-tag>
      </div>
      <el-divider />
      <el-form ref="editFormRef" :model="editForm" :rules="editRules" label-position="top">
        <el-form-item :label="$t('auth.username')" prop="username">
          <el-input v-model="editForm.username" :prefix-icon="UserIcon" />
        </el-form-item>
        <el-form-item :label="$t('auth.email')" prop="email">
          <el-input v-model="editForm.email" :prefix-icon="MessageIcon" />
        </el-form-item>
        <el-form-item :label="$t('admin.role')" prop="role">
          <el-select v-model="editForm.role" style="width: 100%">
            <el-option :label="$t('admin.userRole')" value="user" />
            <el-option v-if="authStore.isSuperAdmin" :label="$t('admin.adminRole')" value="admin" />
          </el-select>
          <div v-if="authStore.isSuperAdmin" class="field-hint">
            {{ $t('admin.superAdminHint') }}
          </div>
        </el-form-item>
        <el-form-item
          :label="$t('admin.maxBots')"
          prop="max_running_bots"
          style="margin-bottom: 2px"
        >
          <el-input
            v-if="editForm.role !== 'user'"
            model-value="∞"
            disabled
            class="infinity-input"
            style="width: 120px"
          />
          <el-input-number
            v-else
            v-model="editForm.max_running_bots"
            :min="0"
            :max="999"
            style="width: 120px"
          />
        </el-form-item>
        <div v-if="editForm.role !== 'user'" class="field-hint" style="margin-bottom: 18px">
          {{ $t('admin.adminNoLimit') }}
        </div>
      </el-form>
      <template #footer>
        <el-button @click="showEdit = false">{{ $t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="editLoading" @click="handleEdit">{{
          $t('common.save')
        }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import api from '../../utils/api'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { useAuthStore } from '../../stores/auth'
import { useHelpers } from '../../utils/helpers'
import { User as UserIcon, Message as MessageIcon, Download } from '@element-plus/icons-vue'

const { t } = useI18n()
const authStore = useAuthStore()
const { copyText } = useHelpers()
const users = ref([])
const loading = ref(false)

// Use unified permission function from authStore
function canManage(row) {
  return authStore.canManageUser(row.role)
}

// --- Search ---
const searchQuery = ref('')
const filteredUsers = computed(() => {
  const q = searchQuery.value.trim().toLowerCase()
  if (!q) return users.value
  return users.value.filter(
    (u) => u.username.toLowerCase().includes(q) || u.email.toLowerCase().includes(q)
  )
})

// --- Copy helpers ---
function copyAccountInfo(username, email, password) {
  copyText(
    `${t('auth.username')}: ${username}\n${t('auth.email')}: ${email}\n${t('auth.password')}: ${password}`
  )
}

function copyPassword(password) {
  copyText(password)
}

// --- Copy user info ---
function handleCopyUserInfo(user) {
  copyText(
    `${t('auth.username')}: ${user.username}\n${t('auth.email')}: ${user.email}`,
    t('admin.infoCopied')
  )
}

// --- Create ---
const showCreate = ref(false)
const createLoading = ref(false)
const createFormRef = ref()
const createForm = ref({ username: '', email: '', role: 'user', max_running_bots: 10 })
const createRules = {
  username: [{ required: true, message: () => t('auth.usernameRequired'), trigger: 'blur' }],
  email: [
    { required: true, type: 'email', message: () => t('admin.emailRequired'), trigger: 'blur' },
  ],
}

function openCreateDialog() {
  createForm.value = { username: '', email: '', role: 'user', max_running_bots: 10 }
  showCreate.value = true
}

async function handleCreate() {
  try {
    await createFormRef.value.validate()
  } catch {
    return
  }
  createLoading.value = true
  try {
    const res = await api.post('/admin/users', createForm.value)
    const { username, new_password } = res.data
    const email = createForm.value.email
    showCreate.value = false
    fetchUsers()
    ElMessageBox.alert(`${t('admin.passwordGenerated')}\n\n${new_password}`, t('common.success'), {
      confirmButtonText: t('admin.copyAll'),
      showCancelButton: true,
      cancelButtonText: t('admin.copyPassword'),
      callback: (action) => {
        if (action === 'confirm') copyAccountInfo(username, email, new_password)
        else if (action === 'cancel') copyPassword(new_password)
      },
    })
  } catch {
    // handled by interceptor (409 duplicate, etc.)
  } finally {
    createLoading.value = false
  }
}

// --- Edit ---
const showEdit = ref(false)
const editLoading = ref(false)
const editFormRef = ref()
const editTarget = ref(null)
const editForm = ref({ username: '', email: '', role: 'user', max_running_bots: 10 })
const editRules = {
  username: [{ required: true, message: () => t('auth.usernameRequired'), trigger: 'blur' }],
  email: [
    { required: true, type: 'email', message: () => t('admin.emailRequired'), trigger: 'blur' },
  ],
}

function openEditDialog(user) {
  editTarget.value = user
  editForm.value = {
    username: user.username,
    email: user.email,
    role: user.role,
    max_running_bots: user.max_running_bots,
  }
  showEdit.value = true
}

async function handleEdit() {
  try {
    await editFormRef.value.validate()
  } catch {
    return
  }
  editLoading.value = true
  try {
    const res = await api.put(`/admin/users/${editTarget.value.id}`, editForm.value)
    Object.assign(editTarget.value, res.data)
    ElMessage.success(t('common.success'))
    showEdit.value = false
    fetchUsers()
  } catch {
    // handled by interceptor
  } finally {
    editLoading.value = false
  }
}

// --- Reset Password ---
async function handleResetPassword(user) {
  try {
    await ElMessageBox.confirm(t('admin.resetPasswordConfirm'), t('common.confirm'), {
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    const res = await api.post(`/admin/users/${user.id}/reset-password`)
    const pw = res.data.new_password
    ElMessageBox.alert(`${t('admin.passwordReset')}\n\n${pw}`, t('common.success'), {
      confirmButtonText: t('admin.copyAll'),
      showCancelButton: true,
      cancelButtonText: t('admin.copyPassword'),
      callback: (action) => {
        if (action === 'confirm') copyAccountInfo(user.username, user.email, pw)
        else if (action === 'cancel') copyPassword(pw)
      },
    })
  } catch {
    // handled by interceptor
  }
}

// --- Fetch ---
async function fetchUsers() {
  loading.value = true
  try {
    const res = await api.get('/admin/users')
    users.value = res.data
  } catch {
    users.value = []
  } finally {
    loading.value = false
  }
}

onMounted(fetchUsers)

async function handleBackup() {
  try {
    const res = await api.get('/admin/backup', { responseType: 'blob' })
    const url = URL.createObjectURL(res.data)
    const a = document.createElement('a')
    a.href = url
    // Extract filename from Content-Disposition or use default
    const cd = res.headers['content-disposition']
    const match = cd?.match(/filename=(.+)/)
    a.download = match ? match[1] : 'lockbot_backup.db'
    a.click()
    URL.revokeObjectURL(url)
  } catch {
    ElMessage.error(t('common.error'))
  }
}
</script>

<style scoped>
.dialog-avatar-section {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding-bottom: 4px;
}
.dialog-avatar {
  background: var(--lb-color-primary);
  font-size: 22px;
  font-weight: 600;
}
.dialog-avatar-name {
  font-size: 16px;
  font-weight: 600;
  color: var(--lb-text-primary);
}
.dialog-avatar-name.placeholder {
  color: var(--lb-text-muted, var(--el-text-color-placeholder));
  font-weight: 400;
}
.field-hint {
  font-size: 12px;
  color: var(--lb-text-secondary);
  line-height: 1.4;
  margin-top: 4px;
}
.infinity-input :deep(.el-input__inner) {
  font-size: 20px;
  font-weight: 600;
}
.user-dialog :deep(.el-divider) {
  margin: 12px 0 20px;
}
.user-dialog :deep(.el-form-item__label) {
  font-weight: 500;
}
.running-highlight {
  color: var(--el-color-success);
  font-weight: 600;
}
</style>
