import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import { router } from './router'
import { setUnauthorizedHandler } from './api/client'
import { useUiStore } from './stores/ui'
import './styles/tokens.css'

const app = createApp(App)
const pinia = createPinia()

app.use(pinia)
app.use(router)

const ui = useUiStore(pinia)

/**
 * 令牌失效的统一处理。
 *
 * 旧实现只弹一条 5 秒后消失的提示，用户在别的标签页或走动一圈回来
 * 就完全错过；且跳转登录页后提示也已消失，不知道发生过什么。
 * 现在置为常驻告警（右上角横幅），登录成功后才清除。
 */
setUnauthorizedHandler(() => {
  const current = router.currentRoute.value
  ui.markSessionExpired()
  if (current.name !== 'login') {
    void router.replace({ name: 'login', query: { redirect: current.fullPath } })
  }
})

ui.initTheme()

app.mount('#app')
