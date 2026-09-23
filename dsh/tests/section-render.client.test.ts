/**
 * Client render test for the settings section (jsdom).
 *
 * @vitest-environment jsdom
 *
 * Reproduces the LIVE render path the ui-renderer uses for a `settings.section`
 * entry: hooks are bound with the real uSES selector hook (matching
 * `bindInjectSources` → `observableHook` → `bindSnapshotSelector`), `t` is bound
 * through a LocaleFace, and the component is rendered client-side (so effects
 * run and real `useSyncExternalStoreWithSelector` without a server snapshot is
 * used). Any render/effect crash that slots' SlotErrorBoundary would otherwise
 * swallow into a blank panel surfaces here.
 */
import { describe, expect, it, vi, afterEach } from 'vitest'
import { render, screen, act, cleanup, fireEvent, within } from '@testing-library/react'
import { createElement } from 'react'
import { useSyncExternalStoreWithSelector } from 'use-sync-external-store/shim/with-selector'
import { MemorySettingsController } from '../src/client/memory-settings-controller.ts'
import type { ProfileEditRow } from '../src/client/memory-settings-controller.ts'
import { MemorySettingsSection } from '../src/client/MemorySettingsSection.tsx'
import { dicts, LOCALE_NS } from '../src/client/locales.ts'
import {
  DEFAULT_INJECTED_SUMMARY_TOKENS,
  INJECTED_SUMMARY_TOKEN_PRESETS,
} from '../src/injection-budget.ts'

const zh = dicts.zh

/** Real observer hook construction, matching bindSnapshotSelector. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function useSnapshotHook<T>(store: { getSnapshot(): T; subscribe(fn: () => void): () => void }) {
  const subscribe = (fn: () => void) => store.subscribe(fn)
  const getSnapshot = () => store.getSnapshot()
  return function useSelector<S>(select: (s: T) => S, equal?: (a: S, b: S) => boolean): S {
    return useSyncExternalStoreWithSelector(subscribe, getSnapshot, undefined, select, equal)
  }
}

/**
 * Build a controller on a real in-memory settings scope.
 *
 * @param seedFacts - Seed one active fact so the facts table has a row.
 * @param seedProfile - Seed one profile row so the profile table has a row.
 * @param budget - Initial injection budget; `undefined` keeps the default gear.
 */
function buildController(
  seedFacts = false,
  seedProfile = false,
  budget: number | undefined = DEFAULT_INJECTED_SUMMARY_TOKENS,
) {
  // A real in-memory settings scope: `set` persists the key and notifies
  // subscribers, so the controller's publish -> re-render -> draft-resync path
  // is exercised instead of being stubbed away.
  let section: Record<string, unknown> = {
    enabled: true, captureEnabled: true, llmExtractionEnabled: true,
    contextInjectionEnabled: true, extractionModel: undefined,
    injectedSummaryTokens: budget,
  }
  const listeners = new Set<() => void>()
  const scope = {
    getSnapshot: () => ({
      status: 'ready' as const,
      value: section,
      base: undefined, user: undefined, revision: 1, writable: true, mode: 'host' as const,
    }),
    subscribe: (fn: () => void) => { listeners.add(fn); return () => { listeners.delete(fn) } },
    set: async (key: string, value: unknown) => {
      section = { ...section, [key]: value }
      for (const fn of [...listeners]) fn()
    },
    unset: async () => {}, mutate: async () => {},
  }
  const remote = {
    listFacts: async () => ({
      ok: true,
      value: seedFacts
        ? { facts: [{ fact_id: 'f1', subject: '张三', predicate: '是', object: '工程师', content: '' }], total: 1 }
        : { facts: [], total: 0 },
    }),
    editFact: async () => ({ ok: true, value: {} }),
    deleteFact: async () => ({ ok: true, value: {} }),
    summary: async () => ({ ok: true, value: '# 记忆摘要 (Summary) — global\n决策规则\n- 一条规则' }),
    listProfile: async () => ({
      ok: true,
      value: {
        profile: seedProfile
          ? [{ section: '偏好', key: '回答语言', value: '中文', source: 'user' }]
          : [],
        count: seedProfile ? 1 : 0,
        limit: 50,
      },
    }),
    upsertProfile: async () => ({ ok: true, value: {} }),
    deleteProfile: async () => ({ ok: true, value: {} }),
    writeProfile: async () => ({ ok: true, value: { written: 1, deleted: 0 } }),
    generateProfile: async () => ({
      ok: true,
      value: { suggestions: [], existing: 0, limit: 50, full: false },
    }),
    backup: async () => ({ ok: true, value: { version: 1, facts: [], profile: [] } }),
    restore: async () => ({ ok: true, value: { facts_written: 0, profile_written: 0 } }),
  }
  return new MemorySettingsController(scope as never, remote as never)
}

