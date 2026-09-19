import { createApp, type Component } from 'vue'
import AuthApp from './App.vue'
import Dashboard from './Dashboard.vue'
import Onboarding from './Onboarding.vue'
import AdminDashboard from './AdminDashboard.vue'
import Provisioning from './Provisioning.vue'
import Webmail from './Webmail.vue'
import Portal from './Portal.vue'
import './styles.css'
import { browserRoute, type RootView } from './routeManifest'
import { startSessionSync } from './api'

const path = location.pathname
const views = { App: AuthApp, Dashboard, AdminDashboard, Onboarding, Provisioning, Webmail, Portal } satisfies Record<RootView, Component>
const route = browserRoute(path)
const Root = views[route?.view || 'App']
if (route?.access === 'session') startSessionSync()

createApp(Root).mount('#app')

// Invitation URLs carry the one-time capability in the query string. Hydrate
// the already-rendered Vue v-model through its native input event so recipients
// can validate the link directly without manually copying the token.
if (path === '/invite') {
  const token = new URLSearchParams(location.search).get('token') || ''
  const input = document.querySelector<HTMLInputElement>('#invite')
  if (token && input) {
    input.value = token
    input.dispatchEvent(new Event('input', { bubbles: true }))
  }
}
