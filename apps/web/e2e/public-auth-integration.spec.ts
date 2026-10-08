import { expect, test } from '@playwright/test'

// Public-route browser tests are synthetic and never send credentials or email.
test('all public account flows are interactive and the favicon resolves', async ({page, request}) => {
  await page.goto('/login')
  await expect(page.getByRole('heading', {name: 'Welcome back'})).toBeVisible()

  const icon = page.locator('link[rel="icon"]')
  await expect(icon).toHaveAttribute('href', '/auth-assets/favicon.svg')
  const iconResponse = await request.get('/auth-assets/favicon.svg')
  expect(iconResponse.status()).toBe(200)
  expect(iconResponse.headers()['content-type']).toContain('image/svg+xml')

  const password = page.getByLabel('Password', {exact: true})
  await expect(password).toHaveAttribute('type', 'password')
  await page.getByRole('button', {name: 'Show'}).click()
  await expect(password).toHaveAttribute('type', 'text')
  await page.getByRole('button', {name: 'Hide'}).click()
  await expect(password).toHaveAttribute('type', 'password')

  await page.getByLabel('Language').selectOption('es')
  await expect(page.locator('html')).toHaveAttribute('lang', 'es')
  await page.getByLabel('Language').selectOption('en')

  await page.getByRole('link', {name: 'Forgot password?'}).click()
  await expect(page).toHaveURL(/\/forgot-password/)
  await expect(page.getByLabel('Email address')).toBeVisible()

  await page.goto('/signup')
  await expect(page.getByRole('heading', {name: 'Create your Klyrow account'})).toBeVisible()
  await page.getByRole('button', {name: 'Create account'}).click()
  await expect(page.locator('[aria-invalid="true"]').first()).toBeFocused()
  expect(await page.locator('[aria-invalid="true"]').count()).toBeGreaterThanOrEqual(3)
})

test('mobile account flow has no horizontal overflow', async ({page}) => {
  await page.setViewportSize({width:390,height:844})
  for (const route of ['/login','/signup','/forgot-password']) {
    await page.goto(route)
    const viewport = await page.evaluate(() => ({
      layout: document.documentElement.scrollWidth,
      inner: window.innerWidth,
    }))
    expect(viewport.layout).toBeLessThanOrEqual(viewport.inner)
    await expect(page.getByRole('main')).toBeVisible()
  }
})
