/**
 * Tests for the Python-side preflight.
 *
 * The behaviour worth pinning is the *distinction* between a failure that a
 * retry can fix and one it cannot: retrying a missing module three times only
 * delays the message the operator needs.
 */
import { describe, expect, it, vi } from 'vitest'
import { checkPythonSide, classifyFailure } from '../src/preflight.ts'

describe('classifyFailure', () => {
  it('treats a missing interpreter as permanent', () => {
    const err = Object.assign(new Error('spawn ENOENT'), { code: 'ENOENT' })
    const verdict = classifyFailure(err, '')
    expect(verdict.ok).toBe(false)
    expect(verdict.permanent).toBe(true)
    expect(verdict.detail).toContain('ENOENT')
  })

  it('treats an unimportable library as permanent, with an actionable detail', () => {
    const err = Object.assign(new Error('exit 1'), { code: 1 })
    const stderr = "Traceback (most recent call last):\nModuleNotFoundError: No module named 'atom_memory'"
    const verdict = classifyFailure(err, stderr)
    expect(verdict.permanent).toBe(true)
    expect(verdict.detail).toContain("No module named 'atom_memory'")
  })

  it('treats a hung probe as transient so a slow start can still succeed', () => {
    const err = Object.assign(new Error('timed out'), { code: 'ETIMEDOUT' })
    const verdict = classifyFailure(err, '')
    expect(verdict.permanent).toBe(false)
  })
})

describe('checkPythonSide', () => {
  it('reports ok with the resolved interpreter and version', async () => {
    const run = vi.fn(async (_bin: string, _args: string[]) => ({ code: 0, stdout: '3.14.0', stderr: '' }))
    const result = await checkPythonSide('/usr/bin/python3', { run })
    expect(result.ok).toBe(true)
    expect(result.bin).toBe('/usr/bin/python3')
    expect(result.detail).toContain('3.14.0')
    expect(run).toHaveBeenCalledTimes(1)
  })

  it('probes for both atom_memory and sqlite_vec', async () => {
    const run = vi.fn(async (_bin: string, _args: string[]) => ({ code: 0, stdout: '3.14.0', stderr: '' }))
    await checkPythonSide('/usr/bin/python3', { run })
    const [bin, args] = run.mock.calls[0]!
    expect(bin).toBe('/usr/bin/python3')
    const script = (args as string[]).join(' ')
    expect(script).toContain('atom_memory')
    expect(script).toContain('sqlite_vec')
  })

  it('marks a permission failure as permanent rather than retrying it', async () => {
    const run = vi.fn(async () => {
      throw Object.assign(new Error('EACCES'), { code: 'EACCES' })
    })
    const result = await checkPythonSide('/root/python', { run })
    expect(result.ok).toBe(false)
    expect(result.permanent).toBe(true)
  })
})
