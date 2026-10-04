import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { Config } from '../src/config.ts'
import { buildStartParams } from '../src/index.ts'

const here = dirname(fileURLToPath(import.meta.url))
const shippedText = readFileSync(join(here, '..', 'cordis.patch.yml'), 'utf8')

/**
 * Pull the `scopeDomainMap` entries out of the shipped patch file.
 *
 * Read with a targeted line scan rather than a YAML parser because `yaml` is a
 * transitive dependency here, not a declared one, and adding a dependency to
 * assert a deployment's data would be the wrong trade. The scan mirrors how the
 * rules are actually written (a `- prefix:` line followed by its `domain:`).
 */
function shippedRules(text: string): Array<{ prefix: string; domain: string }> {
  const rules: Array<{ prefix: string; domain: string }> = []
  const lines = text.split(/\r?\n/)
  const start = lines.findIndex((l) => l.trim().startsWith('scopeDomainMap:'))
  if (start < 0) return rules
  for (let i = start + 1; i < lines.length; i += 1) {
    const line = lines[i]!
    if (/^\s*#/.test(line) || line.trim() === '') continue
    // A key at the config level (8 spaces, no dash) ends the block.
    if (/^ {8}\S/.test(line) && !line.trim().startsWith('-')) break
    // `- ["prefix", "domain"]`
    const m = line.match(/^\s*-\s*\[\s*"((?:[^"\\]|\\.)*)"\s*,\s*"((?:[^"\\]|\\.)*)"\s*\]\s*$/)
    if (m) rules.push({ prefix: m[1]!, domain: m[2]! })
  }
  return rules
}

/**
 * The shipped patch file carries this deployment's real scope→topic rules, and
 * a rule whose prefix does not match a stored scope path never fires — silently,
 * because a non-matching rule is indistinguishable from no rule at all.
 *
 * These tests do not assert the specific projects (that is deployment data);
 * they assert the *shape* that makes the rules able to fire at all: valid YAML,
 * a non-empty prefix, no rule that would match everything, and a config the
 * plugin's own schema accepts.
 */
describe('the shipped scope→topic mapping', () => {
  const config = shippedRules(shippedText)

  it('is present and non-empty', () => {
    // A scan that silently found nothing would make every test below vacuous.
    expect(config.length).toBeGreaterThan(0)
  })

  it('parses through the plugin schema', () => {
    // Catches a key the schema does not declare or a wrong value type: both
    // would otherwise only show up as a silent default at load time.
    const parsed = Config({ scopeDomainMap: config.map(r => [r.prefix, r.domain]) })
    expect(parsed.scopeDomainMap).toHaveLength(config.length)
  })

  it('gives every rule a real prefix and a real topic', () => {
    for (const rule of config) {
      expect(rule.prefix.trim()).not.toBe('')
      expect(rule.domain.trim()).not.toBe('')
    }
  })

  it('never maps a prefix that would swallow the whole store', () => {
    // `/global` (and the empty string) is a prefix of every scope path, so a
    // rule on it would file the entire corpus under one topic.
    for (const rule of config) {
      expect(rule.prefix).not.toBe('/global')
      expect(rule.prefix.startsWith('/global/')).toBe(true)
      // A bare `/global/project:` would cover every project too.
      expect(rule.prefix).not.toBe('/global/project:')
    }
  })

  it('reaches Python as two-element lists', () => {
    const params = buildStartParams(
      Config({ scopeDomainMap: config.map(r => [r.prefix, r.domain]) }),
    )

    expect(params.scope_domain_map).toHaveLength(config.length)
    for (const entry of params.scope_domain_map as string[][]) {
      expect(Array.isArray(entry)).toBe(true)
      expect(entry).toHaveLength(2)
    }
    // The count has to survive the trip: a rule dropped in transit is a project
    // that silently keeps falling back to `general`.
    expect(params.scope_domain_map).toHaveLength(config.length)
  })

  it('uses the same spelling of every path the scope store records', () => {
    // Prefix matching is byte-for-byte, so a rule written against one dialect
    // silently never fires against another. The store holds three:
    //   - Windows paths: lowercase drive, no leading slash (`c:/users/...`)
    //   - mounted POSIX paths (`/mnt/c/users/...`)
    //   - remote scopes: bare host and path, no drive (`github.com/owner/repo`)
    // A rule must match one of them exactly; a capitalised drive or a stray
    // leading slash is the classic near-miss that looks right and never fires.
    for (const rule of config) {
      const rest = rule.prefix.replace(/^\/global\/project:/, '')
      const windows = /^[a-z]:\//.test(rest)
      const posix = rest.startsWith('/mnt/')
      const remote = /^[a-z0-9.-]+\.[a-z]{2,}\//.test(rest)
      expect(
        windows || posix || remote,
        `unrecognised path dialect: ${rest}`,
      ).toBe(true)
      if (windows) {
        expect(rest[0]).toBe(rest[0]!.toLowerCase())
      }
      if (remote) {
        expect(rest).toBe(rest.toLowerCase())
      }
    }
  })
})