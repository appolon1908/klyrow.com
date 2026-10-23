export const paths = ['login','signup','verify-email','verification-expired','verification-success','forgot-password','reset-sent','reset-password','reset-expired','reset-success','invite','logged-out','service-error','account-disabled','terms','privacy'] as const
export type AuthView = typeof paths[number]

export function routeFromLocation(pathname: string, search = ''): AuthView {
  const key = pathname.replace(/^\/+|\/+$/g, '')
  if ((key === '' || key === 'login') && new URLSearchParams(search).get('logged_out') === '1') return 'logged-out'
  return paths.includes(key as AuthView) ? key as AuthView : 'login'
}

export function serverError(status: number, code = '') {
  if (status === 429) return 'rateLimited'
  if (status === 403 && /disabled|suspended/.test(code)) return 'disabledTitle'
  if (status >= 500) return 'unavailable'
  if (status === 409) return 'duplicate'
  return 'invalidCredentials'
}

export function passwordScore(value: string) {
  return [value.length >= 12, /[a-z]/.test(value), /[A-Z]/.test(value), /\d/.test(value), /[^\w\s]/.test(value)].filter(Boolean).length
}

export function maskEmail(value: string) {
  const [name, domain] = value.split('@')
  if (!domain) return 'your email address'
  return `${name.slice(0, 1)}${'•'.repeat(Math.max(2, Math.min(6, name.length - 1)))}@${domain}`
}
