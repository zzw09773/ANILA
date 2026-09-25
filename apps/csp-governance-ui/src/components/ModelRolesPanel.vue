<template>
  <TermBox
    v-if="isAdmin"
    title="模型角色"
    hint="平台自己用的模型在這裡指定。改完約一分鐘內生效，不用重啟服務。"
  >
    <p v-if="loadError" class="role-error">{{ loadError }}</p>
    <table v-else class="term-table role-table">
      <thead>
        <tr>
          <th style="width: 22%">角色</th>
          <th>模型</th>
          <th style="width: 28%">狀態</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="role in roles" :key="role.role">
          <td>
            <div class="cell-strong">{{ role.label }}</div>
            <div class="cell-meta">{{ role.description }}</div>
          </td>
          <td>
            <select
              class="term-select"
              :disabled="savingRole === role.role"
              :value="role.model && role.model.is_active ? role.model.id : ''"
              @change="onChange(role, $event.target.value)"
            >
              <option value="">— 尚未設定 —</option>
              <option
                v-for="model in choices(role)"
                :key="model.id"
                :value="model.id"
              >
                {{ model.display_name || model.name }}（{{ model.name }}）
              </option>
            </select>
            <div v-if="role.model && !role.model.is_active" class="cell-meta">
              目前指向已停用的 {{ role.model.display_name || role.model.name }}
            </div>
            <div v-if="roleAudience(role)" class="role-grant">
              <p v-if="roleAudience(role).granted" class="cell-meta">已授權給所有使用者</p>
              <template v-else>
                <p class="cell-meta">{{ roleAudience(role).warning }}</p>
                <TermButton
                  size="xs"
                  label="授權給所有使用者"
                  :disabled="savingRole === role.role"
                  :loading="savingRole === role.role"
                  @click="grantAll(role)"
                />
              </template>
            </div>
          </td>
          <td>
            <TermBadge v-if="roleWarning(role)" variant="warn">需設定</TermBadge>
            <TermBadge v-else variant="ok">已指定</TermBadge>
            <div v-if="roleWarning(role)" class="cell-meta">{{ roleWarning(role) }}</div>
          </td>
        </tr>
      </tbody>
    </table>
  </TermBox>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { TermBox, TermBadge, TermButton } from './cli'
import { assignModelRole, clearModelRole, grantModelRoleAllUsers, listModelRoles } from '../api/models'
import { extractError } from '../api/errors'
import { useDialog } from '../composables/useDialog'
import { designationConfirm, designationToast } from '../utils/platformEmbedding'
import { eligibleRoleModels, roleAudience, roleWarning } from '../utils/modelRoles'

const props = defineProps({
  isAdmin: { type: Boolean, default: false },
  models: { type: Array, default: () => [] },
})
const emit = defineEmits(['changed'])

const { confirm, toast } = useDialog()
const roles = ref([])
const loadError = ref('')
const savingRole = ref('')

function choices(role) {
  let list = eligibleRoleModels(props.models, role.accepted_types)
  if (role.role === 'router_primary') {
    list = list.filter((model) => model.router_enabled)
  }
  return list
}

async function load() {
  if (!props.isAdmin) return
  try {
    const { data } = await listModelRoles()
    roles.value = Array.isArray(data?.roles) ? data.roles : []
    loadError.value = ''
  } catch (err) {
    loadError.value = extractError(err)
  }
}

async function onChange(role, raw) {
  const nextId = raw === '' ? null : Number(raw)
  if (role.role === 'platform_embedding' && nextId) {
    const gate = designationConfirm(props.models, nextId)
    if (gate.needed) {
      const ok = await confirm({
        title: '更換平台嵌入模型',
        message: gate.message,
        confirmText: gate.confirmText,
        danger: gate.danger,
      })
      if (!ok) {
        await load()
        return
      }
    }
  }
  savingRole.value = role.role
  try {
    const { data } = nextId
      ? await assignModelRole(role.role, nextId)
      : await clearModelRole(role.role)
    const note = designationToast(data)
    if (note) toast(note.message, { tone: note.tone, duration: note.duration })
    else toast(nextId ? `已設定${role.label}` : `已取消${role.label}`, { tone: 'success' })
    emit('changed')
    await load()
  } catch (err) {
    toast(extractError(err, '設定模型角色失敗'), { tone: 'error' })
    await load()
  } finally {
    savingRole.value = ''
  }
}

async function grantAll(role) {
  savingRole.value = role.role
  try {
    await grantModelRoleAllUsers(role.role)
    toast(`已將${role.label}授權給所有使用者`, { tone: 'success' })
    emit('changed')
    await load()
  } catch (err) {
    toast(extractError(err, '授權給所有使用者失敗'), { tone: 'error' })
  } finally {
    savingRole.value = ''
  }
}

onMounted(load)
</script>

<style scoped>
.role-table { width: 100%; }
.role-error { color: var(--term-danger, #b42318); margin: 0; }
.role-grant { margin-top: 8px; }
</style>
