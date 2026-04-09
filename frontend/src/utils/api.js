import axios from 'axios'
import { ElMessage } from 'element-plus'
import router from '../router'
import i18n from '../i18n'
import { isDemoMode } from './demoMode'
import { mockApi } from './api.mock'

const realApi = axios.create({
  baseURL: '/api',
  timeout: 15000,
})

realApi.interceptors.request.use((config) => {
  const token = localStorage.getItem('token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

realApi.interceptors.response.use(
  (res) => res,
  (err) => {
    const status = err.response?.status
    const url = err.config?.url
    if (status === 401 && url !== '/auth/login') {
      localStorage.removeItem('token')
      router.push('/login')
      ElMessage.error(i18n.global.t('auth.sessionExpired'))
    } else if (status === 401 && url === '/auth/login') {
      ElMessage.error(i18n.global.t('auth.loginFailed'))
    } else if (status === 403) {
      ElMessage.error(i18n.global.t('common.permissionDenied'))
    } else if (status === 422) {
      const detail = err.response?.data?.detail
      if (Array.isArray(detail)) {
        ElMessage.error(detail.map((d) => d.msg).join('; '))
      } else if (typeof detail === 'string') {
        ElMessage.error(detail)
      }
    } else {
      ElMessage.error(err.response?.data?.detail || i18n.global.t('common.requestFailed'))
    }
    return Promise.reject(err)
  }
)

export default isDemoMode ? mockApi : realApi
