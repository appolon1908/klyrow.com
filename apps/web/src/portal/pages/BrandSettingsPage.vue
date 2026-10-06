<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ApiError, appApi, type BrowserSession } from '../../api'
import type { PortalRoute } from '../routes'
import PageHeader from '../components/PageHeader.vue'
import PanelCard from '../components/PanelCard.vue'
import LoadingState from '../components/LoadingState.vue'
import ErrorState from '../components/ErrorState.vue'
import ForbiddenState from '../components/ForbiddenState.vue'

defineProps<{ route: PortalRoute; params: Record<string, string>; session: BrowserSession }>()

interface Brand {
  id: string; name: string; company_name: string; website_url: string | null; support_email: string | null
  primary_color: string; secondary_color: string; accent_color: string; background_color: string; text_color: string
  heading_font: string; body_font: string; footer_text: string | null; physical_address: string | null
  logo_asset_id: string | null; icon_asset_id: string | null; status: 'DRAFT' | 'ACTIVE' | 'ARCHIVED'; version: number
}
interface Version { version: number; snapshot: Brand; created_at: string }

const blank = (): Omit<Brand, 'id' | 'version' | 'status'> => ({
  name: 'Primary brand', company_name: '', website_url: '', support_email: '', primary_color: '#1565C0', secondary_color: '#263238',
  accent_color: '#E65100', background_color: '#FFFFFF', text_color: '#17212B', heading_font: 'Inter', body_font: 'Inter',
  footer_text: '', physical_address: '', logo_asset_id: null, icon_asset_id: null,
})
const brand = ref<Brand | null>(null)
const draft = ref(blank())
const history = ref<Version[]>([])
const loading = ref(true)
const saving = ref(false)
const publishing = ref(false)
const error = ref<unknown>(null)
const feedback = ref('')
const contrastWarning = computed(() => draft.value.background_color.toUpperCase() === draft.value.text_color.toUpperCase())
const fonts = ['Inter', 'Arial', 'Georgia', 'Helvetica', 'Roboto', 'Times New Roman', 'Verdana']

function formPayload() { return { ...draft.value } }
function sync(item: Brand) {
  brand.value = item
  draft.value = { ...item, website_url: item.website_url || '', support_email: item.support_email || '', footer_text: item.footer_text || '', physical_address: item.physical_address || '' }
}
async function load() {
  loading.value = true; error.value = null
  try {
    const result = await appApi<{ items: Brand[] }>('/app/api/brands')
    if (result.items[0]) {
      sync(result.items[0])
      history.value = (await appApi<{ items: Version[] }>(`/app/api/brands/${encodeURIComponent(result.items[0].id)}/versions`)).items
    } else brand.value = null
  } catch (reason) { error.value = reason } finally { loading.value = false }
}
async function save() {
  saving.value = true; feedback.value = ''; error.value = null
  try {
    const saved = brand.value
      ? await appApi<Brand>(`/app/api/brands/${encodeURIComponent(brand.value.id)}`, { method: 'PATCH', body: JSON.stringify({ ...formPayload(), version: brand.value.version }) })
      : await appApi<Brand>('/app/api/brands', { method: 'POST', body: JSON.stringify(formPayload()) })
    sync(saved); feedback.value = 'Draft saved.'
  } catch (reason) { error.value = reason } finally { saving.value = false }
}
async function publish() {
  if (!brand.value) return
  publishing.value = true; feedback.value = ''; error.value = null
  try {
    sync(await appApi<Brand>(`/app/api/brands/${encodeURIComponent(brand.value.id)}/publish`, { method: 'POST' }))
    history.value = (await appApi<{ items: Version[] }>(`/app/api/brands/${encodeURIComponent(brand.value.id)}/versions`)).items
    feedback.value = 'Brand published.'
  } catch (reason) { error.value = reason } finally { publishing.value = false }
}
async function restore(version: number) {
  if (!brand.value || !confirm(`Restore version ${version}? This creates a new published version.`)) return
  error.value = null
  try {
    sync(await appApi<Brand>(`/app/api/brands/${encodeURIComponent(brand.value.id)}/versions/${version}/restore`, { method: 'POST' }))
    history.value = (await appApi<{ items: Version[] }>(`/app/api/brands/${encodeURIComponent(brand.value.id)}/versions`)).items
    feedback.value = `Version ${version} restored as a new version.`
  } catch (reason) { error.value = reason }
}
onMounted(load)
</script>

