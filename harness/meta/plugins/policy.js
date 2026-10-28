import { readFileSync } from 'node:fs'
export const name = 'tracewaltz-meta-policy'
export const inject = ['systemPrompt']
export function apply(ctx) {
  ctx.systemPrompt.section({
    name: 'tracewaltz:meta-contract', order: 50,
    text: readFileSync(new URL('../prompts/system.md', import.meta.url), 'utf8'),
  })
}
