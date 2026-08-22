<template>
  <WorkbenchHomeView v-if="authStore.isRegularUser" />
  <DashboardView v-else />
</template>

<script setup>
import { useAuthStore } from '../stores/auth'
import DashboardView from './DashboardView.vue'
import WorkbenchHomeView from './WorkbenchHomeView.vue'

const authStore = useAuthStore()
</script>
