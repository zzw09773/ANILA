<template>
  <div class="page">
    <header class="page-head">
      <div>
        <p class="page-head__eyebrow">admin · platform</p>
        <h1 class="page-head__title">platform-links</h1>
        <p class="page-head__sub">external tooling shown on dashboard · per-link role gate + grant whitelist</p>
      </div>
      <TermButton variant="primary" @click="openCreateModal" label="add link" />
    </header>

    <div v-if="pageError" class="feedback is-err">! {{ pageError }}</div>

    <TermBox :title="`links · ${links.length}`" pad="none" flush>
      <table class="term-table">
        <thead>
          <tr>
            <th>name</th>
            <th>url</th>
            <th style="width: 80px" class="num">order</th>
            <th style="width: 100px">status</th>
            <th style="width: 14%">ops</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="link in links" :key="link.id">
            <td>
              <div class="cell-strong">{{ link.name }}</div>
              <div class="cell-meta">{{ link.description || 'no description' }}</div>
            </td>
            <td><code class="cell-url" :title="link.url">{{ link.url }}</code></td>
            <td class="num tnum">{{ link.sort_order }}</td>
            <td><TermBadge :variant="link.is_active ? 'ok' : 'danger'" dot>{{ link.is_active ? 'active' : 'inactive' }}</TermBadge></td>
            <td>
              <div class="row-actions">
                <button class="term-action" @click="openEditModal(link)">edit</button>
                <span class="row-actions__sep">·</span>
                <button v-if="link.is_active" class="term-action term-action--danger" @click="handleDeactivate(link)">deactivate</button>
                <button v-else class="term-action" @click="handleReactivate(link)">reactivate</button>
              </div>
            </td>
          </tr>
          <tr v-if="links.length === 0">
            <td colspan="5"><TermEmpty message="no platform links · add one to surface external tools on the dashboard" /></td>
          </tr>
        </tbody>
      </table>
    </TermBox>

    <TermModal :visible="showModal" :title="editingId ? 'edit · link' : 'add · link'" width="540px" @close="showModal = false">
      <div class="form-grid">
        <TermField label="name">
          <input v-model="form.name" class="term-input" />
        </TermField>
        <TermField label="url">
          <input v-model="form.url" class="term-input" placeholder="https://…" />
        </TermField>
        <div class="form-row-2">
          <TermField label="icon" hint="workflow · git · notebook · chat · monitor · database · api · docs · cpu">
            <input v-model="form.icon" class="term-input" placeholder="workflow" />
          </TermField>
          <TermField label="sort order">
            <input v-model.number="form.sort_order" type="number" class="term-input" />
          </TermField>
        </div>
        <TermField label="description" optional>
          <textarea v-model="form.description" rows="2" class="term-textarea" />
        </TermField>

        <TermSection title="access control" />

        <label class="form-toggle">
          <input v-model="form.is_public" type="checkbox" />
          <span>
            <span class="form-toggle__title">public</span>
            <span class="form-toggle__hint">any user passing the role gate can see this · no individual grant required</span>
          </span>
        </label>

        <TermField label="required roles" hint="empty = open gate · admin always passes">
          <div class="role-grid">
            <label
              v-for="role in availableRoles"
              :key="role"
              class="role-chip"
              :class="{ 'is-on': form.required_roles.includes(role) }"
            >
              <input
                type="checkbox"
                :value="role"
                :checked="form.required_roles.includes(role)"
                @change="toggleRole(role)"
              />
              <span>{{ role }}</span>
            </label>
          </div>
        </TermField>
      </div>
      <template #footer>
        <TermButton variant="ghost" @click="showModal = false" label="cancel" />
        <TermButton variant="primary" :disabled="!form.name.trim() || !form.url.trim()" :label="editingId ? 'update' : 'create'" @click="handleSubmit" />
      </template>
    </TermModal>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { listPlatformLinks, createPlatformLink, updatePlatformLink, deletePlatformLink } from '../api/platformLinks'
import { TermBox, TermButton, TermField, TermBadge, TermEmpty, TermModal, TermSection } from '../components/cli'

const links = ref([])
const showModal = ref(false)
const editingId = ref(null)
const form = ref({ name: '', url: '', icon: '', description: '', sort_order: 0, is_public: false, required_roles: [] })
const pageError = ref('')

const availableRoles = ['admin', 'developer', 'user']

function toggleRole(role) {
  const idx = form.value.required_roles.indexOf(role)
  if (idx >= 0) form.value.required_roles.splice(idx, 1)
  else form.value.required_roles.push(role)
}

