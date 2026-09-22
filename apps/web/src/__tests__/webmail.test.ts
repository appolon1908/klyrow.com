import { fireEvent, render, screen, waitFor } from '@testing-library/vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  appApi: vi.fn(),
  requireSession: vi.fn(),
  idempotencyKey: vi.fn(() => 'webmail-unit-key'),
}))
vi.mock('../api', async importOriginal => ({
  ...(await importOriginal<typeof import('../api')>()),
  ...api,
}))

import Webmail from '../Webmail.vue'

const session = {
  authenticated: true,
  identity_id: 'identity-owner',
  tenant_id: 'tenant-a',
  user_id: 'owner-a',
  email: 'owner@example.test',
  role: 'OWNER',
  capabilities: ['*'],
  csrf_token: 'csrf',
  expires_at: '2099-01-01T00:00:00Z',
}
const mailbox = {
  id: 'mailbox-a',
  address: 'support@example.test',
  domain: 'example.test',
  display_name: 'Support',
  sending_enabled: true,
  receiving_enabled: true,
  counts: { INBOX: 1, UNREAD: 1, SENT: 0, DRAFTS: 0, ARCHIVE: 0, SPAM: 0, TRASH: 0 },
}
const summary = {
  id: 'message-a',
  thread_id: 'thread-a',
  direction: 'INBOUND',
  folder: 'INBOX',
  from: 'sender@example.net',
  to: ['support@example.test'],
  subject: 'Security alert',
  preview: 'Plain text preview',
  is_read: false,
  is_starred: false,
  delivery_status: 'RECEIVED',
  has_attachments: true,
  occurred_at: '2026-09-21T12:00:00Z',
}
const detail = {
  ...summary,
  cc: [],
  bcc: [],
  reply_to: null,
  text: '<script>alert(1)</script>',
  html: '<img src=x onerror=alert(1)>',
  attachments: [{
    id: 'attachment-a',
    filename: 'evidence.txt',
    content_type: 'text/plain',
    size: 128,
    download_url: '/app/api/mailboxes/mailbox-a/messages/message-a/attachments/attachment-a',
  }],
  message_id: '<message-a@example.test>',
  in_reply_to: null,
  reply_to_message_id: null,
  references: [],
}

beforeEach(() => {
  api.appApi.mockReset()
  api.requireSession.mockReset()
  api.idempotencyKey.mockClear()
  api.requireSession.mockResolvedValue(session)
})

