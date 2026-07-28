import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import './assets/styles/main.css'
import { installErrorHandler } from './errorHandler'

const app = createApp(App)
// W0-7:render 期 throw 在此之前是白畫面零訊息,而這是管理員唯一入口。
installErrorHandler(app)
app.use(createPinia())
app.use(router)
app.mount('#app')
