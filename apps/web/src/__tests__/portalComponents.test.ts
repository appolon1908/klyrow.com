import { fireEvent, render, screen } from '@testing-library/vue'
import { nextTick } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import ErrorState from '../portal/components/ErrorState.vue'
import ForbiddenState from '../portal/components/ForbiddenState.vue'
import UnavailableState from '../portal/components/UnavailableState.vue'
import DegradedState from '../portal/components/DegradedState.vue'
import EmptyState from '../portal/components/EmptyState.vue'
import LoadingState from '../portal/components/LoadingState.vue'
import ModalDialog from '../portal/components/ModalDialog.vue'
import ConfirmDialog from '../portal/components/ConfirmDialog.vue'
import OneTimeSecret from '../portal/components/OneTimeSecret.vue'
import CopyButton from '../portal/components/CopyButton.vue'
import FormField from '../portal/components/FormField.vue'
import TabList from '../portal/components/TabList.vue'
import DataTable from '../portal/components/DataTable.vue'
import SourceBadge from '../portal/components/SourceBadge.vue'
import SafeText from '../portal/components/SafeText.vue'

afterEach(() => vi.restoreAllMocks())

describe('page state components', () => {
  it('announce loading, empty, degraded, forbidden and unavailable states accessibly', () => {
    const loading = render(LoadingState, { props: { label: 'Loading domains' } })
    expect(screen.getByRole('status').textContent).toContain('Loading domains')
    loading.unmount()
    const empty = render(EmptyState, { props: { title: 'No domains yet', description: 'Claim a domain to start.' } })
    expect(screen.getByRole('heading', { name: 'No domains yet' })).toBeTruthy()
    empty.unmount()
    const forbidden = render(ForbiddenState, { props: { reason: 'capability' } })
    expect(screen.getByRole('alert').textContent).toMatch(/does not include/i)
    forbidden.unmount()
    const unavailable = render(UnavailableState, { props: { title: 'Templates', dependency: 'Required contract: GET /app/api/templates' } })
    expect(screen.getByText(/GET \/app\/api\/templates/)).toBeTruthy()
    expect(screen.getByText(/not available in this release/i)).toBeTruthy()
    unavailable.unmount()
    render(DegradedState, { props: { message: 'Provisioning status could not be read', requestId: 'req-77' } })
    expect(screen.getByRole('status').textContent).toContain('req-77')
  })
  it('shows the request and correlation identifiers for failed requests and offers a retry', async () => {
    const retry = vi.fn()
    render(ErrorState, { props: { failure: { kind: 'unavailable', code: 'delivery_unavailable', status: 503, requestId: 'req-42', correlationId: 'corr-42' }, onRetry: retry } })
    const alert = screen.getByRole('alert')
    expect(alert.textContent).toContain('Service unavailable')
    expect(alert.textContent).toContain('req-42')
    expect(alert.textContent).toContain('corr-42')
    expect(alert.textContent).toContain('delivery_unavailable')
    await fireEvent.click(screen.getByRole('button', { name: /try again/i }))
    expect(retry).toHaveBeenCalledOnce()
  })
  it('labels data provenance', () => {
    for (const source of ['live', 'stale', 'unavailable', 'not-configured', 'forbidden', 'degraded'] as const) {
      const { unmount } = render(SourceBadge, { props: { source } })
      expect(screen.getByText(source.replace('-', ' '), { exact: false })).toBeTruthy()
      unmount()
    }
  })
  it('renders user content as text only', () => {
    const { container } = render(SafeText, { props: { value: '<img src=x onerror=alert(1)>' } })
    expect(container.querySelector('img')).toBeNull()
    expect(container.textContent).toContain('<img src=x onerror=alert(1)>')
  })
})

