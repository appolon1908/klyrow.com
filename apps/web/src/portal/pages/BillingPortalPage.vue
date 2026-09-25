<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { appApi, type BrowserSession } from '../../api'
import type { PortalRoute } from '../routes'
import type { BillingInvoice, BillingInvoiceDetail, BillingOverview, BillingPayment, BillingPaymentMethod, BillingRefund, BillingSubscription, BillingWallet } from '../types'
import { usePage } from '../composables/usePage'
import PageHeader from '../components/PageHeader.vue'
import PanelCard from '../components/PanelCard.vue'
import MetricCard from '../components/MetricCard.vue'
import DataTable from '../components/DataTable.vue'
import PaginationControls from '../components/PaginationControls.vue'
import StatusBadge from '../components/StatusBadge.vue'
import LoadingState from '../components/LoadingState.vue'
import ErrorState from '../components/ErrorState.vue'
import ForbiddenState from '../components/ForbiddenState.vue'
import UnavailableState from '../components/UnavailableState.vue'

type Collection<T> = { items: T[]; offset?: number; limit?: number; has_more?: boolean }
type RecordItem = BillingInvoice | BillingPayment | BillingRefund | BillingPaymentMethod
type BillingCreditNote = { id: string; number: string; invoice_id: string; amount: number | string; currency: string; reason: string; created_at: string }

const props = defineProps<{ route: PortalRoute; params: Record<string, string>; session: BrowserSession }>()
const routeName = computed(() => props.route.name)
const statusFilter = ref('')
const offset = ref(0)
const limit = 25
const endpoint = computed(() => {
  const base = ({
    'billing-overview': '/app/api/billing/overview',
    'billing-subscription': '/app/api/billing/subscription',
    'billing-invoices': '/app/api/billing/invoices',
    'billing-invoice': `/app/api/billing/invoices/${encodeURIComponent(props.params.id || '')}`,
    'billing-payments': '/app/api/billing/payments',
    'billing-refunds': '/app/api/billing/refunds',
    'billing-payment-methods': '/app/api/billing/payment-methods',
    'billing-wallet': '/app/api/billing/wallet',
  }[routeName.value] || '')
  if (routeName.value !== 'billing-invoices') return base
  const query = new URLSearchParams({ offset: String(offset.value), limit: String(limit) })
  if (statusFilter.value) query.set('status', statusFilter.value)
  return `${base}?${query}`
})
const page = usePage(() => appApi<unknown>(endpoint.value), {
  cacheKey: computed(() => `billing:${routeName.value}:${props.params.id || ''}:${statusFilter.value}:${offset.value}`).value,
  immediate: props.route.availability !== 'unavailable',
})
const overview = computed(() => page.data.value as BillingOverview | null)
const subscription = computed(() => page.data.value as BillingSubscription | null)
const invoice = computed(() => page.data.value as BillingInvoiceDetail | null)
const wallet = computed(() => page.data.value as BillingWallet | null)
const checkoutEnabled = ref(false)
const checkoutLoading = ref(false)
const checkoutFailure = ref('')
const creditNotes = ref<BillingCreditNote[]>([])
const creditNotesUnavailable = ref(false)
const canManageBilling = computed(() => props.session.capabilities?.includes('billing.manage') === true)
const collection = computed(() => {
  const value = page.data.value as Collection<RecordItem> | RecordItem[] | null
  return Array.isArray(value) ? { items: value, has_more: false } : value || { items: [], has_more: false }
})
const unavailable = computed(() => page.failure.value?.kind === 'not-found' || page.failure.value?.kind === 'unavailable')
const missingBrowserApi = computed(() => props.route.availability === 'unavailable')
const title = computed(() => props.route.title)
const description = computed(() => ({
  'billing-overview': 'Read-only billing status for this organization. No payment or provider action is available here.',
  'billing-subscription': 'Current subscription and plan details supplied by the billing service.',
  'billing-invoices': 'Invoices issued to this organization, with canonical totals from the billing service.',
  'billing-invoice': 'Invoice detail and line items. Sensitive payment credentials are never displayed.',
  'billing-payments': 'Existing payment records only. This view cannot submit or retry a payment.',
  'billing-refunds': 'Existing refund records only. This view cannot request or issue a refund.',
  'billing-payment-methods': 'References to payment methods on file. Full account numbers and security codes are never shown.',
  'billing-wallet': 'Wallet balance and transaction history, when billing capabilities are available.',
}[routeName.value]))
const recordColumns = [
  { key: 'reference', label: 'Reference' }, { key: 'status', label: 'Status' }, { key: 'date', label: 'Date' }, { key: 'amount_display', label: 'Amount' },
]