<template>
  <div>
    <PageHeader title="Brand" eyebrow="Content" description="Create a tenant-specific identity, save a draft, and publish immutable brand versions.">
      <button type="button" class="kp-button" :disabled="loading" @click="load">Refresh</button>
    </PageHeader>
    <LoadingState v-if="loading" label="Loading brand settings…" />
    <ForbiddenState v-else-if="error instanceof ApiError && error.status === 403" reason="server" :code="error.code" :request-id="error.requestId" />
    <ErrorState v-else-if="error" :failure="error as any" @retry="load" />
    <div v-else class="kp-stack">
      <p v-if="feedback" class="kp-notice" role="status" aria-live="polite">{{ feedback }}</p>
      <div class="kp-split">
        <PanelCard title="Brand profile" eyebrow="Draft">
          <form class="kp-form" @submit.prevent="save">
            <label>Profile name<input v-model.trim="draft.name" required maxlength="200"></label>
            <label>Company name<input v-model.trim="draft.company_name" required maxlength="200"></label>
            <label>Website<input v-model.trim="draft.website_url" type="url" placeholder="https://example.com"></label>
            <label>Support email<input v-model.trim="draft.support_email" type="email" placeholder="support@example.com"></label>
            <label>Physical address<textarea v-model="draft.physical_address" rows="3" maxlength="4000"></textarea></label>
            <label>Footer text<textarea v-model="draft.footer_text" rows="3" maxlength="4000"></textarea></label>
            <fieldset><legend>Colors</legend><div class="kp-color-grid">
              <label v-for="field in ['primary_color', 'secondary_color', 'accent_color', 'background_color', 'text_color']" :key="field">{{ field.replace('_color', '').replace('_', ' ') }}
                <input v-model="draft[field as keyof typeof draft]" type="color" :aria-label="field.replace('_', ' ')">
              </label>
            </div></fieldset>
            <p v-if="contrastWarning" class="kp-form-error" role="alert">Background and text colors match. Choose contrasting colors before publishing.</p>
            <label>Heading font<select v-model="draft.heading_font"><option v-for="font in fonts" :key="font">{{ font }}</option></select></label>
            <label>Body font<select v-model="draft.body_font"><option v-for="font in fonts" :key="font">{{ font }}</option></select></label>
            <fieldset disabled><legend>Logo and icon</legend><p>Asset selection becomes available when the tenant Media Library is connected.</p></fieldset>
            <div class="kp-actions"><button class="kp-button" type="submit" :disabled="saving">{{ saving ? 'Saving…' : 'Save draft' }}</button><button v-if="brand" class="kp-button kp-button-primary" type="button" :disabled="publishing || contrastWarning" @click="publish">{{ publishing ? 'Publishing…' : 'Publish' }}</button></div>
          </form>
        </PanelCard>
        <PanelCard title="Live preview" eyebrow="Preview">
          <article class="brand-preview" :style="{ backgroundColor: draft.background_color, color: draft.text_color, fontFamily: draft.body_font }">
            <strong :style="{ color: draft.primary_color, fontFamily: draft.heading_font }">{{ draft.company_name || 'Company name' }}</strong>
            <p>Thoughtful messages, clearly branded.</p><a :style="{ color: draft.accent_color }" href="#preview" @click.prevent>Learn more</a>
            <footer :style="{ borderColor: draft.secondary_color }">{{ draft.footer_text || draft.physical_address || 'Footer text' }}</footer>
          </article>
        </PanelCard>
      </div>
      <PanelCard title="Version history" eyebrow="Published">
        <p v-if="!history.length">No published versions yet.</p>
        <table v-else class="kp-table"><thead><tr><th>Version</th><th>Published</th><th>Company</th><th><span class="sr-only">Actions</span></th></tr></thead><tbody><tr v-for="entry in history" :key="entry.version"><td>{{ entry.version }}</td><td>{{ new Date(entry.created_at).toLocaleString() }}</td><td>{{ entry.snapshot.company_name }}</td><td><button class="kp-button" type="button" @click="restore(entry.version)">Restore</button></td></tr></tbody></table>
      </PanelCard>
    </div>
  </div>
</template>

<style scoped>
.kp-split { display: grid; grid-template-columns: minmax(0, 1.4fr) minmax(16rem, .8fr); gap: 1rem; }
.kp-form { display: grid; gap: .85rem; } .kp-form label, fieldset { display: grid; gap: .35rem; }
.kp-color-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .65rem; text-transform: capitalize; }
.kp-actions { display: flex; gap: .75rem; flex-wrap: wrap; } .kp-notice { color: #096b3f; font-weight: 600; }
.brand-preview { min-height: 18rem; padding: 1.5rem; border: 1px solid #c7d1d8; } .brand-preview strong { font-size: 1.4rem; } .brand-preview footer { margin-top: 5rem; padding-top: 1rem; border-top: 1px solid; }
@media (max-width: 760px) { .kp-split { grid-template-columns: 1fr; } .kp-color-grid { grid-template-columns: 1fr; } }
</style>