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

export interface BillingCatalogPlan { code: string; name: string; features: Record<string, unknown>; price_version: number; currency: string; billing_cycle: string; base_amount: number | string; included_units: number; overage_amount: number | string }
export interface BillingCatalog { items: BillingCatalogPlan[] }
export interface BillingProviderCapabilities { billing_enabled: boolean; checkout_enabled: boolean; stripe: { available: boolean; environment: string }; live_charging: boolean }
export interface BillingEntitlements { status: string; version: number; entitlements: Record<string, unknown> }
export interface BillingUsageBucket { period_start: string; quantity: number }
export interface BillingUsageHistory { granularity: 'day' | 'month'; unit: string; window_start: string; window_end: string; items: BillingUsageBucket[]; next_cursor: string | null }


export interface SupportTicketSummary { id: string; subject: string; category: string; priority: string; status: string; created_at: string; updated_at: string; last_message_at: string }
export interface SupportTicketMessage { id: string; author_kind: string; body: string; created_at: string }
export interface SupportTicketDetail extends SupportTicketSummary { messages: SupportTicketMessage[]; duplicate?: boolean }
export interface SupportTicketPage { items: SupportTicketSummary[]; limit: number; offset: number; has_more: boolean }