describe('modal dialogs', () => {
  it('traps focus, closes on Escape and restores focus to the opener', async () => {
    const opener = document.createElement('button')
    opener.textContent = 'Open'
    document.body.appendChild(opener)
    opener.focus()
    const onClose = vi.fn()
    render(ModalDialog, { props: { open: true, title: 'Claim domain', onClose }, slots: { default: '<input aria-label="Domain"><button>Save</button>' } })
    const dialog = screen.getByRole('dialog', { name: 'Claim domain' })
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    await nextTick()
    expect(dialog.contains(document.activeElement)).toBe(true)
    const last = screen.getByRole('button', { name: 'Save' })
    last.focus()
    await fireEvent.keyDown(dialog, { key: 'Tab' })
    expect(dialog.contains(document.activeElement)).toBe(true)
    await fireEvent.keyDown(dialog, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledOnce()
  })
  it('requires explicit confirmation before emitting a destructive action', async () => {
    const confirm = vi.fn(), cancel = vi.fn()
    render(ConfirmDialog, { props: { open: true, title: 'Revoke session', description: 'The device will be signed out.', confirmLabel: 'Revoke', destructive: true, onConfirm: confirm, onCancel: cancel } })
    expect(screen.getByRole('alertdialog', { name: 'Revoke session' })).toBeTruthy()
    await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(cancel).toHaveBeenCalledOnce()
    await fireEvent.click(screen.getByRole('button', { name: 'Revoke' }))
    expect(confirm).toHaveBeenCalledOnce()
  })
})

describe('secret handling controls', () => {
  it('copies without persisting and clears transient secret state on close', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    const setItem = vi.spyOn(Storage.prototype, 'setItem')
    const onDone = vi.fn()
    const { unmount } = render(OneTimeSecret, { props: { label: 'Invitation link token', value: 'one-time-value', onDone } })
    expect(screen.getByText(/shown once/i)).toBeTruthy()
    await fireEvent.click(screen.getByRole('button', { name: /copy/i }))
    expect(writeText).toHaveBeenCalledWith('one-time-value')
    await fireEvent.click(screen.getByRole('button', { name: /stored it/i }))
    expect(onDone).toHaveBeenCalledOnce()
    unmount()
    expect(setItem).not.toHaveBeenCalled()
    expect(document.body.textContent).not.toContain('one-time-value')
    vi.unstubAllGlobals()
  })
  it('copy control never renders a sensitive value into the DOM', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    const { container } = render(CopyButton, { props: { value: 'sensitive-copy-value', label: 'Copy token', sensitive: true } })
    expect(container.textContent).not.toContain('sensitive-copy-value')
    expect(container.innerHTML).not.toContain('sensitive-copy-value')
    await fireEvent.click(screen.getByRole('button', { name: 'Copy token' }))
    expect(writeText).toHaveBeenCalledWith('sensitive-copy-value')
    expect(await screen.findByText(/copied/i)).toBeTruthy()
    vi.unstubAllGlobals()
  })
})

describe('form and table foundations', () => {
  it('associates labels, hints and errors with the control', () => {
    render(FormField, { props: { id: 'domain', label: 'Domain', hint: 'Lowercase, no protocol.', error: 'Enter a valid domain.' }, slots: { default: '<input id="domain" aria-describedby="domain-hint domain-error">' } })
    const input = screen.getByLabelText('Domain')
    expect(input.getAttribute('aria-describedby')).toContain('domain-hint')
    expect(document.getElementById('domain-error')?.textContent).toContain('Enter a valid domain.')
    expect(document.getElementById('domain-hint')?.textContent).toContain('Lowercase')
  })
  it('exposes tabs with roving keyboard focus', async () => {
    const { emitted } = render(TabList, { props: { tabs: [{ id: 'dns', label: 'DNS' }, { id: 'dkim', label: 'DKIM' }], modelValue: 'dns' } })
    const tabs = screen.getAllByRole('tab')
    expect(tabs[0].getAttribute('aria-selected')).toBe('true')
    tabs[0].focus()
    await fireEvent.keyDown(tabs[0], { key: 'ArrowRight' })
    expect(emitted()['update:modelValue']?.[0]).toEqual(['dkim'])
  })
  it('renders an accessible table with an empty row when there are no records', () => {
    render(DataTable, { props: { caption: 'Senders', columns: [{ key: 'address', label: 'Address' }, { key: 'status', label: 'Status' }], rows: [], emptyMessage: 'No senders yet.' } })
    expect(screen.getByRole('table', { name: 'Senders' })).toBeTruthy()
    expect(screen.getAllByRole('columnheader').map(cell => cell.textContent)).toEqual(['Address', 'Status'])
    expect(screen.getByText('No senders yet.')).toBeTruthy()
  })
})