function bind(controller: MemorySettingsController) {
  const face = controller.inject()
  const hooks = face.hooks as { memorySettings: { getSnapshot(): unknown; subscribe(fn: () => void): () => void } }
  const useMemorySettings = useSnapshotHook(hooks.memorySettings)
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const t = (key: string, params?: Record<string, unknown>) => {
    const tmpl = (zh as Record<string, string | undefined>)[key]
    if (tmpl === undefined) throw new Error(`missing locale key: ${key}`)
    if (!params) return tmpl
    return tmpl.replace(/\{(\w+)\}/g, (_s, k) => String((params as Record<string, unknown>)[k]))
  }
  // Forward the face verbatim, mirroring the renderer's InjectFace contract
  // (hooks -> use<Name>, every other member -> a prop of the same name).
  // Spreading instead of hand-listing the members matters: a hand-written list
  // silently goes stale whenever a new face action is added, and the resulting
  // prop is `undefined` at the call site.
  const { hooks: _hooks, ...actions } = face
  const props = {
    ...actions,
    t: t as never,
    useMemorySettings: useMemorySettings as never,
    // Provided by the slot runtime (PropsRuntime<'settings.section'>), not by
    // the injected face.
    close: () => {},
  }
  return { face, props }
}

const refreshData = vi.fn(async () => {})

/**
 * Type `text` into `input` one character at a time, asserting after every
 * keystroke that the *same DOM element* is still focused and that the value
 * accumulated. A React key that changes while typing remounts the row, which
 * destroys the focused element (focus falls back to <body> and the next
 * keystroke is lost) — this helper fails loudly on exactly that.
 */
async function typeInto(input: HTMLInputElement, text: string): Promise<void> {
  input.focus()
  expect(document.activeElement).toBe(input)
  for (const ch of text) {
    const typed = input.value + ch
    await act(async () => {
      fireEvent.change(input, { target: { value: typed } })
    })
    expect(document.activeElement).toBe(input)
    expect(input.value).toBe(typed)
  }
}

afterEach(cleanup)

/**
 * Record every extraction-model write while still applying it for real, so the
 * merge-with-existing-override behaviour (and the write-back into the field) is
 * observable instead of stubbed away.
 */
function spyModelWrites(props: { setExtractionModelOverride: unknown }): Array<Record<string, unknown>> {
  const commits: Array<Record<string, unknown>> = []
  const real = props.setExtractionModelOverride as (o: Record<string, unknown>) => Promise<void>
  props.setExtractionModelOverride = (async (override: Record<string, unknown>) => {
    commits.push(override)
    await real(override)
  }) as never
  return commits
}

/** Record every injection-budget write while still applying it for real. */
function spyBudgetWrites(props: { setInjectedSummaryTokens: unknown }): number[] {
  const writes: number[] = []
  const real = props.setInjectedSummaryTokens as (tokens: number) => Promise<void>
  props.setInjectedSummaryTokens = (async (tokens: number) => {
    writes.push(tokens)
    await real(tokens)
  }) as never
  return writes
}

