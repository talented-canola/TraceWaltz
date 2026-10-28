// Supply the original headless runner's user task without the OS argv size limit.
import { readFileSync } from 'node:fs'
export const name = 'tracewaltz-input-file-startup'
export function apply(ctx, config) {
  const task = readFileSync(config.path, 'utf8')
  if (!task.trim()) throw new Error('empty TraceWaltz task file')
  ctx.provide('headlessStartup', { task })
}
