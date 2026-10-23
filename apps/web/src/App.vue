<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { maskEmail, passwordScore, routeFromLocation, serverError, type AuthView } from './auth'
import { AuthActionError, requestAuthAction, safeAuthRedirect } from './authActions'
import { safeReturnPath } from './session'
import { messages, type Locale, type MessageKey } from './i18n'

const locale = ref<Locale>((new URLSearchParams(location.search).get('lang') === 'es' || navigator.language.startsWith('es')) ? 'es' : 'en')
const view = ref<AuthView>(routeFromLocation(location.pathname, location.search))
const busy = ref(false), showPassword = ref(false), showConfirm = ref(false), notice = ref(''), formError = ref('')
const email = ref(new URLSearchParams(location.search).get('email') || ''), first = ref(''), last = ref(''), password = ref(''), confirm = ref(''), terms = ref(false), invite = ref('')
const errors = ref<Record<string,string>>({})
const heading = ref<HTMLElement>()
const t = (key: MessageKey) => messages[locale.value][key]
const score = computed(() => passwordScore(password.value))
const strength = computed(() => score.value < 3 ? t('weak') : score.value < 5 ? t('fair') : t('strong'))
const requestedReturnTo = new URLSearchParams(location.search).get('return_to')
const returnTo = computed(() => safeReturnPath(requestedReturnTo, view.value === 'signup' ? '/onboarding' : '/app'))
const initiation = computed(() => `${view.value === 'signup' ? '/auth/signup' : '/auth/login'}?return_to=${encodeURIComponent(returnTo.value)}`)
const isLegal = computed(() => view.value === 'terms' || view.value === 'privacy')
const legalSections = computed(() => {
  const english = {
    terms: [
      ['Authorized use', 'Use Klyrow only for lawful, authorized email and workspace operations. Do not use the service to send abusive, deceptive, unsolicited, or prohibited content.'],
      ['Accounts and security', 'Keep access to your workspace secure and use only accounts, domains, senders, and credentials you are authorized to control. Klyrow identity is brokered through the approved authentication system.'],
      ['Sending controls', 'Consent, suppression, domain verification, quotas, and provider safety gates may restrict sending. Features can remain disabled until operational checks are satisfied.'],
      ['Service operation', 'Klyrow may update or temporarily limit features to protect customers, infrastructure, deliverability, and security.']
    ],
    privacy: [
      ['Data processed', 'Klyrow processes account and workspace information, configuration data, email-operation metadata, delivery events, and security/audit records needed to operate the service.'],
      ['How data is used', 'Data is used to provide workspace features, authenticate users, route authorized operations, troubleshoot delivery, enforce safety controls, and maintain auditability.'],
      ['Service providers', 'Infrastructure and delivery providers receive only the information required for the services they perform, subject to the configured Klyrow integration and provider controls.'],
      ['Security and retention', 'Klyrow applies access controls, audit records, least-privilege service roles, and retention controls appropriate to operational and security requirements.']
    ]
  }
  const spanish = {
    terms: [
      ['Uso autorizado', 'Usa Klyrow únicamente para operaciones legales y autorizadas de correo y espacios de trabajo. No uses el servicio para contenido abusivo, engañoso, no solicitado o prohibido.'],
      ['Cuentas y seguridad', 'Mantén seguro el acceso a tu espacio y utiliza solo cuentas, dominios, remitentes y credenciales que estés autorizado a controlar.'],
      ['Controles de envío', 'El consentimiento, las supresiones, la verificación de dominios, las cuotas y las compuertas de seguridad pueden limitar los envíos.'],
      ['Operación del servicio', 'Klyrow puede actualizar o limitar temporalmente funciones para proteger a los clientes, la infraestructura, la entregabilidad y la seguridad.']
    ],
    privacy: [
      ['Datos procesados', 'Klyrow procesa información de cuenta y espacio de trabajo, configuración, metadatos de operaciones de correo, eventos de entrega y registros de seguridad y auditoría necesarios para operar el servicio.'],
      ['Uso de los datos', 'Los datos se usan para ofrecer funciones, autenticar usuarios, enrutar operaciones autorizadas, resolver problemas de entrega y aplicar controles de seguridad.'],
      ['Proveedores de servicio', 'Los proveedores de infraestructura y entrega reciben únicamente la información necesaria para prestar los servicios configurados.'],
      ['Seguridad y retención', 'Klyrow aplica controles de acceso, auditoría, roles de mínimo privilegio y controles de retención según las necesidades operativas y de seguridad.']
    ]
  }
  const copy = locale.value === 'es' ? spanish : english
  return view.value === 'terms' ? copy.terms : copy.privacy
})