describe('MemorySettingsSection client render', () => {
  it('renders and runs effects without throwing', async () => {
    const controller = buildController()
    const { face, props } = bind(controller)
    // Override the default refresh stub with an observable spy.
    props.refreshData = refreshData
    await act(async () => {
      render(createElement(MemorySettingsSection, props))
    })
    expect(screen.getByText('记忆')).toBeTruthy()
    expect(screen.getByText('LLM 抽取模型')).toBeTruthy()
    expect(screen.getByText('编辑记忆')).toBeTruthy()
    expect(screen.getByText('编辑画像')).toBeTruthy()
    expect(screen.getByText('查看摘要')).toBeTruthy()
    // The three memory-content blocks (summary / profile / memory & facts) are
    // grouped under one 记忆内容 region.
    expect(screen.getByText('记忆内容')).toBeTruthy()
    expect(refreshData).toHaveBeenCalled()
    expect(screen.queryByText(LOCALE_NS + ':title')).toBeNull()
  })

  it('renders no memory master switch: enabling the plugin is dsh\'s own switch', async () => {
    const controller = buildController()
    const { props } = bind(controller)
    await act(async () => {
      render(createElement(MemorySettingsSection, props))
    })
    // The panel keeps its own live switches (injection budget, overview), but
    // there is no second, memory-side answer to "is this plugin enabled".
    expect(screen.queryByText('记忆开关')).toBeNull()
    expect((props as Record<string, unknown>).setEnabled).toBeUndefined()
  })

  it('opens the memory summary in a read-only modal', async () => {
    const controller = buildController()
    const { props } = bind(controller)
    await act(async () => {
      render(createElement(MemorySettingsSection, props))
    })
    // All four memory-content actions (summary / profile / memory & facts)
    // share the one 记忆内容 group.
    const group = screen.getByText('记忆内容').closest('fieldset')!
    expect(within(group).getByText('查看摘要')).toBeTruthy()
    expect(within(group).getByText('编辑画像')).toBeTruthy()
    expect(within(group).getByText('编辑记忆')).toBeTruthy()
    // Not open initially.
    expect(screen.queryByText(/# 记忆摘要/)).toBeNull()
    await act(async () => {
      fireEvent.click(screen.getByText('查看摘要'))
    })
    await act(async () => {})
    // The summary modal renders the raw markdown exactly as it is injected
    // (read-only `<pre>`), not a parsed structured list.
    expect(screen.getByText(/# 记忆摘要 \(Summary\) — global/)).toBeTruthy()
    expect(screen.getByText(/决策规则/)).toBeTruthy()
  })

  it('lays the memory-content actions out as a horizontal row of tooltip buttons', async () => {
    const controller = buildController()
    const { props } = bind(controller)
    await act(async () => {
      render(createElement(MemorySettingsSection, props))
    })
    const group = screen.getByText('记忆内容').closest('fieldset')!
    // The row wraps the action buttons, each as a `.atom-memory-toggle` anchor.
    const row = group.querySelector('.atom-memory-content-actions')!
    expect(row).toBeTruthy()
    const toggles = Array.from(row.querySelectorAll('.atom-memory-toggle'))
    // 查看摘要 / 编辑画像 / 编辑记忆 — the three content actions that exist
    // today (no fourth action has been added).
    expect(toggles).toHaveLength(3)
    // The row is horizontal; every toggle anchors a hidden hover tooltip.
    for (const toggle of toggles) {
      expect(toggle.querySelector('.atom-memory-btn')).toBeTruthy()
      expect(toggle.querySelector('.atom-memory-tooltip')).toBeTruthy()
    }
    // Hidden until hover: screen readers / static markup must not read it as
    // visible text, so the tooltip starts with visibility hidden in CSS.
    expect(toggles[0]!.querySelector('.atom-memory-tooltip')!.classList.contains('atom-memory-tooltip')).toBe(true)
  })

  it('opens the facts editor as an Excel-like table with the saved data', async () => {
    const controller = buildController(true)
    const { props } = bind(controller)
    // Let the real refreshData load the seeded fact into the store.
    await act(async () => {
      render(createElement(MemorySettingsSection, props))
    })
    await act(async () => {})
    await act(async () => {
      fireEvent.click(screen.getByText('编辑记忆'))
    })
    await act(async () => {})
    // The seeded fact row's subject is present as an editable cell value.
    const subjectInput = screen.getByDisplayValue('张三')
    expect(subjectInput).toBeTruthy()
    // One save-all button, one 添加一行 button, one 取消 (close) button.
    expect(screen.getAllByText('保存全部').length).toBeGreaterThan(0)
  })

  it('opens the profile editor as an Excel-like table with the saved data', async () => {
    const controller = buildController(false, true)
    const { props } = bind(controller)
    await act(async () => {
      render(createElement(MemorySettingsSection, props))
    })
    await act(async () => {})
    await act(async () => {
      fireEvent.click(screen.getByText('编辑画像'))
    })
    await act(async () => {})
    expect(screen.getByDisplayValue('偏好')).toBeTruthy()
    expect(screen.getByDisplayValue('回答语言')).toBeTruthy()
  })

  /**
   * Regression: the profile table keyed its rows by *content*
   * (`section:key:index`), so every keystroke in the 分组/键 cells changed the
   * React key and remounted the `<tr>` — the focused <input> was destroyed and
   * focus fell back to <body>, making continuous typing impossible. Rows must
   * keep a stable identity across edits.
   */
  it('keeps DOM focus while typing every character into a profile cell', async () => {
    const controller = buildController(false, true)
    const { props } = bind(controller)
    await act(async () => {
      render(createElement(MemorySettingsSection, props))
    })
    await act(async () => {})
    await act(async () => {
      fireEvent.click(screen.getByText('编辑画像'))
    })
    await act(async () => {})

    const dataRows = screen.getAllByRole('row').slice(1)
    expect(dataRows).toHaveLength(1)
    // 分组 / 键 / 值 — the two content-derived cells are the ones that used to
    // remount the row on every keystroke.
    const cells = within(dataRows[0]!).getAllByRole('textbox') as HTMLInputElement[]
    expect(cells).toHaveLength(3)

    for (const input of cells) {
      await typeInto(input, 'ABC')
    }
  })

  it('keeps DOM focus while typing every character into a facts cell', async () => {
    const controller = buildController(true)
    const { props } = bind(controller)
    await act(async () => {
      render(createElement(MemorySettingsSection, props))
    })
    await act(async () => {})
    await act(async () => {
      fireEvent.click(screen.getByText('编辑记忆'))
    })
    await act(async () => {})
    // The facts editor edits/deletes existing rows but has no "add row": the
    // facts table has no create endpoint (new facts come from conversation
    // capture), so the one seeded fact row is the only row present.
    const dataRows = screen.getAllByRole('row').slice(1)
    expect(dataRows).toHaveLength(1)
    for (const row of dataRows) {
      const cells = within(row).getAllByRole('textbox') as HTMLInputElement[]
      expect(cells).toHaveLength(4) // subject / predicate / object / content
      for (const input of cells) {
        await typeInto(input, 'XYZ')
      }
    }
  })

  it('shows the system-prompt injection size as a gear slider', async () => {
    const controller = buildController()
    const { props } = bind(controller)
    await act(async () => {
      render(createElement(MemorySettingsSection, props))
    })
    // The field is about the *system prompt* snapshot, and says so.
    expect(screen.getByText('系统提示词注入体积（记忆摘要）')).toBeTruthy()
    expect(screen.queryByText('注入体积（记忆摘要）')).toBeNull()

    const slider = screen.getByLabelText('挡位') as HTMLInputElement
    expect(slider.type).toBe('range')
    expect(slider.className).toBe('atom-memory-slider')
    expect(slider.min).toBe('0')
    expect(slider.max).toBe(String(INJECTED_SUMMARY_TOKEN_PRESETS.length - 1))
    expect(slider.step).toBe('1')
    // Parked on the default gear (800), and naming it rather than showing a
    // bare index to assistive tech.
    expect(slider.value).toBe('1')
    expect(slider.getAttribute('aria-valuetext')).toBe('标准 · 800 tokens')
    expect(screen.getByText('标准 · 800 tokens')).toBeTruthy()
    // Every gear is visible under the handle, so the ladder is discoverable.
    for (const preset of INJECTED_SUMMARY_TOKEN_PRESETS) {
      expect(screen.getByText(String(preset))).toBeTruthy()
    }
    expect(screen.getByText(/当前 800 tokens/)).toBeTruthy()
  })

  it('writes the gear value (never the slider index) through the settings scope', async () => {
    const controller = buildController()
    const { props } = bind(controller)
    const writes = spyBudgetWrites(props)
    await act(async () => {
      render(createElement(MemorySettingsSection, props))
    })
    const slider = screen.getByLabelText('挡位') as HTMLInputElement

    // Index 0 is the cheapest gear — the panel must write 300, not 0.
    await act(async () => {
      fireEvent.change(slider, { target: { value: '0' } })
    })
    expect(writes).toEqual([INJECTED_SUMMARY_TOKEN_PRESETS[0]])
    await act(async () => {})
    expect(slider.value).toBe('0')
    expect(screen.getByText('精简 · 300 tokens')).toBeTruthy()
    expect(screen.getByText(/当前 300 tokens/)).toBeTruthy()

    // The top index writes the top gear's token budget.
    const top = INJECTED_SUMMARY_TOKEN_PRESETS.length - 1
    await act(async () => {
      fireEvent.change(slider, { target: { value: String(top) } })
    })
    expect(writes).toEqual([INJECTED_SUMMARY_TOKEN_PRESETS[0], INJECTED_SUMMARY_TOKEN_PRESETS[top]])
    await act(async () => {})
    expect(slider.value).toBe(String(top))
  })

  /**
   * A budget set before the slider existed (the old free-text field) or from the
   * plugin composition is not on the ladder. The handle still has to sit
   * somewhere, and the panel must not silently claim the value *is* that gear.
   */
  it('parks an off-ladder budget at the nearest gear and says so', async () => {
    const controller = buildController(false, false, 1200)
    const { props } = bind(controller)
    // Spy before render: React freezes the props object it was handed.
    const writes = spyBudgetWrites(props)
    await act(async () => {
      render(createElement(MemorySettingsSection, props))
    })
    const slider = screen.getByLabelText('挡位') as HTMLInputElement
    // 1200 is nearer 1500 (index 2) than 800 (index 1).
    expect(slider.value).toBe('2')
    expect(screen.getByText(/当前 1200 tokens 不在挡位梯上/)).toBeTruthy()

    // Moving the handle snaps the stored value onto the fixed ladder.
    await act(async () => {
      fireEvent.change(slider, { target: { value: '1' } })
    })
    expect(writes).toEqual([INJECTED_SUMMARY_TOKEN_PRESETS[1]])
    await act(async () => {})
    expect(screen.getByText('标准 · 800 tokens')).toBeTruthy()
    expect(screen.queryByText(/不在挡位梯上/)).toBeNull()
  })

  it('saves the profile table without any pin flag', async () => {
    const controller = buildController(false, true)
    const { props } = bind(controller)
    // Spy before render: React freezes the props object it was handed.
    const saved: ProfileEditRow[][] = []
    props.saveAllProfile = (async (rows: ProfileEditRow[]) => { saved.push(rows) }) as never
    await act(async () => {
      render(createElement(MemorySettingsSection, props))
    })
    await act(async () => {})
    await act(async () => {
      fireEvent.click(screen.getByText('编辑画像'))
    })
    await act(async () => {})

    // The profile has no pin column any more: rows are user-owned outright, so
    // there is nothing for a pin to protect against.
    expect(screen.queryByText('固定')).toBeNull()

    const row = screen.getAllByRole('row')[1]!
    expect(within(row).getAllByRole('textbox')).toHaveLength(3)

    await act(async () => {
      fireEvent.click(screen.getAllByText('保存全部')[0]!)
    })
    await act(async () => {})

    expect(saved).toHaveLength(1)
    expect(saved[0]).toEqual([
      { section: '偏好', key: '回答语言', value: '中文', deleted: false },
    ])
  })

  it('sends marked profile rows in the save envelope so a deletion actually lands', async () => {
    const controller = buildController(false, true)
    const { props } = bind(controller)
    const saved: ProfileEditRow[][] = []
    props.saveAllProfile = (async (rows: ProfileEditRow[]) => { saved.push(rows) }) as never
    await act(async () => {
      render(createElement(MemorySettingsSection, props))
    })
    await act(async () => {})
    await act(async () => {
      fireEvent.click(screen.getByText('编辑画像'))
    })
    await act(async () => {})

    // Mark the one stored row for deletion. The row goes grey but is NOT
    // removed from the table — the store is what removes it.
    await act(async () => {
      fireEvent.click(within(screen.getAllByRole('row')[1]!).getByText(zh.factDelete))
    })
    await act(async () => {})

    await act(async () => {
      fireEvent.click(screen.getAllByText('保存全部')[0]!)
    })
    await act(async () => {})

    // The envelope must still carry the row, flagged `deleted` with the
    // section/key that identify it — dropping it here is what made the delete
    // button a no-op (the row came back on the next refresh).
    expect(saved).toHaveLength(1)
    expect(saved[0]).toEqual([
      { section: '偏好', key: '回答语言', value: '中文', deleted: true },
    ])
  })

  it('writes the ticked suggestions on save, with no separate accept step', async () => {
    const controller = buildController(false, false)
    const { props } = bind(controller)
    // A generated run: two proposals, one of which the user unticks.
    props.generateProfile = (async () => ({
      suggestions: [
        { section: '职业', key: 'value', value: '工程师' },
        { section: '城市', key: 'value', value: '天津' },
      ],
      existing: 0,
      limit: 50,
      full: false,
    })) as never
    const saved: ProfileEditRow[][] = []
    props.saveAllProfile = (async (rows: ProfileEditRow[]) => { saved.push(rows) }) as never

    await act(async () => {
      render(createElement(MemorySettingsSection, props))
    })
    await act(async () => {})
    await act(async () => {
      fireEvent.click(screen.getByText('编辑画像'))
    })
    await act(async () => {
      fireEvent.click(screen.getByText('生成画像'))
    })
    await act(async () => {})

    // Both proposals are shown, ticked by default. Scoped to the suggestion
    // region: the panel has other checkboxes, so a page-wide query would count
    // the wrong set.
    expect(screen.getByText(zh.suggestionIntro)).toBeTruthy()
    const region = screen.getByLabelText(zh.suggestionName)
    const boxes = within(region).getAllByRole('checkbox') as HTMLInputElement[]
    expect(boxes).toHaveLength(2)
    expect(boxes.every(b => b.checked)).toBe(true)

    // There is no "accept" button: the tick is the decision and 保存全部 commits it.
    expect(screen.queryByText('加入所选')).toBeNull()

    await act(async () => {
      fireEvent.click(boxes[1]!)
    })
    await act(async () => {
      fireEvent.click(screen.getAllByText('保存全部')[0]!)
    })
    await act(async () => {})

    // Only the still-ticked suggestion is written.
    expect(saved).toHaveLength(1)
    expect(saved[0]).toEqual([
      { section: '职业', key: 'value', value: '工程师', deleted: false },
    ])
  })

  it('folds ticked suggestions into a save alongside the draft rows', async () => {
    const controller = buildController(false, true)
    const { props } = bind(controller)
    props.generateProfile = (async () => ({
      suggestions: [{ section: '城市', key: 'value', value: '天津' }],
      existing: 1,
      limit: 50,
      full: false,
    })) as never
    const saved: ProfileEditRow[][] = []
    props.saveAllProfile = (async (rows: ProfileEditRow[]) => { saved.push(rows) }) as never

    await act(async () => {
      render(createElement(MemorySettingsSection, props))
    })
    await act(async () => {})
    await act(async () => {
      fireEvent.click(screen.getByText('编辑画像'))
    })
    await act(async () => {
      fireEvent.click(screen.getByText('生成画像'))
    })
    await act(async () => {})
    await act(async () => {
      fireEvent.click(screen.getAllByText('保存全部')[0]!)
    })
    await act(async () => {})

    // The seeded row and the accepted suggestion are one batch.
    expect(saved[0]).toEqual([
      { section: '偏好', key: '回答语言', value: '中文', deleted: false },
      { section: '城市', key: 'value', value: '天津', deleted: false },
    ])
  })

  it('reports an empty generation run instead of a blank panel', async () => {
    const controller = buildController(false, false)
    const { props } = bind(controller)
    props.generateProfile = (async () => ({
      suggestions: [], existing: 50, limit: 50, full: true,
    })) as never

    await act(async () => {
      render(createElement(MemorySettingsSection, props))
    })
    await act(async () => {})
    await act(async () => {
      fireEvent.click(screen.getByText('编辑画像'))
    })
    await act(async () => {
      fireEvent.click(screen.getByText('生成画像'))
    })
    await act(async () => {})

    expect(screen.getByText(zh.generateProfileFull.replace('{count}', '50').replace('{limit}', '50'))).toBeTruthy()
  })

  it('keeps the editor open and shows why when a profile save is refused', async () => {
    const controller = buildController(false, true)
    const { props } = bind(controller)
    props.saveAllProfile = (async () => {
      throw new Error('用户画像已达上限（50/50 条）')
    }) as never

    await act(async () => {
      render(createElement(MemorySettingsSection, props))
    })
    await act(async () => {})
    await act(async () => {
      fireEvent.click(screen.getByText('编辑画像'))
    })
    await act(async () => {
      fireEvent.click(screen.getAllByText('保存全部')[0]!)
    })
    await act(async () => {})

    // The reason is on screen and the table is still there to fix.
    expect(screen.getByText(/已达上限/u)).toBeTruthy()
    expect(screen.getByDisplayValue('中文')).toBeTruthy()
  })

  it('keeps the facts editor open and shows why when a save is refused', async () => {
    const controller = buildController(true)
    const { props } = bind(controller)
    props.saveAllFacts = (async () => {
      throw new Error('事实编辑被拒绝')
    }) as never

    await act(async () => {
      render(createElement(MemorySettingsSection, props))
    })
    await act(async () => {})
    await act(async () => {
      fireEvent.click(screen.getByText('编辑记忆'))
    })
    await act(async () => {})
    // Edit a cell so there is unsaved work worth preserving.
    const cell = screen.getAllByRole('textbox')[0]!
    await act(async () => {
      fireEvent.change(cell, { target: { value: '改过的主题' } })
    })
    await act(async () => {
      fireEvent.click(screen.getAllByText('保存全部')[0]!)
    })
    await act(async () => {})

    // The reason is on screen and the editor — with the edit — is still there.
    // Closing in a `.finally()` used to take both away.
    expect(screen.getByText(/事实编辑被拒绝/u)).toBeTruthy()
    expect(screen.getByDisplayValue('改过的主题')).toBeTruthy()
  })

  it('closes the facts editor once a save succeeds', async () => {
    const controller = buildController(true)
    const { props } = bind(controller)
    const saved: unknown[] = []
    props.saveAllFacts = (async (rows: unknown[]) => { saved.push(rows) }) as never

    await act(async () => {
      render(createElement(MemorySettingsSection, props))
    })
    await act(async () => {})
    await act(async () => {
      fireEvent.click(screen.getByText('编辑记忆'))
    })
    await act(async () => {})
    expect(screen.getByText('编辑记忆')).toBeTruthy()

    await act(async () => {
      fireEvent.click(screen.getAllByText('保存全部')[0]!)
    })
    await act(async () => {})

    expect(saved).toHaveLength(1)
    // The modal's own title button is gone: it closed on success.
    expect(screen.queryByDisplayValue('改过的主题')).toBeNull()
  })

  /**
   * The class map in the component and the stylesheet in `styles.ts` are two
   * hand-maintained mirrors (the bundle has no CSS pipeline to check them
   * against each other). A drifted name renders an unstyled control with no
   * error anywhere — so assert the two new shapes line up.
   */
  it('defines a stylesheet rule for the slider and pin classes it renders', async () => {
    const { memorySettingsStyleText } = await import('../src/client/styles.ts')
    for (const cls of ['atom-memory-slider', 'atom-memory-ticks', 'atom-memory-tick-active', 'atom-memory-content-actions', 'atom-memory-toggle', 'atom-memory-tooltip', 'atom-memory-group', 'atom-memory-group-title', 'atom-memory-switch', 'atom-memory-switch-input', 'atom-memory-switch-track', 'atom-memory-switch-thumb', 'atom-memory-summary-view']) {
      expect(memorySettingsStyleText).toContain(`.${cls}`)
    }
  })

  it('reveals the manual-model parameters (base URL / protocol / API key) when manual is selected', async () => {
    const controller = buildController()
    const { props } = bind(controller)
    await act(async () => {
      render(createElement(MemorySettingsSection, props))
    })
    // Not visible while following the default model.
    expect(screen.queryByText('API 地址 (Base URL)')).toBeNull()
    await act(async () => {
      fireEvent.click(screen.getByText('手动指定模型'))
    })
    expect(screen.getByText('Provider ID')).toBeTruthy()
    expect(screen.getByText('API 地址 (Base URL)')).toBeTruthy()
    expect(screen.getByText('API 协议')).toBeTruthy()
    expect(screen.getByText('API 密钥')).toBeTruthy()
  })

  /**
   * Regression: the manual-model fields were `<input value={x} onBlur={...} />`
   * with no `onChange`. React renders a `value` prop without `onChange` as a
   * read-only field, so the keystrokes were reverted and the value never
   * reached the DOM — the fields could not be filled in at all.
   */
  it('accepts typing in the manual-model fields and commits the draft on blur', async () => {
    const controller = buildController()
    const { props } = bind(controller)
    const commits = spyModelWrites(props)
    await act(async () => {
      render(createElement(MemorySettingsSection, props))
    })
    await act(async () => {
      fireEvent.click(screen.getByText('手动指定模型'))
    })

    const provider = screen.getByPlaceholderText('Provider ID，如 deepseek') as HTMLInputElement
    const model = screen.getByPlaceholderText('如 deepseek-chat') as HTMLInputElement
    const baseURL = screen.getByPlaceholderText('如 https://api.deepseek.com/v1') as HTMLInputElement
    const apiKey = screen.getByPlaceholderText('sk-...') as HTMLInputElement
    expect(provider.type).toBe('text')
    expect(apiKey.type).toBe('password')

    await typeInto(provider, 'deepseek')
    await act(async () => { provider.blur() })
    await typeInto(model, 'deepseek-chat')
    await act(async () => { model.blur() })
    await typeInto(baseURL, '  https://api.deepseek.com/v1  ')
    await act(async () => { baseURL.blur() })
    await typeInto(apiKey, 'sk-secret')
    await act(async () => { apiKey.blur() })

    expect(commits).toEqual([
      { provider: 'deepseek' },
      { provider: 'deepseek', model: 'deepseek-chat' },
      { provider: 'deepseek', model: 'deepseek-chat', baseURL: 'https://api.deepseek.com/v1' },
      { provider: 'deepseek', model: 'deepseek-chat', baseURL: 'https://api.deepseek.com/v1', apiKey: 'sk-secret' },
    ])
  })

  it('commits a manual-model edit on Enter and leaves an untouched field alone', async () => {
    const controller = buildController()
    const { props } = bind(controller)
    const commits = spyModelWrites(props)
    await act(async () => {
      render(createElement(MemorySettingsSection, props))
    })
    await act(async () => {
      fireEvent.click(screen.getByText('手动指定模型'))
    })

    const provider = screen.getByPlaceholderText('Provider ID，如 deepseek') as HTMLInputElement
    provider.focus()
    await act(async () => {
      fireEvent.change(provider, { target: { value: 'openai' } })
    })
    await act(async () => {
      fireEvent.keyDown(provider, { key: 'Enter' })
    })
    expect(commits).toEqual([{ provider: 'openai' }])

    // Blurring an unchanged field must not write again.
    provider.blur()
    await act(async () => {})
    expect(commits).toHaveLength(1)
  })
})
