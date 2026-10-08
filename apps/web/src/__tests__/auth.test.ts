import { fireEvent, render, screen } from '@testing-library/vue'
import { describe, expect, it } from 'vitest'
import App from '../App.vue'
import { maskEmail, passwordScore, routeFromLocation, serverError } from '../auth'

describe('auth helpers', () => {
  it('routes every supported authentication state', () => {
    for (const path of ['login','signup','verify-email','verification-expired','verification-success','forgot-password','reset-sent','reset-password','reset-expired','reset-success','invite','service-error','account-disabled','terms','privacy']) expect(routeFromLocation(`/${path}`)).toBe(path)
    expect(routeFromLocation('/login','?logged_out=1')).toBe('logged-out')
  })
  it('maps errors without exposing server details', () => {
    expect(serverError(401)).toBe('invalidCredentials'); expect(serverError(429)).toBe('rateLimited'); expect(serverError(503)).toBe('unavailable'); expect(serverError(409)).toBe('duplicate')
  })
  it('scores passwords and masks email', () => { expect(passwordScore('Abcdefghijk1!')).toBe(5); expect(maskEmail('alex@example.com')).toMatch(/^a•+@example.com$/) })
})

describe('authentication UI', () => {
  it('renders legal pages with navigable content', () => {
    history.replaceState({}, '', '/terms'); render(App); expect(screen.getByRole('heading',{name:'Terms of Service'})).toBeTruthy(); expect(screen.getByText('Authorized use')).toBeTruthy()
  })

  it('validates login fields and restores focus to the first error', async () => {
    history.replaceState({}, '', '/login'); render(App); await fireEvent.click(screen.getByRole('button',{name:'Sign in'})); expect(document.activeElement).toBe(screen.getByLabelText('Email address')); expect(screen.getAllByText('This field is required.').length).toBeGreaterThan(0)
  })
  it('toggles password visibility accessibly', async () => {
    history.replaceState({}, '', '/login'); render(App); const input=screen.getByLabelText('Password') as HTMLInputElement; await fireEvent.click(screen.getByRole('button',{name:'Show'})); expect(input.type).toBe('text'); expect(screen.getByRole('button',{name:'Hide'}).getAttribute('aria-pressed')).toBe('true')
  })
  it('renders complete Spanish without English form labels', async () => {
    history.replaceState({}, '', '/signup?lang=es'); render(App); expect(screen.getByRole('heading',{name:'Crea tu cuenta de Klyrow'})).toBeTruthy(); expect(screen.getByLabelText('Correo de trabajo')).toBeTruthy(); expect(screen.queryByText('Create account')).toBeNull()
  })
  it('shows password guidance and mismatch validation', async () => {
    history.replaceState({}, '', '/signup'); render(App); await fireEvent.update(screen.getByLabelText('First name'),'A'); await fireEvent.update(screen.getByLabelText('Last name'),'B'); await fireEvent.update(screen.getByLabelText('Work email'),'a@example.com'); await fireEvent.update(screen.getByLabelText('Password'),'weak'); await fireEvent.update(screen.getByLabelText('Confirm password'),'different'); await fireEvent.click(screen.getByRole('button',{name:'Create account'})); expect(screen.getByText('Choose a stronger password.')).toBeTruthy(); expect(screen.getByText('Passwords do not match.')).toBeTruthy()
  })
})
