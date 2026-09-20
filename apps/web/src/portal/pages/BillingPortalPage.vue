<script setup lang="ts">
import { computed, ref } from 'vue'
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
      <PanelCard title="Invoice details" eyebrow="Canonical record"><dl class="kp-definition-list"><div><dt>Reference</dt><dd>{{ invoice.reference }}</dd></div><div><dt>Status</dt><dd>{{ readable(invoice.status) }}</dd></div><div><dt>Issued</dt><dd>{{ date(invoice.issued_at) }}</dd></div><div><dt>Due</dt><dd>{{ date(invoice.due_at) }}</dd></div><div><dt>Total</dt><dd>{{ money(invoice.total, invoice.currency) }}</dd></div></dl></PanelCard>
      <PanelCard title="Line items" eyebrow="Invoice contents"><DataTable caption="Invoice line items" :columns="[{ key: 'description', label: 'Description' }, { key: 'quantity', label: 'Quantity' }, { key: 'amount', label: 'Amount' }]" :rows="invoice.line_items" empty-message="No line items are reported."><template #cell-amount="{ row }">{{ money(Number(row.amount), String(row.currency || invoice.currency)) }}</template></DataTable></PanelCard>
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
