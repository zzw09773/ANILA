<template>
  <div class="rg" :class="{ 'rg--full': fullscreen }">
    <div class="rg__bar">
      <span class="rg__legend"><i class="dot dot--rule"></i>規則</span>
      <span class="rg__legend"><i class="dot dot--manual"></i>手動</span>
      <span class="rg__legend"><i class="dot dot--llm"></i>LLM</span>
      <span class="rg__legend"><i class="dot dot--sim"></i>相似</span>
      <span class="rg__legend"><i class="dash"></i>未解析</span>
      <span class="rg__sep">·</span>
      <button class="term-btn term-btn--xs" @click="fit">[ 符合 ]</button>
      <button class="term-btn term-btn--xs" @click="toggleFull">{{ fullscreen ? '✕ 關閉' : '⛶ 全螢幕' }}</button>
      <span class="rg__hint cell-meta">拖曳節點 · 滾輪縮放 · 點節點看鄰接{{ fullscreen ? ' · Esc 關閉' : '' }}</span>
    </div>
    <div ref="cyEl" class="rg__canvas"></div>
    <p v-if="empty" class="rg__empty">無關聯可繪製</p>
  </div>
</template>

<script setup>
import { onMounted, onBeforeUnmount, nextTick, ref, watch, computed } from 'vue'
import cytoscape from 'cytoscape'

const props = defineProps({
  relations: { type: Array, default: () => [] },
  documents: { type: Array, default: () => [] },
})

const cyEl = ref(null)
let cy = null

const empty = computed(() => props.relations.length === 0)
const fullscreen = ref(false)

function toggleFull() { fullscreen.value = !fullscreen.value }

function onKey(e) { if (e.key === 'Escape' && fullscreen.value) fullscreen.value = false }

watch(fullscreen, () => {
  // the container changed size — cytoscape must re-measure then refit.
  nextTick(() => { if (cy) { cy.resize(); cy.fit(undefined, 24) } })
})

function cssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return v || fallback
}

function docLabel(id) {
  const d = props.documents.find(x => x.id === id)
  return (d && (d.title || d.filename)) || `#${id}`
}

function buildElements() {
  const nodes = new Map()
  const edges = []
  const addDoc = (id) => {
    const key = `d${id}`
    if (!nodes.has(key)) nodes.set(key, { data: { id: key, label: docLabel(id), kind: 'doc' } })
  }
  for (const r of props.relations) {
    addDoc(r.src_document_id)
    let target
    if (r.resolved && r.dst_document_id != null) {
      addDoc(r.dst_document_id)
      target = `d${r.dst_document_id}`
    } else {
      // dangling target: a cited regulation not (yet) uploaded
      const key = `t:${r.target_ref}`
      if (!nodes.has(key)) {
        nodes.set(key, { data: { id: key, label: r.target_ref, kind: r.ambiguous ? 'ambiguous' : 'missing' } })
      }
      target = key
    }
    edges.push({
      data: {
        id: `e${r.id}`,
        source: `d${r.src_document_id}`,
        target,
        label: r.relation_type,
        kind: r.source,
        resolved: !!r.resolved,
      },
    })
  }
  return [...nodes.values(), ...edges]
}

