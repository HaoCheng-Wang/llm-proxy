<template>
  <div class="jv">
    <!-- Toolbar — fixed, never scrolls -->
    <div class="jv-bar">
      <input v-model.trim="search" class="jv-input" placeholder="搜索 key 或 value..." />
      <template v-if="search">
        <span class="jv-cnt">{{ matchCount }} 处匹配</span>
        <button class="jv-nav" @click="prevMatch">◀</button>
        <button class="jv-nav" @click="nextMatch">▶</button>
      </template>
      <span class="jv-spacer"></span>
      <button class="jv-btn" @click="doExpand">全部展开</button>
      <button class="jv-btn" @click="doCollapse">全部折叠</button>
    </div>

    <!-- JSON tree — scrollable -->
    <div class="jv-body" ref="bodyEl">
      <vue-json-pretty
        v-if="parsed !== null"
        :key="treeKey"
        :data="parsed"
        :deep="currentDeep"
        :show-length="true"
        :show-line="false"
        theme="dark"
      />
      <span v-else class="jv-empty">(empty)</span>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, nextTick } from 'vue'
import VueJsonPretty from 'vue-json-pretty'
import 'vue-json-pretty/lib/styles.css'

const props = defineProps({
  data: { default: null },
})

const search = ref('')
const bodyEl = ref(null)
const matchCount = ref(0)
const currentMatch = ref(-1)
const currentDeep = ref(999)
const treeKey = ref(0)

const parsed = computed(() => {
  const d = props.data
  if (d == null) return null
  if (typeof d === 'string') { try { return JSON.parse(d) } catch { return d } }
  return d
})

// Changing `deep` alone doesn't re-render vue-json-pretty.
// Bump `:key` to force a full re-mount.
function doExpand() {
  currentDeep.value = 999
  treeKey.value++
}

function doCollapse() {
  currentDeep.value = 0
  treeKey.value++
}

// Search highlight + navigation
watch(search, async () => {
  currentMatch.value = -1
  matchCount.value = 0
  await nextTick()
  if (!search.value) return
  highlightMatches()
}, { flush: 'post' })

function highlightMatches() {
  // Remove old highlights
  bodyEl.value?.querySelectorAll('.jv-search-hl').forEach(el => {
    el.replaceWith(document.createTextNode(el.textContent))
  })
  if (!search.value || !bodyEl.value) return

  const s = search.value.toLowerCase()
  // First pass: collect all matching text nodes (don't mutate DOM during walk)
  const textNodes = []
  const walker = document.createTreeWalker(bodyEl.value, NodeFilter.SHOW_TEXT)
  while (walker.nextNode()) {
    if (walker.currentNode.textContent.toLowerCase().includes(s)) {
      textNodes.push(walker.currentNode)
    }
  }
  // Second pass: replace text nodes with highlighted spans
  const marks = []
  for (const node of textNodes) {
    const span = document.createElement('span')
    span.className = 'jv-search-hl'
    span.textContent = node.textContent
    node.parentNode.replaceChild(span, node)
    marks.push(span)
  }
  matchCount.value = marks.length
  if (marks.length > 0) {
    currentMatch.value = 0
    scrollToCurrent()
  }
}

function scrollToCurrent() {
  const marks = bodyEl.value?.querySelectorAll('.jv-search-hl') || []
  if (!marks.length) return
  const idx = ((currentMatch.value % marks.length) + marks.length) % marks.length
  currentMatch.value = idx
  marks[idx].scrollIntoView({ behavior: 'smooth', block: 'center' })
  marks[idx].style.outline = '2px solid var(--color-warning)'
  setTimeout(() => { if (marks[idx]) marks[idx].style.outline = '' }, 2000)
}

function nextMatch() {
  currentMatch.value++
  scrollToCurrent()
}

function prevMatch() {
  currentMatch.value--
  scrollToCurrent()
}
</script>

<style>
.jv {
  font-family: 'Cascadia Code','Fira Code','JetBrains Mono','Consolas',monospace;
  font-size: 12px;
  line-height: 1.7;
  color: var(--text-code);
  display: flex;
  flex-direction: column;
  height: 100%;
}

.jv-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 0 10px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}

.jv-input {
  padding: 4px 10px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--bg-code);
  color: var(--text-code);
  font-size: 12px;
  outline: none;
  width: 180px;
}
.jv-input:focus { border-color: var(--accent); }
.jv-input::placeholder { color: var(--placeholder); }

.jv-cnt { font-size: 11px; color: var(--text-muted); white-space: nowrap; }

.jv-nav {
  padding: 2px 8px;
  border: 1px solid var(--border);
  border-radius: 3px;
  background: transparent;
  color: var(--text-muted);
  font-size: 11px;
  cursor: pointer;
}
.jv-nav:hover { background: var(--accent-bg); color: var(--accent); }

.jv-spacer { flex: 1; }

.jv-btn {
  padding: 3px 10px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: transparent;
  color: var(--text-muted);
  font-size: 11px;
  cursor: pointer;
  white-space: nowrap;
}
.jv-btn:hover { background: var(--accent-bg); color: var(--accent); }

.jv-body {
  overflow: auto;
  flex: 1;
  max-height: 480px;
  padding-top: 8px;
}

.jv-empty { color: var(--color-neutral); font-style: italic; }

/* ---- vue-json-pretty dark theme overrides ---- */
.vjs-tree {
  font-family: inherit !important;
  font-size: inherit !important;
  line-height: inherit !important;
  color: var(--text-code) !important;
}
.vjs-tree .vjs-tree__content { border-left: 1px solid var(--border) !important; }
.vjs-tree .vjs-tree__node { cursor: pointer; }
.vjs-tree .vjs-tree__node:hover { background: var(--accent-bg); }
.vjs-tree .vjs-key { color: var(--accent-hover) !important; }
.vjs-tree .vjs-string { color: var(--color-success) !important; }
.vjs-tree .vjs-number { color: var(--color-warning) !important; }
.vjs-tree .vjs-boolean { color: var(--color-danger) !important; }
.vjs-tree .vjs-null { color: var(--color-neutral) !important; }
.vjs-tree .vjs-toggle { color: var(--placeholder) !important; }
.vjs-tree .vjs-colon { color: var(--text-muted) !important; }
.vjs-tree .vjs-bracket { color: var(--text-muted) !important; }
.vjs-tree .vjs-comma { color: var(--text-muted) !important; }

/* search highlight */
.jv-search-hl {
  background: var(--color-warning-bg);
  color: var(--btn-warning-hover);
  border-radius: 2px;
  padding: 0 1px;
}
</style>