const content: Partial<Record<AuthView, [MessageKey, MessageKey]>> = {
  'verify-email':['verifyTitle','verifyBody'], 'verification-expired':['expiredVerifyTitle','expiredVerifyBody'], 'verification-success':['verifiedTitle','verifiedBody'],
  'forgot-password':['forgotTitle','forgotBody'], 'reset-sent':['sentTitle','sentBody'], 'reset-password':['resetTitle','resetBody'], 'reset-expired':['resetExpiredTitle','resetExpiredBody'],
  'reset-success':['resetSuccessTitle','resetSuccessBody'], invite:['inviteTitle','inviteBody'], 'logged-out':['loggedOutTitle','loggedOutBody'], 'service-error':['serviceTitle','serviceBody'], 'account-disabled':['disabledTitle','disabledBody'],
  terms:['termsPageTitle','termsPageBody'], privacy:['privacyPageTitle','privacyPageBody']
}
const title = computed(() => view.value === 'login' ? t('loginTitle') : view.value === 'signup' ? t('signupTitle') : t(content[view.value]![0]))
const body = computed(() => {
  if (view.value === 'login') return t('loginBody')
  if (view.value === 'signup') return t('signupBody')
  const text = t(content[view.value]![1])
  return view.value === 'verify-email' ? text.replace('{email}', maskEmail(email.value)) : text
})

function go(next: AuthView) {
  const query = new URLSearchParams()
  if (requestedReturnTo) query.set('return_to', returnTo.value)
  if (locale.value === 'es') query.set('lang', 'es')
  history.pushState({}, '', `/${next}${query.size ? '?' + query : ''}`); view.value = next; clearState()
}
function clearState() { errors.value = {}; formError.value = ''; notice.value = ''; busy.value = false }
function setLocale(next: Locale) { locale.value = next; document.documentElement.lang = next }
function validateEmail() { return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.value) }
function validate(kind: 'login'|'signup'|'forgot'|'reset'|'invite') {
  errors.value = {}
  if (kind !== 'reset' && kind !== 'invite' && !email.value) errors.value.email = t('required')
  else if (kind !== 'reset' && kind !== 'invite' && !validateEmail()) errors.value.email = t('invalidEmail')
  if (kind === 'login' && !password.value) errors.value.password = t('required')
  if (kind === 'signup') {
    if (!first.value) errors.value.first = t('required'); if (!last.value) errors.value.last = t('required')
    if (score.value < 5) errors.value.password = t('weakPassword'); if (password.value !== confirm.value) errors.value.confirm = t('mismatch')
    if (!terms.value) errors.value.terms = t('acceptTerms')
  }
  if (kind === 'invite' && !invite.value.trim()) errors.value.invite = t('required')
  if (Object.keys(errors.value).length) { nextTick(() => document.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus()); return false }
  return true
}
async function executeAction(path: string, payload: Record<string, unknown> = {}) {
  busy.value = true
  formError.value = ''
  notice.value = ''
  try {
    const result = await requestAuthAction(path, payload)
    if (!result.redirect_to) throw new AuthActionError(503, 'authentication_redirect_missing')
    location.assign(safeAuthRedirect(result.redirect_to))
  } catch (error) {
    const status = error instanceof AuthActionError ? error.status : 503
    formError.value = t(serverError(status) as MessageKey)
    busy.value = false
  }
}
async function submit(kind: 'login'|'signup'|'forgot'|'reset'|'invite') {
  if (!validate(kind)) return
  if (kind === 'forgot') return executeAction('/auth/actions/recover', { email: email.value })
  if (kind === 'reset') return executeAction('/auth/actions/update-password')
  if (kind === 'invite') {
    busy.value = true
    formError.value = ''
    notice.value = ''
    try {
      const result = await requestAuthAction('/auth/actions/invitation', { token: invite.value.trim() })
      if (!result.valid || !result.redirect_to) {
        notice.value = t('inviteInvalid')
        busy.value = false
        return
      }
      notice.value = t('inviteValid')
      location.assign(safeAuthRedirect(result.redirect_to))
    } catch (error) {
      const status = error instanceof AuthActionError ? error.status : 503
      formError.value = t(serverError(status) as MessageKey)
      busy.value = false
    }
    return
  }
  // Identity credentials are submitted only to Keycloak through same-origin initiation endpoints.
  busy.value = true
  location.assign(`${kind === 'signup' ? '/auth/signup' : '/auth/login'}?return_to=${encodeURIComponent(returnTo.value)}`)
}
function resend() { return executeAction('/auth/actions/verify-email') }
function google() { location.assign(`/auth/google?return_to=${encodeURIComponent(returnTo.value)}`) }
watch(view, async () => { await nextTick(); heading.value?.focus() })
onMounted(() => { document.documentElement.lang = locale.value; addEventListener('popstate', () => view.value = routeFromLocation(location.pathname, location.search)) })
</script>

