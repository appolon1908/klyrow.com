import { expect, test, type Page } from '@playwright/test'

const session = {
  authenticated: true,
  identity_id: 'identity-owner',
  tenant_id: 'tenant-a',
  user_id: 'owner-a',
  email: 'owner@example.test',
  role: 'OWNER',
  capabilities: ['*'],
  csrf_token: 'csrf-webmail',
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

type StoredMessage = {
  id: string
  thread_id: string
  direction: string
  folder: string
  from: string
  to: string[]
  subject: string
  preview: string
  is_read: boolean
  is_starred: boolean
  delivery_status: string
  has_attachments: boolean
  occurred_at: string
  text: string
  html?: string
  attachments: Array<{ id: string; filename: string; content_type: string; size: number; download_url: string }>
  cc: string[]
  bcc: string[]
  references: string[]
  message_id?: string
  in_reply_to?: string
  reply_to_message_id?: string
}

function summary(item: StoredMessage) {
  const { text, html, attachments, cc, bcc, references, message_id, in_reply_to, reply_to_message_id, ...base } = item
  return { ...base, has_attachments: attachments.length > 0 }
}

async function stubWebmail(page: Page) {
  let draftId: string | undefined
  let sentBody: Record<string, unknown> | undefined
  let sendKey = ''
  let permanentlyDeleted = false
  const message: StoredMessage = {
    id: 'message-a',
    thread_id: 'thread-a',
    direction: 'INBOUND',
    folder: 'INBOX',
    from: 'sender@example.net',
    to: ['support@example.test'],
    subject: 'Controlled inbound',
    preview: 'Hello Klyrow inbox',
    is_read: false,
    is_starred: false,
    delivery_status: 'RECEIVED',
    has_attachments: true,
    occurred_at: '2026-09-21T12:00:00Z',
    text: '<script>alert(1)</script>',
    html: '<img src=x onerror=alert(1)>',
    attachments: [{
      id: 'attachment-a',
      filename: 'evidence.txt',
      content_type: 'text/plain',
      size: 128,
      download_url: '/app/api/mailboxes/mailbox-a/messages/message-a/attachments/attachment-a',
    }],
    cc: [],
    bcc: [],
    references: [],
    message_id: '<message-a@example.test>',
  }
  const sent: StoredMessage = {
    ...message,
    id: 'sent-a',
    direction: 'OUTBOUND',
    folder: 'SENT',
    from: 'support@example.test',
    to: ['sender@example.net'],
    subject: 'Re: Controlled inbound',
    preview: 'Thanks',
    is_read: true,
    delivery_status: 'ACCEPTED',
    has_attachments: false,
    text: 'Thanks',
    html: '<p>Thanks</p>',
    attachments: [],
    in_reply_to: '<message-a@example.test>',
    reply_to_message_id: 'message-a',
    references: ['<message-a@example.test>'],
  }

  await page.route('**/auth/session', route => route.fulfill({ json: session }))
  await page.route('**/app/api/mailboxes', route => route.fulfill({ json: [mailbox] }))
  await page.route('**/app/api/mailboxes/mailbox-a/messages?*', route => {
    const url = new URL(route.request().url())
    const folder = url.searchParams.get('folder') || 'INBOX'
    const items = [message, sent]
      .filter(item => item.folder === folder && !permanentlyDeleted)
      .map(summary)
    return route.fulfill({ json: { items } })
  })
  await page.route('**/app/api/mailboxes/mailbox-a/messages/message-a*', async route => {
    const method = route.request().method()
    if (method === 'PATCH') {
      const body = route.request().postDataJSON() as { folder?: string; is_read?: boolean; is_starred?: boolean }
      if (body.folder) message.folder = body.folder
      if (typeof body.is_read === 'boolean') message.is_read = body.is_read
      if (typeof body.is_starred === 'boolean') message.is_starred = body.is_starred
      return route.fulfill({ json: message })
    }
    if (method === 'DELETE') {
      const permanent = new URL(route.request().url()).searchParams.get('permanent') === 'true'
      if (permanent) permanentlyDeleted = true
      else message.folder = 'TRASH'
      return route.fulfill({ status: 204, body: '' })
    }
    return route.fulfill({ json: message })
  })
  await page.route('**/app/api/mailboxes/mailbox-a/drafts', async route => {
    const body = route.request().postDataJSON() as { to?: string; subject?: string; text?: string; reply_to_message_id?: string }
    draftId = 'draft-a'
    return route.fulfill({ status: 201, json: {
      ...sent,
      id: draftId,
      direction: 'DRAFT',
      folder: 'DRAFTS',
      to: body.to ? [body.to] : [],
      subject: body.subject || '',
      text: body.text || '',
      reply_to_message_id: body.reply_to_message_id,
    } })
  })
  await page.route('**/app/api/mailboxes/mailbox-a/send', async route => {
    sendKey = route.request().headers()['idempotency-key'] || ''
    sentBody = route.request().postDataJSON() as Record<string, unknown>
    return route.fulfill({ status: 202, json: { message: sent, delivery: { id: 'core-a', status: 'accepted' } } })
  })

  return {
    state: () => ({ message, sentBody, sendKey, draftId, permanentlyDeleted }),
  }
}

test('webmail browser lifecycle: read, safe render, reply, draft, send, archive and trash', async ({ page }) => {
  const fixture = await stubWebmail(page)
  await page.goto('/app/mail')

  await expect(page.getByText('support@example.test').first()).toBeVisible()
  await expect(page.getByText('Controlled inbound')).toBeVisible()
  await page.getByText('Controlled inbound').click()

  await expect(page.getByText('<script>alert(1)</script>')).toBeVisible()
  await expect(page.locator('.message-content script')).toHaveCount(0)
  await expect(page.locator('.message-content img[src="x"]')).toHaveCount(0)
  await expect(page.getByRole('link', { name: /evidence\.txt/i })).toHaveAttribute(
    'href',
    '/app/api/mailboxes/mailbox-a/messages/message-a/attachments/attachment-a',
  )

  await page.getByRole('button', { name: /reply/i }).click()
  await expect(page.getByRole('dialog')).toBeVisible()
  await expect(page.getByLabel('To')).toHaveValue('sender@example.net')
  await expect(page.getByLabel('Subject')).toHaveValue('Re: Controlled inbound')
  await page.locator('.compose-window textarea').fill('Thanks')
  await page.getByRole('button', { name: /save draft/i }).click()
  await expect(page.getByText('Draft saved')).toBeVisible()
  await page.getByRole('button', { name: /^send/i }).click()
  await expect(page.getByText('Message accepted for delivery')).toBeVisible()
  expect(fixture.state().draftId).toBe('draft-a')
  expect(fixture.state().sendKey).toContain('webmail-send')
  expect(fixture.state().sentBody).toMatchObject({
    to: 'sender@example.net',
    subject: 'Re: Controlled inbound',
    text: 'Thanks',
    draft_id: 'draft-a',
    reply_to_message_id: 'message-a',
  })

  await page.locator('.folder-nav button').filter({ hasText: 'Inbox' }).click()
  await page.getByText('Controlled inbound').click()
  await page.getByRole('button', { name: 'Archive', exact: true }).click()
  expect(fixture.state().message.folder).toBe('ARCHIVE')

  await page.locator('.folder-nav button').filter({ hasText: 'Archive' }).click()
  await page.getByText('Controlled inbound').click()
  await page.getByRole('button', { name: 'Trash', exact: true }).click()
  expect(fixture.state().message.folder).toBe('TRASH')

  await page.locator('.folder-nav button').filter({ hasText: 'Trash' }).click()
  await page.getByText('Controlled inbound').click()
  await page.getByRole('button', { name: /delete forever/i }).click()
  await expect.poll(() => fixture.state().permanentlyDeleted).toBe(true)

  await page.locator('.folder-nav button').filter({ hasText: 'Spam' }).click()
  await expect(page.getByText('No messages here')).toBeVisible()
})

test('webmail remains usable at a narrow mobile viewport', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await stubWebmail(page)
  await page.goto('/app/mail')
  await expect(page.getByRole('button', { name: /compose/i })).toBeVisible()
  await expect(page.getByText('Controlled inbound')).toBeVisible()
  await page.getByText('Controlled inbound').click()
  await expect(page.getByText('<script>alert(1)</script>')).toBeVisible()
})

test('webmail exposes a recoverable degraded state instead of a false empty inbox', async ({ page }) => {
  await page.route('**/auth/session', route => route.fulfill({ json: session }))
  await page.route('**/app/api/mailboxes', route => route.fulfill({ status: 503, json: { detail: 'mailbox_unavailable' } }))
  await page.goto('/app/mail')
  await expect(page.getByRole('alert')).toContainText('mailbox_unavailable')
  await expect(page.getByRole('button', { name: /try again/i })).toBeVisible()
})
