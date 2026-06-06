import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import Login from '../views/Login.vue'
import Register from '../views/Register.vue'
import Dashboard from '../views/Dashboard.vue'
import PortDetail from '../views/PortDetail.vue'
import JsonTreeViewer from '../views/JsonTreeViewer.vue'
import Admin from '../views/Admin.vue'
import ChangePassword from '../views/ChangePassword.vue'

const routes = [
  { path: '/login', name: 'Login', component: Login, meta: { guest: true } },
  { path: '/register', name: 'Register', component: Register, meta: { guest: true } },
  { path: '/', name: 'Dashboard', component: Dashboard, meta: { auth: true } },
  { path: '/port/:id', name: 'PortDetail', component: PortDetail, meta: { auth: true } },
  { path: '/json-viewer/:portId/:requestId', name: 'JsonTreeViewer', component: JsonTreeViewer, meta: { auth: true } },
  { path: '/admin', name: 'Admin', component: Admin, meta: { auth: true, admin: true } },
  { path: '/change-password', name: 'ChangePassword', component: ChangePassword, meta: { auth: true } },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach((to, from, next) => {
  const auth = useAuthStore()

  if (to.meta.auth && !auth.isLoggedIn) {
    return next('/login')
  }
  if (to.meta.admin && !auth.isAdmin) {
    return next('/')
  }
  if (to.meta.guest && auth.isLoggedIn) {
    return next('/')
  }
  next()
})

export default router
