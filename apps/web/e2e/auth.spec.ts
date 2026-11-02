import AxeBuilder from '@axe-core/playwright'
import { expect, test } from '@playwright/test'

for (const viewport of [{name:'mobile',width:375,height:812},{name:'tablet',width:768,height:1024},{name:'desktop',width:1440,height:900}]) test(`${viewport.name} login is responsive and accessible`, async ({page}) => {
  await page.setViewportSize(viewport); await page.goto('/login'); await expect(page.getByRole('heading',{name:'Welcome back'})).toBeVisible(); await expect(page.locator('body')).not.toHaveCSS('overflow-x','scroll'); expect((await new AxeBuilder({page}).analyze()).violations).toEqual([])
})
test('keyboard flow and focus restoration', async ({page}) => { await page.goto('/login'); await page.keyboard.press('Tab'); await expect(page.locator('.skip-link')).toBeFocused(); await page.getByRole('button',{name:'Sign in'}).click(); await expect(page.getByLabel('Email address')).toBeFocused() })
test('Google starts through the Klyrow broker route', async ({page}) => { await page.goto('/login'); const target=page.waitForRequest(req=>req.url().includes('/auth/google')); await page.getByRole('button',{name:'Continue with Google'}).click(); expect((await target).url()).toContain('/auth/google?return_to=') })
test('no auth secrets or tokens are written to browser storage', async ({page}) => { await page.goto('/login'); expect(await page.evaluate(()=>({local:[...Object.keys(localStorage)],session:[...Object.keys(sessionStorage)]}))).toEqual({local:[],session:[]}); const source=await page.locator('html').evaluate(()=>document.documentElement.outerHTML); expect(source).not.toMatch(/client_secret|refresh_token|access_token/) })

test('password recovery stays on the form when the server action fails', async ({page}) => {
  await page.route('**/auth/actions/recover', route => route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'authentication_unavailable'})}))
  await page.goto('/forgot-password')
  await page.getByLabel('Email address').fill('user@example.com')
  await page.getByRole('button',{name:'Send reset instructions'}).click()
  await expect(page).toHaveURL(/\/forgot-password$/)
  await expect(page.getByRole('alert')).toContainText('temporarily unavailable')
})

test('password recovery shows success only after the server returns a redirect', async ({page}) => {
  let submitted = false
  await page.route('**/auth/actions/recover', async route => {
    submitted = route.request().method() === 'POST' && (await route.request().postDataJSON()).email === 'user@example.com'
    await route.fulfill({status:202,contentType:'application/json',body:JSON.stringify({status:'accepted',redirect_to:'/reset-sent'})})
  })
  await page.goto('/forgot-password')
  await page.getByLabel('Email address').fill('user@example.com')
  await page.getByRole('button',{name:'Send reset instructions'}).click()
  await expect(page).toHaveURL(/\/reset-sent$/)
  expect(submitted).toBe(true)
  await expect(page.getByRole('heading',{name:'Check your email for the next step.'})).toBeVisible()
})

test('one-time invitation URL hydrates the validation capability', async ({page}) => {
  const invitationCode = ['test','invitation','code'].join('-')
  await page.goto(`/invite?token=${encodeURIComponent(invitationCode)}`)
  await expect(page.getByRole('heading',{name:'Join your Klyrow team'})).toBeVisible()
  await expect(page.getByLabel('Invitation code')).toHaveValue(invitationCode)
})

test('legal links resolve and remain responsive', async ({page}) => {
  for (const [path, heading] of [['/terms','Terms of Service'], ['/privacy','Privacy Policy']] as const) {
    await page.goto(path)
    await expect(page).toHaveURL(new RegExp(path + '$'))
    await expect(page.getByRole('heading',{name:heading})).toBeVisible()
    await expect(page.locator('body')).not.toHaveCSS('overflow-x','scroll')
  }
})