function styles() {
  const fg = cssVar('--c-fg-1', '#1b2230')
  const fg3 = cssVar('--c-fg-3', '#6b7484')
  const bg = cssVar('--c-surface-1', '#ffffff')
  const border = cssVar('--c-border', '#dfe3ea')
  const accent = cssVar('--c-accent', '#2b4c7e')
  const ok = cssVar('--c-ok', '#2f855a')
  const danger = cssVar('--c-danger', '#b03636')
  const warn = cssVar('--c-warn', '#b7791f')
  const llm = cssVar('--c-vlm', '#6b46c1')
  const sim = cssVar('--c-info', '#2b6cb0')
  return [
    {
      selector: 'node',
      style: {
        'background-color': bg,
        'border-width': 1,
        'border-color': border,
        label: 'data(label)',
        color: fg,
        'font-size': 11,
        'text-wrap': 'wrap',
        'text-max-width': 130,
        'text-valign': 'center',
        'text-halign': 'center',
        shape: 'round-rectangle',
        width: 'label',
        height: 'label',
        padding: '8px',
      },
    },
    { selector: 'node[kind="doc"]', style: { 'border-color': accent } },
    {
      selector: 'node[kind="missing"]',
      style: { 'border-color': danger, 'border-style': 'dashed', color: fg3 },
    },
    {
      selector: 'node[kind="ambiguous"]',
      style: { 'border-color': warn, 'border-style': 'dashed', color: fg3 },
    },
    {
      selector: 'edge',
      style: {
        width: 1.5,
        label: 'data(label)',
        'font-size': 9,
        color: fg3,
        'text-background-color': bg,
        'text-background-opacity': 1,
        'text-background-padding': 2,
        'curve-style': 'bezier',
        'target-arrow-shape': 'triangle',
        'arrow-scale': 0.9,
        'line-color': accent,
        'target-arrow-color': accent,
      },
    },
    { selector: 'edge[kind="manual"]', style: { 'line-color': ok, 'target-arrow-color': ok } },
    { selector: 'edge[kind="llm"]', style: { 'line-color': llm, 'target-arrow-color': llm } },
    {
      // topic-similarity is undirected (no arrow) + dotted, distinct teal
      selector: 'edge[kind="similarity"]',
      style: {
        'line-color': sim, 'line-style': 'dotted', 'target-arrow-shape': 'none',
      },
    },
    {
      selector: 'edge[?resolved = false], edge[resolved = "false"]',
      style: { 'line-style': 'dashed' },
    },
    { selector: 'edge[resolved = 0]', style: { 'line-style': 'dashed' } },
    { selector: '.faded', style: { opacity: 0.12 } },
    { selector: '.hl', style: { 'border-width': 2, width: 3 } },
  ]
}

function render() {
  if (!cy) return
  cy.elements().remove()
  cy.add(buildElements())
  cy.layout({
    name: 'cose',
    animate: false,
    padding: 24,
    nodeRepulsion: 6000,
    idealEdgeLength: 110,
    nodeDimensionsIncludeLabels: true,
  }).run()
  cy.fit(undefined, 24)
}

function fit() { if (cy) cy.fit(undefined, 24) }

function wireInteractions() {
  cy.on('tap', 'node', (evt) => {
    const n = evt.target
    const neighborhood = n.closedNeighborhood()
    cy.elements().addClass('faded')
    neighborhood.removeClass('faded')
    n.addClass('hl')
  })
  cy.on('tap', (evt) => {
    if (evt.target === cy) cy.elements().removeClass('faded hl')
  })
}

onMounted(() => {
  cy = cytoscape({
    container: cyEl.value,
    elements: buildElements(),
    style: styles(),
    layout: { name: 'cose', animate: false, padding: 24, nodeDimensionsIncludeLabels: true },
    wheelSensitivity: 1.0,
    minZoom: 0.2,
    maxZoom: 3,
  })
  wireInteractions()
  cy.ready(() => cy.fit(undefined, 24))
  window.addEventListener('keydown', onKey)
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKey)
  if (cy) { cy.destroy(); cy = null }
})

watch(() => props.relations, render, { deep: false })
</script>

<style scoped>
.rg { display: flex; flex-direction: column; gap: var(--gap-2); }
.rg__bar { display: flex; align-items: center; gap: var(--gap-3); flex-wrap: wrap; font-size: var(--t-2xs); color: var(--c-fg-2); }
.rg__legend { display: inline-flex; align-items: center; gap: 5px; }
.rg__sep { color: var(--c-border-strong); }
.rg__hint { margin-left: auto; }
.dot { width: 18px; height: 0; border-top: 2px solid; display: inline-block; }
.dot--rule { border-color: var(--c-accent); }
.dot--manual { border-color: var(--c-ok); }
.dot--llm { border-color: var(--c-vlm); }
.dot--sim { border-color: var(--c-info); border-top-style: dotted; }
.dash { width: 18px; height: 0; border-top: 2px dashed var(--c-danger); display: inline-block; }
.rg__canvas {
  width: 100%; height: 560px;
  border: var(--border-w) solid var(--c-border);
  background:
    radial-gradient(var(--c-border) 1px, transparent 1px) 0 0 / 22px 22px,
    var(--c-bg);
}
.rg__empty { text-align: center; color: var(--c-fg-3); font-size: var(--t-sm); margin: 0; }

/* Fullscreen overlay — same cytoscape instance, just a bigger container. */
.rg--full {
  position: fixed; inset: 0; z-index: 1000;
  background: var(--c-bg); padding: var(--gap-3);
  display: flex; flex-direction: column; gap: var(--gap-2);
}
.rg--full .rg__canvas { flex: 1; height: auto; }
</style>
