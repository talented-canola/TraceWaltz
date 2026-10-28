import { pathToFileURL } from 'node:url'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { readdir } from 'node:fs/promises'
import { spawnSync } from 'node:child_process'
const project = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const { DshHarnessAdapter } = await import(pathToFileURL(resolve(project, '.runtime/gear-source/lib/adapters/dsh/harness.js')))
const target = resolve(process.argv[2])
await new DshHarnessAdapter().check(target)
async function check(directory) {
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const path = resolve(directory, entry.name)
    if (entry.isDirectory()) await check(path)
    else if (/\.(?:mjs|cjs|js)$/.test(path)) {
      const result = spawnSync(process.execPath, ['--check', path], { encoding: 'utf8' })
      if (result.status !== 0) throw new Error(result.stderr || 'JavaScript syntax check failed')
    }
  }
}
await check(target)
console.log(JSON.stringify({ status: 'valid', checks: ['native DSH composition schema', 'JavaScript syntax'] }))
