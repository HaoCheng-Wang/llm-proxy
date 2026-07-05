import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

const STORAGE_KEY = 'theme'
const VALID_THEMES = ['light', 'dark', 'auto']

function getSystemPrefersDark() {
  return window.matchMedia('(prefers-color-scheme: dark)').matches
}

function applyThemeToDOM(theme) {
  document.documentElement.setAttribute('data-theme', theme)
}

export const useThemeStore = defineStore('theme', () => {
  const stored = localStorage.getItem(STORAGE_KEY)
  const theme = ref(VALID_THEMES.includes(stored) ? stored : 'auto')

  let mediaQuery = null
  let mediaListener = null

  // The effective theme resolves 'auto' to 'light' or 'dark'
  const resolvedTheme = computed(() => {
    if (theme.value === 'auto') {
      return getSystemPrefersDark() ? 'dark' : 'light'
    }
    return theme.value
  })

  function applyTheme() {
    applyThemeToDOM(theme.value)

    // Clean up previous listener
    if (mediaQuery && mediaListener) {
      mediaQuery.removeEventListener('change', mediaListener)
      mediaQuery = null
      mediaListener = null
    }

    // In auto mode, listen for system theme changes
    if (theme.value === 'auto') {
      mediaQuery = window.matchMedia('(prefers-color-scheme: dark)')
      mediaListener = () => {
        // The CSS @media query handles the actual styling automatically,
        // but we keep this listener to update resolvedTheme reactively.
      }
      mediaQuery.addEventListener('change', mediaListener)
    }
  }

  function setTheme(value) {
    if (!VALID_THEMES.includes(value)) return
    theme.value = value
    localStorage.setItem(STORAGE_KEY, value)
    applyTheme()
  }

  function initTheme() {
    applyTheme()
  }

  return { theme, resolvedTheme, setTheme, initTheme }
})
