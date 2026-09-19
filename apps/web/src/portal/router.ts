import { reactive } from 'vue'
import { hasInvalidPathCharacters } from '../session'
import { groupIndexPath, matchPortalRoute } from './routes'

/** Reactive view of the current portal location (history API, no external router). */
export const currentLocation = reactive({ path: '/app/overview', search: '', hash: '' })

const LEGACY_ROOTS = /^\/(app|admin|onboarding)(\/|$)/

function syncFromWindow(): void {
  currentLocation.path = location.pathname
  currentLocation.search = location.search
  currentLocation.hash = location.hash
}

function resolve(to: string): URL | null {
  if (!to || to.length > 2048 || !to.startsWith('/') || to.startsWith('//') || hasInvalidPathCharacters(to)) return null
  const target = new URL(to, location.origin)
  if (target.origin !== location.origin) return null
  if (target.pathname.startsWith('/app/api') || target.pathname.startsWith('/auth')) return null
  return target
}

/**
 * Navigate inside the portal with the history API, fall through to a full
 * navigation for legacy roots (`/app`, `/app/mail`, `/admin`, `/onboarding`),
 * and replace anything unsafe with the overview.
 */
export function navigate(to: string, replace = false): void {
  const target = resolve(to)
  if (!target) return navigate('/app/overview', replace)
  const index = groupIndexPath(target.pathname)
  const path = index || target.pathname
  if (matchPortalRoute(path)) {
    const href = path + target.search + target.hash
    if (replace) history.replaceState(null, '', href)
    else history.pushState(null, '', href)
    currentLocation.path = path
    currentLocation.search = target.search
    currentLocation.hash = target.hash
    return
  }
  if (LEGACY_ROOTS.test(target.pathname)) {
    location.assign(target.pathname + target.search + target.hash)
    return
  }
  navigate('/app/overview', replace)
}

/** Installs popstate tracking and same-origin link interception; returns a disposer. */
export function installRouter(root: HTMLElement | Document = document): () => void {
  syncFromWindow()
  const onPop = () => syncFromWindow()
  const onClick = (event: Event) => {
    const mouse = event as MouseEvent
    if (mouse.defaultPrevented || mouse.button !== 0 || mouse.metaKey || mouse.ctrlKey || mouse.shiftKey || mouse.altKey) return
    const anchor = (event.target as Element | null)?.closest?.('a[href]') as HTMLAnchorElement | null
    if (!anchor || anchor.target === '_blank' || anchor.hasAttribute('download') || anchor.dataset.external === 'true') return
    const href = anchor.getAttribute('href') || ''
    const target = resolve(href)
    if (!target) return
    const index = groupIndexPath(target.pathname)
    if (!matchPortalRoute(index || target.pathname)) return
    event.preventDefault()
    navigate(href)
  }
  addEventListener('popstate', onPop)
  root.addEventListener('click', onClick)
  return () => {
    removeEventListener('popstate', onPop)
    root.removeEventListener('click', onClick)
  }
}
