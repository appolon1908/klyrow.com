import { defineConfig } from '@playwright/test'
export default defineConfig({ testDir:'./e2e', use:{ baseURL:'http://127.0.0.1:4173', locale:'en-US' }, webServer:{ command:'pnpm vite build && node e2e/server.mjs', port:4173, reuseExistingServer:true } })