async function fetchLinks() {
  pageError.value = ''
  try {
    const { data } = await listPlatformLinks({ include_inactive: true })
    links.value = data
  } catch (e) {
    pageError.value = e.response?.data?.detail || 'failed to load links'
  }
}
onMounted(fetchLinks)

function openCreateModal() {
  editingId.value = null
  form.value = { name: '', url: '', icon: '', description: '', sort_order: 0, is_public: false, required_roles: [] }
  showModal.value = true
}
function openEditModal(link) {
  editingId.value = link.id
  form.value = {
    name: link.name, url: link.url, icon: link.icon || '',
    description: link.description || '', sort_order: link.sort_order || 0,
    is_public: !!link.is_public, required_roles: Array.isArray(link.required_roles) ? [...link.required_roles] : [],
  }
  showModal.value = true
}
async function handleSubmit() {
  const payload = {
    name: form.value.name.trim(), url: form.value.url.trim(),
    icon: form.value.icon.trim() || null, description: form.value.description.trim() || null,
    sort_order: form.value.sort_order || 0,
    is_public: !!form.value.is_public, required_roles: form.value.required_roles,
  }
  try {
    if (editingId.value) await updatePlatformLink(editingId.value, payload)
    else await createPlatformLink(payload)
    showModal.value = false
    await fetchLinks()
  } catch (e) { alert(e.response?.data?.detail || 'save failed') }
}
async function handleDeactivate(link) {
  if (!confirm(`deactivate '${link.name}'?`)) return
  try { await deletePlatformLink(link.id); await fetchLinks() }
  catch (e) { alert(e.response?.data?.detail || 'deactivate failed') }
}
async function handleReactivate(link) {
  try { await updatePlatformLink(link.id, { is_active: true }); await fetchLinks() }
  catch (e) { alert(e.response?.data?.detail || 'reactivate failed') }
}
</script>

<style scoped>
.page { display: flex; flex-direction: column; gap: var(--gap-4); padding-bottom: var(--gap-8); }
.page-head { display: flex; justify-content: space-between; align-items: flex-end; gap: var(--gap-3); flex-wrap: wrap; }
.page-head__eyebrow { font-size: var(--t-2xs); letter-spacing: var(--tracking-caps); text-transform: uppercase; color: var(--c-fg-3); }
.page-head__title { font-size: var(--t-2xl); font-weight: 600; letter-spacing: var(--tracking-tight); margin: 4px 0 2px; }
.page-head__sub { font-size: var(--t-xs); color: var(--c-fg-3); }

.feedback { font-size: var(--t-xs); padding: var(--gap-2) var(--gap-3); border: var(--border-w) solid; }
.feedback.is-err { color: var(--c-danger); border-color: var(--c-danger); background: var(--c-danger-soft); }

.cell-strong { color: var(--c-fg-1); font-weight: 500; }
.cell-meta { color: var(--c-fg-3); font-size: var(--t-2xs); }
.cell-url {
  display: inline-block;
  max-width: 360px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--font-mono);
  font-size: var(--t-2xs);
  color: var(--c-fg-2);
  background: var(--c-bg);
  border: var(--border-w) solid var(--c-border);
  padding: 1px 6px;
}
.row-actions { display: inline-flex; gap: 6px; align-items: center; font-size: var(--t-xs); }
.row-actions__sep { color: var(--c-border-strong); }

.form-grid { display: flex; flex-direction: column; gap: var(--gap-3); }
.form-row-2 { display: grid; grid-template-columns: 1fr 1fr; gap: var(--gap-3); }
.form-toggle {
  display: flex;
  gap: var(--gap-3);
  align-items: flex-start;
  cursor: pointer;
  padding: var(--gap-2) var(--gap-3);
  border: var(--border-w) solid var(--c-border);
  background: var(--c-bg);
  font-size: var(--t-sm);
}
.form-toggle input { margin-top: 2px; accent-color: var(--c-accent); }
.form-toggle__title { display: block; color: var(--c-fg-1); font-weight: 500; }
.form-toggle__hint { display: block; color: var(--c-fg-3); font-size: var(--t-2xs); margin-top: 2px; }

.role-grid { display: flex; flex-wrap: wrap; gap: var(--gap-2); }
.role-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border: var(--border-w) solid var(--c-border-strong);
  background: var(--c-bg);
  color: var(--c-fg-2);
  cursor: pointer;
  font-size: var(--t-sm);
}
.role-chip.is-on {
  border-color: var(--c-accent);
  color: var(--c-accent);
  background: var(--c-accent-soft);
}
.role-chip input { accent-color: var(--c-accent); }
</style>