describe('Webmail client certification', () => {
  it('renders only plain text for untrusted message HTML and exposes guarded attachment downloads', async () => {
    api.appApi.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === '/app/api/mailboxes') return [mailbox]
      if (url.startsWith('/app/api/mailboxes/mailbox-a/messages?')) return { items: [summary] }
      if (url === '/app/api/mailboxes/mailbox-a/messages/message-a' && init?.method === 'PATCH') {
        return { ...detail, is_read: true }
      }
      if (url === '/app/api/mailboxes/mailbox-a/messages/message-a') return detail
      throw new Error('unexpected ' + url)
    })

    const { container } = render(Webmail)
    expect(await screen.findByText('Security alert')).toBeTruthy()
    const row = screen.getByText('Security alert').closest('button')
    expect(row).toBeTruthy()
    await fireEvent.click(row!)

    expect(await screen.findByText('<script>alert(1)</script>')).toBeTruthy()
    expect(container.querySelector('script')).toBeNull()
    expect(container.querySelector('img[src="x"]')).toBeNull()

    const attachment = screen.getByRole('link', { name: /evidence\.txt/i })
    expect(attachment.getAttribute('href')).toBe(
      '/app/api/mailboxes/mailbox-a/messages/message-a/attachments/attachment-a',
    )
    await waitFor(() => expect(api.appApi).toHaveBeenCalledWith(
      '/app/api/mailboxes/mailbox-a/messages/message-a',
      expect.objectContaining({ method: 'PATCH' }),
    ))
  })

  it('creates a draft and sends it with a stable idempotency key through the mailbox API', async () => {
    const draft = {
      ...detail,
      id: 'draft-a',
      direction: 'DRAFT',
      folder: 'DRAFTS',
      from: 'support@example.test',
      to: ['person@example.net'],
      subject: 'Draft subject',
      text: 'Draft body',
      attachments: [],
      has_attachments: false,
      is_read: true,
    }
    api.appApi.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === '/app/api/mailboxes') return [mailbox]
      if (url.startsWith('/app/api/mailboxes/mailbox-a/messages?')) return { items: [] }
      if (url === '/app/api/mailboxes/mailbox-a/drafts' && init?.method === 'POST') return draft
      if (url === '/app/api/mailboxes/mailbox-a/send' && init?.method === 'POST') {
        return { message: { ...draft, folder: 'SENT', direction: 'OUTBOUND' }, delivery: { id: 'core-a', status: 'accepted' } }
      }
      throw new Error('unexpected ' + url + ' ' + (init?.method || 'GET'))
    })

    const { container } = render(Webmail)
    await screen.findByRole('button', { name: /Inbox/ })
    await fireEvent.click(screen.getByRole('button', { name: /compose/i }))

    await fireEvent.update(screen.getByLabelText('To'), 'person@example.net')
    await fireEvent.update(screen.getByLabelText('Subject'), 'Draft subject')
    const body = container.querySelector('.compose-window textarea') as HTMLTextAreaElement
    expect(body).toBeTruthy()
    await fireEvent.update(body, 'Draft body')

    await fireEvent.click(screen.getByRole('button', { name: /save draft/i }))
    await waitFor(() => expect(api.appApi).toHaveBeenCalledWith(
      '/app/api/mailboxes/mailbox-a/drafts',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          to: 'person@example.net',
          subject: 'Draft subject',
          text: 'Draft body',
        }),
      }),
    ))
    expect(await screen.findByText('Draft saved')).toBeTruthy()

    await fireEvent.click(screen.getByRole('button', { name: /^send/i }))
    await waitFor(() => expect(api.appApi).toHaveBeenCalledWith(
      '/app/api/mailboxes/mailbox-a/send',
      expect.objectContaining({
        method: 'POST',
        headers: { 'Idempotency-Key': 'webmail-unit-key' },
      }),
    ))
    const sendCall = api.appApi.mock.calls.find(call => call[0] === '/app/api/mailboxes/mailbox-a/send')!
    expect(JSON.parse(String(sendCall[1].body))).toEqual({
      to: 'person@example.net',
      subject: 'Draft subject',
      text: 'Draft body',
      draft_id: 'draft-a',
    })
    expect(await screen.findByText('Message accepted for delivery')).toBeTruthy()
  })

  it('lets tenant managers grant shared mailbox access to workspace members', async () => {
    const sharedMailbox = { ...mailbox, is_shared: true, grant_count: 1, my_access_role: 'OWNER', unread_count: 1 }
    api.appApi.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === '/app/api/mailboxes') return [sharedMailbox]
      if (url.startsWith('/app/api/mailboxes/mailbox-a/messages?')) return { items: [] }
      if (url === '/app/api/mailboxes/mailbox-a/access' && !init?.method) {
        return [{ id: 'grant-reader', user_id: 'dev-a', role: 'READER', created_at: '2026-09-21T12:00:00Z' }]
      }
      if (url === '/app/api/team') {
        return [
          { user_id: 'dev-a', email: 'dev@example.test', role: 'DEVELOPER' },
          { user_id: 'sender-a', email: 'sender@example.test', role: 'DEVELOPER' },
        ]
      }
      if (url === '/app/api/mailboxes/mailbox-a/access' && init?.method === 'POST') {
        return { id: 'grant-sender', user_id: 'sender-a', role: 'SENDER' }
      }
      throw new Error('unexpected ' + url + ' ' + (init?.method || 'GET'))
    })

    render(Webmail)
    await screen.findByRole('button', { name: /Inbox/ })
    expect(screen.getByText(/1 shared/i)).toBeTruthy()
    await fireEvent.click(screen.getByRole('button', { name: /manage mailbox access/i }))

    expect(await screen.findByText('dev@example.test')).toBeTruthy()
    await fireEvent.update(screen.getByLabelText('Workspace member'), 'sender-a')
    await fireEvent.update(screen.getByLabelText('Mailbox role'), 'SENDER')
    await fireEvent.click(screen.getByRole('button', { name: /grant \/ update access/i }))

    await waitFor(() => expect(api.appApi).toHaveBeenCalledWith(
      '/app/api/mailboxes/mailbox-a/access',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ user_id: 'sender-a', role: 'SENDER' }),
      }),
    ))
    expect(await screen.findByText('Mailbox access updated')).toBeTruthy()
  })

  it('shows a recoverable full-page error when mailbox loading is unavailable', async () => {
    api.appApi.mockRejectedValueOnce(new Error('mailbox_unavailable'))
    render(Webmail)
    expect((await screen.findByRole('alert')).textContent).toContain('mailbox_unavailable')
    expect(screen.getByRole('button', { name: /try again/i })).toBeTruthy()
  })
})
