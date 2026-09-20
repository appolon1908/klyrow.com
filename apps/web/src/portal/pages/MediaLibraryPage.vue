<script setup lang="ts">
import { computed, ref } from 'vue'
import { appApi, idempotencyKey, type BrowserSession } from '../../api'
import type { PortalRoute } from '../routes'
import { usePage } from '../composables/usePage'
import PageHeader from '../components/PageHeader.vue'
import LoadingState from '../components/LoadingState.vue'
import ErrorState from '../components/ErrorState.vue'
import ForbiddenState from '../components/ForbiddenState.vue'
import UnavailableState from '../components/UnavailableState.vue'
import StatusBadge from '../components/StatusBadge.vue'

interface MediaAsset {
  id: string
  original_filename: string
  safe_filename: string
  detected_content_type?: string
  declared_content_type: string
  size_bytes: number
  width?: number
  height?: number
  status: string
  version: number
  ready_at?: string
  archived_at?: string
}
interface MediaResponse { items: MediaAsset[]; limit: number; offset: number; has_more: boolean }

defineProps<{ route: PortalRoute; params: Record<string, string>; session: BrowserSession }>()
const statusFilter = ref('')
const typeFilter = ref('')
const pageOffset = ref(0)
const actionError = ref('')
const uploadState = ref<'idle' | 'preparing' | 'uploading' | 'completing' | 'ready' | 'unavailable'>('idle')
const uploadProgress = ref(0)
const uploadReference = ref('')
const selectedFile = ref<File | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)

const page = usePage(() => {
  const params = new URLSearchParams({ limit: '24', offset: String(pageOffset.value) })
  if (statusFilter.value) params.set('status', statusFilter.value)
  if (typeFilter.value) params.set('type', typeFilter.value)
  return appApi<MediaResponse>(`/app/api/media?${params}`)
}, { cacheKey: 'media-library', isEmpty: data => data.items.length === 0 })
const assets = computed(() => page.data.value?.items || [])
const canNext = computed(() => Boolean(page.data.value?.has_more))
const canPrevious = computed(() => pageOffset.value > 0)
const formatBytes = (value: number) => value < 1024 * 1024 ? `${Math.round(value / 1024)} KB` : `${(value / (1024 * 1024)).toFixed(1)} MB`

function applyFilters() { pageOffset.value = 0; page.reload() }
function selectFile(event: Event) {
  selectedFile.value = (event.target as HTMLInputElement).files?.[0] || null
  actionError.value = ''
  uploadState.value = 'idle'
  uploadProgress.value = 0
}
async function prepareUpload() {
  if (!selectedFile.value) return
  actionError.value = ''
  uploadState.value = 'preparing'
  uploadProgress.value = 20
  try {
    const file = selectedFile.value
    const result = await appApi<MediaAsset & { upload_reference: string }>('/app/api/media/uploads', {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey('media-upload') },
      body: JSON.stringify({ original_filename: file.name, media_kind: 'image', declared_content_type: file.type, size_bytes: file.size }),
    })
    uploadReference.value = result.upload_reference
    uploadState.value = 'uploading'
    uploadProgress.value = 35
    await appApi(`/app/api/media/uploads/${encodeURIComponent(uploadReference.value)}`, {
      method: 'PUT',
      headers: { 'Content-Type': file.type || 'application/octet-stream' },
      body: file,
    })
    uploadState.value = 'completing'
    uploadProgress.value = 75
    const completed = await appApi<MediaAsset>(`/app/api/media/${result.id}/complete`, {
      method: 'POST',
      body: JSON.stringify({ upload_reference: uploadReference.value, expected_version: result.version }),
    })
    uploadProgress.value = 100
    uploadState.value = 'ready'
    selectedFile.value = null
    await page.reload()
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : 'Upload preparation failed'
    uploadState.value = 'unavailable'
  }
}
async function archive(asset: MediaAsset) {
  if (!window.confirm(`Archive ${asset.original_filename}?`)) return
  try { await appApi(`/app/api/media/${asset.id}/archive`, { method: 'POST', body: JSON.stringify({ expected_version: asset.version }) }); await page.reload() } catch (error) { actionError.value = error instanceof Error ? error.message : 'Archive failed' }
}
async function remove(asset: MediaAsset) {
  if (!window.confirm(`Delete ${asset.original_filename}?`)) return
  try { await appApi(`/app/api/media/${asset.id}`, { method: 'DELETE', body: JSON.stringify({ expected_version: asset.version }) }); await page.reload() } catch (error) { actionError.value = error instanceof Error ? error.message : 'Delete failed' }
}
function previousPage() { if (canPrevious.value) { pageOffset.value -= 24; page.reload() } }
function nextPage() { if (canNext.value) { pageOffset.value += 24; page.reload() } }
</script>

