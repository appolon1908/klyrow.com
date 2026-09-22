// Response shapes of the existing browser (BFF) APIs consumed by the portal.
export type MetricKey = 'sent_24h' | 'messages_total' | 'quota' | 'delivered' | 'bounced' | 'delivery_rate' | 'contacts' | 'campaigns' | 'suppressions' | 'outbox_active' | 'outbox_failed'

export interface DashboardData {
  metrics: Record<MetricKey, number>
  domains: Array<{ id: string; domain: string; verified: boolean }>
  senders: Array<{ id: string; address: string; role: string }>
  recent_messages: MessageRow[]
  onboarding: OnboardingState | null
}

export interface OnboardingState { step: number; use_case?: string; checklist: Record<string, boolean>; completed: boolean }

export interface MessageRow { id: string; recipient: string; sender: string; subject: string; status: string; created_at: string }

export interface ProvisioningStatus {
  tenant_id: string
  state: string
  provider_mode?: string | null
  organization?: string | null
  server?: string | null
  credential_fingerprint?: string | null
  last_error?: string | null
  attempts: number
  updated_at?: string | null
}

export interface DomainClaim {
  id: string
  tenant_id?: string
  domain: string
  state: string
  dkim_selector?: string
  dkim_version?: number
  return_path?: string
  tracking_domain?: string
  verified_at?: string | null
  suspended_at?: string | null
  created_at?: string
}

export interface DeliverabilityEvidence {
  source: 'durable_snapshot' | 'none'
  checked_at: string | null
  spf: boolean | null
  dkim: boolean | null
  dmarc: boolean | null
  mx: boolean | null
  ptr: boolean | null
  tls: boolean | null
  alerts: Array<{ severity?: string; code?: string }>
  stale: boolean
}

export interface DomainDetail {
  id: string
  domain: string
  state: string
  verified_at?: string | null
  suspended_at?: string | null
  created_at?: string
  dkim: {
    selector?: string | null
    version: number
    history: Array<{ selector: string; version: number; active: boolean; created_at: string; retired_at?: string | null }>
  }
  dns: { return_path?: string | null; tracking_domain?: string | null }
  deliverability: DeliverabilityEvidence
  provider_readiness: { sending_enabled: boolean; inbound_enabled: boolean; status: string }
  history?: DeliverabilityEvidence[]
}

export interface DeliverabilityRow extends DeliverabilityEvidence {
  id: string
  domain: string
  state: string
  verified_at?: string | null
  suspended_at?: string | null
  alert_count: number
  sending_enabled: boolean
  inbound_enabled: boolean
  provider_status: string
}

export interface DeliverabilityResponse {
  items: DeliverabilityRow[]
  limit: number
  offset: number
  has_more: boolean
}

export interface MessageEvidenceEntry {
  id: string
  kind: string
  status: string
  source: string
  occurred_at: string
}

export interface MessageDetail extends MessageRow {
  current_outcome: string
  correlation_id?: string | null
  operation_id?: string | null
  outbox?: {
    state: string
    attempts: number
    created_at: string
    updated_at: string
    next_attempt_at?: string | null
    provider_reference_present: boolean
  } | null
  provider?: {
    status: string
    attempts: number
    sandbox: boolean
    provider_reference_present: boolean
    updated_at: string
  } | null
  timeline: MessageEvidenceEntry[]
}

export interface DomainClaimCreated {
  id: string
  state: string
  dns: {
    ownership: { type: string; name: string; value: string }
    spf?: { type: string; name: string; recommended: string }
    dkim?: { selector: string }
    dmarc?: { type: string; name: string; recommended?: string; value?: string }
    return_path?: unknown
    tracking?: unknown
  }
}

export interface SenderIdentity {
  id: string
  domain_claim_id: string
  address: string
  display_name: string
  reply_to?: string | null
  stream: string
  status: string
  verified: boolean
}

export interface Mailbox {
  id: string
  address: string
  domain: string
  display_name: string
  sending_enabled: boolean
  receiving_enabled: boolean
  counts: Record<string, number>
  is_shared?: boolean
  grant_count?: number
  my_access_role?: string | null
  unread_count?: number
}

export interface TeamMember { user_id: string; email: string | null; role: string; created_at: string }

export interface InvitationCreated { id: string; email: string; role: string; expires_at: string; development_token?: string }

export interface BrowserContext {
  sub: string
  identity_id: string
  tenant: string
  role: string
  sid: string
  profile: { email: string; email_verified: boolean; display_name?: string | null; locale?: string | null } | null
  organizations: Array<{ tenant_id: string; organization_id: string; name: string; slug: string; role: string; enabled: boolean }>
}

export interface BrowserSessionRow {
  id: string
  current: boolean
  identity_id: string
  created_at: string
  last_seen_at?: string | null
  expires_at: string
  revoked_at?: string | null
  user_agent_hash?: string | null
  ip_hash?: string | null
}

export interface BillingCapability { key: string; available: boolean; reason?: string | null }
export interface BillingSubscription { product: string; plan: string; status: string; interval: string; trial_end?: string | null; price?: number | null; currency?: string | null; renews_at?: string | null; cancels_at?: string | null; usage?: Array<{ label: string; used: number; limit?: number | null; unit?: string }> }
export interface BillingInvoice { id: string; reference: string; status: string; issued_at: string; due_at?: string | null; total: number; currency: string; amount_due?: number | null; amount_paid?: number | null }
export interface BillingInvoiceDetail extends BillingInvoice { billing_identity?: { name?: string | null; email?: string | null; address?: string | null } | null; line_items: Array<{ description: string; quantity: number; unit_amount: number; amount: number; currency: string }>; active_checkout?: boolean }
export interface BillingPayment { id: string; reference: string; status: string; created_at: string; amount: number; currency: string; invoice_reference?: string | null }
export interface BillingPaymentMethod { id: string; type: string; display: string; brand?: string | null; last4?: string | null; expires_at?: string | null; status: string }
export interface BillingRefund { id: string; reference: string; status: string; created_at: string; amount: number; currency: string; payment_reference?: string | null }
export interface BillingWalletTransaction { id: string; type: string; status: string; created_at: string; amount: number; currency: string; description?: string | null }
export interface BillingWallet { balance: number; currency: string; transactions: BillingWalletTransaction[] }
export interface BillingOverview { subscription: BillingSubscription | null; outstanding_balance: number; currency: string; wallet_balance: number; most_recent_invoice: BillingInvoice | null; recent_payments: BillingPayment[]; capabilities: BillingCapability[] }
