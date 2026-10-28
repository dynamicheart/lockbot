import { describe, it, expect } from 'vitest'
import { maskSecret } from './secretDisplay'
describe('Consistent credential masking', () => {
  it.each(['token-abcd', 'aes-key-1234', 'https://api.example/send?access_token=secret9876'])(
    'uses stars and four trailing characters for %s',
    (value) => {
      const masked = maskSecret(value)
      expect(masked).toBe('*'.repeat(value.length - 4) + value.slice(-4))
      expect(masked).not.toContain('https://')
      expect(masked).not.toContain('secret')
      expect(masked).not.toContain('•')
    }
  )
  it('masks short secrets entirely and handles absent values', () => {
    expect(maskSecret('abc')).toBe('****')
    expect(maskSecret('')).toBe('-')
    expect(maskSecret(null)).toBe('-')
  })
})
