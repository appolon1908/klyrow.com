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
