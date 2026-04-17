<template>
  <div class="flex min-h-screen">
    <AppSidebar />
    <div class="flex-1 flex flex-col">
      <AppHeader />
      <main class="flex-1 p-6 bg-gray-50 overflow-auto">
        <router-view />
      </main>
    </div>
  </div>
</template>

<script setup>
import AppSidebar from './AppSidebar.vue'
import AppHeader from './AppHeader.vue'
</script>
