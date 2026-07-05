import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import './style.css'

// Apply theme early to prevent flash of wrong theme (FOUC)
;(function () {
  const stored = localStorage.getItem('theme')
  const valid = ['light', 'dark', 'auto']
  const theme = valid.includes(stored) ? stored : 'auto'
  document.documentElement.setAttribute('data-theme', theme)
})()

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.mount('#app')
