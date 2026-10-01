<template>
  <div class="page">
    <PageHead
      title="skill 審核"
      subtitle="單位管理員看自己單位的申請，管理員看全院與各單位。內容只是給模型的指示，平台不會執行。"
    />

    <div v-if="error" class="feedback is-err"><span>!</span><span>{{ error }}</span></div>
    <p v-if="loading" class="hint">讀取中…</p>

    <TermBox title="待審" pad="md">
      <p v-if="!loading && pending.length === 0" class="hint">沒有待審的 skill。</p>
      <ul v-else class="skill-list">
        <li v-for="row in pending" :key="row.version_id" class="skill-row" :data-version-id="row.version_id">
          <div class="skill-row__main">
            <div class="skill-row__meta">
              <strong>{{ row.name }}</strong>
              <TermBadge>待審</TermBadge>
              <span>{{ scopeLabel(row.scope) }}</span>
              <span v-if="row.department_name">{{ row.department_name }}</span>
              <span v-if="row.owner_username">{{ row.owner_username }}</span>
            </div>
            <div class="hint">{{ row.description }}</div>
            <pre class="skill-body">{{ row.body }}</pre>
            <TermField label="退回理由">
              <textarea v-model="reasons[row.version_id]" class="term-textarea" rows="2" maxlength="500" />
            </TermField>
          </div>
          <div class="row-actions">
            <TermButton variant="primary" :disabled="busy" label="核准" @click="approve(row)" />
            <TermButton
              :disabled="busy || !String(reasons[row.version_id] || '').trim()"
              label="退回"
              @click="reject(row)"
            />
          </div>
        </li>
      </ul>
    </TermBox>

    <TermBox title="已發布" pad="md">
      <p v-if="!loading && published.length === 0" class="hint">沒有已發布的 skill。</p>
      <ul v-else class="skill-list">
        <li v-for="row in published" :key="row.version_id" class="skill-row">
          <div class="skill-row__main">
            <div class="skill-row__meta">
              <strong>{{ row.name }}</strong>
              <TermBadge variant="accent">已發布</TermBadge>
              <span>{{ scopeLabel(row.scope) }}</span>
              <span v-if="row.department_name">{{ row.department_name }}</span>
            </div>
            <div class="hint">{{ row.description }}</div>
            <pre class="skill-body">{{ row.body }}</pre>
          </div>
          <div class="row-actions">
            <TermButton :disabled="busy" label="下架" @click="unpublish(row)" />
          </div>
        </li>
      </ul>
    </TermBox>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { PageHead, TermBadge, TermBox, TermButton, TermField } from '../components/cli'
import { extractError } from '../api/errors'
import {
  approveSkillReview,
  listSkillReviews,
  rejectSkillReview,
  unpublishSkillReview,
} from '../api/skills'

const pending = ref([])
const published = ref([])
const reasons = reactive({})
const error = ref('')
const loading = ref(true)
const busy = ref(false)

function scopeLabel(scope) {
  if (scope === 'unit') return '單位'
  if (scope === 'campus') return '全院'
  if (scope === 'personal') return '個人'
  return scope || ''
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const res = await listSkillReviews()
    pending.value = res.data?.pending || []
    published.value = res.data?.published || []
  } catch (err) {
    error.value = extractError(err)
  } finally {
    loading.value = false
  }
}

async function approve(row) {
  busy.value = true
  error.value = ''
  try {
    await approveSkillReview(row.version_id)
    await load()
  } catch (err) {
    error.value = extractError(err)
  } finally {
    busy.value = false
  }
}

async function reject(row) {
  const reason = String(reasons[row.version_id] || '').trim()
  if (!reason) return
  busy.value = true
  error.value = ''
  try {
    await rejectSkillReview(row.version_id, reason)
    await load()
  } catch (err) {
    error.value = extractError(err)
  } finally {
    busy.value = false
  }
}

async function unpublish(row) {
  busy.value = true
  error.value = ''
  try {
    await unpublishSkillReview(row.version_id)
    await load()
  } catch (err) {
    error.value = extractError(err)
  } finally {
    busy.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.skill-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 12px; }
.skill-row { display: grid; gap: 8px; padding-bottom: 12px; border-bottom: 1px solid var(--line, #ddd); }
.skill-row__meta { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.skill-body {
  white-space: pre-wrap;
  margin: 0;
  font-size: 12px;
  line-height: 1.5;
}
.row-actions { display: flex; gap: 8px; }
</style>