<template>
  <div class="media-page">
    <PageHeader title="Media library" eyebrow="Content" description="Tenant-isolated images with validation, lifecycle history, and safe provider-neutral storage references.">
      <label class="kp-button media-upload-button">Choose image<input ref="fileInput" type="file" accept="image/png,image/jpeg,image/webp" class="media-file-input" @change="selectFile"></label>
      <button type="button" class="kp-button kp-button-primary" :disabled="!selectedFile || ['preparing', 'uploading', 'completing'].includes(uploadState)" @click="prepareUpload">Upload image</button>
    </PageHeader>
    <section class="media-toolbar" aria-label="Media filters">
      <label>Status<select v-model="statusFilter" @change="applyFilters"><option value="">All states</option><option>PENDING_UPLOAD</option><option>UPLOADED</option><option>VALIDATING</option><option>READY</option><option>REJECTED</option><option>QUARANTINED</option><option>ARCHIVED</option></select></label>
      <label>Type<select v-model="typeFilter" @change="applyFilters"><option value="">All types</option><option value="image/png">PNG</option><option value="image/jpeg">JPEG</option><option value="image/webp">WebP</option></select></label>
      <button type="button" class="kp-button" :disabled="page.status.value === 'loading'" @click="page.reload">Refresh</button>
    </section>
    <p v-if="selectedFile || uploadState === 'ready'" class="media-selection" role="status"><span v-if="selectedFile">{{ selectedFile.name }} · {{ formatBytes(selectedFile.size) }}</span><span v-if="uploadState === 'preparing'"> · preparing {{ uploadProgress }}%</span><span v-else-if="uploadState === 'uploading'"> · uploading {{ uploadProgress }}%</span><span v-else-if="uploadState === 'completing'"> · validating {{ uploadProgress }}%</span><span v-else-if="uploadState === 'ready'">Upload complete · server confirmed READY</span></p>
    <UnavailableState v-if="uploadState === 'unavailable'" title="Storage unavailable" dependency="The upload reference could not be prepared. No file content was sent to the browser API." />
    <p v-if="actionError" class="media-error" role="alert">{{ actionError }}</p>
    <LoadingState v-if="page.status.value === 'loading'" label="Loading media library..." />
    <ForbiddenState v-else-if="page.status.value === 'forbidden'" reason="server" :request-id="page.failure.value?.requestId" :code="page.failure.value?.code" />
    <ErrorState v-else-if="page.failure.value" :failure="page.failure.value" @retry="page.reload" />
    <section v-else-if="assets.length" class="media-grid" aria-label="Media assets">
      <article v-for="asset in assets" :key="asset.id" class="media-card">
        <div class="media-preview" aria-hidden="true"><span>{{ asset.detected_content_type?.split('/')[1]?.toUpperCase() || 'IMAGE' }}</span></div>
        <div class="media-card-body"><h2>{{ asset.original_filename }}</h2><StatusBadge :value="asset.status" /><p>{{ formatBytes(asset.size_bytes) }}<span v-if="asset.width && asset.height"> · {{ asset.width }} x {{ asset.height }}</span></p></div>
        <div class="media-actions"><button v-if="asset.status === 'READY'" type="button" class="kp-button" @click="archive(asset)">Archive</button><button v-if="asset.status !== 'DELETED'" type="button" class="kp-button kp-button-danger" @click="remove(asset)">Delete</button></div>
      </article>
    </section>
    <section v-else-if="page.status.value === 'empty'" class="media-empty" aria-live="polite"><h2>No media yet</h2><p>Choose a PNG, JPEG, or WebP image to prepare the first tenant-scoped upload.</p></section>
    <nav class="media-pagination" aria-label="Media pages"><button type="button" class="kp-button" :disabled="!canPrevious" @click="previousPage">Previous</button><span>Page {{ Math.floor(pageOffset / 24) + 1 }}</span><button type="button" class="kp-button" :disabled="!canNext" @click="nextPage">Next</button></nav>
  </div>
</template>

<style scoped>
.media-page { display: grid; gap: 1rem; }
.media-toolbar { display: flex; flex-wrap: wrap; align-items: end; gap: 0.75rem; padding: 1rem; border: 1px solid var(--kp-border, #d9dee7); background: var(--kp-surface, #fff); }
.media-toolbar label { display: grid; gap: 0.35rem; font-size: 0.8rem; font-weight: 650; }
.media-toolbar select { min-width: 10rem; padding: 0.55rem; border: 1px solid var(--kp-border, #d9dee7); background: inherit; }
.media-upload-button { position: relative; overflow: hidden; cursor: pointer; }
.media-file-input { position: absolute; inset: 0; opacity: 0; cursor: pointer; }
.media-selection, .media-error { margin: 0; padding: 0.75rem 1rem; background: var(--kp-surface, #fff); border-left: 3px solid var(--kp-accent, #0f766e); }
.media-error { border-left-color: #b42318; color: #8a1c13; }
.media-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(14rem, 1fr)); gap: 1rem; }
.media-card { display: grid; overflow: hidden; border: 1px solid var(--kp-border, #d9dee7); background: var(--kp-surface, #fff); }
.media-preview { display: grid; place-items: center; min-height: 9rem; background: linear-gradient(135deg, #e7f5f2, #f5f7fb); color: #17635e; font-weight: 750; letter-spacing: 0.08em; }
.media-card-body { display: grid; gap: 0.45rem; padding: 1rem; }
.media-card h2 { margin: 0; overflow: hidden; font-size: 1rem; text-overflow: ellipsis; white-space: nowrap; }
.media-card p { margin: 0; color: var(--kp-muted, #667085); font-size: 0.85rem; }
.media-actions { display: flex; gap: 0.5rem; padding: 0 1rem 1rem; }
.media-empty { padding: 3rem 1rem; text-align: center; border: 1px dashed var(--kp-border, #d9dee7); }
.media-empty h2 { margin: 0 0 0.5rem; }
.media-empty p { margin: 0; color: var(--kp-muted, #667085); }
.media-pagination { display: flex; justify-content: center; align-items: center; gap: 1rem; }
@media (max-width: 640px) { .media-toolbar > * { width: 100%; } .media-toolbar select { width: 100%; } }
</style>
