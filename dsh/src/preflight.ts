/**
 * Preflight: is the Python side actually importable?
 *
 * The bridge spawns `python -m atom_memory.rpc`. When the interpreter cannot
 * import the library the child exits immediately, every memory call fails, and
 * — without this module — the only trace is a line in the host log: the plugin
 * looks loaded, the tools look registered, and every write silently fails. The
 * README has always documented that as a limitation; this is the check that
 * turns it into a diagnosable state.
 *
 * The probe is deliberately a *separate* `-c` invocation rather than a retry of
 * the bridge: it answers "can this interpreter import the library at all?",
 * which is a different question from "did the RPC handshake complete", and it
 * separates a permanent failure (wrong interpreter, library not installed, no
 * permission) from a transient one (slow import, cold model files) so the retry
 * policy can treat them differently.
 *
 * @module dsh-atom-memory/preflight
 */
import { execFile } from 'node:child_process'

/** Outcome of an import probe. */
export interface PreflightResult {
  /** Whether the interpreter imported the library. */
  ok: boolean
  /** The interpreter that was probed (the configured one, or `python`). */
  bin: string
  /** Human-readable diagnosis: the failing module on failure, the import target on success. */
  detail: string
  /** Whether the failure looks permanent (so retrying it is pointless). */
  permanent: boolean
}

/** One probe invocation's raw outcome. */
export interface ProbeOutcome {
  /** Process exit code. */
  code: number | null
  /** Captured stdout. */
  stdout: string
  /** Captured stderr. */
  stderr: string
}

/**
 * How the probe executes the interpreter. Injected so the classification logic
 * can be tested without spawning real processes.
 */
export type ProbeRunner = (
  bin: string,
  args: string[],
  timeoutMs: number,
) => Promise<ProbeOutcome>

/** The import probe: the package the bridge needs, plus its native dependency. */
const PROBE =
  'import atom_memory, sqlite_vec; print("atom_memory", atom_memory.__file__)'

/**
 * Failures that will not fix themselves between two retries a second apart.
 *
 * A missing module, a missing or unexecutable interpreter, a permission
 * problem: all of these need an operator action (install the library, fix
 * `pythonBin`), so retrying them only delays the message that would say so.
 */
const PERMANENT_PATTERN =
  /No module named|ModuleNotFoundError|not found|ENOENT|cannot find|EACCES|EPERM|permission denied/i

/**
 * Classify a failed probe.
 *
 * @param error - The thrown error (or the exit error) from the probe.
 * @param stderr - Captured stderr, which is where Python puts the traceback.
 * @returns The failure fields of a {@link PreflightResult}.
 */
export function classifyFailure(
  error: unknown,
  stderr: string,
): { ok: false; detail: string; permanent: boolean } {
  const err = error as { message?: string; code?: string | number; stderr?: string }
  const combined = `${stderr}${err?.stderr ?? ''}${err?.message ?? ''}${err?.code ?? ''}`
  const detail = combined.replace(/\s+/gu, ' ').trim().slice(0, 400) || 'unknown probe failure'
  return { ok: false, detail, permanent: PERMANENT_PATTERN.test(combined) }
}

/** Default runner: a bounded `execFile` that reports non-zero exits as throws. */
const defaultRun: ProbeRunner = (bin, args, timeoutMs) =>
  new Promise<ProbeOutcome>((resolve, reject) => {
    execFile(bin, args, { timeout: timeoutMs, windowsHide: true }, (error, stdout, stderr) => {
      const out = String(stdout ?? '')
      const err = String(stderr ?? '')
      if (error === null || error === undefined) {
        resolve({ code: 0, stdout: out, stderr: err })
        return
      }
      reject(Object.assign(error, { stderr: err, message: `${error.message} ${err}` }))
    })
  })

/**
 * Run the import probe against an interpreter.
 *
 * Never rejects: a probe that cannot run at all (ENOENT, timeout) is reported as
 * a failed probe with a permanent/transient verdict, because the caller has to
 * keep running either way.
 *
 * @param pythonBin - Interpreter to probe; empty/undefined means `python`.
 * @param options - `timeoutMs` bounds the probe (default 20 s: a cold import of
 *   FastEmbed's dependency graph is slow, and a false timeout would be worse
 *   than a slow answer); `run` injects a runner for tests.
 * @returns The probe result.
 */
export async function checkPythonSide(
  pythonBin: string | undefined,
  options: { timeoutMs?: number; run?: ProbeRunner } = {},
): Promise<PreflightResult> {
  const bin = pythonBin && pythonBin.trim().length > 0 ? pythonBin : 'python'
  const timeout = options.timeoutMs ?? 20_000
  const run = options.run ?? defaultRun
  try {
    const outcome = await run(bin, ['-c', PROBE], timeout)
    if (outcome.code !== 0) {
      const failure = classifyFailure(
        Object.assign(new Error(`exit ${outcome.code}`), { code: outcome.code }),
        outcome.stderr,
      )
      return { ...failure, bin }
    }
    return {
      ok: true,
      bin,
      detail: outcome.stdout.trim() || 'import ok',
      permanent: false,
    }
  } catch (error) {
    return { ...classifyFailure(error, ''), bin }
  }
}
