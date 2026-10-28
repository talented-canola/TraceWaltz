// Windows npm is a .cmd launcher: adapt the build process without editing Gear's source.
import childProcess from 'node:child_process'
import { syncBuiltinESMExports } from 'node:module'
import { dirname, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'

const root = resolve(import.meta.dirname, '../.runtime/gear-source')
process.chdir(root)
const original = childProcess.execFileSync
if (process.platform === 'win32') {
  childProcess.execFileSync = function (command, args, options) {
    if (command === 'npm') return original(process.execPath,
      [resolve(dirname(process.execPath), 'node_modules/npm/bin/npm-cli.js'), ...args], options)
    return original(command, args, options)
  }
  syncBuiltinESMExports()
}
await import(pathToFileURL(resolve(root, 'scripts/build-private-tool-fs.mjs')).href)
original(process.execPath, [resolve(root, 'node_modules/typescript/bin/tsc'), '-p', 'tsconfig.build.json'],
  { cwd: root, stdio: 'inherit' })
