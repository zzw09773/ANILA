import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import './assets/styles/main.css'
import { installErrorHandler } from './components/errorPanel.js'

const app = createApp(App)
installErrorHandler(app)
app.use(createPinia())
app.use(router)
app.mount('#app')