function money(value: number | null | undefined, currency = 'USD') {
  if (value === null || value === undefined) return 'Not available'
  return new Intl.NumberFormat(undefined, { style: 'currency', currency }).format(value)
}
function date(value?: string | null) { return value ? new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' }).format(new Date(value)) : 'Not available' }
function readable(value?: string | null) { return value ? value.replaceAll('_', ' ') : 'Not available' }
function displayRecord(item: RecordItem): Record<string, unknown> {
  const id = String((item as { id: string }).id)
  return { id, reference: 'reference' in item ? item.reference : 'display' in item ? item.display : id, status: item.status, date: 'created_at' in item ? date(item.created_at) : 'issued_at' in item ? date(item.issued_at) : 'On file', amount_display: 'amount' in item ? money(item.amount, item.currency) : 'total' in item ? money(item.total, item.currency) : 'Reference only' }
}
function applyFilter() { offset.value = 0; void page.reload() }
function previous() { offset.value = Math.max(0, offset.value - limit); void page.reload() }
function next() { offset.value += limit; void page.reload() }

watch(() => [routeName.value, invoice.value?.id] as const, async ([name, id]) => {
  creditNotes.value = []
  creditNotesUnavailable.value = false
  if (name === 'billing-invoice' && id) {
    try {
      const result = await appApi<{ items: BillingCreditNote[] }>('/app/api/billing/credit-notes')
      creditNotes.value = result.items.filter(item => item.invoice_id === id)
    } catch {
      creditNotes.value = []
      creditNotesUnavailable.value = true
    }
  }
}, { immediate: true })

watch(() => [routeName.value, invoice.value?.id] as const, async ([name, id]) => {
  checkoutEnabled.value = false
  if (name !== 'billing-invoice' || !id || !canManageBilling.value) return
  try {
    const capabilities = await appApi<{ checkout_enabled?: boolean; stripe?: { available?: boolean } }>('/app/api/billing/capabilities')
    checkoutEnabled.value = capabilities.checkout_enabled === true && capabilities.stripe?.available === true
  } catch {
    checkoutEnabled.value = false
  }
}, { immediate: true })

async function startCheckout() {
  if (!invoice.value?.id || checkoutLoading.value) return
  checkoutLoading.value = true
  checkoutFailure.value = ''
  try {
    const key = typeof crypto?.randomUUID === 'function' ? crypto.randomUUID() : `checkout-${Date.now()}`
    const result = await appApi<{ hosted_checkout_url?: string }>(
      `/app/api/billing/invoices/${encodeURIComponent(invoice.value.id)}/checkout`,
      { method: 'POST', headers: { 'Idempotency-Key': key } },
    )
    if (!result.hosted_checkout_url) throw new Error('checkout_url_missing')
    window.location.assign(result.hosted_checkout_url)
  } catch (error) {
    checkoutFailure.value = error instanceof Error ? error.message : 'checkout_unavailable'
  } finally {
    checkoutLoading.value = false
  }
}
</script>

<template>
  <div>
    <PageHeader :title="title" eyebrow="Billing" :description="description">
      <button type="button" class="kp-button" :disabled="page.status.value === 'loading'" @click="page.reload">Refresh</button>
    </PageHeader>
    <div v-if="routeName === 'billing-payment-methods'" class="kp-notice" role="status"><strong>Live payment provider actions are disabled.</strong> This portal only shows non-sensitive references when a browser API is available.</div>
    <UnavailableState v-if="missingBrowserApi" :title="title" :dependency="props.route.dependency" />
    <LoadingState v-else-if="page.status.value === 'loading'" label="Loading billing information…" />
    <ForbiddenState v-else-if="page.status.value === 'forbidden'" reason="server" :request-id="page.failure.value?.requestId" :code="page.failure.value?.code" />
    <UnavailableState v-else-if="unavailable" title="Billing service unavailable" dependency="Billing data is not available for this organization yet. No payment provider action has been attempted." />
    <ErrorState v-else-if="page.failure.value" :failure="page.failure.value" @retry="page.reload" />
    <PanelCard v-else-if="page.status.value === 'empty'" title="No billing records" eyebrow="Nothing to show"><p>No records are available for this organization.</p></PanelCard>
    <div v-else-if="routeName === 'billing-overview' && overview" class="kp-stack">
      <div class="kp-notice" role="status"><strong>Billing capability:</strong> {{ overview.capabilities?.some(item => item.available) ? 'Available for read-only status and history.' : 'Currently disabled or unavailable. No provider is active.' }}</div>
      <section class="kp-grid" aria-label="Billing summary"><MetricCard label="Current plan" :value="overview.subscription?.plan || 'Not available'" source="live" /><MetricCard label="Outstanding balance" :value="money(overview.outstanding_balance, overview.currency)" source="live" /><MetricCard label="Wallet balance" :value="money(overview.wallet_balance, overview.currency)" source="live" /></section>
      <PanelCard title="Subscription status" eyebrow="Current plan"><p v-if="overview.subscription">{{ overview.subscription.product }} · {{ readable(overview.subscription.status) }} · {{ readable(overview.subscription.interval) }}</p><p v-else>No active subscription is reported.</p></PanelCard>
      <PanelCard title="Recent activity" eyebrow="Existing records"><p v-if="!overview.recent_payments?.length">No payments are recorded.</p><ul v-else class="kp-list"><li v-for="payment in overview.recent_payments" :key="payment.id">{{ payment.reference }} · {{ money(payment.amount, payment.currency) }} · {{ readable(payment.status) }}</li></ul></PanelCard>
    </div>
    <div v-else-if="routeName === 'billing-subscription'" class="kp-stack">
      <PanelCard title="Subscription" eyebrow="Read only"><dl v-if="subscription" class="kp-definition-list"><div><dt>Product and plan</dt><dd>{{ subscription.product }} · {{ subscription.plan }}</dd></div><div><dt>Status</dt><dd>{{ readable(subscription.status) }}</dd></div><div><dt>Billing interval</dt><dd>{{ readable(subscription.interval) }}</dd></div><div><dt>Price</dt><dd>{{ money(subscription.price, subscription.currency || undefined) }}</dd></div><div><dt>Renews</dt><dd>{{ date(subscription.renews_at) }}</dd></div><div><dt>Trial ends</dt><dd>{{ date(subscription.trial_end) }}</dd></div></dl><p v-else>No active subscription is reported.</p></PanelCard>
      <PanelCard v-if="subscription?.usage?.length" title="Usage summary" eyebrow="Reported by billing"><ul class="kp-list"><li v-for="entry in subscription.usage" :key="entry.label">{{ entry.label }}: {{ entry.used }}{{ entry.limit == null ? '' : ` / ${entry.limit}` }} {{ entry.unit || '' }}</li></ul></PanelCard>
    </div>
    <div v-else-if="routeName === 'billing-invoice' && invoice" class="kp-stack">
      <PanelCard title="Invoice details" eyebrow="Canonical record"><dl class="kp-definition-list"><div><dt>Reference</dt><dd>{{ invoice.reference }}</dd></div><div><dt>Status</dt><dd>{{ readable(invoice.status) }}</dd></div><div><dt>Issued</dt><dd>{{ date(invoice.issued_at) }}</dd></div><div><dt>Due</dt><dd>{{ date(invoice.due_at) }}</dd></div><div><dt>Total</dt><dd>{{ money(invoice.total, invoice.currency) }}</dd></div><div><dt>Amount due</dt><dd>{{ money(invoice.amount_due, invoice.currency) }}</dd></div></dl><div v-if="canManageBilling && checkoutEnabled && Number(invoice.amount_due || 0) > 0 && !invoice.active_checkout && !['VOID', 'CREDITED', 'PAID'].includes(invoice.status)" class="kp-inline-actions"><button type="button" class="kp-button" :disabled="checkoutLoading" @click="startCheckout">{{ checkoutLoading ? 'Opening checkout…' : 'Pay invoice' }}</button></div><p v-if="invoice.active_checkout" class="kp-notice" role="status">A hosted checkout is already in progress. Reload this invoice to see its current status.</p><p v-if="checkoutFailure" class="kp-notice" role="alert">{{ checkoutFailure }}</p><p v-if="['PAID', 'PARTIALLY_PAID'].includes(invoice.status)" class="kp-notice" role="status">{{ invoice.status === 'PAID' ? 'Payment confirmed.' : 'Payment partially received.' }}</p></PanelCard>
      <PanelCard title="Line items" eyebrow="Invoice contents"><DataTable caption="Invoice line items" :columns="[{ key: 'description', label: 'Description' }, { key: 'quantity', label: 'Quantity' }, { key: 'amount', label: 'Amount' }]" :rows="invoice.line_items" empty-message="No line items are reported."><template #cell-amount="{ row }">{{ money(Number(row.amount), String(row.currency || invoice.currency)) }}</template></DataTable></PanelCard>
      <PanelCard title="Billing documents" eyebrow="Canonical documents">
        <div class="kp-inline-actions">
          <a class="kp-button" :href="`/app/api/billing/invoices/${encodeURIComponent(invoice.id)}/document`" target="_blank" rel="noopener">View canonical invoice document</a>
          <a v-for="note in creditNotes" :key="note.id" class="kp-button" :href="`/app/api/billing/credit-notes/${encodeURIComponent(note.id)}/document`" target="_blank" rel="noopener">Credit note {{ note.number }}</a>
        </div>
        <p v-if="creditNotesUnavailable" class="kp-notice kp-notice--warning" role="status">Credit-note records are temporarily unavailable. The invoice document remains available.</p>
        <p v-else-if="!creditNotes.length" class="kp-muted">No credit notes are recorded for this invoice.</p>
      </PanelCard>
    </div>
    <div v-else-if="routeName === 'billing-wallet' && wallet" class="kp-stack"><MetricCard label="Wallet balance" :value="money(wallet.balance, wallet.currency)" source="live" /><PanelCard title="Wallet transactions" eyebrow="Existing records"><DataTable caption="Wallet transactions" :columns="[{ key: 'description', label: 'Description' }, { key: 'status', label: 'Status' }, { key: 'created_at', label: 'Date' }, { key: 'amount', label: 'Amount' }]" :rows="wallet.transactions" empty-message="No wallet transactions are reported."><template #cell-status="{ row }"><StatusBadge :value="String(row.status)" /></template><template #cell-created_at="{ row }">{{ date(String(row.created_at)) }}</template><template #cell-amount="{ row }">{{ money(Number(row.amount), String(row.currency || wallet.currency)) }}</template></DataTable></PanelCard></div>
    <div v-else class="kp-stack">
      <PanelCard :title="title" eyebrow="Existing records">
        <form v-if="routeName === 'billing-invoices'" class="kp-inline-actions" @submit.prevent="applyFilter"><label for="invoice-status">Status</label><select id="invoice-status" v-model="statusFilter" class="kp-input"><option value="">All statuses</option><option value="paid">Paid</option><option value="open">Outstanding</option><option value="past_due">Overdue</option></select><button type="submit" class="kp-button">Apply</button></form>
        <DataTable :caption="title" :columns="recordColumns" :rows="collection.items.map(displayRecord)" empty-message="No records are available."><template #cell-status="{ row }"><StatusBadge :value="String(row.status)" /></template></DataTable>
        <PaginationControls v-if="routeName === 'billing-invoices'" :offset="offset" :limit="limit" :count="collection.items.length" :has-more="Boolean(collection.has_more)" @previous="previous" @next="next" />
      </PanelCard>
      <PanelCard v-if="routeName === 'billing-payment-methods'" title="Provider actions" eyebrow="Disabled"><p>Adding, removing, or charging a payment method is not available in this portal.</p></PanelCard>
    </div>
  </div>
</template>
