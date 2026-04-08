import { createRouter, createWebHashHistory } from 'vue-router'
import MainLayout from '../layouts/MainLayout.vue'
import Login from '../views/Login.vue'
import Register from '../views/Register.vue'
import BotList from '../views/BotList.vue'
import BotDetail from '../views/BotDetail.vue'
import BotForm from '../views/BotForm.vue'
import UserManage from '../views/admin/UserManage.vue'
import BotMonitor from '../views/admin/BotMonitor.vue'
import ProfileSettings from '../views/ProfileSettings.vue'
import ForceChangePassword from '../views/ForceChangePassword.vue'
import NotFound from '../views/NotFound.vue'

const routes = [
  {
    path: '/login',
    name: 'Login',
    component: Login,
    meta: { guest: true },
  },
  {
    path: '/register',
    name: 'Register',
    component: Register,
    meta: { guest: true },
  },
  {
    path: '/change-password',
    name: 'ForceChangePassword',
    component: ForceChangePassword,
    meta: { auth: true },
  },
  {
    path: '/',
    component: MainLayout,
    meta: { auth: true },
    children: [
      {
        path: '',
        redirect: '/bots',
      },
      {
        path: 'bots',
        name: 'BotList',
        component: BotList,
      },
      {
        path: 'bots/create',
        name: 'BotCreate',
        component: BotForm,
      },
      {
        path: 'bots/:id/edit',
        name: 'BotEdit',
        component: BotForm,
        props: true,
      },
      {
        path: 'bots/:id',
        name: 'BotDetail',
        component: BotDetail,
        props: true,
      },
      {
        path: 'profile',
        name: 'ProfileSettings',
        component: ProfileSettings,
      },
      {
        path: 'admin',
        redirect: '/admin/users',
      },
      {
        path: 'admin/users',
        name: 'UserManage',
        component: UserManage,
        meta: { admin: true },
      },
      {
        path: 'admin/bots',
        name: 'BotMonitor',
        component: BotMonitor,
        meta: { admin: true },
      },
    ],
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'NotFound',
    component: NotFound,
  },
]

const router = createRouter({
  history: createWebHashHistory(),
  routes,
})

router.beforeEach((to, _from, next) => {
  const token = localStorage.getItem('token')
  const userStr = localStorage.getItem('user')
  const user = userStr ? JSON.parse(userStr) : null
  const mustChangePassword = user?.must_change_password === true

  // Force password change redirect
  if (mustChangePassword && to.name !== 'ForceChangePassword') {
    return next('/change-password')
  }

  if (to.meta.auth && !token) {
    return next('/login')
  }
  // Allow logged-in users to access Login page (for switching accounts)
  if (to.name === 'Register' && token) {
    return next('/')
  }
  if (to.meta.admin && !['admin', 'super_admin'].includes(user?.role)) {
    return next('/')
  }
  next()
})

export default router