<template>
  <a class="skip-link" href="#auth-main">{{ t('skip') }}</a>
  <div class="auth-shell">
    <aside class="brand-panel" aria-label="Klyrow">
      <a class="brand" href="/login" aria-label="Klyrow home"><span class="brand-mark" aria-hidden="true">K</span><span>Klyrow</span></a>
      <div class="brand-copy"><p class="eyebrow">EMAIL OPERATIONS</p><h2>{{ t('value') }}</h2><ul><li><span aria-hidden="true">✓</span>{{ t('benefit1') }}</li><li><span aria-hidden="true">✓</span>{{ t('benefit2') }}</li><li><span aria-hidden="true">✓</span>{{ t('benefit3') }}</li></ul></div>
      <p class="security-note"><span aria-hidden="true">◇</span>{{ t('security') }}</p>
    </aside>
    <main id="auth-main" class="auth-main">
      <header class="mobile-header"><a class="brand" href="/login"><span class="brand-mark" aria-hidden="true">K</span><span>Klyrow</span></a><p>{{ t('value') }}</p></header>
      <div class="language"><label for="language">{{ t('language') }}</label><select id="language" :value="locale" @change="setLocale(($event.target as HTMLSelectElement).value as Locale)"><option value="en">{{ t('english') }}</option><option value="es">{{ t('spanish') }}</option></select></div>
      <section class="auth-card" :class="{ 'legal-card': isLegal }" :aria-busy="busy">
        <div class="state-icon" aria-hidden="true" v-if="view !== 'login' && view !== 'signup'">{{ ['verification-success','reset-success','logged-out'].includes(view) ? '✓' : ['service-error','account-disabled'].includes(view) ? '!' : '✦' }}</div>
        <h1 ref="heading" tabindex="-1">{{ title }}</h1><p class="intro">{{ body }}</p>
        <p v-if="formError" class="alert error" role="alert"><strong>{{ t('errorIcon') }}</strong> {{ formError }}</p>
        <p v-if="notice" class="alert status" role="status"><strong>{{ t('statusIcon') }}</strong> {{ notice }}</p>

        <template v-if="view === 'login' || view === 'signup'">
          <button class="button google" type="button" :disabled="busy" @click="google"><span class="google-g" aria-hidden="true">G</span>{{ t('google') }}</button>
          <div class="divider"><span>{{ t(view === 'signup' ? 'signupDivider' : 'divider') }}</span></div>
          <form novalidate @submit.prevent="submit(view === 'signup' ? 'signup' : 'login')">
            <div v-if="view === 'signup'" class="name-grid"><div class="field"><label for="first">{{ t('first') }}</label><input id="first" v-model="first" autocomplete="given-name" :aria-invalid="!!errors.first" aria-describedby="first-error"><p id="first-error" class="field-error">{{ errors.first }}</p></div><div class="field"><label for="last">{{ t('last') }}</label><input id="last" v-model="last" autocomplete="family-name" :aria-invalid="!!errors.last" aria-describedby="last-error"><p id="last-error" class="field-error">{{ errors.last }}</p></div></div>
            <div class="field"><label for="email">{{ t(view === 'signup' ? 'workEmail' : 'email') }}</label><input id="email" v-model="email" type="email" inputmode="email" autocomplete="email" :aria-invalid="!!errors.email" aria-describedby="email-error"><p id="email-error" class="field-error">{{ errors.email }}</p></div>
            <div class="field"><label for="password">{{ t('password') }}</label><div class="password-wrap"><input id="password" v-model="password" :type="showPassword ? 'text' : 'password'" :autocomplete="view === 'signup' ? 'new-password' : 'current-password'" :aria-invalid="!!errors.password" :aria-describedby="view === 'signup' ? 'password-guide password-strength password-error' : 'password-error'"><button type="button" class="visibility" :aria-pressed="showPassword" :aria-label="showPassword ? t('hide') : t('show')" @click="showPassword=!showPassword">{{ showPassword ? t('hide') : t('show') }}</button></div><p v-if="view === 'signup'" id="password-guide" class="hint">{{ t('passwordGuide') }}</p><p v-if="view === 'signup'" id="password-strength" class="strength" aria-live="polite">{{ t('strength') }}: <strong>{{ strength }}</strong></p><p id="password-error" class="field-error">{{ errors.password }}</p></div>
            <div v-if="view === 'signup'" class="field"><label for="confirm">{{ t('confirm') }}</label><div class="password-wrap"><input id="confirm" v-model="confirm" :type="showConfirm ? 'text' : 'password'" autocomplete="new-password" :aria-invalid="!!errors.confirm" aria-describedby="confirm-error"><button type="button" class="visibility" :aria-pressed="showConfirm" :aria-label="showConfirm ? t('hide') : t('show')" @click="showConfirm=!showConfirm">{{ showConfirm ? t('hide') : t('show') }}</button></div><p id="confirm-error" class="field-error">{{ errors.confirm }}</p></div>
            <div v-if="view === 'login'" class="form-row"><label class="check"><input type="checkbox"><span>{{ t('keep') }}</span></label><a href="/forgot-password" @click.prevent="go('forgot-password')">{{ t('forgot') }}</a></div>
            <div v-else class="field"><label class="check"><input v-model="terms" type="checkbox" :aria-invalid="!!errors.terms" aria-describedby="terms-error"><span>{{ t('termsPrefix') }} <a href="/terms">{{ t('terms') }}</a> {{ t('conjunction') }} <a href="/privacy">{{ t('privacy') }}</a>.</span></label><p id="terms-error" class="field-error">{{ errors.terms }}</p></div>
            <button class="button primary" :disabled="busy" type="submit"><span v-if="busy" class="spinner" aria-hidden="true"></span>{{ busy ? t('loading') : t(view === 'signup' ? 'create' : 'signIn') }}</button>
          </form>
          <p class="switch">{{ t(view === 'signup' ? 'existing' : 'newUser') }} <a :href="view === 'signup' ? '/login' : '/signup'" @click.prevent="go(view === 'signup' ? 'login' : 'signup')">{{ t(view === 'signup' ? 'signInLink' : 'createLink') }}</a></p>
        </template>

        <form v-else-if="view === 'forgot-password'" novalidate @submit.prevent="submit('forgot')"><div class="field"><label for="recovery-email">{{ t('email') }}</label><input id="recovery-email" v-model="email" type="email" autocomplete="email" :aria-invalid="!!errors.email" aria-describedby="recovery-error"><p id="recovery-error" class="field-error">{{ errors.email }}</p></div><button class="button primary" :disabled="busy" type="submit"><span v-if="busy" class="spinner" aria-hidden="true"></span>{{ busy ? t('loading') : t('sendReset') }}</button></form>
        <form v-else-if="view === 'reset-password'" novalidate @submit.prevent="submit('reset')"><p class="hint">{{ t('security') }}</p><button class="button primary" :disabled="busy" type="submit"><span v-if="busy" class="spinner" aria-hidden="true"></span>{{ busy ? t('loading') : t('updatePassword') }}</button></form>
        <form v-else-if="view === 'invite'" novalidate @submit.prevent="submit('invite')"><div class="field"><label for="invite">{{ t('inviteCode') }}</label><input id="invite" v-model="invite" autocomplete="off" :aria-invalid="!!errors.invite" aria-describedby="invite-error"><p id="invite-error" class="field-error">{{ errors.invite }}</p></div><button class="button primary" :disabled="busy" type="submit"><span v-if="busy" class="spinner" aria-hidden="true"></span>{{ busy ? t('loading') : t('validateInvite') }}</button></form>
        <div v-else-if="isLegal" class="legal-copy">
          <section v-for="section in legalSections" :key="section[0]">
            <h2>{{ section[0] }}</h2>
            <p>{{ section[1] }}</p>
          </section>
          <a class="button secondary" href="/login" @click.prevent="go('login')">{{ t('returnLogin') }}</a>
        </div>
        <div v-else class="state-actions">
          <button v-if="view === 'verify-email' || view === 'verification-expired'" class="button primary" :disabled="busy" @click="resend">{{ busy ? t('loading') : t('resend') }}</button>
          <a v-if="view === 'verify-email'" class="button secondary" href="/signup" @click.prevent="go('signup')">{{ t('different') }}</a>
          <a v-if="view === 'reset-expired'" class="button primary" href="/forgot-password" @click.prevent="go('forgot-password')">{{ t('sendReset') }}</a>
          <a v-if="['verification-success','reset-success','logged-out','service-error','account-disabled','reset-sent'].includes(view)" class="button primary" :href="initiation">{{ ['verification-success','reset-success'].includes(view) ? t('continueLogin') : t('returnLogin') }}</a>
          <a v-if="view === 'verify-email' || view === 'verification-expired'" class="text-link" href="/login" @click.prevent="go('login')">{{ t('returnLogin') }}</a>
        </div>
        <footer><a href="/terms">{{ t('termsLabel') }}</a><span aria-hidden="true">·</span><a href="/privacy">{{ t('privacyLabel') }}</a></footer>
      </section>
    </main>
  </div>
</template>
