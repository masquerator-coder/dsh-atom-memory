/**
 * dsh-atom-memory — browser half. Contributes a single **记忆** (Memory)
 * settings section to the dsh settings panel.
 *
 * Node half (`../index.ts`) registers the `atom-memory` settings namespace and
 * the `atom-memory` Remote operations; this browser half binds the namespace
 * (features 1 & 2) and calls the Remote operations (features 3-5). It never
 * reads or fabricates model-visible memory content itself.
 *
 * @module dsh-atom-memory/client
 */
import type { Context } from '@deepseek-ai/cordis'
// Type-only: pulls the client Context merges (ctx.locale, ctx.configForms,
// ctx.slots, ctx.remote) from the composed packages.
import type {} from '@deepseek-ai/dsh-client-locale/client'
import type {} from '@deepseek-ai/dsh-client-ui-settings/client'
import type {} from '@deepseek-ai/dsh-client-ui-slots'
import type {} from '@deepseek-ai/dsh-api-remotes/client'
import { MemorySettingsController, type MemorySettingsSection as MemorySection } from './memory-settings-controller.ts'
import { MemorySettingsSection } from './MemorySettingsSection.tsx'
import { dicts, LOCALE_NS } from './locales.ts'
import { ATOM_MEMORY_REMOTE } from './remote.ts'

/** The settings namespace registered by the Host plugin. */
const SETTINGS_NAMESPACE = 'atom-memory'

/**
 * Required services (cordis fiber inject).
 *
 * WHY `configForms` AND NOT `settingsScope`: DSH 0.1.7-alpha.1 rewrote the
 * settings layer as "profile-owned live Config + form projection" and DELETED
 * the client `settingsScope` service outright. Cordis does not error on an
 * unsatisfied `inject` — the fiber simply parks in `pending` forever — but the
 * Web client boot audit (`packages/client/web/src/boot-client.ts`,
 * `assertEntriesActive`) requires EVERY entry to be `active` and otherwise
 * throws, which the boot page renders as "Failed to load plugins". So the stale
 * name did not merely degrade this panel; it took down the whole client boot.
 *
 * The replacement is `ctx.configForms.get(namespace)`, provided by
 * `@deepseek-ai/dsh-client-ui-settings`. Its `ConfigForm` exposes
 * `getSnapshot()` / `subscribe()` / `set()` — the same three calls the
 * controller already made against the old scope, so the form body needed no
 * rewrite.
 */
export const inject = ['slots', 'locale', 'configForms', 'remote'] as const

/**
 * Mount the memory settings section.
 * @param ctx - the browser plugin context.
 */
export async function apply(ctx: Context): Promise<() => void> {
  const t = ctx.locale.bind(LOCALE_NS)
  ctx.effect(() => ctx.locale.register(LOCALE_NS, dicts), 'atom-memory: section dictionaries')

  // The Host `AtomMemoryController` is not one of the `@deepseek-ai/dsh-api-remotes`
  // assembly namespaces, so the browser has no auto-generated descriptors for it.
  // Mount them here, then hand the namespace service to the controller.
  const disposeRemote = await ctx.remote.$mount(ATOM_MEMORY_REMOTE)
  // Resolve the freshly-mounted namespace through `ctx.get` — the inject-free
  // read. `ctx.remote.atomMemory` property access is gated on the `remote.atomMemory`
  // inject entry, which is not (and cannot be) statically declared because the
  // namespace is mounted dynamically inside this same apply().
  const memoryRemote = ctx.get('remote.atomMemory') as unknown
  if (memoryRemote === undefined) {
    // Never fail the loader entry (that blocks dsh startup): degrade the panel
    // to a surfaced error instead. The settings section still renders and the
    // dynamic-data calls will report the missing namespace through `lastError`.
    ctx.logger.warn('[dsh-atom-memory] remote.atomMemory was not provided after mount; memory panel remote calls disabled')
  }

  const controller = new MemorySettingsController(
    // The shared form for THIS bundle's settings namespace, owned by the
    // ui-settings provider (so this plugin never declares `remote.settings`).
    // `get` is idempotent per namespace and takes the namespace directly —
    // unlike the deleted `settingsScope.bind({ namespace })` there is no
    // binding step, and the provider disposes every form when it unloads.
    ctx.configForms.get<MemorySection>(SETTINGS_NAMESPACE),
    memoryRemote,
  )
  ctx.effect(() => () => { controller.dispose() }, 'atom-memory: controller')

  // A single 记忆 row in the Settings sidebar.
  ctx.slots.inject('settings.section', () => ctx.slots.register({
    name: 'settings.section',
    id: 'memory',
    order: 60,
    label: () => t('title'),
    locale: LOCALE_NS,
    inject: () => controller.inject(),
  }, MemorySettingsSection))

  return async () => {
    controller.dispose()
    await disposeRemote()
  }
}
