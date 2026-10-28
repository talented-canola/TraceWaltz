import { pathToFileURL } from 'node:url'
import { resolve } from 'node:path'
const [source, home] = process.argv.slice(2).map(v => resolve(v))
const { ensurePresets, ensureClientPackage } = await import(pathToFileURL(resolve(source, 'harness/launcher.mjs')))
const presets = ensurePresets(home)
ensureClientPackage(home)
console.log(JSON.stringify({ presets, home, status: 'prepared' }))
